import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { hostname, tmpdir } from "node:os";
import path from "node:path";

import { describe, expect, test, vi } from "vitest";

const fsHooks = vi.hoisted(() => ({
  directoryFsync: vi.fn(),
  beforePathOpen: undefined as undefined | ((target: string) => void),
  beforePathRead: undefined as undefined | ((target: string) => void),
}));

vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs")>();
  return {
    ...actual,
    fsyncSync(descriptor: number) {
      if (actual.fstatSync(descriptor).isDirectory()) {
        fsHooks.directoryFsync();
      }
      return actual.fsyncSync(descriptor);
    },
    openSync(
      target: Parameters<typeof actual.openSync>[0],
      ...rest: unknown[]
    ) {
      if (typeof target === "string") {
        fsHooks.beforePathOpen?.(target);
      }
      return (actual.openSync as (...args: unknown[]) => number)(
        target,
        ...rest,
      );
    },
    readFileSync(
      target: Parameters<typeof actual.readFileSync>[0],
      ...rest: unknown[]
    ) {
      if (typeof target === "string") {
        fsHooks.beforePathRead?.(target);
      }
      return (actual.readFileSync as (...args: unknown[]) => unknown)(
        target,
        ...rest,
      );
    },
  };
});

import {
  ContentIdempotencyConflictError,
  ContentJobStore,
  createJobCancelHandler,
} from "../../lib/yfeistai/content-generation";
import { OutlineJobStore } from "../../lib/yfeistai/job-store";
import { signServiceRequest } from "../../lib/yfeistai/service-auth";
import {
  durableFile,
  durableLeaseMatches,
  withDurableLock,
  writeDurableJsonAtomic,
  writeDurableJsonExclusive,
} from "../../lib/yfeistai/durable-state";

function signedCancel(
  tenantId: string,
  jobId: string,
  options: { signedJobId?: string; secret?: string } = {},
) {
  const path = `/api/yfeistai/v1/jobs/${jobId}/cancel`;
  const body = "{}";
  const signed = signServiceRequest({
    method: "POST",
    path,
    body,
    tenantId,
    jobId: options.signedJobId ?? jobId,
    idempotencyKey: `cancel-${jobId}`,
    timestamp: 1_800_000_000,
    secret: options.secret ?? "service-secret",
  });
  return new Request(`http://openmaic${path}`, {
    method: "POST",
    body,
    headers: {
      "x-yfeistai-tenant-id": signed.tenantId,
      "x-yfeistai-job-id": signed.jobId,
      "x-yfeistai-idempotency-key": signed.idempotencyKey,
      "x-yfeistai-timestamp": String(signed.timestamp),
      "x-yfeistai-signature": signed.signature,
    },
  });
}

describe("job cancellation races", () => {
  test("fsyncs the parent directory after durable publication", () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-parent-fsync-"));
    const exclusive = path.join(root, "exclusive.json");
    const atomic = path.join(root, "atomic.json");
    fsHooks.directoryFsync.mockClear();

    expect(writeDurableJsonExclusive(exclusive, { version: 1 })).toBe(true);
    writeDurableJsonAtomic(atomic, { version: 1 });

    expect(fsHooks.directoryFsync).toHaveBeenCalledTimes(2);
  });

  test("does not follow a record replaced by a symlink during an exclusive replay", () => {
    const root = mkdtempSync(
      path.join(tmpdir(), "openmaic-record-symlink-race-"),
    );
    const target = path.join(root, "terminal.json");
    const original = path.join(root, "terminal-original.json");
    const outside = path.join(root, "outside.json");
    writeFileSync(
      target,
      JSON.stringify({ version: 1, status: "succeeded" }),
      "utf8",
    );
    writeFileSync(
      outside,
      JSON.stringify({ version: 1, status: "outside" }),
      "utf8",
    );
    fsHooks.beforePathRead = (readTarget) => {
      if (readTarget !== target || existsSync(original)) {
        return;
      }
      renameSync(target, original);
      symlinkSync(outside, target, "file");
    };

    try {
      const published = writeDurableJsonExclusive(target, {
        version: 1,
        status: "new",
      });
      fsHooks.beforePathRead = undefined;
      expect(published).toBe(false);
      expect(lstatSync(target).isSymbolicLink()).toBe(false);
      expect(JSON.parse(readFileSync(target, "utf8"))).toEqual({
        version: 1,
        status: "succeeded",
      });
    } finally {
      fsHooks.beforePathRead = undefined;
    }
  });

  test("publishes when an EEXIST winner disappears before replay", () => {
    const root = mkdtempSync(
      path.join(tmpdir(), "openmaic-record-disappears-race-"),
    );
    const target = path.join(root, "terminal.json");
    const displaced = path.join(root, "terminal-displaced.json");
    writeFileSync(
      target,
      JSON.stringify({ version: 1, status: "old" }),
      "utf8",
    );
    fsHooks.beforePathOpen = (openTarget) => {
      if (openTarget !== target || existsSync(displaced)) {
        return;
      }
      renameSync(target, displaced);
    };

    try {
      expect(
        writeDurableJsonExclusive(target, { version: 1, status: "new" }),
      ).toBe(true);
      expect(JSON.parse(readFileSync(target, "utf8"))).toEqual({
        version: 1,
        status: "new",
      });
    } finally {
      fsHooks.beforePathOpen = undefined;
    }
  });

  test("never probes a cross-host lock owner with a local PID", () => {
    const directory = mkdtempSync(
      path.join(tmpdir(), "openmaic-cross-host-lock-"),
    );
    const lockPath = path.join(directory, ".state-lock");
    mkdirSync(lockPath, { mode: 0o700 });
    writeFileSync(
      path.join(lockPath, "owner"),
      JSON.stringify({
        version: 2,
        owner: "remote-owner",
        pid: process.pid,
        hostname: `${hostname()}-remote`,
        processInstanceId: "remote-process",
      }),
      "utf8",
    );
    const base = Date.now();
    utimesSync(lockPath, new Date(base - 31_000), new Date(base - 31_000));
    let calls = 0;
    const now = vi.spyOn(Date, "now").mockImplementation(() => {
      calls += 1;
      return calls < 3 ? base : base + 5_001;
    });
    const kill = vi.spyOn(process, "kill");

    try {
      expect(() => withDurableLock(directory, () => undefined)).toThrow(
        /busy/i,
      );
      expect(kill).not.toHaveBeenCalled();
    } finally {
      now.mockRestore();
      kill.mockRestore();
    }
  });

  test("reclaims a stale lock from a reused local PID incarnation", () => {
    const directory = mkdtempSync(
      path.join(tmpdir(), "openmaic-pid-aba-lock-"),
    );
    const lockPath = path.join(directory, ".state-lock");
    mkdirSync(lockPath, { mode: 0o700 });
    writeFileSync(
      path.join(lockPath, "owner"),
      JSON.stringify({
        version: 2,
        owner: "previous-owner",
        pid: process.pid,
        hostname: hostname(),
        processInstanceId: "previous-process-incarnation",
      }),
      "utf8",
    );
    const stale = new Date(Date.now() - 31_000);
    utimesSync(lockPath, stale, stale);

    expect(withDurableLock(directory, () => "recovered")).toBe("recovered");
  });

  test("an expired lease never matches its prior owner and fence", () => {
    const root = mkdtempSync(
      path.join(tmpdir(), "openmaic-expired-lease-match-"),
    );
    const target = durableFile(
      root,
      "content-jobs",
      "jobs",
      ["tenant-a", "job-a"],
      "lease.json",
    );
    writeDurableJsonAtomic(target, {
      version: 1,
      bindingSha256:
        "a3fdd293944dc47d15f611f385abfb967dc94037cd1f8f5887bee922ac571fdf",
      owner: "owner-a",
      fence: 1,
      expiresAt: 1_100,
      updatedAt: "1970-01-01T00:00:01.000Z",
    });

    expect(
      durableLeaseMatches(
        target,
        "tenant-a\0job-a",
        { owner: "owner-a", fence: 1 },
        1_100,
      ),
    ).toBe(false);
  });

  test("an old lock owner cannot release a replacement lock at the same path", () => {
    const directory = mkdtempSync(path.join(tmpdir(), "openmaic-lock-aba-"));
    const lockPath = path.join(directory, ".state-lock");
    withDurableLock(directory, () => {
      renameSync(lockPath, path.join(directory, ".state-lock-old"));
      mkdirSync(lockPath, { mode: 0o700 });
      writeFileSync(path.join(lockPath, "owner"), "replacement-owner", {
        encoding: "utf8",
        mode: 0o600,
      });
    });

    expect(existsSync(path.join(lockPath, "owner"))).toBe(true);
    expect(readFileSync(path.join(lockPath, "owner"), "utf8")).toBe(
      "replacement-owner",
    );
  });

  test("repairs a partial exclusive JSON record before publishing", () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-partial-json-"));
    const target = path.join(root, "terminal.json");
    writeFileSync(target, "{", "utf8");

    expect(
      writeDurableJsonExclusive(target, { version: 1, status: "succeeded" }),
    ).toBe(true);
    expect(JSON.parse(readFileSync(target, "utf8"))).toEqual({
      version: 1,
      status: "succeeded",
    });
  });
  test("canceled jobs never publish a succeeded result", async () => {
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const store = new ContentJobStore();
    const running = store.start(
      {
        tenantId: "tenant-a",
        jobId: "content-a",
        idempotencyKey: "content-idem-a",
        canonicalBody: "{}",
      },
      async () => {
        await blocked;
        return { classroomId: "classroom-a" };
      },
    );

    await expect(store.cancel("tenant-a", "content-a")).resolves.toBe(
      "canceled",
    );
    release();
    await running;

    await expect(store.read("tenant-a", "content-a")).resolves.toMatchObject({
      status: "canceled",
    });
  });

  test("a late cancel never demotes a completed success", async () => {
    const store = new ContentJobStore();
    await store.start(
      {
        tenantId: "tenant-a",
        jobId: "content-a",
        idempotencyKey: "content-idem-a",
        canonicalBody: "{}",
      },
      async () => ({ classroomId: "classroom-a" }),
    );
    await expect(store.cancel("tenant-a", "content-a")).resolves.toBe(
      "succeeded",
    );

    await expect(store.read("tenant-a", "content-a")).resolves.toMatchObject({
      status: "succeeded",
      result: { classroomId: "classroom-a" },
    });
  });
});

describe("job idempotency and tenant isolation", () => {
  test("deduplicates an identical submission and rejects a changed binding", async () => {
    const store = new ContentJobStore();
    const run = vi.fn(async () => ({ classroomId: "classroom-a" }));
    const submission = {
      tenantId: "tenant-a",
      jobId: "content-a",
      idempotencyKey: "content-idem-a",
      canonicalBody: '{"value":1}',
    };

    const first = store.start(submission, run);
    const replay = store.start(submission, run);
    await Promise.all([first, replay]);
    expect(run).toHaveBeenCalledTimes(1);
    expect(() =>
      store.start({ ...submission, canonicalBody: '{"value":2}' }, run),
    ).toThrow(ContentIdempotencyConflictError);
  });

  test("same job and idempotency identifiers remain isolated by tenant", async () => {
    const store = new ContentJobStore();
    await Promise.all([
      store.start(
        {
          tenantId: "tenant-a",
          jobId: "content-a",
          idempotencyKey: "same-idem",
          canonicalBody: "{}",
        },
        async () => ({ classroomId: "classroom-a" }),
      ),
      store.start(
        {
          tenantId: "tenant-b",
          jobId: "content-a",
          idempotencyKey: "same-idem",
          canonicalBody: "{}",
        },
        async () => ({ classroomId: "classroom-b" }),
      ),
    ]);
    await expect(store.read("tenant-a", "content-a")).resolves.toMatchObject({
      result: { classroomId: "classroom-a" },
    });
    await expect(store.read("tenant-b", "content-a")).resolves.toMatchObject({
      result: { classroomId: "classroom-b" },
    });
  });

  test("persists terminal jobs across independent store instances", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-job-restart-"));
    const first = new ContentJobStore(root, "content-jobs");
    const submission = {
      tenantId: "tenant-restart",
      jobId: "content-restart",
      idempotencyKey: "idem-restart",
      canonicalBody: '{"value":1}',
    };
    await first.start(submission, async () => ({ classroomId: "persisted" }));

    const replay = vi.fn(async () => ({ classroomId: "duplicated" }));
    const second = new ContentJobStore(root, "content-jobs");
    await expect(second.start(submission, replay)).resolves.toMatchObject({
      status: "succeeded",
      result: { classroomId: "persisted" },
    });
    expect(replay).not.toHaveBeenCalled();
  });

  test("does not duplicate an unexpired cross-instance execution lease", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-job-lease-"));
    let now = 1_000;
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const submission = {
      tenantId: "tenant-lease",
      jobId: "content-lease",
      idempotencyKey: "idem-lease",
      canonicalBody: "{}",
    };
    const first = new ContentJobStore(
      root,
      "content-jobs",
      1_000,
      () => now,
      false,
    );
    const running = first.start(submission, async () => {
      await blocked;
      return { classroomId: "first" };
    });
    const duplicate = vi.fn(async () => ({ classroomId: "duplicate" }));
    const second = new ContentJobStore(
      root,
      "content-jobs",
      1_000,
      () => now,
      false,
    );
    await expect(second.start(submission, duplicate)).resolves.toMatchObject({
      status: "running",
    });
    expect(duplicate).not.toHaveBeenCalled();
    release();
    await expect(running).resolves.toMatchObject({ status: "succeeded" });
  });

  test("reclaims an expired lease and fences the old owner terminal", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-job-reclaim-"));
    let now = 1_000;
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const submission = {
      tenantId: "tenant-reclaim",
      jobId: "content-reclaim",
      idempotencyKey: "idem-reclaim",
      canonicalBody: "{}",
    };
    const abandoned = new ContentJobStore(
      root,
      "content-jobs",
      100,
      () => now,
      false,
    );
    const oldPublication = vi.fn();
    const oldCompletion = abandoned.start(
      submission,
      async (publication) => {
        await blocked;
        publication.assertActive();
        return { classroomId: "stale-owner" };
      },
      oldPublication,
    );

    now = 1_200;
    const recovered = new ContentJobStore(
      root,
      "content-jobs",
      100,
      () => now,
      false,
    );
    const recoveredPublication = vi.fn();
    await expect(
      recovered.start(
        submission,
        async () => ({ classroomId: "recovered" }),
        recoveredPublication,
      ),
    ).resolves.toMatchObject({
      status: "succeeded",
      result: { classroomId: "recovered" },
    });
    release();
    await expect(oldCompletion).resolves.toMatchObject({
      status: "succeeded",
      result: { classroomId: "recovered" },
    });
    expect(recoveredPublication).toHaveBeenCalledTimes(1);
    expect(oldPublication).not.toHaveBeenCalled();
  });
});

describe("signed cancel route", () => {
  test("durably cancels an outline and fences its old owner across restart", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-outline-cancel-"));
    const now = Date.parse("2026-08-02T02:00:00.000Z");
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const store = new OutlineJobStore(root, 60_000, () => now, false);
    const oldCompletion = store.submit(
      {
        tenantId: "tenant-a",
        jobId: "shared-job",
        idempotencyKey: "outline-idem-a",
        action: "outline",
        canonicalBody: "{}",
      },
      async () => {
        await blocked;
        return {
          tenantId: "tenant-a",
          jobId: "shared-job",
          idempotencyKey: "outline-idem-a",
          phase: "outline",
          status: "succeeded",
          createdAt: new Date(now).toISOString(),
          updatedAt: new Date(now).toISOString(),
        };
      },
    );
    const handler = createJobCancelHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      stores: [store],
    });

    const response = await handler(signedCancel("tenant-a", "shared-job"), {
      params: Promise.resolve({ jobId: "shared-job" }),
    });

    expect(response.status).toBe(202);
    await expect(response.json()).resolves.toEqual({
      jobId: "shared-job",
      status: "canceled",
    });
    const restarted = new OutlineJobStore(root, 60_000, () => now, false);
    await expect(restarted.read("tenant-a", "shared-job")).resolves.toMatchObject({
      status: "canceled",
      error: {
        code: "JOB_CANCELED",
        message: "The job was canceled.",
      },
    });

    release();
    await expect(oldCompletion).resolves.toMatchObject({ status: "canceled" });
    await expect(restarted.read("tenant-a", "shared-job")).resolves.toMatchObject({
      status: "canceled",
    });
  });

  test("cancels running content before an old outline with the same job id", async () => {
    const outlineStore = new OutlineJobStore();
    await outlineStore.submit(
      {
        tenantId: "tenant-a",
        jobId: "shared-job",
        idempotencyKey: "outline-idem-a",
        action: "outline",
        canonicalBody: "{}",
      },
      async () => ({
        tenantId: "tenant-a",
        jobId: "shared-job",
        idempotencyKey: "outline-idem-a",
        phase: "outline",
        status: "succeeded",
        createdAt: "2026-08-02T02:00:00.000Z",
        updatedAt: "2026-08-02T02:00:00.000Z",
      }),
    );
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const contentStore = new ContentJobStore();
    const contentCompletion = contentStore.start(
      {
        tenantId: "tenant-a",
        jobId: "shared-job",
        idempotencyKey: "content-idem-a",
        canonicalBody: "{}",
      },
      async () => {
        await blocked;
        return { classroomId: "classroom-a" };
      },
    );
    const unusedExportStore = { cancel: vi.fn(async () => null) };
    const handler = createJobCancelHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      stores: [contentStore, unusedExportStore, outlineStore],
    });

    const response = await handler(signedCancel("tenant-a", "shared-job"), {
      params: Promise.resolve({ jobId: "shared-job" }),
    });

    expect(response.status).toBe(202);
    expect(unusedExportStore.cancel).not.toHaveBeenCalled();
    await expect(contentStore.read("tenant-a", "shared-job")).resolves.toMatchObject({
      status: "canceled",
    });
    await expect(outlineStore.read("tenant-a", "shared-job")).resolves.toMatchObject({
      status: "succeeded",
    });
    release();
    await contentCompletion;
  });

  test("authenticates before mutation and binds tenant and job", async () => {
    const store = new ContentJobStore();
    await store.start(
      {
        tenantId: "tenant-a",
        jobId: "content-a",
        idempotencyKey: "content-idem-a",
        canonicalBody: "{}",
      },
      async () => ({ classroomId: "classroom-a" }),
    );
    const handler = createJobCancelHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      stores: [store],
    });

    expect(
      (
        await handler(
          signedCancel("tenant-a", "content-a", {
            secret: "wrong-secret",
          }),
          { params: Promise.resolve({ jobId: "content-a" }) },
        )
      ).status,
    ).toBe(401);
    expect(
      (
        await handler(
          signedCancel("tenant-a", "content-a", {
            signedJobId: "other-job",
          }),
          { params: Promise.resolve({ jobId: "content-a" }) },
        )
      ).status,
    ).toBe(403);
    expect(
      (
        await handler(signedCancel("tenant-b", "content-a"), {
          params: Promise.resolve({ jobId: "content-a" }),
        })
      ).status,
    ).toBe(404);
  });

  test("cancel requests are idempotent for the same tenant job", async () => {
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const store = new ContentJobStore();
    const running = store.start(
      {
        tenantId: "tenant-a",
        jobId: "content-a",
        idempotencyKey: "content-idem-a",
        canonicalBody: "{}",
      },
      async () => {
        await blocked;
        return { classroomId: "classroom-a" };
      },
    );
    const handler = createJobCancelHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      stores: [store],
    });
    const context = { params: Promise.resolve({ jobId: "content-a" }) };

    expect(
      (await handler(signedCancel("tenant-a", "content-a"), context)).status,
    ).toBe(202);
    expect(
      (await handler(signedCancel("tenant-a", "content-a"), context)).status,
    ).toBe(202);
    release();
    await running;
    await expect(store.read("tenant-a", "content-a")).resolves.toMatchObject({
      status: "canceled",
    });
  });

  test("notifies the external renderer after a durable export cancellation", async () => {
    const cancel = vi.fn(async () => "canceled" as const);
    const onCanceled = vi.fn(async () => undefined);
    const handler = createJobCancelHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      stores: [{ cancel }],
      onCanceled,
    });

    const response = await handler(signedCancel("tenant-a", "export-a"), {
      params: Promise.resolve({ jobId: "export-a" }),
    });

    expect(response.status).toBe(202);
    expect(onCanceled).toHaveBeenCalledWith("tenant-a", "export-a");
  });
});
