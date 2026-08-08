import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  MP4_EXPORT_DISABLED_REASON,
  ClassroomApiError,
  classroomExportDownloadUrl,
  classroomExportFailureDetails,
  createClassroomExportAttemptRegistry,
  createDraftClassroomExport,
  createVersionClassroomExport,
  getClassroomExport,
  listClassroomExportOptions,
  pollClassroomExport,
  shouldRetainClassroomExportAttempt,
  uploadDraftClassroomMedia,
} from "../lib/classroom-api";
import { classroomMediaUrl } from "../lib/openmaic-adapter/contracts";

const SHA256 = "b".repeat(64);

function exportPayload(overrides: Record<string, unknown> = {}) {
  return {
    job_id: "export-1",
    job_kind: "export",
    phase: "export",
    status: "queued",
    progress_percent: 0,
    waiting_reason: null,
    cancellable: true,
    retryable: false,
    outline: null,
    error_category: null,
    error_code: null,
    retry_of_job_id: null,
    export_format: "pptx",
    download_ready: false,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function withFetch<T>(
  implementation: typeof fetch,
  run: () => Promise<T>,
): Promise<T> {
  const original = globalThis.fetch;
  globalThis.fetch = implementation;
  try {
    return await run();
  } finally {
    globalThis.fetch = original;
  }
}

test("media URLs are always yFeiSTAI routes", () => {
  const url = classroomMediaUrl("version-1", "asset-2");
  assert.equal(url, "/api/v1/classrooms/versions/version-1/media/asset-2");
  assert.equal(url.includes("openmaic"), false);
});

test("draft media upload hashes the file and uses the asset-scoped route", async () => {
  const expectedSha256 = createHash("sha256").update("image").digest("hex");
  let seenInput: RequestInfo | URL | undefined;
  let seenInit: RequestInit | undefined;
  const media = await withFetch(
    async (input, init) => {
      seenInput = input;
      seenInit = init;
      return jsonResponse({
        id: "media-1",
        relativePath: "media/media-1/slide.png",
        mimeType: "image/png",
        sizeBytes: 5,
        sha256: expectedSha256,
      });
    },
    () =>
      uploadDraftClassroomMedia(
        "asset / 1",
        new Blob(["image"], { type: "image/png" }),
        "slide.png",
      ),
  );

  assert.equal(
    seenInput,
    "/api/v1/classrooms/asset%20%2F%201/draft-media",
  );
  assert.equal(seenInit?.method, "POST");
  assert.ok(seenInit?.body instanceof FormData);
  assert.deepEqual([...seenInit.body.keys()], ["file", "sha256"]);
  assert.equal(seenInit.body.get("sha256"), expectedSha256);
  assert.deepEqual(media, {
    mediaId: "media-1",
    relativePath: "media/media-1/slide.png",
    readUrl: "/api/v1/classrooms/asset%20%2F%201/draft-media/media-1",
    mimeType: "image/png",
    sizeBytes: 5,
    sha256: expectedSha256,
  });
});

test("draft media derives its read route and rejects storage details", async () => {
  const expectedSha256 = createHash("sha256").update("image").digest("hex");
  await withFetch(
    async () =>
      jsonResponse({
        id: "media-1",
        relativePath: "media/media-1/slide.png",
        mimeType: "image/png",
        sizeBytes: 5,
        sha256: expectedSha256,
        objectKey: "tenant-a/media-1",
      }),
    () =>
      assert.rejects(
        uploadDraftClassroomMedia(
          "asset-1",
          new Blob(["image"]),
          "slide.png",
        ),
        /unexpected media response field/i,
      ),
  );
});

test("draft media rejects a receipt hash that differs from the uploaded blob", async () => {
  await withFetch(
    async () =>
      jsonResponse({
        id: "media-1",
        relativePath: "media/media-1/slide.png",
        mimeType: "image/png",
        sizeBytes: 5,
        sha256: SHA256,
      }),
    () =>
      assert.rejects(
        uploadDraftClassroomMedia(
          "asset-1",
          new Blob(["image"], { type: "image/png" }),
          "slide.png",
        ),
        /does not match/i,
      ),
  );
});

test("draft media accepts only portable relative paths", async () => {
  const expectedSha256 = createHash("sha256").update("image").digest("hex");
  for (const relativePath of [
    "../media.png",
    "media/../media.png",
    "/media/media.png",
    "media\\media.png",
    "https://cdn.example/media.png",
    "data:image/png;base64,ZmFrZQ==",
    "media/./media.png",
  ]) {
    await withFetch(
      async () =>
        jsonResponse({
          id: "media-1",
          relativePath,
          mimeType: "image/png",
          sizeBytes: 5,
          sha256: expectedSha256,
        }),
      () =>
        assert.rejects(
          uploadDraftClassroomMedia(
            "asset-1",
            new Blob(["image"], { type: "image/png" }),
            "slide.png",
          ),
          /relativePath.*portable/i,
        ),
    );
  }
});

test("draft and immutable-version exports use only yFeiSTAI job routes", async () => {
  const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
  await withFetch(
    async (input, init) => {
      requests.push({ input, init });
      const format = init?.body
        ? (JSON.parse(String(init.body)).format as string)
        : "pptx";
      return jsonResponse(exportPayload({ export_format: format }));
    },
    async () => {
      await createDraftClassroomExport("asset-1", "pptx", {
        revision: '"revision-2"',
        idempotencyKey: "request-1",
      });
      await createVersionClassroomExport("version-1", "offline_html", {
        idempotencyKey: "request-2",
      });
      await getClassroomExport("export-1", "pptx");
    },
  );

  assert.deepEqual(
    requests.map(({ input }) => input),
    [
      "/api/v1/classrooms/asset-1/draft/exports",
      "/api/v1/classroom-versions/version-1/exports",
      "/api/v1/classroom-exports/export-1",
    ],
  );
  assert.deepEqual(JSON.parse(String(requests[0].init?.body)), {
    format: "pptx",
  });
  assert.deepEqual(requests[0].init?.headers, {
    "Content-Type": "application/json",
    "Idempotency-Key": "request-1",
    "If-Match": '"revision-2"',
  });
  assert.deepEqual(JSON.parse(String(requests[1].init?.body)), {
    format: "offline_html",
  });
  assert.deepEqual(requests[1].init?.headers, {
    "Content-Type": "application/json",
    "Idempotency-Key": "request-2",
  });
  assert.equal(
    requests.some(({ input }) => String(input).includes("openmaic")),
    false,
  );
});

test("draft export requires a canonical strong ETag before making a request", async () => {
  let called = false;
  await withFetch(
    async () => {
      called = true;
      return jsonResponse(exportPayload());
    },
    () =>
      assert.rejects(
        createDraftClassroomExport("asset-1", "pptx", {
          revision: "revision-2",
          idempotencyKey: "request-1",
        }),
        /strong ETag/i,
      ),
  );
  assert.equal(called, false);
});

test("export polling stops only on a backend terminal state", async () => {
  const statuses = [
    exportPayload({ status: "queued" }),
    exportPayload({
      status: "exporting",
      progress_percent: 50,
    }),
    exportPayload({
      status: "succeeded",
      progress_percent: 100,
      cancellable: false,
      download_ready: true,
    }),
  ];
  let calls = 0;
  const updates: string[] = [];

  const result = await pollClassroomExport("export-1", {
    intervalMs: 0,
    expectedFormat: "pptx",
    fetchStatus: async () => {
      const payload = statuses[calls];
      calls += 1;
      return payload as never;
    },
    onUpdate: job => updates.push(job.status),
  });

  assert.equal(calls, 3);
  assert.deepEqual(updates, ["queued", "exporting", "succeeded"]);
  assert.equal(result.status, "succeeded");
  assert.equal(result.downloadReady, true);
});

test("export polling honors cancellation and never fabricates completion", async () => {
  const controller = new AbortController();
  let calls = 0;

  await assert.rejects(
    pollClassroomExport("export-1", {
      intervalMs: 1,
      expectedFormat: "pptx",
      signal: controller.signal,
      fetchStatus: async () => {
        calls += 1;
        controller.abort();
        return exportPayload() as never;
      },
    }),
    error => error instanceof DOMException && error.name === "AbortError",
  );
  assert.equal(calls, 1);
});

test("export creation and polling reject format drift", async () => {
  await withFetch(
    async () => jsonResponse(exportPayload({ export_format: "mp4" })),
    () =>
      assert.rejects(
        createVersionClassroomExport("version-1", "pptx", {
          idempotencyKey: "request-1",
        }),
        /format does not match/i,
      ),
  );

  let calls = 0;
  await assert.rejects(
    pollClassroomExport("export-1", {
      intervalMs: 0,
      expectedFormat: "pptx",
      fetchStatus: async () => {
        calls += 1;
        return exportPayload({
          status: "exporting",
          progress_percent: calls * 10,
          export_format: calls === 1 ? "pptx" : "offline_html",
        }) as never;
      },
    }),
    /format does not match/i,
  );
  assert.equal(calls, 2);
});

test("export polling rejects regressing progress and impossible terminal tuples", async () => {
  let calls = 0;
  await assert.rejects(
    pollClassroomExport("export-1", {
      intervalMs: 0,
      expectedFormat: "pptx",
      initialProgressPercent: 40,
      fetchStatus: async () => {
        calls += 1;
        return exportPayload({
          status: "exporting",
          progress_percent: 30,
        }) as never;
      },
    }),
    /progress regressed/i,
  );
  assert.equal(calls, 1);

  await withFetch(
    async () =>
      jsonResponse(
        exportPayload({
          status: "succeeded",
          progress_percent: 0,
          cancellable: true,
          download_ready: true,
        }),
      ),
    () =>
      assert.rejects(
        getClassroomExport("export-1", "pptx"),
        /terminal state|100 percent/i,
      ),
  );
});

test("export polling rejects lifecycle regression except a declared retry backoff", async () => {
  await assert.rejects(
    pollClassroomExport("export-1", {
      intervalMs: 0,
      expectedFormat: "pptx",
      initialProgressPercent: 40,
      initialStatus: "exporting",
      fetchStatus: async () =>
        exportPayload({
          status: "queued",
          progress_percent: 40,
        }) as never,
    }),
    /status regressed/i,
  );

  const retryStatuses = [
    exportPayload({
      status: "queued",
      progress_percent: 40,
      waiting_reason: "retry_backoff",
    }),
    exportPayload({
      status: "succeeded",
      progress_percent: 100,
      cancellable: false,
      download_ready: true,
    }),
  ];
  const retried = await pollClassroomExport("export-1", {
    intervalMs: 0,
    expectedFormat: "pptx",
    initialProgressPercent: 40,
    initialStatus: "exporting",
    fetchStatus: async () => retryStatuses.shift() as never,
  });
  assert.equal(retried.status, "succeeded");
});

test("terminal export states cannot retain a waiting reason", async () => {
  await withFetch(
    async () =>
      jsonResponse(
        exportPayload({
          status: "canceled",
          progress_percent: 40,
          waiting_reason: "retry_backoff",
          cancellable: false,
          retryable: true,
          error_category: "canceled",
          error_code: "job_canceled",
        }),
      ),
    () =>
      assert.rejects(
        getClassroomExport("export-1", "pptx"),
        /terminal export must not report a waiting reason/i,
      ),
  );
});

test("only ambiguous create failures retain their idempotency key", () => {
  assert.equal(
    shouldRetainClassroomExportAttempt(new ClassroomApiError("conflict", 409)),
    false,
  );
  assert.equal(
    shouldRetainClassroomExportAttempt(new ClassroomApiError("timeout", 408)),
    true,
  );
  assert.equal(
    shouldRetainClassroomExportAttempt(
      new ClassroomApiError("invalid success body", 201),
    ),
    true,
  );
  assert.equal(
    shouldRetainClassroomExportAttempt(new ClassroomApiError("unavailable", 503)),
    true,
  );
  assert.equal(shouldRetainClassroomExportAttempt(new TypeError("network")), true);
});

test("ambiguous create attempts retain independent keys for every format", () => {
  const keys = ["key-pptx", "key-offline", "key-new"];
  const registry = createClassroomExportAttemptRegistry(() => {
    const next = keys.shift();
    assert.ok(next);
    return next;
  });

  assert.equal(registry.keyFor("draft-1", "pptx"), "key-pptx");
  assert.equal(registry.keyFor("draft-1", "offline_html"), "key-offline");
  assert.equal(registry.keyFor("draft-1", "pptx"), "key-pptx");
  registry.settle("draft-1", "pptx");
  assert.equal(registry.keyFor("draft-1", "pptx"), "key-new");
});

test("unresolved export attempts are bounded without evicting idempotency keys", () => {
  let sequence = 0;
  const registry = createClassroomExportAttemptRegistry(() => `key-${sequence++}`);
  for (let index = 0; index < 64; index += 1) {
    assert.equal(registry.keyFor(`draft-${index}`, "pptx"), `key-${index}`);
  }
  assert.throws(
    () => registry.keyFor("draft-overflow", "pptx"),
    /too many unresolved/i,
  );
  assert.equal(registry.keyFor("draft-0", "pptx"), "key-0");
  registry.settle("draft-0", "pptx");
  assert.equal(registry.keyFor("draft-overflow", "pptx"), "key-64");
});

test("MP4 availability and download URLs are controlled by stable policy", () => {
  const disabled = listClassroomExportOptions({ mp4Enabled: false });
  assert.deepEqual(
    disabled.map(option => [option.format, option.enabled, option.reason]),
    [
      ["classroom_zip", true, null],
      ["pptx", true, null],
      ["offline_html", true, null],
      ["mp4", false, MP4_EXPORT_DISABLED_REASON],
    ],
  );
  assert.equal(
    classroomExportDownloadUrl("export / 1"),
    "/api/v1/classroom-exports/export%20%2F%201/download",
  );
});

test("failed exports expose only the stable backend category and code", async () => {
  const failed = await withFetch(
    async () =>
      jsonResponse(
        exportPayload({
          status: "failed",
          progress_percent: 70,
          cancellable: false,
          retryable: true,
          error_category: "rendering",
          error_code: "pptx_materialization_failed",
        }),
      ),
    () => getClassroomExport("export-1", "pptx"),
  );
  assert.deepEqual(classroomExportFailureDetails(failed), {
    errorCategory: "rendering",
    errorCode: "pptx_materialization_failed",
  });
  assert.equal(
    classroomExportFailureDetails({ ...failed, status: "queued" }),
    null,
  );
});
