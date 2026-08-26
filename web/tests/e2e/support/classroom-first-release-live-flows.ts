import {
  expect,
  type APIResponse,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
  type Response,
} from "@playwright/test";
import {
  loginLiveIdentity,
  type LiveCatalogRecords,
  type LiveIdentity,
  type LiveProvisionedFixture,
  type LiveRole,
} from "./classroom-first-release-live-fixture";

const ACTION_TIMEOUT_MS = 30_000;
const SYNC_ATTEMPTS = 180;
const SYNC_INTERVAL_MS = 1_000;

const COURSE_NAME = /^(Course|课程)$/;
const CLASS_NAME = /^(Class|班级)$/;
const TITLE_NAME = /^(Classroom title|课堂标题)$/;
const OBJECTIVE_NAME = /^(Teaching objective|教学目标)$/;
const GRADE_NAME = /^(Grade band|年级学段)$/;
const SOURCE_NAME = /^(Source|来源)$/;
const POINT_NAME = /^(Primary knowledge point|主要知识点)$/;
const POINT_DESCRIPTION_NAME = /^(Knowledge point description|知识点说明)$/;
const GENERATE_NAME = /^(Generate outline|生成大纲)$/;
const CONFIRM_NAME = /^(Confirm and generate content|确认并生成正文)$/;
const VALIDATE_NAME = /^(Validate|运行校验)$/;
const SUBMIT_NAME = /^(Submit|提交)$/;
const SCOPE_NAME = /^(Submission scope|提交范围)$/;
const VALIDATION_NAME = /^(Validation report|校验报告)$/;
const VALIDATION_READY = /Ready|可继续/;
const REVIEW_EMPTY = /^(No reviewable submissions are available\.|暂无可审核的提交。)$/;
const REVIEW_COMMENT = /^(Decision comment|审核意见)$/;
const APPROVE_NAME = /^(Approve|通过)$/;
const REJECT_NAME = /^(Reject|退回)$/;
const PUBLISH_NAME = /^(Publish to tenant library|发布到租户内容库)$/;

type JsonRecord = Record<string, unknown>;
type SubmissionScope = "class" | "tenant";

type ActorSession = {
  context: BrowserContext;
  page: Page;
};

type PendingSubmission = {
  assetId: string;
  reviewId: string;
  title: string;
};

function record(value: unknown, label: string): JsonRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`live ${label} response is invalid`);
  }
  return value as JsonRecord;
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`live ${label} response is invalid`);
  }
  return value;
}

function integer(value: unknown, label: string): number {
  if (!Number.isInteger(value) || (value as number) < 1) {
    throw new Error(`live ${label} response is invalid`);
  }
  return value as number;
}

function identityFor(
  fixture: LiveProvisionedFixture,
  role: LiveRole,
): LiveIdentity {
  const identity = fixture.identities.find((candidate) => candidate.role === role);
  if (!identity?.userId) throw new Error("live actor identity is incomplete");
  return identity;
}

function catalogFor(fixture: LiveProvisionedFixture): LiveCatalogRecords {
  const catalog = fixture.catalog;
  if (!catalog?.source) throw new Error("live teaching catalog is incomplete");
  return catalog;
}

function endpointMatches(
  response: Response,
  method: string,
  pathname: string,
): boolean {
  return (
    response.request().method() === method &&
    new URL(response.url()).pathname === pathname
  );
}

function waitForEndpoint(
  page: Page,
  method: string,
  pathname: string,
): Promise<Response> {
  return page.waitForResponse(
    (response) => endpointMatches(response, method, pathname),
    { timeout: ACTION_TIMEOUT_MS },
  );
}

function requireStatus(
  response: APIResponse | Response,
  expected: number,
  label: string,
): void {
  if (response.status() !== expected) {
    throw new Error(`live ${label} request failed`);
  }
}

async function responseRecord(
  response: APIResponse | Response,
  label: string,
): Promise<JsonRecord> {
  try {
    return record(await response.json(), label);
  } catch {
    throw new Error(`live ${label} response is invalid`);
  }
}

async function pollRecord(
  page: Page,
  pathname: string,
  label: string,
  ready: (value: JsonRecord) => boolean,
): Promise<JsonRecord> {
  for (let attempt = 0; attempt < SYNC_ATTEMPTS; attempt += 1) {
    let response: APIResponse;
    try {
      response = await page.request.get(pathname, { timeout: ACTION_TIMEOUT_MS });
    } catch {
      throw new Error(`live ${label} synchronization failed`);
    }
    requireStatus(response, 200, `${label} synchronization`);
    const value = await responseRecord(response, `${label} synchronization`);
    if (ready(value)) return value;
    if (attempt + 1 < SYNC_ATTEMPTS) {
      await page.waitForTimeout(SYNC_INTERVAL_MS);
    }
  }
  throw new Error(`live ${label} synchronization timed out`);
}

function safeOptionValue(value: string): string {
  if (!/^[A-Za-z0-9_.:-]+$/.test(value)) {
    throw new Error("live tenant option is invalid");
  }
  return value;
}

async function selectTenant(page: Page, tenantId: string): Promise<void> {
  const safeTenantId = safeOptionValue(tenantId);
  const switcher = page.locator(
    `select:has(option[value="${safeTenantId}"])`,
  );
  await expect(switcher).toHaveCount(1, { timeout: ACTION_TIMEOUT_MS });
  const current = await switcher.inputValue();
  if (current === tenantId) {
    await switcher.selectOption(tenantId);
    return;
  }
  const switched = waitForEndpoint(page, "PUT", "/api/v1/tenants/active");
  await switcher.selectOption(tenantId);
  const response = await switched;
  requireStatus(response, 200, "tenant selection");
  const payload = await responseRecord(response, "tenant selection");
  if (payload.active_tenant_id !== tenantId) {
    throw new Error("live tenant selection response is invalid");
  }
  await expect(switcher).toHaveValue(tenantId);
}

async function openActorSession(
  browser: Browser,
  baseUrl: string,
  identity: LiveIdentity,
  tenantId: string,
): Promise<ActorSession> {
  const context = await browser.newContext({
    baseURL: baseUrl,
    locale: "en-US",
    timezoneId: "UTC",
  });
  try {
    const page = await context.newPage();
    await loginLiveIdentity(page, identity);
    await selectTenant(page, tenantId);
    return { context, page };
  } catch {
    await context.close();
    throw new Error("live actor session setup failed");
  }
}

async function selectValue(control: Locator, value: string): Promise<void> {
  await control.selectOption(value, { timeout: ACTION_TIMEOUT_MS });
  await expect(control).toHaveValue(value);
}

function classroomTitle(identity: LiveIdentity, recipe: "teacher" | "content"): string {
  return `Live ${recipe} classroom ${identity.suffix}`;
}

async function submitTeachingBrief(
  page: Page,
  fixture: LiveProvisionedFixture,
  identity: LiveIdentity,
  recipe: "teacher" | "content",
): Promise<{ assetId: string; title: string }> {
  const catalog = catalogFor(fixture);
  const source = catalog.source;
  if (!source) throw new Error("live teaching source is incomplete");
  const title = classroomTitle(identity, recipe);

  await page.goto("/teaching/classrooms/new");
  const form = page.getByTestId("teaching-brief-form");
  await expect(form).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
  await selectValue(form.getByLabel(COURSE_NAME), catalog.course.id);
  await selectValue(form.getByLabel(CLASS_NAME), catalog.teachingClass.id);
  await form.getByLabel(TITLE_NAME).fill(title);
  await form
    .getByLabel(OBJECTIVE_NAME)
    .fill("Build and review a source-grounded classroom from the controlled PDF.");
  await form.getByLabel(GRADE_NAME).fill("secondary");
  await selectValue(
    form.getByLabel(SOURCE_NAME),
    `${source.sourceType}:${source.bindingId}`,
  );
  await form.getByLabel(POINT_NAME).fill("Source-grounded explanation");
  await form
    .getByLabel(POINT_DESCRIPTION_NAME)
    .fill("Explain the controlled source with verifiable classroom evidence.");

  const created = waitForEndpoint(page, "POST", "/api/v1/classrooms");
  await form.getByRole("button", { name: GENERATE_NAME }).click();
  const response = await created;
  requireStatus(response, 202, "classroom creation");
  const payload = await responseRecord(response, "classroom creation");
  const assetId = text(payload.assetId, "classroom creation");
  if (
    payload.title !== title ||
    payload.courseId !== catalog.course.id ||
    payload.classId !== catalog.teachingClass.id
  ) {
    throw new Error("live classroom creation response is invalid");
  }
  return { assetId, title };
}

async function confirmOutline(page: Page, assetId: string, title: string): Promise<void> {
  await pollRecord(
    page,
    `/api/v1/classrooms/${encodeURIComponent(assetId)}`,
    "classroom outline",
    (value) =>
      value.assetId === assetId &&
      value.lifecycleState === "awaiting_outline" &&
      value.status === "awaiting_confirmation" &&
      value.outline !== null &&
      typeof value.outline === "object" &&
      !Array.isArray(value.outline),
  );

  await page.goto(`/teaching/classrooms/${encodeURIComponent(assetId)}/outline`);
  await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible({
    timeout: ACTION_TIMEOUT_MS,
  });
  const confirmed = waitForEndpoint(
    page,
    "POST",
    `/api/v1/classrooms/${encodeURIComponent(assetId)}/confirm-outline`,
  );
  await page.getByRole("button", { name: CONFIRM_NAME }).click();
  const response = await confirmed;
  requireStatus(response, 202, "outline confirmation");

  await pollRecord(
    page,
    `/api/v1/classrooms/${encodeURIComponent(assetId)}/draft`,
    "classroom draft",
    (value) =>
      value.assetId === assetId &&
      value.lifecycleState === "editing" &&
      value.document !== null &&
      typeof value.document === "object" &&
      !Array.isArray(value.document) &&
      typeof value.classroomVersionId === "string" &&
      value.classroomVersionId.length > 0,
  );
}

async function validateAndSubmit(
  page: Page,
  assetId: string,
  title: string,
  scope: SubmissionScope,
): Promise<PendingSubmission> {
  const encodedAssetId = encodeURIComponent(assetId);
  await page.goto(`/teaching/classrooms/${encodedAssetId}/edit`);
  await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible({
    timeout: ACTION_TIMEOUT_MS,
  });

  const validated = waitForEndpoint(
    page,
    "POST",
    `/api/v1/classrooms/${encodedAssetId}/validate`,
  );
  await page.getByRole("button", { name: VALIDATE_NAME }).click();
  const validationResponse = await validated;
  requireStatus(validationResponse, 200, "classroom validation");
  const validationPayload = await responseRecord(
    validationResponse,
    "classroom validation",
  );
  const validationReport = record(
    validationPayload.validationReport,
    "classroom validation",
  );
  if (validationPayload.assetId !== assetId || validationReport.valid !== true) {
    throw new Error("live classroom validation response is invalid");
  }
  await expect(
    page.locator("section[aria-label]").filter({ hasText: VALIDATION_READY }),
  ).toHaveCount(1, { timeout: ACTION_TIMEOUT_MS });

  await selectValue(page.getByLabel(SCOPE_NAME), scope);
  const submitted = waitForEndpoint(
    page,
    "POST",
    `/api/v1/classrooms/${encodedAssetId}/submit`,
  );
  await page.getByRole("button", { name: SUBMIT_NAME }).click();
  const submitResponse = await submitted;
  requireStatus(submitResponse, 201, "classroom submission");
  const review = await responseRecord(submitResponse, "classroom submission");
  const reviewId = text(review.id, "classroom submission");
  text(review.draftId, "classroom submission");
  integer(review.draftRevision, "classroom submission");
  text(review.documentSha256, "classroom submission");
  text(review.validationReportSha256, "classroom submission");
  if (review.assetId !== assetId || review.status !== "pending") {
    throw new Error("live classroom submission response is invalid");
  }
  const pendingRecord = page.getByRole("status").filter({ hasText: reviewId });
  await expect(pendingRecord).toContainText("pending", {
    timeout: ACTION_TIMEOUT_MS,
  });
  return { assetId, reviewId, title };
}

async function createPendingSubmission(
  page: Page,
  fixture: LiveProvisionedFixture,
  identity: LiveIdentity,
  recipe: "teacher" | "content",
  scope: SubmissionScope,
): Promise<PendingSubmission> {
  const classroom = await submitTeachingBrief(page, fixture, identity, recipe);
  await confirmOutline(page, classroom.assetId, classroom.title);
  return validateAndSubmit(page, classroom.assetId, classroom.title, scope);
}

export async function runLiveTeacherRecipe(
  browser: Browser,
  baseUrl: string,
  fixture: LiveProvisionedFixture,
): Promise<void> {
  const teacher = identityFor(fixture, "teacher");
  const session = await openActorSession(
    browser,
    baseUrl,
    teacher,
    fixture.tenant.tenantId,
  );
  try {
    await createPendingSubmission(session.page, fixture, teacher, "teacher", "class");
  } finally {
    await session.context.close();
  }
}

async function assertAuthorCannotDecide(page: Page): Promise<void> {
  const listed = waitForEndpoint(page, "GET", "/api/v1/classroom-reviews");
  await page.goto("/teaching/reviews");
  const response = await listed;
  requireStatus(response, 200, "author review list");
  await expect(page.getByText(REVIEW_EMPTY)).toBeVisible({
    timeout: ACTION_TIMEOUT_MS,
  });
  await expect(page.getByRole("button", { name: APPROVE_NAME })).toHaveCount(0);
  await expect(page.getByRole("button", { name: REJECT_NAME })).toHaveCount(0);
}

async function approveSubmission(
  browser: Browser,
  baseUrl: string,
  fixture: LiveProvisionedFixture,
  submission: PendingSubmission,
): Promise<void> {
  const reviewer = identityFor(fixture, "reviewer");
  const session = await openActorSession(
    browser,
    baseUrl,
    reviewer,
    fixture.tenant.tenantId,
  );
  try {
    const listed = waitForEndpoint(session.page, "GET", "/api/v1/classroom-reviews");
    await session.page.goto("/teaching/reviews");
    requireStatus(await listed, 200, "reviewer review list");
    const reviewChoice = session.page
      .getByRole("button")
      .filter({ hasText: submission.assetId });
    await expect(reviewChoice).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
    await reviewChoice.click();
    const comment = session.page.getByLabel(REVIEW_COMMENT);
    await expect(comment).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
    await comment.fill("Approved by the isolated live reviewer.");

    const approved = waitForEndpoint(
      session.page,
      "POST",
      `/api/v1/classroom-reviews/${encodeURIComponent(submission.reviewId)}/approve`,
    );
    await session.page.getByRole("button", { name: APPROVE_NAME }).click();
    const response = await approved;
    requireStatus(response, 200, "classroom approval");
    const review = await responseRecord(response, "classroom approval");
    if (
      review.id !== submission.reviewId ||
      review.assetId !== submission.assetId ||
      review.status !== "approved" ||
      review.reviewerId !== reviewer.userId
    ) {
      throw new Error("live classroom approval response is invalid");
    }
    await expect(reviewChoice).toHaveCount(0, { timeout: ACTION_TIMEOUT_MS });
  } finally {
    await session.context.close();
  }
}

async function publishSubmission(
  browser: Browser,
  baseUrl: string,
  fixture: LiveProvisionedFixture,
  submission: PendingSubmission,
): Promise<void> {
  const publisher = identityFor(fixture, "publisher");
  const session = await openActorSession(
    browser,
    baseUrl,
    publisher,
    fixture.tenant.tenantId,
  );
  try {
    const listed = waitForEndpoint(
      session.page,
      "GET",
      "/api/v1/classroom-publications",
    );
    await session.page.goto("/teaching/library");
    requireStatus(await listed, 200, "publication list");
    const candidate = session.page.getByRole("article").filter({
      has: session.page.getByRole("button", { name: PUBLISH_NAME }),
      hasText: submission.assetId,
    });
    await expect(candidate).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
    await expect(candidate).toContainText(submission.title);

    const published = waitForEndpoint(
      session.page,
      "POST",
      `/api/v1/classrooms/${encodeURIComponent(submission.assetId)}/publish`,
    );
    await candidate.getByRole("button", { name: PUBLISH_NAME }).click();
    const response = await published;
    requireStatus(response, 201, "classroom publication");
    const version = await responseRecord(response, "classroom publication");
    const versionId = text(version.versionId, "classroom publication");
    const documentSha256 = text(version.documentSha256, "classroom publication");
    integer(version.versionNumber, "classroom publication");
    text(version.idempotencyKey, "classroom publication");
    if (
      version.assetId !== submission.assetId ||
      version.publicationScope !== "tenant" ||
      version.classId !== null ||
      !/^[0-9a-f]{64}$/.test(documentSha256)
    ) {
      throw new Error("live classroom publication response is invalid");
    }
    await expect(candidate).toHaveCount(0, { timeout: ACTION_TIMEOUT_MS });
    const publishedRecord = session.page
      .getByRole("article")
      .filter({ hasText: submission.assetId })
      .filter({ hasText: versionId });
    await expect(publishedRecord).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
    await expect(publishedRecord).toContainText(submission.title);
    await expect(publishedRecord).toContainText(documentSha256);
  } finally {
    await session.context.close();
  }
}

export async function runLiveContentOperationsRecipe(
  browser: Browser,
  baseUrl: string,
  fixture: LiveProvisionedFixture,
): Promise<void> {
  const author = identityFor(fixture, "author");
  const reviewer = identityFor(fixture, "reviewer");
  const publisher = identityFor(fixture, "publisher");
  if (
    new Set([author.userId, reviewer.userId, publisher.userId]).size !== 3 ||
    author.tenantRole !== "content_author" ||
    reviewer.tenantRole !== "content_reviewer" ||
    publisher.tenantRole !== "teacher"
  ) {
    throw new Error("live content actors are invalid");
  }
  const authorSession = await openActorSession(
    browser,
    baseUrl,
    author,
    fixture.tenant.tenantId,
  );
  let submission: PendingSubmission;
  try {
    submission = await createPendingSubmission(
      authorSession.page,
      fixture,
      author,
      "content",
      "tenant",
    );
    await assertAuthorCannotDecide(authorSession.page);
  } finally {
    await authorSession.context.close();
  }
  await approveSubmission(browser, baseUrl, fixture, submission);
  await publishSubmission(browser, baseUrl, fixture, submission);
}
