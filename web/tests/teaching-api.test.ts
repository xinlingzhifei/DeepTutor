import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  classroomNextRoute,
  confirmSelectedBatchOutlines,
  createTeachingClassroom,
  getTeachingReviewDetail,
  listTeachingBatches,
  listTeachingPublications,
  listTeachingReviews,
  publishTeachingClassroom,
  retryBatchItem,
  validateTeachingClassroom,
  TeachingApiError,
  type TeachingClassroomCreateInput,
} from "../lib/teaching-api";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const NOW = "2026-07-30T12:00:00+08:00";

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

function classroomPayload(overrides: Record<string, unknown> = {}) {
  return {
    assetId: "asset-1",
    draftId: "draft-1",
    jobId: "job-1",
    lifecycleState: "awaiting_outline",
    status: "awaiting_confirmation",
    title: "Motion",
    courseId: "course-a",
    classId: "class-a",
    ownerId: "teacher-a",
    revision: 3,
    outline: { title: "Motion outline" },
    document: null,
    classroomVersionId: null,
    confirmedOutlineSha256: null,
    validationReport: null,
    idempotencyKey: "classroom-request-1",
    ...overrides,
  };
}

function classroomInput(): TeachingClassroomCreateInput {
  return {
    title: "Motion",
    courseId: "course-a",
    classId: "class-a",
    objective: "Explain motion",
    gradeBand: "grade-8",
    audience: "intermediate",
    durationMinutes: 45,
    classroomMode: "full",
    webPolicy: "disabled",
    mediaPolicy: "image_audio",
    allowedWebDomains: [],
    templateId: "template-a",
    templateVersion: "1",
    knowledgePoints: [
      {
        knowledgePointId: "kp-motion",
        title: "Motion",
        description: "Describe displacement and velocity",
      },
    ],
    contentMode: "open_creation",
    openCreationAcknowledged: true,
    sourceType: null,
    sourceRef: null,
    requestedExports: ["classroom_zip"],
  };
}

function reviewPayload(overrides: Record<string, unknown> = {}) {
  return {
    id: "review-1",
    assetId: "asset-1",
    draftId: "draft-1",
    draftRevision: 3,
    documentSha256: "a".repeat(64),
    validationReportSha256: "b".repeat(64),
    submittedBy: "teacher-a",
    scope: "tenant",
    classId: null,
    status: "pending",
    warnings: [],
    reviewerId: null,
    comment: null,
    ...overrides,
  };
}

function classroomDocument() {
  return {
    schemaVersion: "1.0",
    classroomId: "asset-1",
    classroomVersionId: "version-1",
    contentMode: "open_creation",
    openCreation: true,
    openmaic: {
      dslVersion: "0.1.0",
      stage: { id: "stage-1", name: "Motion", createdAt: NOW, updatedAt: NOW },
      scenes: [
        {
          id: "slide-1",
          stageId: "stage-1",
          title: "Motion",
          order: 0,
          type: "slide",
          content: { type: "slide", canvas: { elements: [] } },
          actions: [],
        },
      ],
    },
    interactionIds: [],
    sourceRefs: [],
    knowledgePointMappings: [
      { knowledgePointId: "kp-motion", sceneIds: ["slide-1"], sourceRefs: [] },
    ],
    mediaManifest: [],
    fileSha256: SHA_A,
    exportManifest: [],
    generationMetadata: {
      generator: "test",
      generatorVersion: "1",
      modelId: "model",
      generatedAt: NOW,
      teachingBriefId: "brief-1",
      teachingBriefSha256: SHA_A,
      templateId: "template-1",
      templateVersion: "1",
    },
    auditMetadata: {
      templateId: "template-1",
      templateVersion: "1",
      teachingBriefId: "brief-1",
      teachingBriefSha256: SHA_A,
      parentClassroomVersionId: null,
    },
    validationResult: { valid: true, issues: [], validatedAt: NOW },
    migrationRecords: [],
  };
}

function reviewDetailPayload(overrides: Record<string, unknown> = {}) {
  return {
    review: reviewPayload(),
    title: "Motion",
    courseId: "course-a",
    targetClassId: "class-a",
    document: classroomDocument(),
    validationReport: { valid: true },
    sourceFragments: [
      {
        fragmentId: "fragment-1",
        sourceId: "source-1",
        text: "Velocity is displacement over time.",
        contentSha256: SHA_A,
      },
    ],
    baseline: {
      versionId: "version-0",
      versionNumber: 1,
      documentSha256: SHA_B,
    },
    changedPaths: ["/openmaic/scenes/0"],
    ...overrides,
  };
}

test("awaiting confirmation maps to the outline review route", () => {
  assert.equal(
    classroomNextRoute({
      assetId: "asset-1",
      status: "awaiting_confirmation",
    }),
    "/teaching/classrooms/asset-1/outline",
  );
});

test("editing maps to the editor route", () => {
  assert.equal(
    classroomNextRoute({ assetId: "asset-1", status: "editing" }),
    "/teaching/classrooms/asset-1/edit",
  );
});

test("classroom routes encode opaque asset ids and keep non-editor states on the list", () => {
  assert.equal(
    classroomNextRoute({ assetId: "asset / 1", status: "generating_content" }),
    "/teaching/classrooms",
  );
  assert.equal(
    classroomNextRoute({ assetId: "asset / 1", status: "editing" }),
    "/teaching/classrooms/asset%20%2F%201/edit",
  );
});

test("classroom creation preserves the exact backend brief contract", async () => {
  let request: { input: RequestInfo | URL; init?: RequestInit } | undefined;
  const result = await withFetch(
    async (input, init) => {
      request = { input, init };
      return jsonResponse(classroomPayload());
    },
    () => createTeachingClassroom(classroomInput(), "classroom-request-1"),
  );

  assert.equal(request?.input, "/api/v1/classrooms");
  assert.equal(request?.init?.method, "POST");
  assert.deepEqual(request?.init?.headers, {
    "Content-Type": "application/json",
    "Idempotency-Key": "classroom-request-1",
  });
  assert.deepEqual(JSON.parse(String(request?.init?.body)), classroomInput());
  assert.equal(result.assetId, "asset-1");
  assert.equal(result.revision, 3);
});

test("classroom responses reject unknown fields instead of silently widening", async () => {
  await assert.rejects(
    () =>
      withFetch(
        async () => jsonResponse(classroomPayload({ unexpected: true })),
        () => createTeachingClassroom(classroomInput(), "classroom-request-1"),
      ),
    (error: unknown) =>
      error instanceof TeachingApiError && /unexpected key/.test(error.message),
  );
});

test("validation reports require a boolean valid flag and exact issue enums", async () => {
  await assert.rejects(
    () =>
      withFetch(
        async () =>
          jsonResponse(
            classroomPayload({ validationReport: { valid: "yes", issues: [] } }),
          ),
        () => validateTeachingClassroom("asset-1"),
      ),
    (error: unknown) =>
      error instanceof TeachingApiError && /valid/.test(error.message),
  );

  await assert.rejects(
    () =>
      withFetch(
        async () =>
          jsonResponse(
            classroomPayload({
              validationReport: {
                valid: false,
                issues: [
                  {
                    severity: "fatal",
                    code: "BAD",
                    message: "bad",
                    path: "/openmaic",
                  },
                ],
              },
            }),
          ),
        () => validateTeachingClassroom("asset-1"),
      ),
    (error: unknown) =>
      error instanceof TeachingApiError && /severity/.test(error.message),
  );
});

test("review responses reject invalid scopes, statuses, and SHA-256 values", async () => {
  for (const review of [
    reviewPayload({ scope: "private" }),
    reviewPayload({ status: "mystery" }),
    reviewPayload({ documentSha256: "not-a-sha" }),
  ]) {
    await assert.rejects(
      () =>
        withFetch(
          async () => jsonResponse({ items: [review] }),
          () => listTeachingReviews(),
        ),
      TeachingApiError,
    );
  }
});

test("review warnings and scope bindings stay inside the exact review contract", async () => {
  for (const review of [
    reviewPayload({ scope: "class", classId: null }),
    reviewPayload({ scope: "tenant", classId: "class-a" }),
    reviewPayload({
      warnings: [
        {
          severity: "notice",
          code: "SOURCE_TRACE",
          message: "Review this claim",
          path: "/openmaic/scenes/0",
        },
      ],
    }),
    reviewPayload({
      warnings: [
        {
          severity: "warning",
          code: "SOURCE_TRACE",
          message: "Review this claim",
          path: "/openmaic/scenes/0",
          extra: true,
        },
      ],
    }),
  ]) {
    await assert.rejects(
      () =>
        withFetch(
          async () => jsonResponse({ items: [review] }),
          () => listTeachingReviews(),
        ),
      TeachingApiError,
    );
  }
});

test("batch and item statuses reject unknown state-machine values", async () => {
  const batch = {
    id: "batch-1",
    tenantId: "tenant-a",
    actorId: "teacher-a",
    status: "running",
    itemCount: 1,
    succeededCount: 0,
    failedCount: 0,
    items: [
      {
        id: "item-1",
        batchId: "batch-1",
        status: "queued",
        generationJobId: null,
        classroomDraftId: null,
        classroomAssetId: null,
      },
    ],
    createdAt: null,
    updatedAt: null,
  };
  for (const invalid of [
    { ...batch, status: "mystery" },
    { ...batch, items: [{ ...batch.items[0], status: "mystery" }] },
  ]) {
    await assert.rejects(
      () =>
        withFetch(
          async () => jsonResponse({ items: [invalid] }),
          () => listTeachingBatches(),
        ),
      TeachingApiError,
    );
  }
});

test("review detail uses the immutable review evidence route and parses exact evidence", async () => {
  let requested = "";
  const detail = await withFetch(
    async input => {
      requested = String(input);
      return jsonResponse(reviewDetailPayload());
    },
    () => getTeachingReviewDetail("review / 1"),
  );

  assert.equal(requested, "/api/v1/classroom-reviews/review%20%2F%201");
  assert.equal(detail.review.id, "review-1");
  assert.equal(detail.sourceFragments[0]?.text, "Velocity is displacement over time.");
  assert.equal(detail.baseline?.versionNumber, 1);
  assert.deepEqual(detail.changedPaths, ["/openmaic/scenes/0"]);
});

test("review detail rejects unknown evidence fields and invalid fragment hashes", async () => {
  for (const detail of [
    reviewDetailPayload({ unexpected: true }),
    reviewDetailPayload({
      sourceFragments: [
        {
          fragmentId: "fragment-1",
          sourceId: "source-1",
          text: "Evidence",
          contentSha256: "bad",
        },
      ],
    }),
  ]) {
    await assert.rejects(
      () =>
        withFetch(
          async () => jsonResponse(detail),
          () => getTeachingReviewDetail("review-1"),
        ),
      TeachingApiError,
    );
  }
});

test("organization library parses only publication records and approved candidates", async () => {
  let requested = "";
  const result = await withFetch(
    async input => {
      requested = String(input);
      return jsonResponse({
        items: [
          {
            publicationId: "publication-1",
            versionId: "version-2",
            assetId: "asset-1",
            versionNumber: 2,
            title: "Motion",
            courseId: "course-a",
            documentSha256: SHA_A,
            publishedBy: "publisher-a",
            createdAt: NOW,
          },
        ],
        candidates: [
          {
            reviewId: "review-1",
            assetId: "asset-2",
            title: "Energy",
            courseId: "course-a",
            draftRevision: 4,
            documentSha256: SHA_B,
            submittedBy: "teacher-b",
          },
        ],
      });
    },
    () => listTeachingPublications(),
  );

  assert.equal(requested, "/api/v1/classroom-publications");
  assert.equal(result.items[0]?.publicationId, "publication-1");
  assert.equal(result.candidates[0]?.reviewId, "review-1");
});

test("tenant publication uses a stable idempotency header and exact scope body", async () => {
  let request: { input: RequestInfo | URL; init?: RequestInit } | undefined;
  const result = await withFetch(
    async (input, init) => {
      request = { input, init };
      return jsonResponse({
        versionId: "version-2",
        assetId: "asset-1",
        versionNumber: 2,
        documentSha256: SHA_A,
        publicationScope: "tenant",
        classId: null,
        idempotencyKey: "tenant-publication-1",
      });
    },
    () => publishTeachingClassroom("asset / 1", "tenant-publication-1"),
  );

  assert.equal(request?.input, "/api/v1/classrooms/asset%20%2F%201/publish");
  assert.deepEqual(request?.init?.headers, {
    "Content-Type": "application/json",
    "Idempotency-Key": "tenant-publication-1",
  });
  assert.deepEqual(JSON.parse(String(request?.init?.body)), {
    scope: "tenant",
    classId: null,
  });
  assert.equal(result.versionId, "version-2");
});

test("selected batch confirmations send only reviewed revisions and hashes", async () => {
  let request: { input: RequestInfo | URL; init?: RequestInit } | undefined;
  await withFetch(
    async (input, init) => {
      request = { input, init };
      return jsonResponse({
        id: "batch-1",
        tenantId: "tenant-a",
        actorId: "author-a",
        status: "running",
        itemCount: 2,
        succeededCount: 0,
        failedCount: 0,
        items: [],
        createdAt: null,
        updatedAt: null,
      });
    },
    () =>
      confirmSelectedBatchOutlines("batch / 1", [
        { itemId: "item-a", revision: 3, outlineSha256: "a".repeat(64) },
      ]),
  );

  assert.equal(
    request?.input,
    "/api/v1/classroom-batches/batch%20%2F%201/confirm-outlines",
  );
  assert.deepEqual(JSON.parse(String(request?.init?.body)), {
    items: [
      { itemId: "item-a", revision: 3, outlineSha256: "a".repeat(64) },
    ],
  });
});

test("single-item retry never widens to sibling items", async () => {
  const inputs: string[] = [];
  await withFetch(
    async (input) => {
      inputs.push(String(input));
      return jsonResponse({
        parentItemId: "failed-b",
        item: {
          id: "failed-b-retry-1",
          batchId: "batch-1",
          status: "queued",
          generationJobId: "job-retry-1",
          classroomDraftId: "draft-b",
          classroomAssetId: "asset-b",
        },
      });
    },
    () => retryBatchItem("batch-1", "failed-b"),
  );

  assert.deepEqual(inputs, [
    "/api/v1/classroom-batches/batch-1/items/failed-b/retry",
  ]);
});

test("the teaching editor merges imports and keeps saved parent state canonical", () => {
  const source = readFileSync(
    path.join(
      process.cwd(),
      "app",
      "(utility)",
      "teaching",
      "classrooms",
      "[assetId]",
      "edit",
      "page.tsx",
    ),
    "utf8",
  );

  assert.match(source, /useRef<ClassroomEditorHandle>/);
  assert.match(source, /ref=\{editorRef\}/);
  assert.match(source, /draftMediaAssetId=\{classroom\.assetId\}/);
  assert.doesNotMatch(source, /editorRef\.current\?\.importSlides/);
  assert.match(source, /const editor = editorRef\.current;[\s\S]*if \(!editor\)[\s\S]*throw new Error/);
  assert.match(source, /editor\.importSlides\(result\)/);
  assert.match(source, /onDirtyChange=\{handleEditorDirtyChange\}/);
  assert.match(source, /validationReport:\s*null/);
  assert.match(source, /disabled=\{workflowFrozen \|\| editorDirty\}/);
  assert.match(
    source,
    /<ClassroomExportMenu[\s\S]*disabled=\{editorDirty \|\| operationLocked\}/,
  );
  assert.doesNotMatch(source, /Boolean\(workflowMessage\)/);
  assert.match(
    source,
    /const workflowFrozen =[\s\S]*submissionComplete \|\|[\s\S]*isTeachingClassroomEditable\(classroom\.lifecycleState\)/,
  );
  assert.match(source, /!classroom\.validationReport\?\.valid/);
  assert.match(
    source,
    /onSaved=\{\(document, nextRevision\)\s*=>\s*\{[\s\S]*const next = \{ \.\.\.current, document, validationReport: null \};[\s\S]*setClassroom\(next\)[\s\S]*setRevision\(nextRevision\)/,
  );
  assert.match(source, /current\.assetId !== classroom\.assetId/);
});

test("the classroom export menu refuses stale draft exports while disabled", () => {
  const source = readFileSync(
    path.join(
      process.cwd(),
      "components",
      "classroom",
      "ClassroomExportMenu.tsx",
    ),
    "utf8",
  );

  assert.match(source, /disabled\?: boolean/);
  assert.match(
    source,
    /const startExport[\s\S]*if \(disabled \|\| pendingFormat \|\| controllerRef\.current\) return/,
  );
  assert.match(
    source,
    /disabled=\{disabled \|\| !option\.enabled \|\| pendingFormat !== null\}/,
  );
  assert.match(source, /classroomExportFailureDetails/);
  assert.match(source, /failureDetails\.errorCategory/);
  assert.match(source, /failureDetails\.errorCode/);
});
