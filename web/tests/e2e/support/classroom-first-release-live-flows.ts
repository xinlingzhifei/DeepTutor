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
  pollLiveStudentClassroom,
  loginLiveIdentity,
  type LiveStudentClassroomMode,
  type LiveStudentClassroomPollState,
  type LiveStudentGenerationJobPollState,
  type LiveCatalogRecords,
  type LiveIdentity,
  type LiveProvisionedFixture,
  type LiveRole,
} from "./classroom-first-release-live-fixture";

const ACTION_TIMEOUT_MS = 30_000;
const SYNC_ATTEMPTS = 180;
const SYNC_INTERVAL_MS = 1_000;
const STUDENT_CREATION_ATTEMPTS = 60;
const STUDENT_GENERATION_ATTEMPTS = 300;

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
const MICRO_NAME = /Micro-classroom|微课堂/;
const FULL_NAME = /Full classroom|完整课堂/;
const OPEN_CREATION_NAME = /Open creation|开放创作/;
const CONFIG_CONFIRM_NAME = /^(Confirm|确定)$/;
const SEND_NAME = /^(Send|发送)$/;
const SAVE_OUTLINE_NAME = /^(Save outline|保存大纲)$/;
const CONFIRM_OUTLINE_NAME = /^(Confirm outline|确认大纲)$/;
const CHAT_NAME = /^(Chat|聊天)$/;
const STUDENT_CLASSROOM_NAME = /Student Classroom|学生课堂/;

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

type StudentClassroomRecord = Omit<
  LiveStudentClassroomPollState,
  "generationJobId"
> & {
  requestId: string;
  approvalId: string | null;
  generationJobId: string | null;
  revision: number;
  outline: JsonRecord | null;
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

function percentage(value: unknown, label: string): number {
  if (
    !Number.isInteger(value) ||
    (value as number) < 0 ||
    (value as number) > 100
  ) {
    throw new Error(`live ${label} response is invalid`);
  }
  return value as number;
}

function nullableText(value: unknown, label: string): string | null {
  if (value === null) return null;
  return text(value, label);
}

function nullableRecord(value: unknown, label: string): JsonRecord | null {
  if (value === null) return null;
  return record(value, label);
}

function items(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new Error(`live ${label} response is invalid`);
  }
  return value;
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

function waitForStudentEstimate(
  page: Page,
  courseId: string,
  mode: LiveStudentClassroomMode,
): Promise<Response> {
  return page.waitForResponse(
    (response) => {
      if (
        !endpointMatches(
          response,
          "POST",
          "/api/v1/student-classrooms/estimate",
        )
      ) {
        return false;
      }
      try {
        const body = record(
          response.request().postDataJSON(),
          "student classroom estimate request",
        );
        const keys = Object.keys(body);
        return (
          keys.length === 3 &&
          Object.prototype.hasOwnProperty.call(body, "courseId") &&
          Object.prototype.hasOwnProperty.call(body, "mode") &&
          Object.prototype.hasOwnProperty.call(body, "contentMode") &&
          body.courseId === courseId &&
          body.mode === mode &&
          body.contentMode === "open_creation"
        );
      } catch {
        return false;
      }
    },
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

async function sameSessionGetRecord(
  page: Page,
  pathname: string,
  label: string,
): Promise<JsonRecord> {
  let response: APIResponse;
  try {
    response = await page.request.get(pathname, { timeout: ACTION_TIMEOUT_MS });
  } catch {
    throw new Error(`live ${label} synchronization failed`);
  }
  requireStatus(response, 200, `${label} synchronization`);
  return responseRecord(response, `${label} synchronization`);
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

function parseStudentClassroomRecord(
  value: JsonRecord,
  label: string,
): StudentClassroomRecord {
  const mode = text(value.mode, label);
  if (mode !== "micro" && mode !== "full") {
    throw new Error(`live ${label} response is invalid`);
  }
  return {
    assetId: text(value.assetId, label),
    requestId: text(value.requestId, label),
    approvalId: nullableText(value.approvalId, label),
    generationJobId: nullableText(value.generationJobId, label),
    status: text(value.status, label),
    courseId: text(value.courseId, label),
    classId: text(value.classId, label),
    mode,
    ownerId: text(value.ownerId, label),
    revision: integer(value.revision, label),
    outline: nullableRecord(value.outline, label),
    classroomVersionId: nullableText(value.classroomVersionId, label),
  };
}

async function readStudentClassroom(
  page: Page,
  assetId: string,
): Promise<StudentClassroomRecord> {
  const value = await sameSessionGetRecord(
    page,
    `/api/v1/student-classrooms/${encodeURIComponent(assetId)}`,
    "student classroom",
  );
  const classroom = parseStudentClassroomRecord(value, "student classroom");
  if (classroom.assetId !== assetId) {
    throw new Error("live student classroom binding is invalid");
  }
  return classroom;
}

async function readStudentGenerationJob(
  page: Page,
  jobId: string,
): Promise<LiveStudentGenerationJobPollState> {
  const value = await sameSessionGetRecord(
    page,
    `/api/v1/classroom-jobs/${encodeURIComponent(jobId)}`,
    "student generation job",
  );
  const phase = text(value.phase, "student generation job");
  if (phase !== "outline" && phase !== "content") {
    throw new Error("live student generation job response is invalid");
  }
  if (value.export_format !== null || value.download_ready !== false) {
    throw new Error("live student generation job response is invalid");
  }
  return {
    jobId: text(value.job_id, "student generation job"),
    jobKind: text(value.job_kind, "student generation job"),
    phase,
    status: text(value.status, "student generation job"),
    progressPercent: percentage(
      value.progress_percent,
      "student generation job",
    ),
  };
}

async function studentClassroomAssetBaseline(page: Page): Promise<Set<string>> {
  const value = await sameSessionGetRecord(
    page,
    "/api/v1/student-classrooms",
    "student classroom list",
  );
  return new Set(
    items(value.items, "student classroom list").map((item) =>
      text(
        record(item, "student classroom list item").assetId,
        "student classroom list item",
      ),
    ),
  );
}

function verifyStudentBinding(
  value: StudentClassroomRecord,
  expected: {
    courseId: string;
    classId: string;
    mode: LiveStudentClassroomMode;
    ownerId: string;
  },
): void {
  if (
    value.courseId !== expected.courseId ||
    value.classId !== expected.classId ||
    value.mode !== expected.mode ||
    value.ownerId !== expected.ownerId
  ) {
    throw new Error("live student classroom binding is invalid");
  }
}

async function pollCreatedStudentClassroom(
  page: Page,
  baseline: ReadonlySet<string>,
  expected: {
    courseId: string;
    classId: string;
    mode: LiveStudentClassroomMode;
    ownerId: string;
  },
): Promise<StudentClassroomRecord & { generationJobId: string }> {
  for (let attempt = 0; attempt < STUDENT_CREATION_ATTEMPTS; attempt += 1) {
    const value = await sameSessionGetRecord(
      page,
      "/api/v1/student-classrooms",
      "student classroom list",
    );
    const created = items(value.items, "student classroom list")
      .map((item) =>
        parseStudentClassroomRecord(
          record(item, "student classroom list item"),
          "student classroom list item",
        ),
      )
      .filter((item) => !baseline.has(item.assetId));
    if (created.length > 1) {
      throw new Error("live student classroom creation is ambiguous");
    }
    if (created.length === 1) {
      const listed = created[0];
      verifyStudentBinding(listed, expected);
      if (listed.approvalId !== null || !listed.generationJobId) {
        throw new Error("live student classroom creation response is invalid");
      }
      const generationJobId = listed.generationJobId;
      const classroom = await readStudentClassroom(page, listed.assetId);
      verifyStudentBinding(classroom, expected);
      if (
        classroom.requestId !== listed.requestId ||
        classroom.approvalId !== null ||
        classroom.generationJobId !== generationJobId
      ) {
        throw new Error("live student classroom creation response is invalid");
      }
      return {
        ...classroom,
        generationJobId,
      };
    }
    if (attempt + 1 < STUDENT_CREATION_ATTEMPTS) {
      await page.waitForTimeout(SYNC_INTERVAL_MS);
    }
  }
  throw new Error("live student classroom creation timed out");
}

async function submitStudentClassroomPrompt(
  page: Page,
  catalog: LiveCatalogRecords,
  mode: LiveStudentClassroomMode,
  prompt: string,
): Promise<void> {
  await page.goto("/home");
  const capability = page.getByRole("button", { name: CHAT_NAME });
  await expect(capability).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
  await capability.click();
  const studentClassroom = page.getByRole("button", {
    name: STUDENT_CLASSROOM_NAME,
  });
  await expect(studentClassroom).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
  await studentClassroom.click();
  const config = page.getByTestId("student-classroom-config");
  await expect(config).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
  const estimated = waitForStudentEstimate(page, catalog.course.id, mode);
  const course = config.getByLabel(COURSE_NAME);
  await selectValue(course, catalog.course.id);
  const openCreation = config
    .locator("label")
    .filter({ hasText: OPEN_CREATION_NAME })
    .locator('input[name="student-classroom-content-mode"]');
  await expect(openCreation).toHaveCount(1);
  await openCreation.check();

  const classroomMode = config
    .locator("label")
    .filter({ hasText: mode === "micro" ? MICRO_NAME : FULL_NAME })
    .locator('input[name="student-classroom-mode"]');
  await expect(classroomMode).toHaveCount(1);
  await classroomMode.check();
  requireStatus(await estimated, 200, "student classroom estimate");

  const configCard = config.locator("xpath=ancestor::section[1]");
  const confirm = configCard.getByRole("button", {
    name: CONFIG_CONFIRM_NAME,
  });
  await expect(confirm).toBeEnabled({ timeout: ACTION_TIMEOUT_MS });
  await confirm.click();

  const composer = page.locator('textarea[maxlength="32000"]');
  await expect(composer).toHaveCount(1);
  await composer.fill(prompt);
  const send = page.getByRole("button", { name: SEND_NAME });
  await expect(send).toBeEnabled({ timeout: ACTION_TIMEOUT_MS });
  await send.click();
}

async function assertStudentPlayer(page: Page, versionId: string): Promise<void> {
  await page.goto(`/learn/classrooms/${encodeURIComponent(versionId)}`);
  const player = page.locator("section[data-playback-state]");
  await expect(player).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
  await expect(player.locator("h2").first()).toHaveText(/\S/, {
    timeout: ACTION_TIMEOUT_MS,
  });
  await expect(player.getByRole("alert")).toHaveCount(0);
}

async function saveAndConfirmStudentOutline(
  page: Page,
  student: LiveIdentity,
  classroom: LiveStudentClassroomPollState,
): Promise<string> {
  const before = await readStudentClassroom(page, classroom.assetId);
  verifyStudentBinding(before, {
    courseId: classroom.courseId,
    classId: classroom.classId,
    mode: classroom.mode,
    ownerId: classroom.ownerId,
  });
  if (
    before.assetId !== classroom.assetId ||
    before.approvalId !== null ||
    before.generationJobId !== classroom.generationJobId ||
    before.status !== "awaiting_confirmation" ||
    before.classroomVersionId !== null
  ) {
    throw new Error("live student classroom confirmation is invalid");
  }
  const encodedAssetId = encodeURIComponent(classroom.assetId);
  const card = page.getByTestId("student-classroom-job-card");
  await expect(card).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
  await expect(card).toHaveAttribute("data-job-id", classroom.generationJobId);
  const outline = card.getByRole("textbox");
  await expect(outline).toHaveCount(1, { timeout: ACTION_TIMEOUT_MS });
  const original = await outline.inputValue();
  const uniqueTitle = `Live full classroom ${student.suffix}`;
  let edited: string;
  try {
    const current = record(JSON.parse(original), "student outline");
    text(current.title, "student outline");
    edited = original.replace(
      /("title"\s*:\s*)"(?:\\.|[^"\\])*"/,
      `$1"${uniqueTitle}"`,
    );
    const retained = record(JSON.parse(edited), "student outline");
    if (retained.title !== uniqueTitle || edited === original) {
      throw new Error("invalid");
    }
  } catch {
    throw new Error("live student outline response is invalid");
  }
  await outline.fill(edited);

  const saved = waitForEndpoint(
    page,
    "PUT",
    `/api/v1/student-classrooms/${encodedAssetId}/outline`,
  );
  const saveOutline = card.getByRole("button", { name: SAVE_OUTLINE_NAME });
  await expect(saveOutline).toBeEnabled({ timeout: ACTION_TIMEOUT_MS });
  await saveOutline.click();
  const savedResponse = await saved;
  requireStatus(savedResponse, 200, "student outline save");
  const savedPayload = await responseRecord(savedResponse, "student outline save");
  const savedClassroom = parseStudentClassroomRecord(
    savedPayload,
    "student outline save",
  );
  const savedOutline = record(savedPayload.outline, "student outline save");
  verifyStudentBinding(savedClassroom, {
    courseId: classroom.courseId,
    classId: classroom.classId,
    mode: classroom.mode,
    ownerId: classroom.ownerId,
  });
  if (
    savedClassroom.assetId !== classroom.assetId ||
    savedClassroom.requestId !== before.requestId ||
    savedClassroom.approvalId !== null ||
    savedClassroom.generationJobId !== classroom.generationJobId ||
    savedClassroom.status !== "awaiting_confirmation" ||
    savedClassroom.revision <= before.revision ||
    savedClassroom.classroomVersionId !== null ||
    savedOutline.title !== uniqueTitle
  ) {
    throw new Error("live student outline save response is invalid");
  }

  const confirmed = waitForEndpoint(
    page,
    "POST",
    `/api/v1/student-classrooms/${encodedAssetId}/confirm-outline`,
  );
  const confirmOutline = card.getByRole("button", {
    name: CONFIRM_OUTLINE_NAME,
  });
  await expect(confirmOutline).toBeEnabled({ timeout: ACTION_TIMEOUT_MS });
  await confirmOutline.click();
  const confirmedResponse = await confirmed;
  requireStatus(confirmedResponse, 202, "student outline confirmation");
  const confirmedPayload = await responseRecord(
    confirmedResponse,
    "student outline confirmation",
  );
  const confirmedClassroom = parseStudentClassroomRecord(
    confirmedPayload,
    "student outline confirmation",
  );
  verifyStudentBinding(confirmedClassroom, {
    courseId: classroom.courseId,
    classId: classroom.classId,
    mode: classroom.mode,
    ownerId: classroom.ownerId,
  });
  if (
    confirmedClassroom.assetId !== classroom.assetId ||
    confirmedClassroom.requestId !== before.requestId ||
    confirmedClassroom.approvalId !== null ||
    confirmedClassroom.generationJobId !== classroom.generationJobId ||
    confirmedClassroom.classroomVersionId !== null ||
    confirmedClassroom.status !== "queued" ||
    confirmedClassroom.revision <= savedClassroom.revision ||
    confirmedClassroom.outline?.title !== uniqueTitle
  ) {
    throw new Error("live student outline confirmation response is invalid");
  }
  return uniqueTitle;
}

async function runLiveStudentRecipe(
  browser: Browser,
  baseUrl: string,
  fixture: LiveProvisionedFixture,
  mode: LiveStudentClassroomMode,
): Promise<void> {
  const student = identityFor(fixture, "student");
  const ownerId = student.userId;
  if (!ownerId) throw new Error("live student identity is incomplete");
  const catalog = fixture.catalog;
  if (!catalog || catalog.source !== null) {
    throw new Error("live student catalog is invalid");
  }
  if (
    student.tenantRole !== "student" ||
    catalog.enrollments.length !== 1 ||
    catalog.enrollments[0].classId !== catalog.teachingClass.id ||
    catalog.enrollments[0].userId !== ownerId
  ) {
    throw new Error("live student enrollment is invalid");
  }
  const session = await openActorSession(
    browser,
    baseUrl,
    student,
    fixture.tenant.tenantId,
  );
  try {
    let confirmedOutlineTitle: string | null = null;
    const baseline = await studentClassroomAssetBaseline(session.page);
    await submitStudentClassroomPrompt(
      session.page,
      catalog,
      mode,
      mode === "micro"
        ? `Create a concise micro-classroom about proportional reasoning for ${student.suffix}.`
        : `Create a full classroom about proportional reasoning for ${student.suffix}.`,
    );
    const created = await pollCreatedStudentClassroom(session.page, baseline, {
      courseId: catalog.course.id,
      classId: catalog.teachingClass.id,
      mode,
      ownerId,
    });
    const card = session.page.getByTestId("student-classroom-job-card");
    await expect(card).toBeVisible({ timeout: ACTION_TIMEOUT_MS });
    await expect(card).toHaveAttribute("data-job-id", created.generationJobId);

    const result = await pollLiveStudentClassroom({
      expected: {
        assetId: created.assetId,
        generationJobId: created.generationJobId,
        courseId: catalog.course.id,
        classId: catalog.teachingClass.id,
        mode,
        ownerId,
      },
      pollAttempts: STUDENT_GENERATION_ATTEMPTS,
      pollIntervalMs: SYNC_INTERVAL_MS,
      pause: (milliseconds) => session.page.waitForTimeout(milliseconds),
      readClassroom: async () => {
        const classroom = await readStudentClassroom(
          session.page,
          created.assetId,
        );
        if (
          !classroom.generationJobId ||
          classroom.requestId !== created.requestId ||
          classroom.approvalId !== null ||
          (confirmedOutlineTitle !== null &&
            classroom.outline?.title !== confirmedOutlineTitle)
        ) {
          throw new Error("live student classroom job binding is invalid");
        }
        return { ...classroom, generationJobId: classroom.generationJobId };
      },
      readGenerationJob: (jobId) =>
        readStudentGenerationJob(session.page, jobId),
      ...(mode === "full"
        ? {
            onAwaitingConfirmation: async (
              classroom: LiveStudentClassroomPollState,
            ) => {
              if (
                classroom.status !== "awaiting_confirmation" ||
                classroom.classroomVersionId !== null ||
                classroom.generationJobId !== created.generationJobId
              ) {
                throw new Error(
                  "live student classroom confirmation is invalid",
                );
              }
              confirmedOutlineTitle = await saveAndConfirmStudentOutline(
                session.page,
                student,
                classroom,
              );
              return undefined;
            },
          }
        : {}),
    });
    if (mode === "full") {
      if (confirmedOutlineTitle === null) {
        throw new Error("live student classroom confirmation is invalid");
      }
      const completed = await readStudentClassroom(
        session.page,
        result.classroom.assetId,
      );
      if (
        completed.classroomVersionId !== result.classroom.classroomVersionId ||
        completed.outline?.title !== confirmedOutlineTitle
      ) {
        throw new Error("live student classroom confirmed outline is invalid");
      }
    }
    await assertStudentPlayer(
      session.page,
      result.classroom.classroomVersionId,
    );
  } finally {
    await session.context.close();
  }
}

export async function runLiveStudentMicroRecipe(
  browser: Browser,
  baseUrl: string,
  fixture: LiveProvisionedFixture,
): Promise<void> {
  await runLiveStudentRecipe(browser, baseUrl, fixture, "micro");
}

export async function runLiveStudentFullRecipe(
  browser: Browser,
  baseUrl: string,
  fixture: LiveProvisionedFixture,
): Promise<void> {
  await runLiveStudentRecipe(browser, baseUrl, fixture, "full");
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
