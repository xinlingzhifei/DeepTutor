import { apiFetch } from "@/lib/api";
export { studentClassroomPlayRoute } from "@/lib/capability-routes";

export type StudentClassroomMode = "micro" | "full";
export type StudentClassroomContentMode =
  | "source_grounded"
  | "open_creation";

export interface StudentClassroomConfigInput {
  courseId: string;
  mode: StudentClassroomMode | null;
  contentMode?: StudentClassroomContentMode;
}

export interface StudentClassroomFormConfig {
  courseId: string;
  mode: StudentClassroomMode | null;
  contentMode: StudentClassroomContentMode;
}

export interface StudentClassroomOption {
  courseId: string;
  title: string;
  allowedModes: readonly StudentClassroomMode[];
  allowedContentModes: readonly StudentClassroomContentMode[];
}

export function createStudentClassroomConfig(): StudentClassroomFormConfig {
  return {
    courseId: "",
    mode: null,
    contentMode: "source_grounded",
  };
}

export function restoreStudentClassroomConfig(
  stored: unknown,
): StudentClassroomFormConfig {
  if (!stored || typeof stored !== "object" || Array.isArray(stored)) {
    return createStudentClassroomConfig();
  }
  const value = stored as Record<string, unknown>;
  if (typeof value.courseId !== "string") {
    return createStudentClassroomConfig();
  }
  return {
    courseId: value.courseId.trim(),
    mode:
      value.mode === "micro" || value.mode === "full" ? value.mode : null,
    // Local storage is never policy authority. Open creation must be allowed
    // by fresh server options and explicitly selected again by the learner.
    contentMode: "source_grounded",
  };
}

export type StudentClassroomConfigValidation =
  | { ok: true }
  | {
      ok: false;
      error: "classroom_course_required" | "classroom_mode_required";
    };

export function validateStudentClassroomConfig(
  input: StudentClassroomConfigInput,
): StudentClassroomConfigValidation {
  if (!input.courseId.trim()) {
    return { ok: false, error: "classroom_course_required" };
  }
  if (input.mode !== "micro" && input.mode !== "full") {
    return { ok: false, error: "classroom_mode_required" };
  }
  return { ok: true };
}

export function toCapabilityConfig(
  input: StudentClassroomConfigInput,
): Record<string, string> {
  const validation = validateStudentClassroomConfig(input);
  if (!validation.ok) throw new Error(validation.error);
  const mode = input.mode;
  if (mode === null) throw new Error("classroom_mode_required");
  return {
    course_id: input.courseId.trim(),
    mode,
    content_mode: input.contentMode ?? "source_grounded",
  };
}

export function canConfirmStudentClassroomConfig(
  input: StudentClassroomConfigInput,
  availability: {
    authorizedSourceCount: number;
    option: StudentClassroomOption | null;
    estimateReady: boolean;
  },
): boolean {
  if (!validateStudentClassroomConfig(input).ok) return false;
  const option = availability.option;
  if (!option || option.courseId !== input.courseId.trim() || input.mode === null) {
    return false;
  }
  if (!option.allowedModes.includes(input.mode)) return false;
  const contentMode = input.contentMode ?? "source_grounded";
  if (!option.allowedContentModes.includes(contentMode)) return false;
  if (!availability.estimateReady) return false;
  return contentMode === "source_grounded"
    ? availability.authorizedSourceCount === 1
    : true;
}

export function studentClassroomEffectiveKnowledgeBases(
  contentMode: StudentClassroomContentMode,
  authorizedSourceNames: readonly string[],
): string[] | null {
  if (contentMode === "open_creation") return [];
  return authorizedSourceNames.length === 1 ? [...authorizedSourceNames] : null;
}

export function studentClassroomRequiresOutline(status: string): boolean {
  return (
    status === "awaiting_confirmation" ||
    status === "awaiting_outline_confirmation"
  );
}

export interface StudentClassroomEstimate {
  sceneRange: [number, number];
  durationMinutesRange: [number, number];
  quotaUnits: number;
  requiresOutlineConfirmation: boolean;
  requiresApproval: boolean;
}

export interface StudentClassroomEstimateInput {
  courseId: string;
  mode: StudentClassroomMode;
  contentMode: StudentClassroomContentMode;
  sourceRef?: string;
}

export interface StudentClassroomEstimateReadiness {
  requestKey: string;
  status: "loading" | "failed" | "ready";
}

export function studentClassroomEstimateRequestKey(
  input: StudentClassroomEstimateInput,
): string {
  return JSON.stringify([
    input.courseId.trim(),
    input.mode,
    input.contentMode,
    input.contentMode === "source_grounded"
      ? (input.sourceRef?.trim() ?? "")
      : "",
  ]);
}

export function studentClassroomEstimateIsReady(
  currentRequestKey: string | null,
  readiness: StudentClassroomEstimateReadiness | null,
): boolean {
  return (
    currentRequestKey !== null &&
    readiness?.requestKey === currentRequestKey &&
    readiness.status === "ready"
  );
}

export interface StudentClassroomTask {
  assetId: string;
  jobId: string | null;
  approvalId: string | null;
  status: string;
  mode: StudentClassroomMode;
  revision: number;
  outline: Record<string, unknown> | null;
  estimate: StudentClassroomEstimate;
}

export interface StudentClassroomState {
  assetId: string;
  requestId: string;
  approvalId: string | null;
  generationJobId: string | null;
  status: string;
  courseId: string;
  classId: string;
  mode: StudentClassroomMode;
  ownerId: string;
  revision: number;
  outline: Record<string, unknown> | null;
  classroomVersionId: string | null;
}

export interface StudentClassroomJob {
  jobId: string;
  phase: string;
  status: string;
  progressPercent: number;
  waitingReason: string | null;
  outline: Record<string, unknown> | null;
  errorCategory: string | null;
  errorCode: string | null;
}

const STUDENT_CLASSROOM_STATUSES = new Set([
  "preparing",
  "awaiting_approval",
  "draft",
  "created",
  "quota_reserved",
  "queued",
  "generating_outline",
  "awaiting_confirmation",
  "awaiting_outline_confirmation",
  "generating_content",
  "validating",
  "materializing",
  "succeeded",
  "failed",
  "canceled",
  "rejected",
  "expired",
]);

const GENERATION_JOB_STATUSES = new Set([
  "created",
  "quota_reserved",
  "queued",
  "generating_outline",
  "awaiting_confirmation",
  "generating_content",
  "validating",
  "materializing",
  "succeeded",
  "failed",
  "canceled",
]);

const GENERATION_JOB_PHASES = new Set(["outline", "content", "micro"]);

export interface StudentClassroomCardState {
  jobId: string | null;
  status: string;
  outline: Record<string, unknown> | null;
  approvalId: string | null;
  classroomVersionId: string | null;
}

export function resolveStudentClassroomCardState(
  task: StudentClassroomTask,
  classroom: StudentClassroomState | null,
): StudentClassroomCardState {
  if (classroom) {
    return {
      jobId: classroom.generationJobId,
      status: classroom.status,
      outline: classroom.outline,
      approvalId: classroom.approvalId,
      classroomVersionId: classroom.classroomVersionId,
    };
  }
  return {
    jobId: task.jobId,
    status: task.status,
    outline: task.outline,
    approvalId: task.approvalId,
    classroomVersionId: null,
  };
}

const TERMINAL_STUDENT_CLASSROOM_STATUSES = new Set([
  "succeeded",
  "failed",
  "canceled",
  "rejected",
  "expired",
]);

const OWNER_ACTION_STUDENT_CLASSROOM_STATUSES = new Set([
  "awaiting_confirmation",
  "awaiting_outline_confirmation",
]);

export function shouldPollStudentClassroom(
  classroom: Pick<StudentClassroomState, "status" | "classroomVersionId">,
): boolean {
  return (
    !TERMINAL_STUDENT_CLASSROOM_STATUSES.has(classroom.status) &&
    !OWNER_ACTION_STUDENT_CLASSROOM_STATUSES.has(classroom.status)
  );
}

export type StudentClassroomStatusKind =
  | "success"
  | "failure"
  | "waiting"
  | "running";

export function studentClassroomStatusKind(
  status: string,
): StudentClassroomStatusKind {
  if (status === "succeeded") return "success";
  if (TERMINAL_STUDENT_CLASSROOM_STATUSES.has(status)) return "failure";
  if (
    status === "awaiting_approval" ||
    OWNER_ACTION_STUDENT_CLASSROOM_STATUSES.has(status)
  ) {
    return "waiting";
  }
  return "running";
}

export class StudentClassroomRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "StudentClassroomRequestError";
  }
}

export function studentClassroomPollIntervalMs(status: string): number {
  return status === "awaiting_approval" ? 15_000 : 2_500;
}

export function studentClassroomPollRetryDelay(
  error: unknown,
  failureCount: number,
): number | null {
  if (
    (error instanceof StudentClassroomRequestError &&
      (error.status === 403 || error.status === 404)) ||
    (error instanceof Error && error.name === "AbortError") ||
    !Number.isInteger(failureCount) ||
    failureCount < 1
  ) {
    return null;
  }
  return failureCount <= 3 ? 2500 * 2 ** (failureCount - 1) : 30_000;
}

export type StudentClassroomApprovalState =
  | "waiting"
  | "required"
  | "approved"
  | "notApproved"
  | "notRequired";

export function studentClassroomApprovalState(
  state: Pick<StudentClassroomCardState, "status" | "approvalId" | "jobId">,
): StudentClassroomApprovalState {
  if (state.status === "awaiting_approval") return "waiting";
  if (state.status === "rejected" || state.status === "expired") {
    return "notApproved";
  }
  if (state.approvalId === null) return "notRequired";
  return state.jobId !== null || !["created", "draft"].includes(state.status)
    ? "approved"
    : "required";
}

interface ResultEventLike {
  type?: unknown;
  source?: unknown;
  metadata?: unknown;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
  label: string,
): void {
  const expectedSet = new Set(expected);
  const unknown = Object.keys(value).filter(key => !expectedSet.has(key));
  const missing = expected.filter(key => !(key in value));
  if (unknown.length > 0 || missing.length > 0) {
    throw new Error(`${label} has unknown fields or missing fields`);
  }
}

function enumArray<T extends string>(
  value: Record<string, unknown>,
  key: string,
  allowed: ReadonlySet<string>,
  label: string,
): T[] {
  const items = value[key];
  if (
    !Array.isArray(items) ||
    items.length === 0 ||
    items.some(item => typeof item !== "string" || !allowed.has(item)) ||
    new Set(items).size !== items.length
  ) {
    throw new Error(`${label}.${key} is invalid`);
  }
  return items as T[];
}

function parseStudentClassroomOptions(input: unknown): StudentClassroomOption[] {
  const payload = record(input, "student classroom options response");
  exactKeys(payload, ["items"], "student classroom options response");
  if (!Array.isArray(payload.items)) {
    throw new Error("student classroom options response.items must be an array");
  }
  const seenCourses = new Set<string>();
  return payload.items.map((item, index) => {
    const label = `student classroom options response.items[${index}]`;
    const value = record(item, label);
    exactKeys(
      value,
      ["courseId", "title", "allowedModes", "allowedContentModes"],
      label,
    );
    const courseId = requiredString(value, "courseId", label);
    if (seenCourses.has(courseId)) {
      throw new Error("student classroom options response has duplicate courses");
    }
    seenCourses.add(courseId);
    return {
      courseId,
      title: requiredString(value, "title", label),
      allowedModes: enumArray<StudentClassroomMode>(
        value,
        "allowedModes",
        new Set(["micro", "full"]),
        label,
      ),
      allowedContentModes: enumArray<StudentClassroomContentMode>(
        value,
        "allowedContentModes",
        new Set(["source_grounded", "open_creation"]),
        label,
      ),
    };
  });
}

function parseStudentClassroomEstimate(input: unknown): StudentClassroomEstimate {
  const value = record(input, "student classroom estimate response");
  exactKeys(
    value,
    [
      "sceneRange",
      "durationMinutesRange",
      "quotaUnits",
      "requiresOutlineConfirmation",
      "requiresApproval",
    ],
    "student classroom estimate response",
  );
  const quotaUnits = requiredInteger(
    value,
    "quotaUnits",
    "student classroom estimate response",
  );
  if (quotaUnits < 1) {
    throw new Error("student classroom estimate response.quotaUnits is invalid");
  }
  return {
    sceneRange: numberRange(
      value,
      "sceneRange",
      "student classroom estimate response",
    ),
    durationMinutesRange: numberRange(
      value,
      "durationMinutesRange",
      "student classroom estimate response",
    ),
    quotaUnits,
    requiresOutlineConfirmation: requiredBoolean(
      value,
      "requiresOutlineConfirmation",
      "student classroom estimate response",
    ),
    requiresApproval: requiredBoolean(
      value,
      "requiresApproval",
      "student classroom estimate response",
    ),
  };
}

function requiredString(
  value: Record<string, unknown>,
  key: string,
  label: string,
): string {
  const result = value[key];
  if (typeof result !== "string" || !result.trim()) {
    throw new Error(`${label}.${key} must be a non-empty string`);
  }
  return result;
}

function enumString(
  value: Record<string, unknown>,
  key: string,
  allowed: ReadonlySet<string>,
  label: string,
): string {
  const result = requiredString(value, key, label);
  if (!allowed.has(result)) {
    throw new Error(`${label}.${key} is invalid`);
  }
  return result;
}

function nullableString(
  value: Record<string, unknown>,
  key: string,
  label: string,
): string | null {
  if (value[key] === null) return null;
  return requiredString(value, key, label);
}

function requiredInteger(
  value: Record<string, unknown>,
  key: string,
  label: string,
): number {
  const result = value[key];
  if (!Number.isInteger(result)) {
    throw new Error(`${label}.${key} must be an integer`);
  }
  return result as number;
}

function nullableRecord(
  value: Record<string, unknown>,
  key: string,
  label: string,
): Record<string, unknown> | null {
  if (value[key] === null) return null;
  return record(value[key], `${label}.${key}`);
}

function numberRange(
  value: Record<string, unknown>,
  key: string,
  label: string,
): [number, number] {
  const range = value[key];
  if (
    !Array.isArray(range) ||
    range.length !== 2 ||
    !range.every(item => Number.isInteger(item) && item > 0) ||
    range[0] > range[1]
  ) {
    throw new Error(`${label}.${key} must be a positive ascending range`);
  }
  return [range[0] as number, range[1] as number];
}

function requiredBoolean(
  value: Record<string, unknown>,
  key: string,
  label: string,
): boolean {
  if (typeof value[key] !== "boolean") {
    throw new Error(`${label}.${key} must be a boolean`);
  }
  return value[key] as boolean;
}

function parseStudentClassroomTask(metadata: unknown): StudentClassroomTask {
  const result = record(metadata, "interactive classroom result");
  exactKeys(
    result,
    [
      "response",
      "estimate",
      "approval_id",
      "job_id",
      "outline",
      "classroom",
      "metadata",
    ],
    "interactive classroom result",
  );
  const classroom = record(result.classroom, "interactive classroom result.classroom");
  exactKeys(
    classroom,
    [
      "asset_id",
      "request_id",
      "approval_id",
      "generation_job_id",
      "status",
      "course_id",
      "class_id",
      "mode",
      "owner_id",
      "revision",
      "outline",
      "classroom_version_id",
    ],
    "interactive classroom result.classroom",
  );
  const estimate = record(result.estimate, "interactive classroom result.estimate");
  exactKeys(
    estimate,
    [
      "scene_range",
      "duration_minutes_range",
      "quota_units",
      "requires_outline_confirmation",
      "requires_approval",
    ],
    "interactive classroom result.estimate",
  );
  const mode = requiredString(classroom, "mode", "interactive classroom result.classroom");
  if (mode !== "micro" && mode !== "full") {
    throw new Error("interactive classroom result.classroom.mode is invalid");
  }
  const topLevelJobId = nullableString(
    result,
    "job_id",
    "interactive classroom result",
  );
  const classroomJobId = nullableString(
    classroom,
    "generation_job_id",
    "interactive classroom result.classroom",
  );
  if (topLevelJobId !== classroomJobId) {
    throw new Error("interactive classroom result job identity is inconsistent");
  }
  const topLevelApprovalId = nullableString(
    result,
    "approval_id",
    "interactive classroom result",
  );
  const classroomApprovalId = nullableString(
    classroom,
    "approval_id",
    "interactive classroom result.classroom",
  );
  if (topLevelApprovalId !== classroomApprovalId) {
    throw new Error("interactive classroom result approval identity is inconsistent");
  }
  const topLevelOutline = nullableRecord(
    result,
    "outline",
    "interactive classroom result",
  );
  const classroomOutline = nullableRecord(
    classroom,
    "outline",
    "interactive classroom result.classroom",
  );
  if (JSON.stringify(topLevelOutline) !== JSON.stringify(classroomOutline)) {
    throw new Error("interactive classroom result outline is inconsistent");
  }
  const status = enumString(
    classroom,
    "status",
    STUDENT_CLASSROOM_STATUSES,
    "interactive classroom result.classroom",
  );
  const revision = requiredInteger(
    classroom,
    "revision",
    "interactive classroom result.classroom",
  );
  if (revision < 1) {
    throw new Error("interactive classroom result.classroom.revision is invalid");
  }
  const quotaUnits = requiredInteger(
    estimate,
    "quota_units",
    "interactive classroom result.estimate",
  );
  if (quotaUnits < 1) {
    throw new Error("interactive classroom result.estimate.quota_units is invalid");
  }
  const requiresOutlineConfirmation = requiredBoolean(
    estimate,
    "requires_outline_confirmation",
    "interactive classroom result.estimate",
  );
  if (requiresOutlineConfirmation !== (mode === "full")) {
    throw new Error("interactive classroom result outline policy is inconsistent");
  }
  const requiresApproval = requiredBoolean(
    estimate,
    "requires_approval",
    "interactive classroom result.estimate",
  );
  if (requiresApproval !== (topLevelApprovalId !== null)) {
    throw new Error("interactive classroom result approval policy is inconsistent");
  }
  if (
    status === "awaiting_approval" &&
    (topLevelApprovalId === null || topLevelJobId !== null)
  ) {
    throw new Error("interactive classroom result approval workflow is inconsistent");
  }
  return {
    assetId: requiredString(
      classroom,
      "asset_id",
      "interactive classroom result.classroom",
    ),
    jobId: topLevelJobId,
    approvalId: topLevelApprovalId,
    status,
    mode,
    revision,
    outline: classroomOutline,
    estimate: {
      sceneRange: numberRange(estimate, "scene_range", "interactive classroom result.estimate"),
      durationMinutesRange: numberRange(
        estimate,
        "duration_minutes_range",
        "interactive classroom result.estimate",
      ),
      quotaUnits,
      requiresOutlineConfirmation,
      requiresApproval,
    },
  };
}

export function extractStudentClassroomTaskFromEvents(
  events: readonly ResultEventLike[] | undefined,
): StudentClassroomTask | null {
  if (!events) return null;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.type !== "result" || event.source !== "interactive_classroom") continue;
    try {
      return parseStudentClassroomTask(event.metadata);
    } catch {
      return null;
    }
  }
  return null;
}

async function requestJson(input: string, init?: RequestInit): Promise<unknown> {
  const response = await apiFetch(input, init);
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new StudentClassroomRequestError(
      `Student classroom request failed (${response.status})`,
      response.status,
    );
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && !Array.isArray(payload)
        ? (payload as Record<string, unknown>).detail
        : undefined;
    throw new StudentClassroomRequestError(
      typeof detail === "string"
        ? detail
        : `Student classroom request failed (${response.status})`,
      response.status,
    );
  }
  return payload;
}

function safeSegment(value: string, label: string): string {
  if (!value.trim()) throw new Error(`${label} is required`);
  return encodeURIComponent(value);
}

function parseStudentClassroomState(
  input: unknown,
  expectedAssetId: string,
): StudentClassroomState {
  const value = record(input, "student classroom response");
  exactKeys(
    value,
    [
      "assetId",
      "requestId",
      "approvalId",
      "generationJobId",
      "status",
      "courseId",
      "classId",
      "mode",
      "ownerId",
      "revision",
      "outline",
      "classroomVersionId",
    ],
    "student classroom response",
  );
  const assetId = requiredString(value, "assetId", "student classroom response");
  if (assetId !== expectedAssetId) {
    throw new Error("Student classroom response asset ID does not match the request");
  }
  const mode = requiredString(value, "mode", "student classroom response");
  if (mode !== "micro" && mode !== "full") {
    throw new Error("student classroom response.mode is invalid");
  }
  const revision = requiredInteger(value, "revision", "student classroom response");
  if (revision < 1) throw new Error("student classroom response.revision is invalid");
  const status = enumString(
    value,
    "status",
    STUDENT_CLASSROOM_STATUSES,
    "student classroom response",
  );
  const approvalId = nullableString(value, "approvalId", "student classroom response");
  const generationJobId = nullableString(
    value,
    "generationJobId",
    "student classroom response",
  );
  const outline = nullableRecord(value, "outline", "student classroom response");
  const classroomVersionId = nullableString(
    value,
    "classroomVersionId",
    "student classroom response",
  );
  if (
    ["awaiting_approval", "rejected", "expired"].includes(status) &&
    (approvalId === null || generationJobId !== null)
  ) {
    throw new Error("student classroom response approval workflow is inconsistent");
  }
  if (
    studentClassroomRequiresOutline(status) &&
    (mode !== "full" || outline === null)
  ) {
    throw new Error("student classroom response outline workflow is inconsistent");
  }
  if (classroomVersionId !== null && status !== "succeeded") {
    throw new Error("student classroom response version workflow is inconsistent");
  }
  return {
    assetId,
    requestId: requiredString(value, "requestId", "student classroom response"),
    approvalId,
    generationJobId,
    status,
    courseId: requiredString(value, "courseId", "student classroom response"),
    classId: requiredString(value, "classId", "student classroom response"),
    mode,
    ownerId: requiredString(value, "ownerId", "student classroom response"),
    revision,
    outline,
    classroomVersionId,
  };
}

function parseStudentClassroomJob(
  input: unknown,
  expectedJobId: string,
): StudentClassroomJob {
  const value = record(input, "classroom job response");
  exactKeys(
    value,
    [
      "job_id",
      "job_kind",
      "phase",
      "status",
      "progress_percent",
      "waiting_reason",
      "cancellable",
      "retryable",
      "outline",
      "error_category",
      "error_code",
      "retry_of_job_id",
      "export_format",
      "download_ready",
    ],
    "classroom job response",
  );
  const jobId = requiredString(value, "job_id", "classroom job response");
  if (jobId !== expectedJobId) {
    throw new Error("Classroom job response ID does not match the request");
  }
  if (value.job_kind !== "generation") {
    throw new Error("Classroom job response has the wrong job kind");
  }
  const progressPercent = requiredInteger(
    value,
    "progress_percent",
    "classroom job response",
  );
  if (progressPercent < 0 || progressPercent > 100) {
    throw new Error("classroom job response.progress_percent is invalid");
  }
  const phase = enumString(
    value,
    "phase",
    GENERATION_JOB_PHASES,
    "classroom job response",
  );
  const status = enumString(
    value,
    "status",
    GENERATION_JOB_STATUSES,
    "classroom job response",
  );
  const waitingReason = nullableString(
    value,
    "waiting_reason",
    "classroom job response",
  );
  const outline = nullableRecord(value, "outline", "classroom job response");
  const errorCategory = nullableString(
    value,
    "error_category",
    "classroom job response",
  );
  const errorCode = nullableString(value, "error_code", "classroom job response");
  const terminal = ["succeeded", "failed", "canceled"].includes(status);
  if (status === "succeeded" && progressPercent !== 100) {
    throw new Error("classroom job response.progress_percent is inconsistent");
  }
  if (
    (status === "generating_outline" && phase !== "outline") ||
    (status === "awaiting_confirmation" &&
      (phase !== "outline" || outline === null)) ||
    (status === "generating_content" && !["content", "micro"].includes(phase))
  ) {
    throw new Error("classroom job response phase workflow is inconsistent");
  }
  if (
    requiredBoolean(value, "cancellable", "classroom job response") === terminal ||
    requiredBoolean(value, "retryable", "classroom job response") !==
      ["failed", "canceled"].includes(status) ||
    (terminal && waitingReason !== null)
  ) {
    throw new Error("classroom job response terminal workflow is inconsistent");
  }
  if (
    (errorCategory === null) !== (errorCode === null) ||
    (!["failed", "canceled"].includes(status) && errorCategory !== null)
  ) {
    throw new Error("classroom job response error workflow is inconsistent");
  }
  if (value.export_format !== null || value.download_ready !== false) {
    throw new Error("classroom job response generation artifact is inconsistent");
  }
  nullableString(value, "retry_of_job_id", "classroom job response");
  return {
    jobId,
    phase,
    status,
    progressPercent,
    waitingReason,
    outline,
    errorCategory,
    errorCode,
  };
}

export async function getStudentClassroom(
  assetId: string,
  signal?: AbortSignal,
): Promise<StudentClassroomState> {
  const encoded = safeSegment(assetId, "asset ID");
  return parseStudentClassroomState(
    await requestJson(`/api/v1/student-classrooms/${encoded}`, {
      cache: "no-store",
      signal,
    }),
    assetId,
  );
}

export async function listStudentClassroomOptions(
  signal?: AbortSignal,
): Promise<StudentClassroomOption[]> {
  return parseStudentClassroomOptions(
    await requestJson("/api/v1/student-classrooms/options", {
      cache: "no-store",
      signal,
    }),
  );
}

export async function estimateStudentClassroom(
  input: StudentClassroomEstimateInput,
  signal?: AbortSignal,
): Promise<StudentClassroomEstimate> {
  const courseId = input.courseId.trim();
  if (!courseId) throw new Error("classroom_course_required");
  const sourceRef = input.sourceRef?.trim();
  if (input.contentMode === "source_grounded" && !sourceRef) {
    throw new Error("classroom_source_required");
  }
  if (input.contentMode === "open_creation" && sourceRef) {
    throw new Error("open_creation_cannot_select_source");
  }
  return parseStudentClassroomEstimate(
    await requestJson("/api/v1/student-classrooms/estimate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        courseId,
        mode: input.mode,
        contentMode: input.contentMode,
        ...(sourceRef
          ? { sourceType: "knowledge_base", sourceRef }
          : {}),
      }),
      signal,
    }),
  );
}

export async function getStudentClassroomJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<StudentClassroomJob> {
  const encoded = safeSegment(jobId, "job ID");
  return parseStudentClassroomJob(
    await requestJson(`/api/v1/classroom-jobs/${encoded}`, {
      cache: "no-store",
      signal,
    }),
    jobId,
  );
}

export async function updateStudentClassroomOutline(
  assetId: string,
  outline: Record<string, unknown>,
  revision: number,
  signal?: AbortSignal,
): Promise<StudentClassroomState> {
  if (!Number.isInteger(revision) || revision < 1) {
    throw new Error("classroom revision is invalid");
  }
  const encoded = safeSegment(assetId, "asset ID");
  return parseStudentClassroomState(
    await requestJson(`/api/v1/student-classrooms/${encoded}/outline`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "If-Match": `"revision-${revision}"`,
      },
      body: JSON.stringify({ outline }),
      signal,
    }),
    assetId,
  );
}

export async function confirmStudentClassroomOutline(
  assetId: string,
  signal?: AbortSignal,
): Promise<StudentClassroomState> {
  const encoded = safeSegment(assetId, "asset ID");
  return parseStudentClassroomState(
    await requestJson(`/api/v1/student-classrooms/${encoded}/confirm-outline`, {
      method: "POST",
      signal,
    }),
    assetId,
  );
}

export async function getStudentClassroomVersionId(
  assetId: string,
  signal?: AbortSignal,
): Promise<string | null> {
  return (await getStudentClassroom(assetId, signal)).classroomVersionId;
}
