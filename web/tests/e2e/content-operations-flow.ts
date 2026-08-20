import { createHash } from "node:crypto";

import type { Page, Request, Route } from "@playwright/test";

import {
  expect,
  test,
  type TeachingDownloadState,
} from "./support/teaching-flow-test";

const NOW = "2026-08-09T00:00:00.000Z";
const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const BATCH_OUTLINE = {
  title: "Batch energy outline",
  scenes: [{ title: "Compare two energy pathways" }],
};

type ApiCall = {
  body: unknown;
  headers: Record<string, string>;
};

function requestCall(request: Request): ApiCall {
  const raw = request.postData();
  return {
    body: raw ? (JSON.parse(raw) as unknown) : null,
    headers: request.headers(),
  };
}

async function json(route: Route, payload: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map(key => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
    .join(",")}}`;
}

function classroomDocument(assetId: string, title: string) {
  return {
    schemaVersion: "1.0",
    classroomId: assetId,
    classroomVersionId: `draft-version-${assetId}`,
    contentMode: "open_creation",
    openCreation: true,
    openmaic: {
      dslVersion: "0.1.0",
      stage: { id: `stage-${assetId}`, name: title, createdAt: NOW, updatedAt: NOW },
      scenes: [
        {
          id: `slide-${assetId}`,
          stageId: `stage-${assetId}`,
          title,
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
      {
        knowledgePointId: `kp-${assetId}`,
        sceneIds: [`slide-${assetId}`],
        sourceRefs: [],
      },
    ],
    mediaManifest: [],
    fileSha256: SHA_A,
    exportManifest: [],
    generationMetadata: {
      generator: "e2e",
      generatorVersion: "1",
      modelId: "model-e2e",
      generatedAt: NOW,
      teachingBriefId: `brief-${assetId}`,
      teachingBriefSha256: SHA_A,
      templateId: "guided-classroom",
      templateVersion: "1",
    },
    auditMetadata: {
      templateId: "guided-classroom",
      templateVersion: "1",
      teachingBriefId: `brief-${assetId}`,
      teachingBriefSha256: SHA_A,
      parentClassroomVersionId: null,
    },
    validationResult: { valid: true, issues: [], validatedAt: NOW },
    migrationRecords: [],
  };
}

function classroomPayload(assetId: string, title: string) {
  return {
    assetId,
    draftId: `draft-${assetId}`,
    jobId: null,
    lifecycleState: "outline_review",
    status: "awaiting_confirmation",
    title,
    courseId: "course-operations",
    classId: "class-operations",
    ownerId: "teacher-operations",
    revision: 4,
    outline: BATCH_OUTLINE,
    document: null,
    classroomVersionId: null,
    confirmedOutlineSha256: null,
    validationReport: null,
    idempotencyKey: null,
  };
}

function batchItem(
  id: string,
  status:
    | "queued"
    | "running"
    | "awaiting_confirmation"
    | "succeeded"
    | "failed",
  assetId: string | null,
) {
  return {
    id,
    batchId: "batch-operations",
    status,
    generationJobId: `job-${id}`,
    classroomDraftId: assetId ? `draft-${assetId}` : null,
    classroomAssetId: assetId,
  };
}

function reviewPayload(
  kind: "self" | "other",
  approved = false,
) {
  const self = kind === "self";
  return {
    id: self ? "review-self" : "review-other",
    assetId: self ? "asset-review-self" : "asset-review-other",
    draftId: self ? "draft-review-self" : "draft-review-other",
    draftRevision: self ? 5 : 6,
    documentSha256: self ? SHA_A : SHA_B,
    validationReportSha256: self ? SHA_B : SHA_A,
    submittedBy: self ? "reviewer-e2e" : "teacher-other",
    scope: "tenant",
    classId: null,
    status: approved && !self ? "approved" : "pending",
    warnings: [],
    reviewerId: approved && !self ? "reviewer-e2e" : null,
    comment: approved && !self ? "Evidence verified" : null,
  };
}

function reviewDetail(kind: "self" | "other", approved = false) {
  const self = kind === "self";
  const review = reviewPayload(kind, approved);
  return {
    review,
    title: self ? "Self review classroom" : "Independent review classroom",
    courseId: "course-operations",
    targetClassId: "class-operations",
    document: classroomDocument(review.assetId, self ? "Self evidence" : "Independent evidence"),
    validationReport: { valid: true },
    sourceFragments: [
      {
        fragmentId: self ? "fragment-self" : "fragment-independent",
        sourceId: "source-operations",
        text: self
          ? "Self review evidence must not unlock decisions."
          : "Independent evidence proves the reviewed classroom claim.",
        contentSha256: self ? SHA_A : SHA_B,
      },
    ],
    baseline: {
      versionId: "version-baseline",
      versionNumber: 1,
      documentSha256: SHA_A,
    },
    changedPaths: ["/openmaic/scenes/0"],
  };
}

async function installContentOperationsBackend(page: Page) {
  const state = {
    retried: false,
    confirmed: false,
    approved: false,
    published: false,
    retryCalls: [] as ApiCall[],
    confirmationCalls: [] as ApiCall[],
    decisionCalls: [] as ApiCall[],
    publicationCalls: [] as ApiCall[],
    unexpected: [] as string[],
  };

  const currentBatch = () => {
    const items = state.retried
      ? [
          batchItem(
            "item-ready",
            state.confirmed ? "running" : "awaiting_confirmation",
            "asset-batch-ready",
          ),
          batchItem("item-failed-retry-1", "queued", null),
          batchItem("item-succeeded", "succeeded", "asset-batch-success"),
        ]
      : [
          batchItem("item-ready", "awaiting_confirmation", "asset-batch-ready"),
          batchItem("item-failed", "failed", null),
          batchItem("item-succeeded", "succeeded", "asset-batch-success"),
        ];
    return {
      id: "batch-operations",
      tenantId: "tenant-e2e",
      actorId: "content-operator",
      status: state.retried ? "running" : "partially_succeeded",
      itemCount: 3,
      succeededCount: 1,
      failedCount: state.retried ? 0 : 1,
      items,
      createdAt: NOW,
      updatedAt: NOW,
    };
  };

  const publications = () => ({
    items: [
      {
        publicationId: "publication-existing",
        versionId: "version-existing",
        assetId: "asset-existing",
        versionNumber: 1,
        title: "Existing tenant classroom",
        courseId: "course-operations",
        documentSha256: SHA_A,
        publishedBy: "reviewer-e2e",
        createdAt: NOW,
      },
      ...(state.published
        ? [
            {
              publicationId: "publication-approved",
              versionId: "version-published-e2e",
              assetId: "asset-review-other",
              versionNumber: 2,
              title: "Independent review classroom",
              courseId: "course-operations",
              documentSha256: SHA_B,
              publishedBy: "reviewer-e2e",
              createdAt: NOW,
            },
          ]
        : []),
    ],
    candidates:
      state.approved && !state.published
        ? [
            {
              reviewId: "review-other",
              assetId: "asset-review-other",
              title: "Independent review classroom",
              courseId: "course-operations",
              draftRevision: 6,
              documentSha256: SHA_B,
              submittedBy: "teacher-other",
            },
          ]
        : [],
  });

  await page.route("**/api/v1/**", async route => {
    const request = route.request();
    const method = request.method();
    const pathname = new URL(request.url()).pathname;

    if (method === "GET" && pathname === "/api/v1/settings") {
      await json(route, { catalog: {} });
      return;
    }
    if (method === "GET" && pathname === "/api/v1/sessions") {
      await json(route, { sessions: [] });
      return;
    }
    if (method === "GET" && pathname === "/api/v1/auth/status") {
      await json(route, {
        enabled: false,
        authenticated: true,
        user_id: "reviewer-e2e",
        role: "reviewer",
        is_admin: false,
        active_tenant_id: "tenant-e2e",
        tenants: [],
      });
      return;
    }
    if (method === "GET" && pathname === "/api/v1/auth/is_first_user") {
      await json(route, { is_first_user: false });
      return;
    }
    if (method === "GET" && pathname === "/api/v1/classroom-batches") {
      await json(route, { items: [currentBatch()] });
      return;
    }
    if (
      method === "GET" &&
      pathname === "/api/v1/classrooms/asset-batch-ready"
    ) {
      await json(
        route,
        classroomPayload("asset-batch-ready", "Batch-ready classroom"),
      );
      return;
    }
    if (
      method === "POST" &&
      pathname ===
        "/api/v1/classroom-batches/batch-operations/items/item-failed/retry"
    ) {
      state.retryCalls.push(requestCall(request));
      state.retried = true;
      await json(route, {
        parentItemId: "item-failed",
        item: batchItem("item-failed-retry-1", "queued", null),
      });
      return;
    }
    if (
      method === "POST" &&
      pathname === "/api/v1/classroom-batches/batch-operations/confirm-outlines"
    ) {
      state.confirmationCalls.push(requestCall(request));
      state.confirmed = true;
      await json(route, currentBatch());
      return;
    }
    if (method === "GET" && pathname === "/api/v1/classroom-reviews") {
      await json(route, {
        items: [reviewPayload("self"), reviewPayload("other", state.approved)],
      });
      return;
    }
    if (
      method === "GET" &&
      (pathname === "/api/v1/classroom-reviews/review-self" ||
        pathname === "/api/v1/classroom-reviews/review-other")
    ) {
      await json(
        route,
        pathname.endsWith("review-self")
          ? reviewDetail("self")
          : reviewDetail("other", state.approved),
      );
      return;
    }
    if (
      method === "POST" &&
      pathname === "/api/v1/classroom-reviews/review-other/approve"
    ) {
      state.decisionCalls.push(requestCall(request));
      state.approved = true;
      await json(route, reviewPayload("other", true));
      return;
    }
    if (method === "GET" && pathname === "/api/v1/classroom-publications") {
      await json(route, publications());
      return;
    }
    if (
      method === "POST" &&
      pathname === "/api/v1/classrooms/asset-review-other/publish"
    ) {
      const call = requestCall(request);
      state.publicationCalls.push(call);
      if (state.publicationCalls.length === 1) {
        await json(route, { detail: "ambiguous publish outcome" }, 503);
        return;
      }
      state.published = true;
      await json(route, {
        versionId: "version-published-e2e",
        assetId: "asset-review-other",
        versionNumber: 2,
        documentSha256: SHA_B,
        publicationScope: "tenant",
        classId: null,
        idempotencyKey: call.headers["idempotency-key"],
      });
      return;
    }

    state.unexpected.push(`${method} ${pathname}`);
    await json(route, { detail: "Unexpected E2E API request" }, 404);
  });

  return state;
}

export async function runContentOperationsFlow({
  page,
  teachingDownload,
}: {
  page: Page;
  teachingDownload: TeachingDownloadState;
}) {
  test.setTimeout(120_000);
  await page.addInitScript(() => {
    localStorage.setItem("deeptutor-language", "en");
  });
  const state = await installContentOperationsBackend(page);
  const downloadCallsBefore = teachingDownload.downloadCalls;

  await page.goto("/teaching/batches");
  await expect(page.getByText("partially_succeeded", { exact: false })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "item-failed", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retry item" }).click();
  await expect(
    page.getByRole("heading", { name: "item-failed-retry-1", exact: true }),
  ).toBeVisible();
  expect(state.retryCalls).toHaveLength(1);
  expect(state.retryCalls[0].body).toBeNull();

  await page.getByLabel("Select reviewed outline").check();
  await page.getByRole("button", { name: "Confirm selected outlines" }).click();
  await expect(
    page.getByRole("heading", { name: "item-ready", exact: true }),
  ).toBeVisible();
  expect(state.confirmationCalls).toHaveLength(1);
  expect(state.confirmationCalls[0].body).toEqual({
    items: [
      {
        itemId: "item-ready",
        revision: 4,
        outlineSha256: createHash("sha256")
          .update(canonicalJson(BATCH_OUTLINE))
          .digest("hex"),
      },
    ],
  });

  await page.goto("/teaching/reviews");
  await expect(
    page.getByText("Self review evidence must not unlock decisions."),
  ).toBeVisible();
  await expect(
    page.getByText("You cannot review your own submission.", { exact: false }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Reject" })).toHaveCount(0);

  await page.getByRole("button", { name: /asset-review-other/ }).click();
  await expect(
    page.getByText("Independent evidence proves the reviewed classroom claim."),
  ).toBeVisible();
  const versionDiff = page
    .getByRole("heading", { name: "Revision difference", exact: true })
    .locator("..");
  await expect(
    versionDiff.getByText("version-baseline", { exact: false }),
  ).toBeVisible();
  await expect(
    versionDiff.getByText("/openmaic/scenes/0", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Decision comment").fill("Evidence verified");
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
  expect(state.decisionCalls).toHaveLength(1);
  expect(state.decisionCalls[0].body).toEqual({ comment: "Evidence verified" });

  await page.goto("/teaching/library");
  await expect(
    page.getByRole("heading", {
      name: "Existing tenant classroom",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Independent review classroom",
      exact: true,
    }),
  ).toBeVisible();
  const publish = page.getByRole("button", { name: "Publish to tenant library" });
  await publish.click();
  await expect(
    page.locator('[role="alert"]').filter({ hasText: "503" }),
  ).toContainText("ambiguous publish outcome");
  await publish.click();
  await expect(page.getByRole("button", { name: "Publish to tenant library" })).toHaveCount(0);
  const publishedCard = page.getByRole("article").filter({
    has: page.getByRole("heading", {
      name: "Independent review classroom",
      exact: true,
    }),
  });
  await expect(
    publishedCard.getByText("version-published-e2e", { exact: false }),
  ).toBeVisible();

  expect(state.publicationCalls).toHaveLength(2);
  for (const call of state.publicationCalls) {
    expect(call.body).toEqual({ scope: "tenant", classId: null });
    expect(call.headers["idempotency-key"]).toMatch(/^tenant-publication-/);
  }
  expect(state.publicationCalls[1].headers["idempotency-key"]).toBe(
    state.publicationCalls[0].headers["idempotency-key"],
  );
  expect(teachingDownload.downloadCalls).toBe(downloadCallsBefore);
  expect(state.unexpected).toEqual([]);
}
