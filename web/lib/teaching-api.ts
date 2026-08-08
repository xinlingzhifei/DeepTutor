import { apiFetch, apiUrl } from "@/lib/api";
import {
  parseYFeClassroomDocument,
  type ClassroomDocument,
  type JsonObject,
} from "@/lib/openmaic-adapter/contracts";

export type TeachingContentMode = "source_grounded" | "open_creation";
export type TeachingSourceType = "knowledge_base" | "pdf";
export type TeachingMediaPolicy = "text_only" | "image_audio";
export type TeachingExportFormat =
  | "classroom_zip"
  | "pptx"
  | "offline_html"
  | "mp4";

export interface TeachingCourse {
  id: string;
  title: string;
  status: string;
  createdAt: string | null;
}

export interface TeachingClass {
  id: string;
  courseId: string;
  name: string;
  status: string;
  createdAt: string | null;
}

export interface TeachingSource {
  bindingId: string;
  sourceType: TeachingSourceType;
  sourceId: string;
  filename: string | null;
  sha256: string;
  sizeBytes: number | null;
  courseId: string | null;
  classId: string | null;
  createdAt: string | null;
}

export interface TeachingKnowledgePointInput {
  knowledgePointId: string;
  title: string;
  description: string;
}

export interface TeachingClassroomCreateInput {
  title: string;
  courseId: string;
  classId: string;
  objective: string;
  gradeBand: string;
  audience: string;
  durationMinutes: number;
  classroomMode: "full";
  webPolicy: "disabled" | "enabled";
  mediaPolicy: TeachingMediaPolicy;
  allowedWebDomains: string[];
  templateId: string;
  templateVersion: string;
  knowledgePoints: TeachingKnowledgePointInput[];
  contentMode: TeachingContentMode;
  openCreationAcknowledged: boolean;
  sourceType: TeachingSourceType | null;
  sourceRef: string | null;
  requestedExports: TeachingExportFormat[];
}

export interface TeachingValidationIssue {
  severity: "error" | "warning";
  code: string;
  message: string;
  path: string;
}

export interface TeachingValidationReport {
  valid: boolean;
  issues?: TeachingValidationIssue[];
  severeFindings?: TeachingValidationIssue[];
  warnings?: TeachingValidationIssue[];
  sections?: Partial<
    Record<
      TeachingValidationSectionName,
      {
        status: "pass" | "warning" | "error";
        issues: TeachingValidationIssue[];
      }
    >
  >;
  draftRevision?: number;
  documentSha256?: string;
}

export type TeachingValidationSectionName =
  | "dsl_integrity"
  | "media_integrity"
  | "knowledge_point_coverage"
  | "source_traceability"
  | "unsupported_claims"
  | "quiz_answerability"
  | "interactive_security"
  | "accessibility"
  | "export_readiness";

export interface TeachingClassroom {
  assetId: string;
  draftId: string;
  jobId: string | null;
  lifecycleState: string;
  status: string;
  title: string;
  courseId: string;
  classId: string;
  ownerId: string;
  revision: number;
  outline: JsonObject | null;
  document: ClassroomDocument | null;
  classroomVersionId: string | null;
  confirmedOutlineSha256: string | null;
  validationReport: TeachingValidationReport | null;
  idempotencyKey: string | null;
}

export interface TeachingReview {
  id: string;
  assetId: string;
  draftId: string;
  draftRevision: number;
  documentSha256: string;
  validationReportSha256: string;
  submittedBy: string;
  scope: "class" | "tenant" | "platform";
  classId: string | null;
  status: "pending" | "approved" | "rejected";
  warnings: TeachingValidationIssue[];
  reviewerId: string | null;
  comment: string | null;
}

export interface TeachingReviewSourceFragment {
  fragmentId: string;
  sourceId: string;
  text: string;
  contentSha256: string;
}

export interface TeachingReviewBaseline {
  versionId: string;
  versionNumber: number;
  documentSha256: string;
}

export interface TeachingReviewDetail {
  review: TeachingReview;
  title: string;
  courseId: string;
  targetClassId: string;
  document: ClassroomDocument;
  validationReport: TeachingValidationReport;
  sourceFragments: TeachingReviewSourceFragment[];
  baseline: TeachingReviewBaseline | null;
  changedPaths: string[];
}

export interface TeachingPublication {
  publicationId: string;
  versionId: string;
  assetId: string;
  versionNumber: number;
  title: string;
  courseId: string;
  documentSha256: string;
  publishedBy: string;
  createdAt: string;
}

export interface TeachingPublicationCandidate {
  reviewId: string;
  assetId: string;
  title: string;
  courseId: string;
  draftRevision: number;
  documentSha256: string;
  submittedBy: string;
}

export interface TeachingPublicationList {
  items: TeachingPublication[];
  candidates: TeachingPublicationCandidate[];
}

export interface TeachingPublishedVersion {
  versionId: string;
  assetId: string;
  versionNumber: number;
  documentSha256: string;
  publicationScope: "class" | "tenant" | "platform";
  classId: string | null;
  idempotencyKey: string;
}

export interface TeachingBatchItem {
  id: string;
  batchId: string;
  status:
    | "queued"
    | "running"
    | "awaiting_confirmation"
    | "succeeded"
    | "failed"
    | "canceled";
  generationJobId: string | null;
  classroomDraftId: string | null;
  classroomAssetId: string | null;
}

export interface TeachingBatch {
  id: string;
  tenantId: string;
  actorId: string;
  status:
    | "queued"
    | "running"
    | "succeeded"
    | "partially_succeeded"
    | "failed"
    | "canceled";
  itemCount: number;
  succeededCount: number;
  failedCount: number;
  items: TeachingBatchItem[];
  createdAt: string | null;
  updatedAt: string | null;
}

export interface BatchOutlineConfirmation {
  itemId: string;
  revision: number;
  outlineSha256: string;
}

export interface TeachingBatchRetry {
  parentItemId: string;
  item: TeachingBatchItem;
}

export class TeachingApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null = null,
  ) {
    super(message);
    this.name = "TeachingApiError";
  }
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TeachingApiError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function string(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new TeachingApiError(`${label} must be a non-empty string`);
  }
  return value;
}

function nullableString(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  return string(value, label);
}

function integer(value: unknown, label: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    throw new TeachingApiError(`${label} must be an integer >= ${minimum}`);
  }
  return value as number;
}

function array(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new TeachingApiError(`${label} must be an array`);
  }
  return value;
}

function exactKeys(
  value: Record<string, unknown>,
  label: string,
  required: readonly string[],
  optional: readonly string[] = [],
): void {
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new TeachingApiError(`${label} has unexpected key: ${key}`);
    }
  }
  for (const key of required) {
    if (!Object.hasOwn(value, key)) {
      throw new TeachingApiError(`${label} is missing key: ${key}`);
    }
  }
}

function boolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new TeachingApiError(`${label} must be a boolean`);
  }
  return value;
}

function oneOf<const T extends string>(
  value: unknown,
  label: string,
  allowed: readonly T[],
): T {
  const parsed = string(value, label);
  if (!allowed.includes(parsed as T)) {
    throw new TeachingApiError(`${label} is invalid`);
  }
  return parsed as T;
}

function sha256(value: unknown, label: string): string {
  const parsed = string(value, label);
  if (!/^[a-f0-9]{64}$/.test(parsed)) {
    throw new TeachingApiError(`${label} must be a lowercase SHA-256`);
  }
  return parsed;
}

function safeSegment(value: string, label: string): string {
  if (
    value.length === 0 ||
    value.trim() !== value ||
    value === "." ||
    value === ".." ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new TeachingApiError(`${label} must be a safe identifier`);
  }
  return encodeURIComponent(value);
}

function safeHeader(value: string, label: string): string {
  if (
    value.length < 8 ||
    value.length > 128 ||
    value.trim() !== value ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]+$/.test(value)
  ) {
    throw new TeachingApiError(`${label} is invalid`);
  }
  return value;
}

async function responseError(response: Response): Promise<TeachingApiError> {
  let detail = "";
  try {
    const payload = object(await response.json(), "error response");
    if (typeof payload.detail === "string") detail = `: ${payload.detail}`;
  } catch {
    // The status is still stable when the response has no JSON body.
  }
  return new TeachingApiError(
    `Teaching API request failed (${response.status})${detail}`,
    response.status,
  );
}

async function requestJson(path: string, init?: RequestInit): Promise<unknown> {
  const response = await apiFetch(apiUrl(path), init);
  if (!response.ok) throw await responseError(response);
  try {
    return await response.json();
  } catch {
    throw new TeachingApiError("Teaching API returned invalid JSON", response.status);
  }
}

function jsonRequest(body?: unknown, headers?: Record<string, string>): RequestInit {
  return {
    headers: { "Content-Type": "application/json", ...headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}

function parseCourse(input: unknown): TeachingCourse {
  const value = object(input, "course");
  exactKeys(value, "course", ["id", "title", "status", "createdAt"]);
  return {
    id: string(value.id, "course.id"),
    title: string(value.title, "course.title"),
    status: string(value.status, "course.status"),
    createdAt: nullableString(value.createdAt, "course.createdAt"),
  };
}

function parseClass(input: unknown): TeachingClass {
  const value = object(input, "class");
  exactKeys(value, "class", ["id", "courseId", "name", "status", "createdAt"]);
  return {
    id: string(value.id, "class.id"),
    courseId: string(value.courseId, "class.courseId"),
    name: string(value.name, "class.name"),
    status: string(value.status, "class.status"),
    createdAt: nullableString(value.createdAt, "class.createdAt"),
  };
}

function parseSource(input: unknown): TeachingSource {
  const value = object(input, "source");
  exactKeys(value, "source", [
    "bindingId",
    "sourceType",
    "sourceId",
    "filename",
    "sha256",
    "sizeBytes",
    "courseId",
    "classId",
    "createdAt",
  ]);
  const sourceType = string(value.sourceType, "source.sourceType");
  if (sourceType !== "knowledge_base" && sourceType !== "pdf") {
    throw new TeachingApiError("source.sourceType is invalid");
  }
  return {
    bindingId: string(value.bindingId, "source.bindingId"),
    sourceType,
    sourceId: string(value.sourceId, "source.sourceId"),
    filename: nullableString(value.filename, "source.filename"),
    sha256: sha256(value.sha256, "source.sha256"),
    sizeBytes:
      value.sizeBytes === null || value.sizeBytes === undefined
        ? null
        : integer(value.sizeBytes, "source.sizeBytes"),
    courseId: nullableString(value.courseId, "source.courseId"),
    classId: nullableString(value.classId, "source.classId"),
    createdAt: nullableString(value.createdAt, "source.createdAt"),
  };
}

function parseJsonObject(value: unknown, label: string): JsonObject {
  return object(value, label) as JsonObject;
}

function parseValidationIssue(
  input: unknown,
  label: string,
): TeachingValidationIssue {
  const issue = object(input, label);
  exactKeys(issue, label, ["severity", "code", "message", "path"]);
  return {
    severity: oneOf(issue.severity, `${label}.severity`, [
      "error",
      "warning",
    ] as const),
    code: string(issue.code, `${label}.code`),
    message: string(issue.message, `${label}.message`),
    path: string(issue.path, `${label}.path`),
  };
}

function parseValidationReport(value: unknown): TeachingValidationReport {
  const report = object(value, "validation report");
  exactKeys(report, "validation report", ["valid"], [
    "issues",
    "severeFindings",
    "warnings",
    "sections",
    "draftRevision",
    "documentSha256",
  ]);
  const parseIssues = (raw: unknown, label: string): TeachingValidationIssue[] =>
    array(raw, label).map((entry, index) =>
      parseValidationIssue(entry, `${label}[${index}]`),
    );
  const result: TeachingValidationReport = {
    valid: boolean(report.valid, "validation report.valid"),
  };
  for (const key of ["issues", "severeFindings", "warnings"] as const) {
    if (report[key] !== undefined) {
      result[key] = parseIssues(report[key], `validation report.${key}`);
    }
  }
  if (report.sections !== undefined) {
    const sections = object(report.sections, "validation report.sections");
    const names = [
      "dsl_integrity",
      "media_integrity",
      "knowledge_point_coverage",
      "source_traceability",
      "unsupported_claims",
      "quiz_answerability",
      "interactive_security",
      "accessibility",
      "export_readiness",
    ] as const;
    exactKeys(sections, "validation report.sections", [], names);
    const parsedSections: TeachingValidationReport["sections"] = {};
    for (const name of names) {
      if (sections[name] === undefined) continue;
      const section = object(
        sections[name],
        `validation report.sections.${name}`,
      );
      exactKeys(section, `validation report.sections.${name}`, [
        "status",
        "issues",
      ]);
      parsedSections[name] = {
        status: oneOf(
          section.status,
          `validation report.sections.${name}.status`,
          ["pass", "warning", "error"] as const,
        ),
        issues: parseIssues(
          section.issues,
          `validation report.sections.${name}.issues`,
        ),
      };
    }
    result.sections = parsedSections;
  }
  if (report.draftRevision !== undefined) {
    result.draftRevision = integer(
      report.draftRevision,
      "validation report.draftRevision",
      1,
    );
  }
  if (report.documentSha256 !== undefined) {
    result.documentSha256 = sha256(
      report.documentSha256,
      "validation report.documentSha256",
    );
  }
  return result;
}

function parseClassroom(input: unknown): TeachingClassroom {
  const value = object(input, "classroom");
  exactKeys(value, "classroom", [
    "assetId",
    "draftId",
    "jobId",
    "lifecycleState",
    "status",
    "title",
    "courseId",
    "classId",
    "ownerId",
    "revision",
    "outline",
    "document",
    "classroomVersionId",
    "confirmedOutlineSha256",
    "validationReport",
    "idempotencyKey",
  ]);
  return {
    assetId: string(value.assetId, "classroom.assetId"),
    draftId: string(value.draftId, "classroom.draftId"),
    jobId: nullableString(value.jobId, "classroom.jobId"),
    lifecycleState: string(value.lifecycleState, "classroom.lifecycleState"),
    status: string(value.status, "classroom.status"),
    title: string(value.title, "classroom.title"),
    courseId: string(value.courseId, "classroom.courseId"),
    classId: string(value.classId, "classroom.classId"),
    ownerId: string(value.ownerId, "classroom.ownerId"),
    revision: integer(value.revision, "classroom.revision", 1),
    outline:
      value.outline === null || value.outline === undefined
        ? null
        : parseJsonObject(value.outline, "classroom.outline"),
    document:
      value.document === null || value.document === undefined
        ? null
        : parseYFeClassroomDocument(value.document),
    classroomVersionId: nullableString(
      value.classroomVersionId,
      "classroom.classroomVersionId",
    ),
    confirmedOutlineSha256:
      value.confirmedOutlineSha256 === null ||
      value.confirmedOutlineSha256 === undefined
        ? null
        : sha256(
            value.confirmedOutlineSha256,
            "classroom.confirmedOutlineSha256",
          ),
    validationReport:
      value.validationReport === null || value.validationReport === undefined
        ? null
        : parseValidationReport(value.validationReport),
    idempotencyKey: nullableString(
      value.idempotencyKey,
      "classroom.idempotencyKey",
    ),
  };
}

function parseReview(input: unknown): TeachingReview {
  const value = object(input, "review");
  exactKeys(value, "review", [
    "id",
    "assetId",
    "draftId",
    "draftRevision",
    "documentSha256",
    "validationReportSha256",
    "submittedBy",
    "scope",
    "classId",
    "status",
    "warnings",
    "reviewerId",
    "comment",
  ]);
  const scope = oneOf(value.scope, "review.scope", [
    "class",
    "tenant",
    "platform",
  ] as const);
  const classId = nullableString(value.classId, "review.classId");
  if ((scope === "class") !== (classId !== null)) {
    throw new TeachingApiError("review class binding is invalid");
  }
  return {
    id: string(value.id, "review.id"),
    assetId: string(value.assetId, "review.assetId"),
    draftId: string(value.draftId, "review.draftId"),
    draftRevision: integer(value.draftRevision, "review.draftRevision", 1),
    documentSha256: sha256(value.documentSha256, "review.documentSha256"),
    validationReportSha256: sha256(
      value.validationReportSha256,
      "review.validationReportSha256",
    ),
    submittedBy: string(value.submittedBy, "review.submittedBy"),
    scope,
    classId,
    status: oneOf(value.status, "review.status", ["pending", "approved", "rejected"] as const),
    warnings: array(value.warnings, "review.warnings").map((item, index) =>
      parseValidationIssue(item, `review.warnings[${index}]`),
    ),
    reviewerId: nullableString(value.reviewerId, "review.reviewerId"),
    comment: nullableString(value.comment, "review.comment"),
  };
}

function parseReviewDetail(input: unknown): TeachingReviewDetail {
  const value = object(input, "review detail");
  exactKeys(value, "review detail", [
    "review",
    "title",
    "courseId",
    "targetClassId",
    "document",
    "validationReport",
    "sourceFragments",
    "baseline",
    "changedPaths",
  ]);
  const review = parseReview(value.review);
  const sourceFragments = array(
    value.sourceFragments,
    "review detail.sourceFragments",
  ).map((entry, index): TeachingReviewSourceFragment => {
    const fragment = object(
      entry,
      `review detail.sourceFragments[${index}]`,
    );
    exactKeys(fragment, `review detail.sourceFragments[${index}]`, [
      "fragmentId",
      "sourceId",
      "text",
      "contentSha256",
    ]);
    return {
      fragmentId: string(
        fragment.fragmentId,
        `review detail.sourceFragments[${index}].fragmentId`,
      ),
      sourceId: string(
        fragment.sourceId,
        `review detail.sourceFragments[${index}].sourceId`,
      ),
      text: string(
        fragment.text,
        `review detail.sourceFragments[${index}].text`,
      ),
      contentSha256: sha256(
        fragment.contentSha256,
        `review detail.sourceFragments[${index}].contentSha256`,
      ),
    };
  });
  let baseline: TeachingReviewBaseline | null = null;
  if (value.baseline !== null) {
    const raw = object(value.baseline, "review detail.baseline");
    exactKeys(raw, "review detail.baseline", [
      "versionId",
      "versionNumber",
      "documentSha256",
    ]);
    baseline = {
      versionId: string(raw.versionId, "review detail.baseline.versionId"),
      versionNumber: integer(
        raw.versionNumber,
        "review detail.baseline.versionNumber",
        1,
      ),
      documentSha256: sha256(
        raw.documentSha256,
        "review detail.baseline.documentSha256",
      ),
    };
  }
  const changedPaths = array(
    value.changedPaths,
    "review detail.changedPaths",
  ).map((path, index) => {
    const parsed = string(path, `review detail.changedPaths[${index}]`);
    if (!parsed.startsWith("/") || /[\u0000-\u001f\u007f]/.test(parsed)) {
      throw new TeachingApiError(
        `review detail.changedPaths[${index}] must be a JSON pointer`,
      );
    }
    return parsed;
  });
  return {
    review,
    title: string(value.title, "review detail.title"),
    courseId: string(value.courseId, "review detail.courseId"),
    targetClassId: string(
      value.targetClassId,
      "review detail.targetClassId",
    ),
    document: parseYFeClassroomDocument(value.document),
    validationReport: parseValidationReport(value.validationReport),
    sourceFragments,
    baseline,
    changedPaths,
  };
}

function parsePublication(input: unknown): TeachingPublication {
  const value = object(input, "publication");
  exactKeys(value, "publication", [
    "publicationId",
    "versionId",
    "assetId",
    "versionNumber",
    "title",
    "courseId",
    "documentSha256",
    "publishedBy",
    "createdAt",
  ]);
  return {
    publicationId: string(value.publicationId, "publication.publicationId"),
    versionId: string(value.versionId, "publication.versionId"),
    assetId: string(value.assetId, "publication.assetId"),
    versionNumber: integer(value.versionNumber, "publication.versionNumber", 1),
    title: string(value.title, "publication.title"),
    courseId: string(value.courseId, "publication.courseId"),
    documentSha256: sha256(
      value.documentSha256,
      "publication.documentSha256",
    ),
    publishedBy: string(value.publishedBy, "publication.publishedBy"),
    createdAt: string(value.createdAt, "publication.createdAt"),
  };
}

function parsePublicationCandidate(input: unknown): TeachingPublicationCandidate {
  const value = object(input, "publication candidate");
  exactKeys(value, "publication candidate", [
    "reviewId",
    "assetId",
    "title",
    "courseId",
    "draftRevision",
    "documentSha256",
    "submittedBy",
  ]);
  return {
    reviewId: string(value.reviewId, "publication candidate.reviewId"),
    assetId: string(value.assetId, "publication candidate.assetId"),
    title: string(value.title, "publication candidate.title"),
    courseId: string(value.courseId, "publication candidate.courseId"),
    draftRevision: integer(
      value.draftRevision,
      "publication candidate.draftRevision",
      1,
    ),
    documentSha256: sha256(
      value.documentSha256,
      "publication candidate.documentSha256",
    ),
    submittedBy: string(
      value.submittedBy,
      "publication candidate.submittedBy",
    ),
  };
}

function parsePublishedVersion(input: unknown): TeachingPublishedVersion {
  const value = object(input, "published version");
  exactKeys(value, "published version", [
    "versionId",
    "assetId",
    "versionNumber",
    "documentSha256",
    "publicationScope",
    "classId",
    "idempotencyKey",
  ]);
  const publicationScope = oneOf(
    value.publicationScope,
    "published version.publicationScope",
    ["class", "tenant", "platform"] as const,
  );
  const classId = nullableString(value.classId, "published version.classId");
  if ((publicationScope === "class") !== (classId !== null)) {
    throw new TeachingApiError("published version class binding is invalid");
  }
  return {
    versionId: string(value.versionId, "published version.versionId"),
    assetId: string(value.assetId, "published version.assetId"),
    versionNumber: integer(
      value.versionNumber,
      "published version.versionNumber",
      1,
    ),
    documentSha256: sha256(
      value.documentSha256,
      "published version.documentSha256",
    ),
    publicationScope,
    classId,
    idempotencyKey: string(
      value.idempotencyKey,
      "published version.idempotencyKey",
    ),
  };
}

function parseBatchItem(input: unknown): TeachingBatchItem {
  const value = object(input, "batch item");
  exactKeys(value, "batch item", [
    "id",
    "batchId",
    "status",
    "generationJobId",
    "classroomDraftId",
    "classroomAssetId",
  ]);
  return {
    id: string(value.id, "batch item.id"),
    batchId: string(value.batchId, "batch item.batchId"),
    status: oneOf(value.status, "batch item.status", [
      "queued",
      "running",
      "awaiting_confirmation",
      "succeeded",
      "failed",
      "canceled",
    ] as const),
    generationJobId: nullableString(
      value.generationJobId,
      "batch item.generationJobId",
    ),
    classroomDraftId: nullableString(
      value.classroomDraftId,
      "batch item.classroomDraftId",
    ),
    classroomAssetId: nullableString(
      value.classroomAssetId,
      "batch item.classroomAssetId",
    ),
  };
}

function parseBatch(input: unknown): TeachingBatch {
  const value = object(input, "batch");
  exactKeys(value, "batch", [
    "id",
    "tenantId",
    "actorId",
    "status",
    "itemCount",
    "succeededCount",
    "failedCount",
    "items",
    "createdAt",
    "updatedAt",
  ]);
  return {
    id: string(value.id, "batch.id"),
    tenantId: string(value.tenantId, "batch.tenantId"),
    actorId: string(value.actorId, "batch.actorId"),
    status: oneOf(value.status, "batch.status", [
      "queued",
      "running",
      "succeeded",
      "partially_succeeded",
      "failed",
      "canceled",
    ] as const),
    itemCount: integer(value.itemCount, "batch.itemCount"),
    succeededCount: integer(value.succeededCount, "batch.succeededCount"),
    failedCount: integer(value.failedCount, "batch.failedCount"),
    items: array(value.items, "batch.items").map(parseBatchItem),
    createdAt: nullableString(value.createdAt, "batch.createdAt"),
    updatedAt: nullableString(value.updatedAt, "batch.updatedAt"),
  };
}

function parseList<T>(
  input: unknown,
  label: string,
  parseItem: (value: unknown) => T,
): T[] {
  const value = object(input, label);
  exactKeys(value, label, ["items"]);
  return array(value.items, `${label}.items`).map(parseItem);
}

export function classroomNextRoute(input: {
  assetId: string;
  status: string;
}): string {
  const assetId = safeSegment(input.assetId, "asset ID");
  if (input.status === "awaiting_confirmation") {
    return `/teaching/classrooms/${assetId}/outline`;
  }
  if (input.status === "editing") {
    return `/teaching/classrooms/${assetId}/edit`;
  }
  return "/teaching/classrooms";
}

export function classroomRevisionEtag(revision: number): string {
  return `"revision-${integer(revision, "classroom revision", 1)}"`;
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TeachingApiError("JSON must be finite");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  const record = object(value, "JSON value");
  return `{${Object.keys(record)
    .sort()
    .map(key => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
    .join(",")}}`;
}

export async function canonicalOutlineSha256(outline: JsonObject): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(outline));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map(value => value.toString(16).padStart(2, "0"))
    .join("");
}

export async function listTeachingCourses(): Promise<TeachingCourse[]> {
  return parseList(
    await requestJson("/api/v1/teaching/courses", { cache: "no-store" }),
    "course list",
    parseCourse,
  );
}

export async function listTeachingClasses(courseId: string): Promise<TeachingClass[]> {
  return parseList(
    await requestJson(
      `/api/v1/teaching/courses/${safeSegment(courseId, "course ID")}/classes`,
      { cache: "no-store" },
    ),
    "class list",
    parseClass,
  );
}

export async function listTeachingSources(): Promise<TeachingSource[]> {
  return parseList(
    await requestJson("/api/v1/teaching/sources", { cache: "no-store" }),
    "source list",
    parseSource,
  );
}

export async function uploadTeachingPdfSource(
  file: File,
  courseId: string,
  classId: string | null,
): Promise<TeachingSource> {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("courseId", courseId);
  if (classId) form.append("classId", classId);
  return parseSource(
    await requestJson("/api/v1/teaching/sources/pdf", {
      method: "POST",
      body: form,
    }),
  );
}

export async function createTeachingClassroom(
  input: TeachingClassroomCreateInput,
  idempotencyKey: string,
): Promise<TeachingClassroom> {
  return parseClassroom(
    await requestJson("/api/v1/classrooms", {
      method: "POST",
      ...jsonRequest(input, {
        "Idempotency-Key": safeHeader(idempotencyKey, "idempotency key"),
      }),
    }),
  );
}

export async function listTeachingClassrooms(): Promise<TeachingClassroom[]> {
  return parseList(
    await requestJson("/api/v1/classrooms", { cache: "no-store" }),
    "classroom list",
    parseClassroom,
  );
}

export async function getTeachingClassroom(
  assetId: string,
  options: { draft?: boolean } = {},
): Promise<TeachingClassroom> {
  const suffix = options.draft ? "/draft" : "";
  return parseClassroom(
    await requestJson(
      `/api/v1/classrooms/${safeSegment(assetId, "asset ID")}${suffix}`,
      { cache: "no-store" },
    ),
  );
}

export async function updateTeachingOutline(
  assetId: string,
  outline: JsonObject,
  revision: number,
): Promise<TeachingClassroom> {
  return parseClassroom(
    await requestJson(
      `/api/v1/classrooms/${safeSegment(assetId, "asset ID")}/outline`,
      {
        method: "PUT",
        ...jsonRequest(
          { outline },
          { "If-Match": classroomRevisionEtag(revision) },
        ),
      },
    ),
  );
}

export async function confirmTeachingOutline(
  assetId: string,
): Promise<TeachingClassroom> {
  return parseClassroom(
    await requestJson(
      `/api/v1/classrooms/${safeSegment(assetId, "asset ID")}/confirm-outline`,
      { method: "POST" },
    ),
  );
}

export async function validateTeachingClassroom(
  assetId: string,
): Promise<TeachingClassroom> {
  return parseClassroom(
    await requestJson(
      `/api/v1/classrooms/${safeSegment(assetId, "asset ID")}/validate`,
      { method: "POST" },
    ),
  );
}

export async function submitTeachingClassroom(
  assetId: string,
  input: { scope: "class" | "tenant" | "platform"; classId: string | null },
  idempotencyKey: string,
): Promise<TeachingReview> {
  return parseReview(
    await requestJson(
      `/api/v1/classrooms/${safeSegment(assetId, "asset ID")}/submit`,
      {
        method: "POST",
        ...jsonRequest(input, {
          "Idempotency-Key": safeHeader(idempotencyKey, "idempotency key"),
        }),
      },
    ),
  );
}

export async function listTeachingReviews(): Promise<TeachingReview[]> {
  return parseList(
    await requestJson("/api/v1/classroom-reviews", { cache: "no-store" }),
    "review list",
    parseReview,
  );
}

export async function getTeachingReviewDetail(
  reviewId: string,
): Promise<TeachingReviewDetail> {
  return parseReviewDetail(
    await requestJson(
      `/api/v1/classroom-reviews/${safeSegment(reviewId, "review ID")}`,
      { cache: "no-store" },
    ),
  );
}

export async function decideTeachingReview(
  reviewId: string,
  decision: "approve" | "reject",
  comment: string,
): Promise<TeachingReview> {
  return parseReview(
    await requestJson(
      `/api/v1/classroom-reviews/${safeSegment(reviewId, "review ID")}/${decision}`,
      { method: "POST", ...jsonRequest({ comment }) },
    ),
  );
}

export async function listTeachingPublications(): Promise<TeachingPublicationList> {
  const value = object(
    await requestJson("/api/v1/classroom-publications", { cache: "no-store" }),
    "publication list",
  );
  exactKeys(value, "publication list", ["items", "candidates"]);
  return {
    items: array(value.items, "publication list.items").map(parsePublication),
    candidates: array(
      value.candidates,
      "publication list.candidates",
    ).map(parsePublicationCandidate),
  };
}

export async function publishTeachingClassroom(
  assetId: string,
  idempotencyKey: string,
): Promise<TeachingPublishedVersion> {
  return parsePublishedVersion(
    await requestJson(
      `/api/v1/classrooms/${safeSegment(assetId, "asset ID")}/publish`,
      {
        method: "POST",
        ...jsonRequest(
          { scope: "tenant", classId: null },
          { "Idempotency-Key": safeHeader(idempotencyKey, "idempotency key") },
        ),
      },
    ),
  );
}

export async function listTeachingBatches(): Promise<TeachingBatch[]> {
  return parseList(
    await requestJson("/api/v1/classroom-batches", { cache: "no-store" }),
    "batch list",
    parseBatch,
  );
}

export async function getTeachingBatch(batchId: string): Promise<TeachingBatch> {
  return parseBatch(
    await requestJson(
      `/api/v1/classroom-batches/${safeSegment(batchId, "batch ID")}`,
      { cache: "no-store" },
    ),
  );
}

export async function confirmSelectedBatchOutlines(
  batchId: string,
  items: BatchOutlineConfirmation[],
): Promise<TeachingBatch> {
  if (items.length === 0) {
    throw new TeachingApiError("At least one reviewed outline is required");
  }
  return parseBatch(
    await requestJson(
      `/api/v1/classroom-batches/${safeSegment(batchId, "batch ID")}/confirm-outlines`,
      { method: "POST", ...jsonRequest({ items }) },
    ),
  );
}

export async function retryBatchItem(
  batchId: string,
  itemId: string,
): Promise<TeachingBatchRetry> {
  const value = object(
    await requestJson(
      `/api/v1/classroom-batches/${safeSegment(batchId, "batch ID")}/items/${safeSegment(itemId, "item ID")}/retry`,
      { method: "POST" },
    ),
    "batch retry",
  );
  exactKeys(value, "batch retry", ["parentItemId", "item"]);
  return {
    parentItemId: string(value.parentItemId, "batch retry.parentItemId"),
    item: parseBatchItem(value.item),
  };
}
