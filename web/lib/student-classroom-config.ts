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
    hasAuthorizedSource: boolean;
    option: StudentClassroomOption | null;
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
  return contentMode === "source_grounded"
    ? availability.hasAuthorizedSource
    : true;
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
    !range.every(item => Number.isInteger(item) && item >= 0)
  ) {
    throw new Error(`${label}.${key} must be a two-integer range`);
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
  const classroom = record(result.classroom, "interactive classroom result.classroom");
  const estimate = record(result.estimate, "interactive classroom result.estimate");
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
  return {
    assetId: requiredString(
      classroom,
      "asset_id",
      "interactive classroom result.classroom",
    ),
    jobId: topLevelJobId,
    approvalId: nullableString(
      result,
      "approval_id",
      "interactive classroom result",
    ),
    status: requiredString(
      classroom,
      "status",
      "interactive classroom result.classroom",
    ),
    mode,
    revision: requiredInteger(
      classroom,
      "revision",
      "interactive classroom result.classroom",
    ),
    outline: nullableRecord(
      classroom,
      "outline",
      "interactive classroom result.classroom",
    ),
    estimate: {
      sceneRange: numberRange(estimate, "scene_range", "interactive classroom result.estimate"),
      durationMinutesRange: numberRange(
        estimate,
        "duration_minutes_range",
        "interactive classroom result.estimate",
      ),
      quotaUnits: requiredInteger(
        estimate,
        "quota_units",
        "interactive classroom result.estimate",
      ),
      requiresOutlineConfirmation: requiredBoolean(
        estimate,
        "requires_outline_confirmation",
        "interactive classroom result.estimate",
      ),
      requiresApproval: requiredBoolean(
        estimate,
        "requires_approval",
        "interactive classroom result.estimate",
      ),
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
    throw new Error(`Student classroom request failed (${response.status})`);
  }
  if (!response.ok) {
    const detail = record(payload, "student classroom error").detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : `Student classroom request failed (${response.status})`,
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
  return {
    assetId,
    requestId: requiredString(value, "requestId", "student classroom response"),
    approvalId: nullableString(value, "approvalId", "student classroom response"),
    generationJobId: nullableString(
      value,
      "generationJobId",
      "student classroom response",
    ),
    status: requiredString(value, "status", "student classroom response"),
    courseId: requiredString(value, "courseId", "student classroom response"),
    classId: requiredString(value, "classId", "student classroom response"),
    mode,
    ownerId: requiredString(value, "ownerId", "student classroom response"),
    revision,
    outline: nullableRecord(value, "outline", "student classroom response"),
    classroomVersionId: nullableString(
      value,
      "classroomVersionId",
      "student classroom response",
    ),
  };
}

function parseStudentClassroomJob(
  input: unknown,
  expectedJobId: string,
): StudentClassroomJob {
  const value = record(input, "classroom job response");
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
  return {
    jobId,
    phase: requiredString(value, "phase", "classroom job response"),
    status: requiredString(value, "status", "classroom job response"),
    progressPercent,
    waitingReason: nullableString(value, "waiting_reason", "classroom job response"),
    outline: nullableRecord(value, "outline", "classroom job response"),
    errorCategory: nullableString(value, "error_category", "classroom job response"),
    errorCode: nullableString(value, "error_code", "classroom job response"),
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
