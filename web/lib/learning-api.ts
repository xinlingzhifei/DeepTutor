import { apiUrl, apiFetch } from "./api";
import type {
  EventIngestionResult,
  LearningEvent,
} from "./classroom-events";

export interface ModuleInit {
  id: string;
  name: string;
  order: number;
  pass_threshold?: number;
  knowledge_points: {
    id: string;
    name: string;
    type: string;
    module_id: string;
  }[];
}

export interface LearningKnowledgePoint {
  id: string;
  name: string;
  type: string;
}

export interface LearningModule {
  id: string;
  name: string;
  order: number;
  pass_threshold: number;
  knowledge_points: LearningKnowledgePoint[];
}

export interface ProgressDetail {
  book_id: string;
  modules: LearningModule[];
  mastery_levels: Record<string, number>;
  current_module_id?: string;
  current_stage?: string;
  diagnostic?: unknown;
}

export async function fetchProgress(bookId: string): Promise<ProgressDetail> {
  const res = await apiFetch(apiUrl(`/api/v1/learning/progress/${bookId}`));
  if (!res.ok) throw new Error(`Failed to fetch progress: ${res.status}`);
  return res.json() as Promise<ProgressDetail>;
}

export async function initModules(bookId: string, modules: ModuleInit[]) {
  const res = await apiFetch(
    apiUrl(`/api/v1/learning/progress/${bookId}/init-modules`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modules }),
    },
  );
  if (!res.ok) throw new Error(`Failed to init modules: ${res.status}`);
  return res.json();
}

// ── Mastery map (the dashboard view) ──────────────────────────────────────
// Mirrors deeptutor/learning/policy.py map_summary + next_objective.

export type ObjectiveStatus = "new" | "learning" | "mastered";

export interface MapKnowledgePoint {
  id: string;
  name: string;
  type: string;
  status: ObjectiveStatus;
  mastery: number;
}

export interface MapModule {
  id: string;
  name: string;
  order: number;
  mastered: number;
  total: number;
  knowledge_points: MapKnowledgePoint[];
}

export interface MasteryMap {
  counts: { mastered: number; learning: number; new: number; total: number };
  due_reviews: number;
  complete: boolean;
  modules: MapModule[];
}

export interface NextStep {
  action: string;
  knowledge_point_id: string;
  knowledge_point_name: string;
  knowledge_point_type: string;
  status: string;
  gate: string;
  mastery: number;
  threshold: number;
  reason: string;
  /** The outstanding question's text, when `action` is `answer_pending`. */
  pending_prompt: string;
}

export interface MasteryMapResult {
  book_id: string;
  path_revision: number;
  next: NextStep;
  map: MasteryMap;
}

export async function fetchMasteryMap(
  pathId: string,
  init?: RequestInit,
): Promise<MasteryMapResult> {
  const res = await apiFetch(
    apiUrl(`/api/v1/learning/progress/${encodeURIComponent(pathId)}/map`),
    init,
  );
  if (!res.ok) throw new Error(`Failed to fetch mastery map: ${res.status}`);
  return res.json() as Promise<MasteryMapResult>;
}

// ── Activity feed ─────────────────────────────────────────────────────────
// Mirrors deeptutor/learning/models.py MasteryEvent. Every committed change to
// a path emits one, numbered by the path's revision — which is what lets the
// dashboard follow along with a tutoring session running in another tab.

export interface MasteryEvent {
  id: number;
  revision: number;
  event_type: string;
  payload: Record<string, unknown>;
  session_id: string;
  turn_id: string;
  created_at: number;
}

export async function fetchProgressEvents(
  pathId: string,
  afterRevision = 0,
  init?: RequestInit,
): Promise<MasteryEvent[]> {
  const res = await apiFetch(
    apiUrl(
      `/api/v1/learning/progress/${encodeURIComponent(pathId)}/events?after_revision=${afterRevision}`,
    ),
    init,
  );
  if (!res.ok) throw new Error(`Failed to fetch path events: ${res.status}`);
  return (await res.json()).events as MasteryEvent[];
}

// ── One objective's evidence trail ────────────────────────────────────────
// Mirrors deeptutor/learning/policy.py objective_report.

export interface ObjectiveAttempt {
  question_id: string;
  prompt: string;
  answer: string;
  is_correct: boolean;
  error_type: string;
  at: number;
}

export interface ObjectiveReview {
  due_at: number | null;
  interval_index: number;
  consecutive_correct: number;
  consecutive_wrong: number;
}

export interface ObjectiveErrorRecord {
  id: string;
  error_type: string;
  status: string;
  self_attribution: string;
  retries: number;
  created_at: number;
}

export interface ObjectiveReport {
  id: string;
  name: string;
  type: string;
  module_name: string;
  status: ObjectiveStatus;
  gate: "quantitative" | "qualitative";
  mastered: boolean;
  mastery: number;
  threshold: number;
  attempts: ObjectiveAttempt[];
  correct_count: number;
  explanation: string;
  review: ObjectiveReview | null;
  errors: ObjectiveErrorRecord[];
}

export async function fetchObjectiveReport(
  pathId: string,
  objectiveId: string,
  init?: RequestInit,
): Promise<ObjectiveReport> {
  const res = await apiFetch(
    apiUrl(
      `/api/v1/learning/progress/${encodeURIComponent(pathId)}/objectives/${encodeURIComponent(objectiveId)}`,
    ),
    init,
  );
  if (!res.ok) throw new Error(`Failed to fetch objective: ${res.status}`);
  return (await res.json()).objective as ObjectiveReport;
}

export interface ProgressSummary {
  book_id: string;
  name: string;
  modules_count: number;
  kp_count: number;
  current_stage: string;
  avg_mastery_pct: number;
  updated_at: number;
}

export interface ProgressListResult {
  summaries: ProgressSummary[];
  errors: { book_id: string; error: string }[];
}

export async function fetchAllProgress(): Promise<ProgressListResult> {
  const res = await apiFetch(apiUrl("/api/v1/learning/progress"));
  if (!res.ok) throw new Error(`Failed to fetch all progress: ${res.status}`);
  return res.json();
}

export async function deleteProgress(bookId: string) {
  const res = await apiFetch(
    apiUrl(`/api/v1/learning/progress/${encodeURIComponent(bookId)}`),
    { method: "DELETE" },
  );
  if (!res.ok) throw new Error(`Failed to delete progress: ${res.status}`);
  return res.json();
}

export async function redoProgress(bookId: string) {
  const res = await apiFetch(
    apiUrl(`/api/v1/learning/progress/${encodeURIComponent(bookId)}/redo`),
    { method: "POST" },
  );
  if (!res.ok) throw new Error(`Failed to redo progress: ${res.status}`);
  return res.json();
}

/** Drop an outstanding question, keeping every mastery level already earned. */
export async function skipPendingQuestion(bookId: string) {
  const res = await apiFetch(
    apiUrl(
      `/api/v1/learning/progress/${encodeURIComponent(bookId)}/skip-question`,
    ),
    { method: "POST" },
  );
  if (!res.ok) throw new Error(`Failed to skip question: ${res.status}`);
  return res.json();
}

export async function importFromBook(
  bookId: string,
  chapters: { title: string; knowledge_points: string[] }[],
) {
  const res = await apiFetch(
    apiUrl(
      `/api/v1/learning/progress/${encodeURIComponent(bookId)}/import-from-book`,
    ),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chapters }),
    },
  );
  if (!res.ok) throw new Error(`Failed to import from book: ${res.status}`);
  return res.json();
}

export async function generateModulesFromNotebook(
  bookId: string,
  notebookId: string,
  records: { id: string; type: string; title: string; output: string }[],
): Promise<{ modules: ModuleInit[] }> {
  const res = await apiFetch(
    apiUrl(
      `/api/v1/learning/progress/${encodeURIComponent(bookId)}/generate-from-notebook`,
    ),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notebook_id: notebookId, records }),
    },
  );
  if (!res.ok)
    throw new Error(`Failed to generate modules from notebook: ${res.status}`);
  return res.json();
}

export interface ClassroomLearningSession {
  id: string;
  classroomVersionId: string;
  assignmentId: string | null;
  studentAssetId: string | null;
  status: "active" | "completed" | "abandoned";
  lastCursor: Record<string, unknown> | null;
  startedAt: string;
  completedAt: string | null;
}

export function classroomSessionNeedsCompletionRecovery(
  session: ClassroomLearningSession,
  totalScenes: number,
): boolean {
  if (session.status !== "active" || !Number.isSafeInteger(totalScenes) || totalScenes < 1) {
    return false;
  }
  const cursor = session.lastCursor;
  return (
    cursor !== null &&
    cursor.sceneIndex === totalScenes &&
    cursor.actionIndex === 0
  );
}

export type ClassroomSessionAuthority =
  | { assignmentId: string; studentAssetId?: never }
  | { assignmentId?: never; studentAssetId: string };

export interface LearningReportMastery {
  knowledgePointId: string;
  level: number;
  evidenceCount: number;
}

export interface LearningReportMetrics {
  sessionCount: number;
  completedCount: number;
  completionRate: number;
  completedSceneCount: number;
  validQuizCount: number;
  correctQuizCount: number;
  hintCount: number;
  pblMilestoneCount: number;
  mastery: LearningReportMastery[];
  projectionLagSeconds: number;
}

export interface ClassLearningReport extends LearningReportMetrics {
  classId: string;
}

export interface ClassroomLearningReport extends LearningReportMetrics {
  classroomVersionId: string;
}

function learningRouteSegment(value: string, label: string): string {
  const normalized = value.trim();
  if (!normalized || /[\u0000-\u001f\u007f]/.test(normalized)) {
    throw new Error(`${label} is invalid`);
  }
  return encodeURIComponent(normalized);
}

export class ClassroomLearningApiError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`Classroom learning request failed: ${status}`);
    this.name = "ClassroomLearningApiError";
    this.status = status;
  }
}

async function learningJson(path: string, init?: RequestInit): Promise<unknown> {
  const response = await apiFetch(apiUrl(path), init);
  if (!response.ok) throw new ClassroomLearningApiError(response.status);
  try {
    return await response.json();
  } catch {
    throw new Error("Classroom learning response is invalid");
  }
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} is invalid`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || !value) throw new Error(`${label} is invalid`);
  return value;
}

function nullableText(value: unknown, label: string): string | null {
  if (value === null) return null;
  return text(value, label);
}

function parseClassroomLearningSession(value: unknown): ClassroomLearningSession {
  const item = object(value, "classroom learning session");
  const status = text(item.status, "classroom learning session status");
  if (status !== "active" && status !== "completed" && status !== "abandoned") {
    throw new Error("classroom learning session status is invalid");
  }
  const cursor = item.last_cursor;
  const normalizedCursor =
    cursor !== null &&
    typeof cursor === "object" &&
    !Array.isArray(cursor) &&
    Object.keys(cursor).length === 1 &&
    (cursor as Record<string, unknown>).last_event_seq === 0
      ? null
      : cursor;
  return {
    id: text(item.id, "classroom learning session ID"),
    classroomVersionId: text(
      item.classroom_version_id,
      "classroom learning session version",
    ),
    assignmentId: nullableText(item.assignment_id, "classroom assignment ID"),
    studentAssetId: nullableText(item.student_asset_id, "student classroom asset ID"),
    status,
    lastCursor:
      normalizedCursor === null
        ? null
        : object(normalizedCursor, "classroom learning cursor"),
    startedAt: text(item.started_at, "classroom learning start time"),
    completedAt: nullableText(item.completed_at, "classroom learning completion time"),
  };
}

function authorityBody(authority: ClassroomSessionAuthority): Record<string, string> {
  const assignmentId = authority.assignmentId?.trim();
  const studentAssetId = authority.studentAssetId?.trim();
  if (Boolean(assignmentId) === Boolean(studentAssetId)) {
    throw new Error("classroom learning session requires exactly one authority reference");
  }
  return assignmentId
    ? { assignment_id: assignmentId }
    : { student_asset_id: studentAssetId as string };
}

export async function createClassroomLearningSession(
  authority: ClassroomSessionAuthority,
): Promise<ClassroomLearningSession> {
  return parseClassroomLearningSession(
    await learningJson("/api/v1/classroom-sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(authorityBody(authority)),
    }),
  );
}

export async function getClassroomLearningSession(
  sessionId: string,
): Promise<ClassroomLearningSession> {
  const session = learningRouteSegment(sessionId, "session ID");
  return parseClassroomLearningSession(
    await learningJson(`/api/v1/classroom-sessions/${session}`, { cache: "no-store" }),
  );
}

export async function restoreOrCreateClassroomLearningSession(
  versionId: string,
  authority: ClassroomSessionAuthority,
  existingSessionId?: string | null,
): Promise<ClassroomLearningSession> {
  if (existingSessionId) {
    try {
      const existing = await getClassroomLearningSession(existingSessionId);
      if (existing.classroomVersionId !== versionId) {
        throw new Error("classroom learning version binding is invalid");
      }
      if (existing.status !== "abandoned") return existing;
    } catch (reason) {
      if (!(reason instanceof ClassroomLearningApiError) || reason.status !== 403) {
        throw reason;
      }
    }
  }
  const created = await createClassroomLearningSession(authority);
  if (created.classroomVersionId !== versionId) {
    throw new Error("classroom learning version binding is invalid");
  }
  return created;
}

export async function updateClassroomLearningCursor(
  sessionId: string,
  cursor: Record<string, unknown>,
): Promise<ClassroomLearningSession> {
  const session = learningRouteSegment(sessionId, "session ID");
  return parseClassroomLearningSession(
    await learningJson(`/api/v1/classroom-sessions/${session}/cursor`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cursor }),
    }),
  );
}

export async function completeClassroomLearningSession(
  sessionId: string,
): Promise<ClassroomLearningSession> {
  const session = learningRouteSegment(sessionId, "session ID");
  try {
    return parseClassroomLearningSession(
      await learningJson(`/api/v1/classroom-sessions/${session}/complete`, {
        method: "POST",
      }),
    );
  } catch (reason) {
    try {
      const authoritative = await getClassroomLearningSession(sessionId);
      if (authoritative.status === "completed") return authoritative;
    } catch {
      // Preserve the original completion failure when authoritative recovery is unavailable.
    }
    throw reason;
  }
}

async function classroomTicket(
  sessionId: string,
  suffix: "event-ticket" | "read-ticket",
  body?: Record<string, string>,
): Promise<string> {
  const session = learningRouteSegment(sessionId, "session ID");
  const value = object(
    await learningJson(`/api/v1/classroom-sessions/${session}/${suffix}`, {
      method: "POST",
      ...(body
        ? {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }
        : {}),
    }),
    "classroom ticket response",
  );
  return text(value.ticket, "classroom ticket");
}

function parseEventIngestion(value: unknown): EventIngestionResult {
  const response = object(value, "learning event response");
  const accepted = Array.isArray(response.accepted) ? response.accepted : null;
  const duplicate = Array.isArray(response.duplicate) ? response.duplicate : null;
  const quarantined = Array.isArray(response.quarantined) ? response.quarantined : null;
  if (!accepted || !duplicate || !quarantined) {
    throw new Error("learning event response is invalid");
  }
  return {
    accepted: accepted.map(raw => {
      const item = object(raw, "accepted learning event");
      const seq = item.seq;
      if (!Number.isInteger(seq) || (seq as number) < 1) {
        throw new Error("accepted learning event sequence is invalid");
      }
      return { event_id: text(item.event_id, "accepted event ID"), seq: seq as number };
    }),
    duplicate: duplicate.map(raw => {
      const item = object(raw, "duplicate learning event");
      const seq = item.seq;
      if (!Number.isInteger(seq) || (seq as number) < 1) {
        throw new Error("duplicate learning event sequence is invalid");
      }
      return { event_id: text(item.event_id, "duplicate event ID"), seq: seq as number };
    }),
    quarantined: quarantined.map(raw => {
      const item = object(raw, "quarantined learning event");
      return {
        event_id: text(item.event_id, "quarantined event ID"),
        reason: text(item.reason, "quarantine reason"),
      };
    }),
  };
}

export async function appendClassroomEvents(
  sessionId: string,
  events: readonly LearningEvent[],
): Promise<EventIngestionResult> {
  const session = learningRouteSegment(sessionId, "session ID");
  const ticket = await classroomTicket(sessionId, "event-ticket");
  return parseEventIngestion(
    await learningJson(`/api/v1/classroom-sessions/${session}/events`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Classroom-Ticket": ticket,
      },
      body: JSON.stringify({ events }),
    }),
  );
}

async function readTicket(
  sessionId: string,
  action: "classroom.document.read" | "classroom.media.read",
  resourceId: string,
): Promise<string> {
  return classroomTicket(sessionId, "read-ticket", {
    action,
    resource_id: resourceId,
  });
}

export async function fetchClassroomLearningDocument(
  sessionId: string,
  versionId: string,
): Promise<unknown> {
  const version = learningRouteSegment(versionId, "classroom version ID");
  const ticket = await readTicket(sessionId, "classroom.document.read", versionId);
  return learningJson(`/api/v1/classroom-versions/${version}/document`, {
    headers: { "X-Classroom-Ticket": ticket },
    cache: "no-store",
  });
}

export async function fetchClassroomLearningMedia(
  sessionId: string,
  versionId: string,
  mediaId: string,
): Promise<Blob> {
  const version = learningRouteSegment(versionId, "classroom version ID");
  const media = learningRouteSegment(mediaId, "classroom media ID");
  const ticket = await readTicket(sessionId, "classroom.media.read", mediaId);
  const response = await apiFetch(
    apiUrl(`/api/v1/classroom-versions/${version}/media/${media}`),
    { headers: { "X-Classroom-Ticket": ticket }, cache: "no-store" },
  );
  if (!response.ok) throw new Error(`Classroom media request failed: ${response.status}`);
  return response.blob();
}

function number(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${label} is invalid`);
  }
  return value;
}

function nonNegativeInteger(value: unknown, label: string): number {
  const parsed = number(value, label);
  if (!Number.isInteger(parsed) || parsed < 0) throw new Error(`${label} is invalid`);
  return parsed;
}

function nonNegativeNumber(value: unknown, label: string): number {
  const parsed = number(value, label);
  if (parsed < 0) throw new Error(`${label} is invalid`);
  return parsed;
}

function unitInterval(value: unknown, label: string): number {
  const parsed = number(value, label);
  if (parsed < 0 || parsed > 1) throw new Error(`${label} is invalid`);
  return parsed;
}

function parseLearningReport(value: unknown): LearningReportMetrics {
  const report = object(value, "learning report");
  const mastery = report.mastery;
  if (!Array.isArray(mastery)) throw new Error("learning report mastery is invalid");
  const metrics: LearningReportMetrics = {
    sessionCount: nonNegativeInteger(report.sessionCount, "learning report session count"),
    completedCount: nonNegativeInteger(
      report.completedCount,
      "learning report completion count",
    ),
    completionRate: unitInterval(report.completionRate, "learning report completion rate"),
    completedSceneCount: nonNegativeInteger(
      report.completedSceneCount,
      "learning report scene count",
    ),
    validQuizCount: nonNegativeInteger(report.validQuizCount, "learning report quiz count"),
    correctQuizCount: nonNegativeInteger(
      report.correctQuizCount,
      "learning report correct quiz count",
    ),
    hintCount: nonNegativeInteger(report.hintCount, "learning report hint count"),
    pblMilestoneCount: nonNegativeInteger(
      report.pblMilestoneCount,
      "learning report milestone count",
    ),
    mastery: mastery.map(raw => {
      const item = object(raw, "learning report mastery item");
      return {
        knowledgePointId: text(item.knowledgePointId, "knowledge point ID"),
        level: unitInterval(item.level, "learning report mastery level"),
        evidenceCount: nonNegativeInteger(
          item.evidenceCount,
          "learning report mastery evidence count",
        ),
      };
    }),
    projectionLagSeconds: nonNegativeNumber(
      report.projectionLagSeconds,
      "learning report projection lag",
    ),
  };
  if (
    metrics.completedCount > metrics.sessionCount ||
    metrics.correctQuizCount > metrics.validQuizCount
  ) {
    throw new Error("learning report aggregate is invalid");
  }
  return metrics;
}

export async function fetchClassLearningReport(classId: string): Promise<ClassLearningReport> {
  const encoded = learningRouteSegment(classId, "class ID");
  const value = await learningJson(`/api/v1/teaching-reports/classes/${encoded}`, {
    cache: "no-store",
  });
  const report = object(value, "class learning report");
  const returnedClassId = text(report.classId, "class learning report class ID");
  if (returnedClassId !== classId) throw new Error("class learning report binding is invalid");
  return { classId: returnedClassId, ...parseLearningReport(report) };
}

export async function fetchClassroomLearningReport(
  versionId: string,
): Promise<ClassroomLearningReport> {
  const encoded = learningRouteSegment(versionId, "classroom version ID");
  const value = await learningJson(`/api/v1/teaching-reports/classrooms/${encoded}`, {
    cache: "no-store",
  });
  const report = object(value, "classroom learning report");
  const returnedVersionId = text(
    report.classroomVersionId,
    "classroom learning report version ID",
  );
  if (returnedVersionId !== versionId) {
    throw new Error("classroom learning report binding is invalid");
  }
  return { classroomVersionId: returnedVersionId, ...parseLearningReport(report) };
}

export async function resolveStudentClassroomAuthority(
  versionId: string,
): Promise<{ studentAssetId: string } | null> {
  const value = object(
    await learningJson("/api/v1/student-classrooms", { cache: "no-store" }),
    "student classroom list",
  );
  if (!Array.isArray(value.items)) throw new Error("student classroom list is invalid");
  const matches = value.items.filter(raw => {
    const item = object(raw, "student classroom item");
    return item.status === "succeeded" && item.classroomVersionId === versionId;
  });
  if (matches.length > 1) throw new Error("student classroom version binding is ambiguous");
  if (matches.length === 0) return null;
  const item = object(matches[0], "student classroom item");
  return { studentAssetId: text(item.assetId, "student classroom asset ID") };
}
