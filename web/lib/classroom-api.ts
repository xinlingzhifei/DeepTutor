import { apiFetch, apiUrl } from "@/lib/api";

export const CLASSROOM_EXPORT_FORMATS = [
  "classroom_zip",
  "pptx",
  "offline_html",
  "mp4",
] as const;

export type ClassroomExportFormat = (typeof CLASSROOM_EXPORT_FORMATS)[number];

export const MP4_EXPORT_DISABLED_REASON =
  "MP4_EXPORT_DISABLED_BY_TENANT_POLICY" as const;

export interface DraftClassroomMedia {
  mediaId: string;
  readUrl: string;
  mimeType: string;
  sizeBytes: number;
  sha256: string;
}

export type ClassroomExportStatus =
  | "created"
  | "quota_reserved"
  | "queued"
  | "exporting"
  | "validating"
  | "materializing"
  | "succeeded"
  | "failed"
  | "canceled";

export type ClassroomExportPhase = "export";

export interface ClassroomExportJob {
  jobId: string;
  phase: ClassroomExportPhase;
  status: ClassroomExportStatus;
  progressPercent: number;
  waitingReason: string | null;
  cancellable: boolean;
  retryable: boolean;
  errorCategory: string | null;
  errorCode: string | null;
  retryOfJobId: string | null;
  format: ClassroomExportFormat;
  downloadReady: boolean;
}

export interface ClassroomExportOption {
  format: ClassroomExportFormat;
  enabled: boolean;
  reason: typeof MP4_EXPORT_DISABLED_REASON | null;
}

export interface ClassroomExportPolicy {
  mp4Enabled: boolean;
}

export class ClassroomApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null = null,
  ) {
    super(message);
    this.name = "ClassroomApiError";
  }
}

const EXPORT_STATUS = new Set<string>([
  "created",
  "quota_reserved",
  "queued",
  "exporting",
  "validating",
  "materializing",
  "succeeded",
  "failed",
  "canceled",
]);
const EXPORT_FORMAT = new Set<string>(CLASSROOM_EXPORT_FORMATS);
const TERMINAL_EXPORT_STATUS = new Set<ClassroomExportStatus>([
  "succeeded",
  "failed",
  "canceled",
]);
const EXPORT_STATUS_RANK: Record<ClassroomExportStatus, number> = {
  created: 0,
  quota_reserved: 1,
  queued: 2,
  exporting: 3,
  validating: 4,
  materializing: 5,
  succeeded: 6,
  failed: 6,
  canceled: 6,
};
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const MIME_PATTERN = /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/i;

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ClassroomApiError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  allowed: ReadonlySet<string>,
  label: string,
): void {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new ClassroomApiError(`Unexpected ${label} field: ${key}`);
    }
  }
}

function requiredString(
  value: Record<string, unknown>,
  key: string,
  label: string,
): string {
  const item = value[key];
  if (typeof item !== "string" || item.length === 0) {
    throw new ClassroomApiError(`${label}.${key} must be a non-empty string`);
  }
  return item;
}

function nullableString(
  value: Record<string, unknown>,
  key: string,
  label: string,
): string | null {
  const item = value[key];
  if (item === null) return null;
  if (typeof item !== "string" || item.length === 0) {
    throw new ClassroomApiError(`${label}.${key} must be null or a non-empty string`);
  }
  return item;
}

function requiredBoolean(
  value: Record<string, unknown>,
  key: string,
  label: string,
): boolean {
  const item = value[key];
  if (typeof item !== "boolean") {
    throw new ClassroomApiError(`${label}.${key} must be a boolean`);
  }
  return item;
}

function safeRouteSegment(value: string, label: string): string {
  if (
    value.length === 0 ||
    value.trim() !== value ||
    value === "." ||
    value === ".." ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new ClassroomApiError(`${label} must be a non-empty safe identifier`);
  }
  return encodeURIComponent(value);
}

function safeHeaderValue(value: string, label: string): string {
  if (
    value.length === 0 ||
    value.trim() !== value ||
    value.length > 128 ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new ClassroomApiError(`${label} must be a safe non-empty value`);
  }
  return value;
}

function safeFilename(value: string): string {
  if (
    value.length === 0 ||
    value.trim() !== value ||
    value.length > 255 ||
    value === "." ||
    value === ".." ||
    /[\\/\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new ClassroomApiError("filename must be a safe basename");
  }
  return value;
}

async function responseError(response: Response): Promise<ClassroomApiError> {
  let detail = "";
  try {
    const payload = record(await response.json(), "error response");
    if (typeof payload.detail === "string") detail = `: ${payload.detail}`;
  } catch {
    // The stable status remains useful when the body is empty or non-JSON.
  }
  return new ClassroomApiError(
    `Classroom API request failed (${response.status})${detail}`,
    response.status,
  );
}

async function requestJson(
  path: string,
  init?: RequestInit,
): Promise<unknown> {
  const response = await apiFetch(apiUrl(path), init);
  if (!response.ok) throw await responseError(response);
  try {
    return await response.json();
  } catch {
    throw new ClassroomApiError("Classroom API returned invalid JSON", response.status);
  }
}

const MEDIA_RESPONSE_KEYS = new Set([
  "id",
  "read_url",
  "mime_type",
  "size_bytes",
  "sha256",
]);

function parseDraftMedia(
  input: unknown,
  encodedAssetId: string,
): DraftClassroomMedia {
  const value = record(input, "media response");
  exactKeys(value, MEDIA_RESPONSE_KEYS, "media response");
  const mediaId = requiredString(value, "id", "media response");
  const encodedMediaId = safeRouteSegment(mediaId, "media ID");
  const readUrl = requiredString(value, "read_url", "media response");
  const expectedReadUrl = `/api/v1/classrooms/${encodedAssetId}/draft-media/${encodedMediaId}`;
  if (readUrl !== expectedReadUrl) {
    throw new ClassroomApiError(
      "Media response must use the controlled yFeiSTAI media route",
    );
  }
  const mimeType = requiredString(value, "mime_type", "media response");
  if (!MIME_PATTERN.test(mimeType)) {
    throw new ClassroomApiError("media response.mime_type is invalid");
  }
  const sizeBytes = value.size_bytes;
  if (!Number.isSafeInteger(sizeBytes) || (sizeBytes as number) < 0) {
    throw new ClassroomApiError("media response.size_bytes must be a non-negative integer");
  }
  const sha256 = requiredString(value, "sha256", "media response");
  if (!SHA256_PATTERN.test(sha256)) {
    throw new ClassroomApiError("media response.sha256 must be a lowercase SHA-256 hash");
  }
  return {
    mediaId,
    readUrl,
    mimeType,
    sizeBytes: sizeBytes as number,
    sha256,
  };
}

export async function uploadDraftClassroomMedia(
  assetId: string,
  blob: Blob,
  filename: string,
  signal?: AbortSignal,
): Promise<DraftClassroomMedia> {
  if (blob.size === 0) {
    throw new ClassroomApiError("media file must not be empty");
  }
  const encodedAssetId = safeRouteSegment(assetId, "asset ID");
  const form = new FormData();
  form.append("file", blob, safeFilename(filename));
  const payload = await requestJson(
    `/api/v1/classrooms/${encodedAssetId}/draft-media`,
    { method: "POST", body: form, signal },
  );
  return parseDraftMedia(payload, encodedAssetId);
}

const EXPORT_RESPONSE_KEYS = new Set([
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
]);

function parseExportJob(
  input: unknown,
  expectedJobId?: string,
  expectedFormat?: ClassroomExportFormat,
): ClassroomExportJob {
  const value = record(input, "export response");
  exactKeys(value, EXPORT_RESPONSE_KEYS, "export response");
  const jobId = requiredString(value, "job_id", "export response");
  if (expectedJobId !== undefined && jobId !== expectedJobId) {
    throw new ClassroomApiError("Export response job ID does not match the request");
  }
  if (value.job_kind !== "export") {
    throw new ClassroomApiError("Export response has the wrong job kind");
  }
  const phase = requiredString(value, "phase", "export response");
  if (phase !== "export") {
    throw new ClassroomApiError("Export response phase is invalid");
  }
  const status = requiredString(value, "status", "export response");
  if (!EXPORT_STATUS.has(status)) {
    throw new ClassroomApiError("Export response status is invalid");
  }
  const progressPercent = value.progress_percent;
  if (
    !Number.isInteger(progressPercent) ||
    (progressPercent as number) < 0 ||
    (progressPercent as number) > 100
  ) {
    throw new ClassroomApiError("Export response progress is invalid");
  }
  if (value.outline !== null) {
    throw new ClassroomApiError("Export response must not include an outline");
  }
  const format = requiredString(value, "export_format", "export response");
  if (!EXPORT_FORMAT.has(format)) {
    throw new ClassroomApiError("Export response format is invalid");
  }
  if (expectedFormat !== undefined && format !== expectedFormat) {
    throw new ClassroomApiError("Export response format does not match the request");
  }
  const typedStatus = status as ClassroomExportStatus;
  const terminal = TERMINAL_EXPORT_STATUS.has(typedStatus);
  const waitingReason = nullableString(value, "waiting_reason", "export response");
  const cancellable = requiredBoolean(value, "cancellable", "export response");
  const retryable = requiredBoolean(value, "retryable", "export response");
  const errorCategory = nullableString(value, "error_category", "export response");
  const errorCode = nullableString(value, "error_code", "export response");
  const downloadReady = requiredBoolean(value, "download_ready", "export response");
  if (cancellable !== !terminal) {
    throw new ClassroomApiError("Export response terminal state is inconsistent");
  }
  if (retryable !== (status === "failed" || status === "canceled")) {
    throw new ClassroomApiError("Export response retry state is inconsistent");
  }
  if (downloadReady !== (status === "succeeded")) {
    throw new ClassroomApiError("Export download readiness does not match job status");
  }
  if (terminal && waitingReason !== null) {
    throw new ClassroomApiError("A terminal export must not report a waiting reason");
  }
  if (status === "succeeded") {
    if (progressPercent !== 100) {
      throw new ClassroomApiError("A succeeded export must report 100 percent progress");
    }
    if (waitingReason !== null || errorCategory !== null || errorCode !== null) {
      throw new ClassroomApiError("A succeeded export must not report waiting or error state");
    }
  } else if (status === "failed" || status === "canceled") {
    if (errorCategory === null || errorCode === null) {
      throw new ClassroomApiError(
        "A failed or canceled export must report a stable error",
      );
    }
  } else if (errorCategory !== null || errorCode !== null) {
    throw new ClassroomApiError("A running export must not report stale error state");
  }
  return {
    jobId,
    phase,
    status: typedStatus,
    progressPercent: progressPercent as number,
    waitingReason,
    cancellable,
    retryable,
    errorCategory,
    errorCode,
    retryOfJobId: nullableString(value, "retry_of_job_id", "export response"),
    format: format as ClassroomExportFormat,
    downloadReady,
  };
}

function exportHeaders(
  idempotencyKey: string,
  revision?: string,
): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "Idempotency-Key": safeHeaderValue(idempotencyKey, "idempotency key"),
  };
  if (revision !== undefined) {
    const safeRevision = safeHeaderValue(revision, "draft revision");
    if (!/^"[\x21\x23-\x7e]+"$/.test(safeRevision)) {
      throw new ClassroomApiError("draft revision must be a canonical strong ETag");
    }
    headers["If-Match"] = safeRevision;
  }
  return headers;
}

function exportBody(format: ClassroomExportFormat): string {
  if (!EXPORT_FORMAT.has(format)) {
    throw new ClassroomApiError("Unsupported classroom export format");
  }
  return JSON.stringify({ format });
}

export async function createDraftClassroomExport(
  assetId: string,
  format: ClassroomExportFormat,
  options: {
    revision: string;
    idempotencyKey: string;
    signal?: AbortSignal;
  },
): Promise<ClassroomExportJob> {
  const payload = await requestJson(
    `/api/v1/classrooms/${safeRouteSegment(assetId, "asset ID")}/draft/exports`,
    {
      method: "POST",
      headers: exportHeaders(options.idempotencyKey, options.revision),
      body: exportBody(format),
      signal: options.signal,
    },
  );
  return parseExportJob(payload, undefined, format);
}

export async function createVersionClassroomExport(
  versionId: string,
  format: ClassroomExportFormat,
  options: { idempotencyKey: string; signal?: AbortSignal },
): Promise<ClassroomExportJob> {
  const payload = await requestJson(
    `/api/v1/classroom-versions/${safeRouteSegment(versionId, "version ID")}/exports`,
    {
      method: "POST",
      headers: exportHeaders(options.idempotencyKey),
      body: exportBody(format),
      signal: options.signal,
    },
  );
  return parseExportJob(payload, undefined, format);
}

async function fetchClassroomExportPayload(
  exportId: string,
  signal?: AbortSignal,
): Promise<unknown> {
  return requestJson(
    `/api/v1/classroom-exports/${safeRouteSegment(exportId, "export ID")}`,
    { cache: "no-store", signal },
  );
}

export async function getClassroomExport(
  exportId: string,
  expectedFormat: ClassroomExportFormat,
  signal?: AbortSignal,
): Promise<ClassroomExportJob> {
  const payload = await fetchClassroomExportPayload(exportId, signal);
  return parseExportJob(payload, exportId, expectedFormat);
}

export function classroomExportDownloadUrl(exportId: string): string {
  return `/api/v1/classroom-exports/${safeRouteSegment(exportId, "export ID")}/download`;
}

export function listClassroomExportOptions(
  policy: ClassroomExportPolicy,
): ClassroomExportOption[] {
  return CLASSROOM_EXPORT_FORMATS.map(format => ({
    format,
    enabled: format !== "mp4" || policy.mp4Enabled,
    reason:
      format === "mp4" && !policy.mp4Enabled
        ? MP4_EXPORT_DISABLED_REASON
        : null,
  }));
}

export interface ClassroomExportAttemptRegistry {
  keyFor(targetKey: string, format: ClassroomExportFormat): string;
  settle(targetKey: string, format: ClassroomExportFormat): void;
}

export function createClassroomExportAttemptRegistry(
  createKey: () => string = () => `classroom-export-${crypto.randomUUID()}`,
): ClassroomExportAttemptRegistry {
  const attempts = new Map<string, string>();
  const maxUnresolvedAttempts = 64;
  const attemptKey = (targetKey: string, format: ClassroomExportFormat) =>
    `${targetKey.length}:${targetKey}:${format}`;
  return {
    keyFor(targetKey, format) {
      const key = attemptKey(targetKey, format);
      const existing = attempts.get(key);
      if (existing !== undefined) return existing;
      if (attempts.size >= maxUnresolvedAttempts) {
        throw new ClassroomApiError("Too many unresolved classroom export attempts");
      }
      const created = createKey();
      attempts.set(key, created);
      return created;
    },
    settle(targetKey, format) {
      attempts.delete(attemptKey(targetKey, format));
    },
  };
}

export function shouldRetainClassroomExportAttempt(error: unknown): boolean {
  if (!(error instanceof ClassroomApiError)) return true;
  return (
    error.status === null ||
    (error.status >= 200 && error.status < 300) ||
    error.status === 408 ||
    error.status === 425 ||
    error.status >= 500
  );
}

function exportStatusCanFollow(
  previous: ClassroomExportStatus,
  next: ClassroomExportJob,
): boolean {
  if (
    next.status === "queued" &&
    next.waitingReason === "retry_backoff" &&
    !TERMINAL_EXPORT_STATUS.has(previous)
  ) {
    return true;
  }
  return EXPORT_STATUS_RANK[next.status] >= EXPORT_STATUS_RANK[previous];
}

function throwIfAborted(signal?: AbortSignal): void {
  signal?.throwIfAborted();
}

function wait(intervalMs: number, signal?: AbortSignal): Promise<void> {
  if (intervalMs <= 0) {
    return Promise.resolve().then(() => throwIfAborted(signal));
  }
  return new Promise((resolve, reject) => {
    throwIfAborted(signal);
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, intervalMs);
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export async function pollClassroomExport(
  exportId: string,
  options: {
    expectedFormat: ClassroomExportFormat;
    initialProgressPercent?: number;
    initialStatus?: ClassroomExportStatus;
    intervalMs?: number;
    signal?: AbortSignal;
    onUpdate?: (job: ClassroomExportJob) => void;
    fetchStatus?: (exportId: string, signal?: AbortSignal) => Promise<unknown>;
  },
): Promise<ClassroomExportJob> {
  const intervalMs = options.intervalMs ?? 1_000;
  if (!Number.isFinite(intervalMs) || intervalMs < 0) {
    throw new ClassroomApiError("poll interval must be a non-negative number");
  }
  safeRouteSegment(exportId, "export ID");
  if (!EXPORT_FORMAT.has(options.expectedFormat)) {
    throw new ClassroomApiError("Unsupported classroom export format");
  }
  let previousProgress = options.initialProgressPercent ?? -1;
  let previousStatus = options.initialStatus;
  if (
    !Number.isInteger(previousProgress) ||
    previousProgress < -1 ||
    previousProgress > 100
  ) {
    throw new ClassroomApiError("initial export progress is invalid");
  }
  const fetchStatus = options.fetchStatus ?? fetchClassroomExportPayload;
  for (;;) {
    throwIfAborted(options.signal);
    const job = parseExportJob(
      await fetchStatus(exportId, options.signal),
      exportId,
      options.expectedFormat,
    );
    if (job.progressPercent < previousProgress) {
      throw new ClassroomApiError("Export response progress regressed");
    }
    if (previousStatus !== undefined && !exportStatusCanFollow(previousStatus, job)) {
      throw new ClassroomApiError("Export response status regressed");
    }
    previousProgress = job.progressPercent;
    previousStatus = job.status;
    throwIfAborted(options.signal);
    options.onUpdate?.(job);
    if (TERMINAL_EXPORT_STATUS.has(job.status)) return job;
    await wait(intervalMs, options.signal);
  }
}
