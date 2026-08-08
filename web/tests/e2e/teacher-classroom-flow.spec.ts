import type { Page, Request, Route } from "@playwright/test";

import { expect, test } from "./support/teaching-flow-test";

const NOW = "2026-08-09T00:00:00.000Z";
const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const ASSET_ID = "asset-teacher-flow";
const DOWNLOAD_PATH = "/api/v1/classroom-exports/export-teacher-2/download";

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

function classroomDocument() {
  return {
    schemaVersion: "1.0",
    classroomId: ASSET_ID,
    classroomVersionId: "draft-version-teacher",
    contentMode: "source_grounded",
    openCreation: false,
    openmaic: {
      dslVersion: "0.1.0",
      stage: {
        id: "stage-teacher",
        name: "Energy transfer",
        createdAt: NOW,
        updatedAt: NOW,
      },
      scenes: [
        {
          id: "slide-teacher",
          stageId: "stage-teacher",
          title: "Energy pathway",
          order: 0,
          type: "slide",
          content: { type: "slide", canvas: { elements: [] } },
          actions: [],
        },
      ],
    },
    interactionIds: [],
    sourceRefs: [
      {
        citationId: "citation-energy",
        sourceId: "kb-energy",
        fragmentId: "fragment-energy",
      },
    ],
    knowledgePointMappings: [
      {
        knowledgePointId: "kp-energy-transfer",
        sceneIds: ["slide-teacher"],
        sourceRefs: [
          {
            citationId: "citation-energy",
            sourceId: "kb-energy",
            fragmentId: "fragment-energy",
          },
        ],
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
      teachingBriefId: "brief-teacher",
      teachingBriefSha256: SHA_A,
      templateId: "guided-classroom",
      templateVersion: "1",
    },
    auditMetadata: {
      templateId: "guided-classroom",
      templateVersion: "1",
      teachingBriefId: "brief-teacher",
      teachingBriefSha256: SHA_A,
      parentClassroomVersionId: null,
    },
    validationResult: { valid: true, issues: [], validatedAt: NOW },
    migrationRecords: [],
  };
}

function exportJob(status: "failed" | "succeeded", attempt: number) {
  const succeeded = status === "succeeded";
  return {
    job_id: `export-teacher-${attempt}`,
    job_kind: "export",
    phase: "export",
    status,
    progress_percent: succeeded ? 100 : 70,
    waiting_reason: null,
    cancellable: false,
    retryable: !succeeded,
    outline: null,
    error_category: succeeded ? null : "rendering",
    error_code: succeeded ? null : "pptx_materialization_failed",
    retry_of_job_id: null,
    export_format: "pptx",
    download_ready: succeeded,
  };
}

async function installTeacherBackend(page: Page) {
  const initialOutline = {
    title: "Energy transfer",
    scenes: [{ title: "Trace the energy pathway" }],
  };
  const state = {
    phase: "outline" as "outline" | "editing" | "submitted",
    revision: 1,
    outline: initialOutline as Record<string, unknown>,
    validated: false,
    createCalls: [] as ApiCall[],
    outlineCalls: [] as ApiCall[],
    confirmCalls: 0,
    validateCalls: 0,
    submitCalls: [] as ApiCall[],
    exportCalls: [] as ApiCall[],
    unexpected: [] as string[],
  };

  const validationReport = {
    valid: true,
    issues: [],
    draftRevision: 3,
    documentSha256: SHA_A,
  };
  const classroom = () => ({
    assetId: ASSET_ID,
    draftId: "draft-teacher-flow",
    jobId: null,
    lifecycleState:
      state.phase === "outline"
        ? "outline_review"
        : state.phase === "editing"
          ? "editing"
          : "submitted",
    status:
      state.phase === "outline"
        ? "awaiting_confirmation"
        : state.phase === "editing"
          ? "editing"
          : "submitted",
    title: "Energy Transfer Lab",
    courseId: "course-energy",
    classId: "class-7a",
    ownerId: "teacher-e2e",
    revision: state.revision,
    outline: state.outline,
    document: state.phase === "outline" ? null : classroomDocument(),
    classroomVersionId: null,
    confirmedOutlineSha256: state.phase === "outline" ? null : SHA_A,
    validationReport: state.validated ? validationReport : null,
    idempotencyKey: state.createCalls[0]?.headers["idempotency-key"] ?? null,
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
        user_id: "teacher-e2e",
        role: "teacher",
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
    if (method === "GET" && pathname === "/api/v1/teaching/courses") {
      await json(route, {
        items: [
          {
            id: "course-energy",
            title: "Energy Science",
            status: "active",
            createdAt: NOW,
          },
        ],
      });
      return;
    }
    if (
      method === "GET" &&
      pathname === "/api/v1/teaching/courses/course-energy/classes"
    ) {
      await json(route, {
        items: [
          {
            id: "class-7a",
            courseId: "course-energy",
            name: "Class 7A",
            status: "active",
            createdAt: NOW,
          },
        ],
      });
      return;
    }
    if (method === "GET" && pathname === "/api/v1/teaching/sources") {
      await json(route, {
        items: [
          {
            bindingId: "binding-kb-energy",
            sourceType: "knowledge_base",
            sourceId: "kb-energy",
            filename: null,
            sha256: SHA_A,
            sizeBytes: null,
            courseId: "course-energy",
            classId: "class-7a",
            createdAt: NOW,
          },
        ],
      });
      return;
    }
    if (method === "POST" && pathname === "/api/v1/classrooms") {
      state.createCalls.push(requestCall(request));
      await json(route, classroom());
      return;
    }
    if (method === "GET" && pathname === `/api/v1/classrooms/${ASSET_ID}`) {
      await json(route, classroom());
      return;
    }
    if (
      method === "PUT" &&
      pathname === `/api/v1/classrooms/${ASSET_ID}/outline`
    ) {
      const call = requestCall(request);
      state.outlineCalls.push(call);
      state.outline = (call.body as { outline: Record<string, unknown> }).outline;
      state.revision = 2;
      await json(route, classroom());
      return;
    }
    if (
      method === "POST" &&
      pathname === `/api/v1/classrooms/${ASSET_ID}/confirm-outline`
    ) {
      state.confirmCalls += 1;
      state.phase = "editing";
      state.revision = 3;
      await json(route, classroom());
      return;
    }
    if (
      method === "GET" &&
      pathname === `/api/v1/classrooms/${ASSET_ID}/draft`
    ) {
      await json(route, classroom());
      return;
    }
    if (
      method === "POST" &&
      pathname === `/api/v1/classrooms/${ASSET_ID}/validate`
    ) {
      state.validateCalls += 1;
      state.validated = true;
      await json(route, classroom());
      return;
    }
    if (
      method === "POST" &&
      pathname === `/api/v1/classrooms/${ASSET_ID}/submit`
    ) {
      state.submitCalls.push(requestCall(request));
      state.phase = "submitted";
      await json(route, {
        id: "review-teacher-flow",
        assetId: ASSET_ID,
        draftId: "draft-teacher-flow",
        draftRevision: 3,
        documentSha256: SHA_A,
        validationReportSha256: SHA_B,
        submittedBy: "teacher-e2e",
        scope: "tenant",
        classId: null,
        status: "pending",
        warnings: [],
        reviewerId: null,
        comment: null,
      });
      return;
    }
    if (
      method === "POST" &&
      pathname === `/api/v1/classrooms/${ASSET_ID}/draft/exports`
    ) {
      state.exportCalls.push(requestCall(request));
      await json(
        route,
        exportJob(state.exportCalls.length === 1 ? "failed" : "succeeded", state.exportCalls.length),
      );
      return;
    }
    if (method === "GET" && pathname === DOWNLOAD_PATH) {
      await route.continue();
      return;
    }
    state.unexpected.push(`${method} ${pathname}`);
    await json(route, { detail: "Unexpected E2E API request" }, 404);
  });

  return state;
}

test.use({ locale: "en-US", timezoneId: "UTC" });
test.describe.configure({ mode: "serial" });

test("teacher creates, confirms, submits a frozen draft, and retries export", async ({
  page,
  teachingDownload,
}) => {
  test.setTimeout(180_000);
  await page.addInitScript(() => {
    localStorage.setItem("deeptutor-language", "en");
  });
  const state = await installTeacherBackend(page);
  const downloadCallsBefore = teachingDownload.downloadCalls;

  await page.goto("/teaching/classrooms/new");
  const form = page.getByTestId("teaching-brief-form");
  await expect(form).toBeVisible({ timeout: 60_000 });
  const course = form.getByRole("combobox", {
    name: "Course",
    exact: true,
  });
  const classroom = form.getByRole("combobox", {
    name: "Class",
    exact: true,
  });
  await expect(course).toHaveValue("course-energy", { timeout: 60_000 });
  await expect(classroom).toHaveValue("class-7a", { timeout: 60_000 });
  await form.getByLabel("Classroom title").fill("Energy Transfer Lab");
  await form
    .getByLabel("Teaching objective")
    .fill("Explain how energy moves through a system");
  await form.getByLabel("Grade band").fill("7");
  await form
    .locator("label")
    .filter({ hasText: "Media policy" })
    .getByRole("combobox")
    .selectOption("text_only");
  await form
    .getByRole("heading", { name: "Source and creation mode", exact: true })
    .locator("..")
    .getByRole("combobox", { name: "Source", exact: true })
    .selectOption("knowledge_base:binding-kb-energy");
  await form.getByLabel("Primary knowledge point").fill("Energy transfer");
  await form
    .getByLabel("Knowledge point description")
    .fill("Trace energy from source to receiver");
  const generate = form.getByRole("button", {
    name: "Generate outline",
    exact: true,
  });
  await expect(generate).toBeEnabled({ timeout: 60_000 });
  await generate.click();
  await expect.poll(() => state.createCalls.length).toBe(1);

  await expect(page).toHaveURL(
    new RegExp(`/teaching/classrooms/${ASSET_ID}/outline$`),
    { timeout: 60_000 },
  );
  expect(state.createCalls).toHaveLength(1);
  expect(state.createCalls[0].body).toEqual({
    title: "Energy Transfer Lab",
    courseId: "course-energy",
    classId: "class-7a",
    objective: "Explain how energy moves through a system",
    gradeBand: "7",
    audience: "intermediate",
    durationMinutes: 45,
    classroomMode: "full",
    webPolicy: "disabled",
    mediaPolicy: "text_only",
    allowedWebDomains: [],
    templateId: "guided-classroom",
    templateVersion: "1",
    knowledgePoints: [
      {
        knowledgePointId: "kp-energy-transfer",
        title: "Energy transfer",
        description: "Trace energy from source to receiver",
      },
    ],
    contentMode: "source_grounded",
    openCreationAcknowledged: false,
    sourceType: "knowledge_base",
    sourceRef: "kb-energy",
    requestedExports: ["classroom_zip"],
  });
  expect(state.createCalls[0].headers["idempotency-key"]).toMatch(
    /^teaching-attempt-/,
  );

  const editedOutline = {
    title: "Energy transfer",
    scenes: [{ title: "Trace, compare, explain" }],
  };
  await page.getByLabel("Outline JSON").fill(JSON.stringify(editedOutline));
  await page.getByRole("button", { name: "Confirm and generate content" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/teaching/classrooms/${ASSET_ID}/edit$`),
    { timeout: 60_000 },
  );
  expect(state.outlineCalls).toHaveLength(1);
  expect(state.outlineCalls[0].body).toEqual({ outline: editedOutline });
  expect(state.outlineCalls[0].headers["if-match"]).toBe('"revision-1"');
  expect(state.confirmCalls).toBe(1);

  await expect(page.getByRole("heading", { name: "Energy Transfer Lab" })).toBeVisible();
  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  expect(state.validateCalls).toBe(1);

  const scope = page.getByRole("combobox", {
    name: "Submission scope",
    exact: true,
  });
  await scope.selectOption("tenant");
  await page.getByRole("button", { name: "Submit", exact: true }).click();
  await expect(page.getByText("Submission review-teacher-flow is pending.")).toBeVisible();
  expect(state.submitCalls).toHaveLength(1);
  expect(state.submitCalls[0].body).toEqual({ scope: "tenant", classId: null });
  expect(state.submitCalls[0].headers["idempotency-key"]).toMatch(
    /^teaching-attempt-/,
  );
  await expect(scope).toBeDisabled();
  await expect(page.getByRole("button", { name: "Import PPTX" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Validate" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Submit", exact: true })).toBeDisabled();

  await page.reload();
  await expect(
    page.getByRole("combobox", { name: "Submission scope", exact: true }),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: "Import PPTX" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Validate" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Submit", exact: true })).toBeDisabled();

  const pptx = page.getByRole("button", { name: "PowerPoint" });
  await expect(pptx).toBeEnabled();
  await pptx.click();
  await expect(page.getByText("rendering", { exact: true })).toBeVisible();
  await expect(
    page.getByText("pptx_materialization_failed", { exact: true }),
  ).toBeVisible();
  await pptx.click();
  const downloadLink = page.getByRole("link", { name: "Download export" });
  await expect(downloadLink).toHaveAttribute(
    "href",
    DOWNLOAD_PATH,
  );

  expect(state.exportCalls).toHaveLength(2);
  for (const call of state.exportCalls) {
    expect(call.body).toEqual({ format: "pptx" });
    expect(call.headers["if-match"]).toBe('"revision-3"');
    expect(call.headers["idempotency-key"]).toMatch(/^classroom-export-/);
  }
  expect(state.exportCalls[1].headers["idempotency-key"]).not.toBe(
    state.exportCalls[0].headers["idempotency-key"],
  );

  const fixtureDownloadUrl = new URL(
    DOWNLOAD_PATH,
    teachingDownload.baseURL,
  ).toString();
  await downloadLink.evaluate(
    (link, href) => link.setAttribute("href", href),
    fixtureDownloadUrl,
  );
  const downloadPromise = page.waitForEvent("download");
  await downloadLink.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("energy-transfer.pptx");
  expect(await download.failure()).toBeNull();
  expect(teachingDownload.downloadCalls).toBe(downloadCallsBefore + 1);
  expect(state.unexpected).toEqual([]);
});
