import { createHash } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { describe, expect, test, vi } from "vitest";

import {
  ArtifactStore,
  assertArtifactReadTarget,
  assertArtifactWriteTarget,
  createArtifactEntry,
  createArtifactGetHandler,
  normalizeArtifactPath,
} from "../../lib/yfeistai/artifact-manifest";
import { signServiceRequest } from "../../lib/yfeistai/service-auth";

function validEntry(overrides: Record<string, unknown> = {}) {
  const bytes = new TextEncoder().encode("artifact");
  return {
    ...createArtifactEntry({
      relativePath: "media/lesson.mp4",
      bytes,
      mime: "video/mp4",
      expiresAt: "2026-07-31T00:00:00.000Z",
    }),
    ...overrides,
  };
}

function signedArtifactRequest(input: {
  tenantId: string;
  jobId: string;
  signedJobId?: string;
  secret?: string;
}) {
  const path = `/api/yfeistai/v1/artifacts/${input.jobId}/media/lesson.mp4`;
  const signed = signServiceRequest({
    method: "GET",
    path,
    body: "",
    tenantId: input.tenantId,
    jobId: input.signedJobId ?? input.jobId,
    timestamp: 1_800_000_000,
    secret: input.secret ?? "service-secret",
  });
  return new Request(`http://openmaic${path}`, {
    headers: {
      "x-yfeistai-tenant-id": signed.tenantId,
      "x-yfeistai-job-id": signed.jobId,
      "x-yfeistai-timestamp": String(signed.timestamp),
      "x-yfeistai-signature": signed.signature,
    },
  });
}

describe("artifact manifest boundary", () => {
  const artifactRoot = path.resolve("controlled-artifacts", "content-a");

  test.each([
    "../secret.txt",
    "nested/../../secret.txt",
    "/etc/passwd",
    "C:\\Windows\\system.ini",
    "\\\\server\\share\\secret.txt",
    "media\\lesson.mp4",
    "media//lesson.mp4",
    "media/./lesson.mp4",
    "media/../lesson.mp4",
    "media/%2e%2e/secret.txt",
    "media/%5csecret.txt",
    "media/%00secret.txt",
    "media/lesson.mp4/",
    "media/lesson:copy.mp4",
    "media/lesson.mp4.",
    "media/lesson.mp4 ",
    "media/CON.txt",
    "media/control\u0001.txt",
    "\0media/lesson.mp4",
  ])("rejects a non-portable artifact path: %s", (path) => {
    expect(() => normalizeArtifactPath(path)).toThrow(/artifact path/i);
  });

  test("records and verifies path, hash, MIME, bytes, and expiry", () => {
    const bytes = new TextEncoder().encode('{"schemaVersion":"1.0"}');
    const expectedSha256 = createHash("sha256").update(bytes).digest("hex");
    const entry = createArtifactEntry({
      relativePath: "classroom/classroom.json",
      bytes,
      mime: "application/json",
      expiresAt: "2026-07-31T00:00:00.000Z",
      expectedSha256,
    });

    expect(entry).toEqual({
      relativePath: "classroom/classroom.json",
      sha256: expectedSha256,
      bytes: bytes.byteLength,
      mime: "application/json",
      downloadPath: "classroom/classroom.json",
      expiresAt: "2026-07-31T00:00:00.000Z",
    });
    expect(() =>
      createArtifactEntry({
        relativePath: "classroom/classroom.json",
        bytes,
        mime: "application/json",
        expiresAt: "2026-07-31T00:00:00.000Z",
        expectedSha256: "0".repeat(64),
      }),
    ).toThrow(/hash mismatch/i);
  });

  test("persists manifest entries and secure bytes across store instances", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-artifacts-"));
    const bytes = new TextEncoder().encode("persistent artifact");
    const first = new ArtifactStore(root);
    const entry = await first.put({
      tenantId: "tenant-restart",
      jobId: "job-restart",
      relativePath: "media/persistent.txt",
      bytes,
      mime: "text/plain",
      expiresAt: "2099-07-31T00:00:00.000Z",
    });

    const restarted = new ArtifactStore(root);
    expect(restarted.list("tenant-restart", "job-restart")).toEqual([entry]);
    const persisted = await restarted.read(
      "tenant-restart",
      "job-restart",
      "media/persistent.txt",
      new Date("2099-07-30T00:00:00.000Z"),
    );
    expect(persisted?.entry).toEqual(entry);
    expect(Array.from(persisted?.bytes ?? [])).toEqual(Array.from(bytes));
  });

  test("rechecks the execution fence at byte and manifest publication", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-artifact-fence-"));
    const assertPublicationActive = vi.fn();

    await new ArtifactStore(root).put({
      tenantId: "tenant-fence",
      jobId: "job-fence",
      relativePath: "media/fenced.txt",
      bytes: new TextEncoder().encode("fenced artifact"),
      mime: "text/plain",
      expiresAt: "2099-07-31T00:00:00.000Z",
      assertPublicationActive,
    });

    expect(assertPublicationActive).toHaveBeenCalledTimes(2);
  });

  test("recovers an idempotent artifact replay after bytes and manifest were persisted", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-artifact-replay-"));
    const bytes = new TextEncoder().encode("replay-safe artifact");
    const input = {
      tenantId: "tenant-replay",
      jobId: "job-replay",
      relativePath: "media/replay.txt",
      bytes,
      mime: "text/plain",
      expiresAt: "2099-07-31T00:00:00.000Z",
    };
    const first = await new ArtifactStore(root).put(input);

    await expect(new ArtifactStore(root).put(input)).resolves.toEqual(first);
  });

  test("repairs a corrupt manifest when the staged artifact bytes still match", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "openmaic-artifact-repair-"));
    const store = new ArtifactStore(root);
    const input = {
      tenantId: "tenant-repair",
      jobId: "job-repair",
      relativePath: "media/repair.txt",
      bytes: new TextEncoder().encode("repair-safe artifact"),
      mime: "text/plain",
      expiresAt: "2099-07-31T00:00:00.000Z",
    };
    const expected = await store.put(input);
    const manifestName = `${createHash("sha256")
      .update(input.relativePath)
      .digest("hex")}.json`;
    writeFileSync(
      path.join(
        store.rootFor(input.tenantId, input.jobId),
        ".yfeistai-manifest",
        manifestName,
      ),
      "{",
      "utf8",
    );

    const restarted = new ArtifactStore(root);
    await expect(restarted.put(input)).resolves.toEqual(expected);
    expect(restarted.list(input.tenantId, input.jobId)).toEqual([expected]);
  });

  test("rejects an artifact replay when persisted bytes conflict", async () => {
    const root = mkdtempSync(
      path.join(tmpdir(), "openmaic-artifact-conflict-"),
    );
    const store = new ArtifactStore(root);
    const input = {
      tenantId: "tenant-conflict",
      jobId: "job-conflict",
      relativePath: "media/conflict.txt",
      bytes: new TextEncoder().encode("first"),
      mime: "text/plain",
      expiresAt: "2099-07-31T00:00:00.000Z",
    };
    await store.put(input);

    await expect(
      new ArtifactStore(root).put({
        ...input,
        bytes: new TextEncoder().encode("different"),
      }),
    ).rejects.toThrow(/conflict/i);
  });

  test.each(["", "text/plain\r\nx-forged: true", "not-a-mime", "video/"])(
    "rejects invalid artifact MIME: %s",
    (mime) => {
      expect(() =>
        createArtifactEntry({
          relativePath: "media/lesson.mp4",
          bytes: new Uint8Array([1, 2, 3]),
          mime,
          expiresAt: "2026-07-31T00:00:00.000Z",
        }),
      ).toThrow(/MIME/i);
    },
  );

  test("rejects expired and unregistered manifest paths before filesystem reads", async () => {
    const lstat = vi.fn(async () => ({
      isSymbolicLink: () => false,
    }));
    await expect(
      assertArtifactReadTarget({
        root: artifactRoot,
        relativePath: "media/lesson.mp4",
        manifest: [validEntry({ expiresAt: "2026-07-29T00:00:00.000Z" })],
        now: new Date("2026-07-30T00:00:00.000Z"),
        lstat,
      }),
    ).rejects.toThrow(/expired/i);
    await expect(
      assertArtifactReadTarget({
        root: artifactRoot,
        relativePath: "media/not-registered.mp4",
        manifest: [validEntry()],
        now: new Date("2026-07-30T00:00:00.000Z"),
        lstat,
      }),
    ).rejects.toThrow(/manifest/i);
    expect(lstat).not.toHaveBeenCalled();
  });

  test("rejects a symbolic-link in every parent component", async () => {
    const visited: string[] = [];
    await expect(
      assertArtifactReadTarget({
        root: artifactRoot,
        relativePath: "media/lesson.mp4",
        manifest: [validEntry()],
        now: new Date("2026-07-30T00:00:00.000Z"),
        lstat: async (target) => {
          visited.push(target);
          return {
            isSymbolicLink: () => /[\\/]media$/.test(target),
          };
        },
      }),
    ).rejects.toThrow(/symbolic link/i);
    expect(visited).toHaveLength(2);
    expect(visited[0]).toBe(artifactRoot);
    expect(visited[1]).toMatch(/[\\/]media$/);
  });

  test("allows only the exact unexpired manifest entry after checking every component", async () => {
    const visited: string[] = [];
    const target = await assertArtifactReadTarget({
      root: artifactRoot,
      relativePath: "media/lesson.mp4",
      manifest: [validEntry()],
      now: new Date("2026-07-30T00:00:00.000Z"),
      lstat: async (path) => {
        visited.push(path);
        return { isSymbolicLink: () => false };
      },
    });
    expect(target).toMatch(/[\\/]media[\\/]lesson\.mp4$/);
    expect(visited).toHaveLength(3);
    expect(visited[0]).toBe(artifactRoot);
  });

  test("rejects a symbolic-link parent before writing an artifact", async () => {
    const visited: string[] = [];
    await expect(
      assertArtifactWriteTarget({
        baseRoot: path.resolve("controlled-artifacts"),
        tenantId: "tenant-a",
        jobId: "content-a",
        relativePath: "media/lesson.mp4",
        mkdir: async () => undefined,
        lstat: async (target) => {
          visited.push(target);
          return {
            isSymbolicLink: () => /[\\/]content-a$/.test(target),
            isDirectory: () => true,
          };
        },
      }),
    ).rejects.toThrow(/unsafe parent/i);
    expect(visited.some((target) => /[\\/]content-a$/.test(target))).toBe(true);
  });
});

describe("signed artifact route", () => {
  test("serves artifacts with attachment and sniffing defenses", async () => {
    const bytes = new TextEncoder().encode("safe artifact");
    const store = new ArtifactStore(path.resolve("controlled-artifacts"));
    vi.spyOn(store, "read").mockResolvedValue({
      root: path.resolve("controlled-artifacts", "tenant-a", "content-a"),
      entry: createArtifactEntry({
        relativePath: "media/lesson.mp4",
        bytes,
        mime: "video/mp4",
        expiresAt: "2099-07-31T00:00:00.000Z",
      }),
      bytes,
    });
    const handler = createArtifactGetHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      store,
    });
    const response = await handler(
      signedArtifactRequest({ tenantId: "tenant-a", jobId: "content-a" }),
      {
        params: Promise.resolve({
          jobId: "content-a",
          path: ["media", "lesson.mp4"],
        }),
      },
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
    expect(response.headers.get("content-disposition")).toContain("attachment");
    expect(response.headers.get("content-security-policy")).toBe("sandbox");
  });

  test("authenticates and binds tenant/job before manifest lookup", async () => {
    const store = new ArtifactStore(path.resolve("controlled-artifacts"));
    const get = vi.spyOn(store, "get").mockReturnValue(null);
    const handler = createArtifactGetHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      store,
    });
    const context = {
      params: Promise.resolve({
        jobId: "content-a",
        path: ["media", "lesson.mp4"],
      }),
    };

    expect(
      (
        await handler(
          signedArtifactRequest({
            tenantId: "tenant-a",
            jobId: "content-a",
            secret: "wrong-secret",
          }),
          context,
        )
      ).status,
    ).toBe(401);
    expect(get).not.toHaveBeenCalled();

    expect(
      (
        await handler(
          signedArtifactRequest({
            tenantId: "tenant-a",
            jobId: "content-a",
            signedJobId: "content-b",
          }),
          context,
        )
      ).status,
    ).toBe(403);
    expect(get).not.toHaveBeenCalled();

    expect(
      (
        await handler(
          signedArtifactRequest({
            tenantId: "tenant-b",
            jobId: "content-a",
          }),
          context,
        )
      ).status,
    ).toBe(404);
    expect(get).toHaveBeenCalledWith(
      "tenant-b",
      "content-a",
      "media/lesson.mp4",
    );
  });
});
