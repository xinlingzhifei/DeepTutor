import { createHash, createHmac } from "node:crypto";

export const LIVE_FIXTURE_POLICY_VERSION = 2 as const;

export type LiveEvidence =
  | "teacher_flow"
  | "student_micro_flow"
  | "student_full_flow"
  | "content_operations_flow"
  | "tailwind4_visual_matrix";

export type LiveRole =
  | "teacher"
  | "student"
  | "author"
  | "reviewer"
  | "publisher";

type TenantRole =
  | "teacher"
  | "student"
  | "content_author"
  | "content_reviewer";

export type LiveApiFile = {
  name: string;
  mimeType: string;
  buffer: Buffer;
};

export type LiveApiRequestOptions = {
  headers?: Record<string, string>;
  data?: unknown;
  multipart?: Record<string, string | number | boolean | LiveApiFile>;
};

export interface LiveApiResponse {
  status(): number;
  json(): Promise<unknown>;
}

export interface LiveApiRequestContext {
  get(url: string, options?: LiveApiRequestOptions): Promise<LiveApiResponse>;
  post(url: string, options?: LiveApiRequestOptions): Promise<LiveApiResponse>;
  put(url: string, options?: LiveApiRequestOptions): Promise<LiveApiResponse>;
}

export interface LivePage {
  goto(url: string): Promise<unknown>;
  locator(selector: string): {
    fill(value: string): Promise<unknown>;
    click(): Promise<unknown>;
  };
  waitForURL(url: string): Promise<unknown>;
}

export type LiveStudentClassroomMode = "micro" | "full";

export interface LiveStudentClassroomPollState {
  assetId: string;
  generationJobId: string;
  status: string;
  courseId: string;
  classId: string;
  mode: LiveStudentClassroomMode;
  ownerId: string;
  classroomVersionId: string | null;
}

export interface LiveStudentGenerationJobPollState {
  jobId: string;
  jobKind: string;
  phase: "outline" | "content";
  status: string;
  progressPercent: number;
}

export interface PollLiveStudentClassroomOptions {
  expected: {
    assetId: string;
    generationJobId: string;
    courseId: string;
    classId: string;
    mode: LiveStudentClassroomMode;
    ownerId: string;
  };
  pollAttempts: number;
  pollIntervalMs: number;
  pause: (milliseconds: number) => Promise<void>;
  readClassroom: () => Promise<LiveStudentClassroomPollState>;
  readGenerationJob: (
    jobId: string,
  ) => Promise<LiveStudentGenerationJobPollState>;
  onAwaitingConfirmation?: (
    classroom: LiveStudentClassroomPollState,
  ) => Promise<void>;
}

export interface PollLiveStudentClassroomResult {
  classroom: LiveStudentClassroomPollState & { classroomVersionId: string };
  generationJob: LiveStudentGenerationJobPollState;
}

const STUDENT_CLASSROOM_FAILURE_STATUSES = new Set([
  "failed",
  "canceled",
  "rejected",
  "expired",
]);

function requiredStudentPollValue(value: string): string {
  const normalized = value.trim();
  if (!normalized) {
    throw new Error("live student classroom binding is invalid");
  }
  return normalized;
}

export async function pollLiveStudentClassroom(
  options: PollLiveStudentClassroomOptions,
): Promise<PollLiveStudentClassroomResult> {
  const expected = {
    assetId: requiredStudentPollValue(options.expected.assetId),
    generationJobId: requiredStudentPollValue(
      options.expected.generationJobId,
    ),
    courseId: requiredStudentPollValue(options.expected.courseId),
    classId: requiredStudentPollValue(options.expected.classId),
    mode: options.expected.mode,
    ownerId: requiredStudentPollValue(options.expected.ownerId),
  };
  if (!(["micro", "full"] as const).includes(expected.mode)) {
    throw new Error("live student classroom binding is invalid");
  }
  if (
    !Number.isInteger(options.pollAttempts) ||
    options.pollAttempts < 1 ||
    options.pollAttempts > 300 ||
    !Number.isInteger(options.pollIntervalMs) ||
    options.pollIntervalMs < 0 ||
    options.pollIntervalMs > 30_000
  ) {
    throw new Error("live student classroom poll bound is invalid");
  }

  let awaitingConfirmationObserved = false;
  let confirmationHandled = false;

  for (let attempt = 0; attempt < options.pollAttempts; attempt += 1) {
    let classroom: LiveStudentClassroomPollState;
    try {
      classroom = await options.readClassroom();
    } catch {
      throw new Error("live student classroom synchronization failed");
    }

    if (
      classroom.assetId !== expected.assetId ||
      classroom.courseId !== expected.courseId ||
      classroom.classId !== expected.classId ||
      classroom.mode !== expected.mode ||
      classroom.ownerId !== expected.ownerId
    ) {
      throw new Error("live student classroom binding is invalid");
    }
    if (STUDENT_CLASSROOM_FAILURE_STATUSES.has(classroom.status)) {
      throw new Error("live student classroom failed");
    }
    if (classroom.generationJobId !== expected.generationJobId) {
      throw new Error("live student classroom job binding is invalid");
    }

    if (expected.mode === "full") {
      if (classroom.status === "awaiting_confirmation") {
        if (classroom.classroomVersionId !== null) {
          throw new Error("live student classroom version is invalid");
        }
        awaitingConfirmationObserved = true;
      } else {
        if (classroom.status === "succeeded" && !awaitingConfirmationObserved) {
          throw new Error(
            "live student classroom awaiting_confirmation was not observed",
          );
        }
      }
    }

    let generationJob: LiveStudentGenerationJobPollState;
    try {
      generationJob = await options.readGenerationJob(expected.generationJobId);
    } catch {
      throw new Error("live student classroom generation synchronization failed");
    }
    if (generationJob.jobId !== expected.generationJobId) {
      throw new Error("live student classroom job binding is invalid");
    }
    if (generationJob.jobKind !== "generation") {
      throw new Error("live student classroom generation job is invalid");
    }
    if (
      !Number.isInteger(generationJob.progressPercent) ||
      generationJob.progressPercent < 0 ||
      generationJob.progressPercent > 100
    ) {
      throw new Error("live student classroom generation job is invalid");
    }
    if (STUDENT_CLASSROOM_FAILURE_STATUSES.has(generationJob.status)) {
      throw new Error("live student classroom generation failed");
    }
    const expectedPhase =
      expected.mode === "micro" || confirmationHandled ? "content" : "outline";
    if (generationJob.phase !== expectedPhase) {
      throw new Error("live student classroom generation job phase is invalid");
    }

    if (
      expected.mode === "full" &&
      classroom.status === "awaiting_confirmation" &&
      !confirmationHandled
    ) {
      if (!options.onAwaitingConfirmation) {
        throw new Error(
          "live student classroom awaiting_confirmation handler is required",
        );
      }
      try {
        const result = await options.onAwaitingConfirmation(classroom);
        if (result !== undefined) {
          throw new Error("invalid confirmation result");
        }
      } catch {
        throw new Error("live student classroom confirmation failed");
      }
      confirmationHandled = true;
    }

    if (classroom.status === "succeeded") {
      const versionId = classroom.classroomVersionId?.trim();
      if (!versionId) {
        throw new Error("live student classroom version is invalid");
      }
      if (
        generationJob.status === "succeeded" &&
        generationJob.progressPercent === 100
      ) {
        return {
          classroom: { ...classroom, classroomVersionId: versionId },
          generationJob,
        };
      }
    }

    if (attempt + 1 < options.pollAttempts) {
      try {
        await options.pause(options.pollIntervalMs);
      } catch {
        throw new Error("live student classroom synchronization failed");
      }
    }
  }

  throw new Error("live student classroom completion timed out");
}

export interface LiveTenantRecord {
  tenantId: string;
  name: string;
  status: "active";
  jobId: string;
}

export interface LiveGrantRecord {
  tenantId: string;
  userId: string;
  role: TenantRole;
  scopeType: "tenant";
  scopeId: string;
}

export interface LiveCourseRecord {
  tenantId: string;
  id: string;
  title: string;
  status: "active";
}

export interface LiveCourseGenerationPolicyRecord {
  tenantId: string;
  courseId: string;
  allowStudentMicro: boolean;
  allowStudentFull: boolean;
  allowedContentModes: readonly ["open_creation"];
  allowWebSearch: boolean;
  requireApprovalForRestrictedTopics: boolean;
  minorSafetyMode: boolean;
  microSceneLimit: number;
  fullSceneLimit: number;
  dailyStudentUnits: number;
  monthlyStudentUnits: number;
  updatedBy: string;
  updatedAt: string;
}

export interface LiveClassRecord {
  tenantId: string;
  id: string;
  courseId: string;
  name: string;
  status: "active";
}

export interface LiveSourceRecord {
  tenantId: string;
  bindingId: string;
  sourceType: "pdf";
  sourceId: string;
  filename: string;
  sha256: string;
  sizeBytes: number;
  courseId: string;
  classId: string;
}

export interface LiveEnrollmentRecord {
  tenantId: string;
  classId: string;
  userId: string;
  status: "active";
}

export interface LiveFixtureRecords {
  identities: LiveIdentity[];
  tenants: LiveTenantRecord[];
  grants: LiveGrantRecord[];
  courses: LiveCourseRecord[];
  classes: LiveClassRecord[];
  sources: LiveSourceRecord[];
  enrollments: LiveEnrollmentRecord[];
}

export type LiveFixtureContextOptions = {
  request: LiveApiRequestContext;
  adminToken: string;
  releaseRun: string;
  environment: string;
  evidence: LiveEvidence;
  provisioningPollAttempts?: number;
  provisioningPollIntervalMs?: number;
  pause?: (milliseconds: number) => Promise<void>;
};

type ContextPrivate = {
  request: LiveApiRequestContext;
  adminToken: string;
  pause: (milliseconds: number) => Promise<void>;
};

const contextPrivate = new WeakMap<LiveFixtureContext, ContextPrivate>();
const identityPasswords = new WeakMap<LiveIdentity, string>();

const EVIDENCE_ALIASES: Record<LiveEvidence, string> = {
  teacher_flow: "teacher",
  student_micro_flow: "micro",
  student_full_flow: "full",
  content_operations_flow: "content",
  tailwind4_visual_matrix: "visual",
};

const ROLE_ALIASES: Record<LiveRole, string> = {
  teacher: "teacher",
  student: "student",
  author: "author",
  reviewer: "reviewer",
  publisher: "publish",
};

const TENANT_ROLES: Record<LiveRole, TenantRole> = {
  teacher: "teacher",
  student: "student",
  author: "content_author",
  reviewer: "content_reviewer",
  publisher: "teacher",
};

const LIVE_EVIDENCE = new Set<LiveEvidence>(
  Object.keys(EVIDENCE_ALIASES) as LiveEvidence[],
);

function defaultPause(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function requiredPublicValue(value: string, label: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`live fixture ${label} is required`);
  return normalized;
}

export class LiveFixtureContext {
  readonly policyVersion = LIVE_FIXTURE_POLICY_VERSION;
  readonly releaseRun: string;
  readonly environment: string;
  readonly evidence: LiveEvidence;
  readonly provisioningPollAttempts: number;
  readonly provisioningPollIntervalMs: number;
  readonly records: LiveFixtureRecords = {
    identities: [],
    tenants: [],
    grants: [],
    courses: [],
    classes: [],
    sources: [],
    enrollments: [],
  };

  constructor(options: LiveFixtureContextOptions) {
    const adminToken = options.adminToken.trim();
    if (!adminToken) throw new Error("live fixture admin token is required");
    if (!LIVE_EVIDENCE.has(options.evidence)) {
      throw new Error("live fixture evidence is unsupported");
    }
    const pollAttempts = options.provisioningPollAttempts ?? 60;
    const pollInterval = options.provisioningPollIntervalMs ?? 1_000;
    if (!Number.isInteger(pollAttempts) || pollAttempts < 1 || pollAttempts > 300) {
      throw new Error("live fixture provisioning poll bound is invalid");
    }
    if (!Number.isInteger(pollInterval) || pollInterval < 0 || pollInterval > 30_000) {
      throw new Error("live fixture provisioning poll interval is invalid");
    }
    this.releaseRun = requiredPublicValue(options.releaseRun, "release run");
    this.environment = requiredPublicValue(options.environment, "environment");
    this.evidence = options.evidence;
    this.provisioningPollAttempts = pollAttempts;
    this.provisioningPollIntervalMs = pollInterval;
    contextPrivate.set(this, {
      request: options.request,
      adminToken,
      pause: options.pause ?? defaultPause,
    });
  }
}

export class LiveIdentity {
  readonly policyVersion = LIVE_FIXTURE_POLICY_VERSION;

  constructor(
    readonly evidence: LiveEvidence,
    readonly role: LiveRole,
    readonly tenantRole: TenantRole,
    readonly username: string,
    readonly suffix: string,
    readonly userId: string | null,
    password: string,
  ) {
    identityPasswords.set(this, password);
  }
}

function privateContext(context: LiveFixtureContext): ContextPrivate {
  const value = contextPrivate.get(context);
  if (!value) throw new Error("live fixture context is invalid");
  return value;
}

function identityPassword(identity: LiveIdentity): string {
  const value = identityPasswords.get(identity);
  if (!value) throw new Error("live fixture identity is invalid");
  return value;
}

function hmacDigest(context: LiveFixtureContext, role: LiveRole | "fixture"): Buffer {
  const secret = privateContext(context);
  const material = [
    `policyVersion=${LIVE_FIXTURE_POLICY_VERSION}`,
    `releaseRun=${context.releaseRun}`,
    `environment=${context.environment}`,
    `evidence=${context.evidence}`,
    `role=${role}`,
  ].join("\0");
  return createHmac("sha256", secret.adminToken).update(material).digest();
}

function safeSlug(value: string, maxLength: number): string {
  const normalized = value
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, maxLength)
    .replace(/-+$/g, "");
  return normalized || "run";
}

export function deriveLiveIdentity(
  context: LiveFixtureContext,
  role: LiveRole,
): LiveIdentity {
  if (!(role in TENANT_ROLES)) throw new Error("live fixture role is unsupported");
  const digest = hmacDigest(context, role);
  const suffix = digest.toString("hex").slice(0, 12);
  const runSlug = safeSlug(context.releaseRun, 8);
  let username = [
    "yflive",
    runSlug,
    EVIDENCE_ALIASES[context.evidence],
    ROLE_ALIASES[role],
    suffix,
  ].join("-");
  username = `${username}@example.invalid`;
  const adminToken = privateContext(context).adminToken;
  if (username.toLowerCase().includes(adminToken.toLowerCase())) {
    username = [
      "yflive",
      digest.toString("hex").slice(12, 20),
      EVIDENCE_ALIASES[context.evidence],
      ROLE_ALIASES[role],
      suffix,
    ].join("-");
    username = `${username}@example.invalid`;
  }
  const password = `Yf!2-${digest.toString("base64url")}`;
  return new LiveIdentity(
    context.evidence,
    role,
    TENANT_ROLES[role],
    username,
    suffix,
    null,
    password,
  );
}

function provisionedIdentity(identity: LiveIdentity, userId: string): LiveIdentity {
  return new LiveIdentity(
    identity.evidence,
    identity.role,
    identity.tenantRole,
    identity.username,
    identity.suffix,
    userId,
    identityPassword(identity),
  );
}

function adminHeaders(context: LiveFixtureContext): Record<string, string> {
  return { Authorization: `Bearer ${privateContext(context).adminToken}` };
}

async function guardedRequest(
  label: string,
  operation: () => Promise<LiveApiResponse>,
): Promise<LiveApiResponse> {
  try {
    return await operation();
  } catch {
    throw new Error(`live fixture ${label} request failed`);
  }
}

async function responseRecord(
  response: LiveApiResponse,
  label: string,
): Promise<Record<string, unknown>> {
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new Error(`live fixture ${label} response is malformed`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`live fixture ${label} response is malformed`);
  }
  return value as Record<string, unknown>;
}

function requiredString(
  value: Record<string, unknown>,
  key: string,
  label: string,
): string {
  const candidate = value[key];
  if (typeof candidate !== "string" || !candidate.trim()) {
    throw new Error(`live fixture ${label} response is malformed`);
  }
  return candidate;
}

function requiredArray(
  value: Record<string, unknown>,
  key: string,
  label: string,
): unknown[] {
  const candidate = value[key];
  if (!Array.isArray(candidate)) {
    throw new Error(`live fixture ${label} response is malformed`);
  }
  return candidate;
}

function objectItem(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`live fixture ${label} response is malformed`);
  }
  return value as Record<string, unknown>;
}

function retainByKey<T>(items: T[], key: (item: T) => string, record: T): void {
  const index = items.findIndex((item) => key(item) === key(record));
  if (index === -1) items.push(record);
  else items[index] = record;
}

function verifyUser(
  payload: Record<string, unknown>,
  identity: LiveIdentity,
): string {
  const userId = requiredString(payload, "user_id", "user");
  if (
    payload.ok !== true ||
    payload.username !== identity.username ||
    payload.role !== "user" ||
    payload.is_admin !== false
  ) {
    throw new Error("live fixture user identity mismatch");
  }
  return userId;
}

export async function ensureLiveUser(
  context: LiveFixtureContext,
  role: LiveRole,
): Promise<LiveIdentity> {
  const identity = deriveLiveIdentity(context, role);
  const secret = privateContext(context);
  const password = identityPassword(identity);
  const created = await guardedRequest("user creation", () =>
    secret.request.post("/api/v1/auth/users", {
      headers: adminHeaders(context),
      data: { username: identity.username, password },
    }),
  );
  let payload: Record<string, unknown>;
  if (created.status() === 201) {
    payload = await responseRecord(created, "user creation");
  } else if (created.status() === 409) {
    const login = await guardedRequest("user ownership", () =>
      secret.request.post("/api/v1/auth/login", {
        data: { username: identity.username, password },
      }),
    );
    if (login.status() !== 200) {
      throw new Error("live fixture user ownership could not be proven");
    }
    payload = await responseRecord(login, "user ownership");
  } else {
    throw new Error("live fixture user creation failed");
  }
  const result = provisionedIdentity(identity, verifyUser(payload, identity));
  retainByKey(context.records.identities, (item) => item.username, result);
  return result;
}

function fixtureHex(context: LiveFixtureContext): string {
  return hmacDigest(context, "fixture").toString("hex");
}

function tenantName(context: LiveFixtureContext): string {
  return `yFeiSTAI live ${EVIDENCE_ALIASES[context.evidence]} ${fixtureHex(context).slice(0, 12)}`;
}

function tenantIdempotencyKey(context: LiveFixtureContext): string {
  return `yfeistai-live-v2-${fixtureHex(context).slice(0, 32)}`;
}

type ProvisioningPayload = {
  tenantId: string;
  jobId: string;
  status: string;
  jobStatus?: string;
};

function provisioningPayload(
  value: Record<string, unknown>,
  label: string,
): ProvisioningPayload {
  return {
    tenantId: requiredString(value, "tenant_id", label),
    jobId: requiredString(value, "job_id", label),
    status: requiredString(value, "status", label),
    jobStatus:
      typeof value.job_status === "string" && value.job_status
        ? value.job_status
        : undefined,
  };
}

export async function ensureLiveTenant(
  context: LiveFixtureContext,
): Promise<LiveTenantRecord> {
  const secret = privateContext(context);
  const name = tenantName(context);
  const created = await guardedRequest("tenant creation", () =>
    secret.request.post("/api/v1/tenants", {
      headers: {
        ...adminHeaders(context),
        "Idempotency-Key": tenantIdempotencyKey(context),
      },
      data: { name },
    }),
  );
  if (created.status() !== 202) {
    throw new Error("live fixture tenant creation failed");
  }
  let state = provisioningPayload(
    await responseRecord(created, "tenant creation"),
    "tenant creation",
  );
  let active = state.status === "active";
  for (let attempt = 0; !active && attempt < context.provisioningPollAttempts; attempt += 1) {
    const polled = await guardedRequest("tenant provisioning", () =>
      secret.request.get(
        `/api/v1/tenants/${encodeURIComponent(state.tenantId)}/provisioning`,
        { headers: adminHeaders(context) },
      ),
    );
    if (polled.status() !== 200) {
      throw new Error("live fixture tenant provisioning failed");
    }
    const next = provisioningPayload(
      await responseRecord(polled, "tenant provisioning"),
      "tenant provisioning",
    );
    if (next.tenantId !== state.tenantId || next.jobId !== state.jobId) {
      throw new Error("live fixture tenant provisioning identity mismatch");
    }
    state = next;
    if (state.status === "failed" || state.jobStatus === "failed") {
      throw new Error("live fixture tenant provisioning failed");
    }
    active = state.status === "active" && state.jobStatus === "completed";
    if (!active && attempt + 1 < context.provisioningPollAttempts) {
      try {
        await secret.pause(context.provisioningPollIntervalMs);
      } catch {
        throw new Error("live fixture tenant provisioning wait failed");
      }
    }
  }
  if (!active) throw new Error("live fixture tenant did not become active");

  const switched = await guardedRequest("tenant switch", () =>
    secret.request.put("/api/v1/tenants/active", {
      headers: adminHeaders(context),
      data: { tenant_id: state.tenantId },
    }),
  );
  if (switched.status() !== 200) throw new Error("live fixture tenant switch failed");
  const switchedPayload = await responseRecord(switched, "tenant switch");
  if (switchedPayload.active_tenant_id !== state.tenantId) {
    throw new Error("live fixture tenant switch identity mismatch");
  }
  const tenant: LiveTenantRecord = {
    tenantId: state.tenantId,
    name,
    status: "active",
    jobId: state.jobId,
  };
  retainByKey(context.records.tenants, (item) => item.tenantId, tenant);
  return tenant;
}

function verifyGrant(
  payload: Record<string, unknown>,
  expected: LiveGrantRecord,
): void {
  const roles = requiredArray(payload, "roles", "tenant membership");
  const grants = requiredArray(payload, "grants", "tenant membership");
  if (
    payload.tenant_id !== expected.tenantId ||
    payload.user_id !== expected.userId ||
    roles.length !== 1 ||
    roles[0] !== expected.role ||
    grants.length !== 1
  ) {
    throw new Error("live fixture tenant membership mismatch");
  }
  const grant = objectItem(grants[0], "tenant membership");
  if (
    grant.role !== expected.role ||
    grant.scope_type !== expected.scopeType ||
    grant.scope_id !== expected.scopeId
  ) {
    throw new Error("live fixture tenant membership mismatch");
  }
}

export async function ensureLiveTenantMembership(
  context: LiveFixtureContext,
  tenant: LiveTenantRecord,
  identity: LiveIdentity,
): Promise<LiveGrantRecord> {
  if (!identity.userId) throw new Error("live fixture user is not provisioned");
  const grant: LiveGrantRecord = {
    tenantId: tenant.tenantId,
    userId: identity.userId,
    role: identity.tenantRole,
    scopeType: "tenant",
    scopeId: tenant.tenantId,
  };
  const secret = privateContext(context);
  const response = await guardedRequest("tenant membership", () =>
    secret.request.post(
      `/api/v1/tenants/${encodeURIComponent(tenant.tenantId)}/members`,
      {
        headers: adminHeaders(context),
        data: {
          user_id: identity.userId,
          grants: [
            {
              role: grant.role,
              scope_type: grant.scopeType,
              scope_id: grant.scopeId,
            },
          ],
        },
      },
    ),
  );
  if (response.status() !== 200) {
    throw new Error("live fixture tenant membership failed");
  }
  verifyGrant(await responseRecord(response, "tenant membership"), grant);
  retainByKey(
    context.records.grants,
    (item) => `${item.tenantId}\0${item.userId}\0${item.scopeType}\0${item.scopeId}`,
    grant,
  );
  return grant;
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function digestId(prefix: string, ...values: string[]): string {
  return `${prefix}-${sha256(values.join("\0"))}`;
}

function controlledPdf(marker: string): Buffer {
  const content = `BT\n/F1 12 Tf\n72 720 Td\n(yFeiSTAI live ${marker}) Tj\nET\n`;
  const objects = [
    "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
    "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
    "3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n",
    "4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    `5 0 obj\n<< /Length ${Buffer.byteLength(content, "ascii")} >>\nstream\n${content}endstream\nendobj\n`,
  ].map((value) => Buffer.from(value, "ascii"));
  const header = Buffer.from("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", "latin1");
  const offsets: number[] = [];
  let offset = header.length;
  for (const object of objects) {
    offsets.push(offset);
    offset += object.length;
  }
  const xrefOffset = offset;
  const xref = [
    "xref\n0 6\n",
    "0000000000 65535 f \n",
    ...offsets.map((value) => `${String(value).padStart(10, "0")} 00000 n \n`),
    "trailer\n<< /Size 6 /Root 1 0 R >>\n",
    `startxref\n${xrefOffset}\n%%EOF\n`,
  ].join("");
  return Buffer.concat([header, ...objects, Buffer.from(xref, "ascii")]);
}

function deterministicCatalog(context: LiveFixtureContext, tenantId: string) {
  const digest = fixtureHex(context);
  const courseId = `live-course-${digest.slice(0, 20)}`;
  const classId = `live-class-${digest.slice(20, 40)}`;
  const title = `Plan07 live ${EVIDENCE_ALIASES[context.evidence]} ${digest.slice(0, 8)}`;
  const name = `Plan07 live class ${digest.slice(8, 16)}`;
  const filename = `live-source-${digest.slice(0, 12)}.pdf`;
  const pdf = controlledPdf(digest.slice(0, 24));
  const contentSha256 = sha256(pdf);
  const uploadId = digestId("upload", tenantId, contentSha256);
  const snapshotId = digestId(
    "pdf-source",
    tenantId,
    uploadId,
    courseId,
    classId,
    filename,
  );
  const bindingId = `source-binding-${sha256(
    [tenantId, snapshotId, courseId, classId].join("\0"),
  )}`;
  return {
    courseId,
    classId,
    title,
    name,
    filename,
    pdf,
    contentSha256,
    uploadId,
    bindingId,
  };
}

function verifyCourse(
  payload: Record<string, unknown>,
  expected: LiveCourseRecord,
): void {
  if (
    payload.id !== expected.id ||
    payload.title !== expected.title ||
    payload.status !== "active"
  ) {
    throw new Error("live fixture retained course mismatch");
  }
}

const RETAINED_COURSE_GENERATION_POLICY_MISMATCH =
  "live fixture retained course generation policy mismatch";

const STUDENT_COURSE_GENERATION_POLICY = {
  allowStudentMicro: true,
  allowStudentFull: true,
  allowedContentModes: ["open_creation"],
  allowWebSearch: false,
  requireApprovalForRestrictedTopics: true,
  minorSafetyMode: true,
  microSceneLimit: 5,
  fullSceneLimit: 24,
  dailyStudentUnits: 40,
  monthlyStudentUnits: 400,
} as const;

function retainedCourseGenerationPolicyMismatch(): never {
  throw new Error(RETAINED_COURSE_GENERATION_POLICY_MISMATCH);
}

function verifyCourseGenerationPolicy(
  payload: unknown,
  course: Pick<LiveCourseRecord, "tenantId" | "id">,
): LiveCourseGenerationPolicyRecord {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return retainedCourseGenerationPolicyMismatch();
  }
  const record = payload as Record<string, unknown>;
  const modes = record.allowedContentModes;
  const updatedBy = record.updatedBy;
  const updatedAt = record.updatedAt;
  if (
    record.tenantId !== course.tenantId ||
    record.courseId !== course.id ||
    record.allowStudentMicro !==
      STUDENT_COURSE_GENERATION_POLICY.allowStudentMicro ||
    record.allowStudentFull !==
      STUDENT_COURSE_GENERATION_POLICY.allowStudentFull ||
    !Array.isArray(modes) ||
    modes.length !== 1 ||
    modes[0] !== "open_creation" ||
    record.allowWebSearch !==
      STUDENT_COURSE_GENERATION_POLICY.allowWebSearch ||
    record.requireApprovalForRestrictedTopics !==
      STUDENT_COURSE_GENERATION_POLICY.requireApprovalForRestrictedTopics ||
    record.minorSafetyMode !==
      STUDENT_COURSE_GENERATION_POLICY.minorSafetyMode ||
    record.microSceneLimit !==
      STUDENT_COURSE_GENERATION_POLICY.microSceneLimit ||
    record.fullSceneLimit !==
      STUDENT_COURSE_GENERATION_POLICY.fullSceneLimit ||
    record.dailyStudentUnits !==
      STUDENT_COURSE_GENERATION_POLICY.dailyStudentUnits ||
    record.monthlyStudentUnits !==
      STUDENT_COURSE_GENERATION_POLICY.monthlyStudentUnits ||
    typeof updatedBy !== "string" ||
    !updatedBy.trim() ||
    typeof updatedAt !== "string" ||
    !updatedAt.trim() ||
    Number.isNaN(Date.parse(updatedAt))
  ) {
    return retainedCourseGenerationPolicyMismatch();
  }
  return {
    tenantId: course.tenantId,
    courseId: course.id,
    ...STUDENT_COURSE_GENERATION_POLICY,
    updatedBy,
    updatedAt,
  };
}

async function requestCourseGenerationPolicy(
  operation: () => Promise<LiveApiResponse>,
  course: Pick<LiveCourseRecord, "tenantId" | "id">,
): Promise<LiveCourseGenerationPolicyRecord> {
  try {
    const response = await operation();
    if (response.status() !== 200) {
      return retainedCourseGenerationPolicyMismatch();
    }
    return verifyCourseGenerationPolicy(await response.json(), course);
  } catch {
    return retainedCourseGenerationPolicyMismatch();
  }
}

function sameCourseGenerationPolicy(
  left: LiveCourseGenerationPolicyRecord,
  right: LiveCourseGenerationPolicyRecord,
): boolean {
  return (
    left.tenantId === right.tenantId &&
    left.courseId === right.courseId &&
    left.allowStudentMicro === right.allowStudentMicro &&
    left.allowStudentFull === right.allowStudentFull &&
    left.allowedContentModes.length === right.allowedContentModes.length &&
    left.allowedContentModes.every(
      (mode, index) => mode === right.allowedContentModes[index],
    ) &&
    left.allowWebSearch === right.allowWebSearch &&
    left.requireApprovalForRestrictedTopics ===
      right.requireApprovalForRestrictedTopics &&
    left.minorSafetyMode === right.minorSafetyMode &&
    left.microSceneLimit === right.microSceneLimit &&
    left.fullSceneLimit === right.fullSceneLimit &&
    left.dailyStudentUnits === right.dailyStudentUnits &&
    left.monthlyStudentUnits === right.monthlyStudentUnits &&
    left.updatedBy === right.updatedBy &&
    left.updatedAt === right.updatedAt
  );
}

function verifyClass(
  payload: Record<string, unknown>,
  expected: LiveClassRecord,
): void {
  if (
    payload.id !== expected.id ||
    payload.courseId !== expected.courseId ||
    payload.name !== expected.name ||
    payload.status !== "active"
  ) {
    throw new Error("live fixture retained class mismatch");
  }
}

function verifySource(
  payload: Record<string, unknown>,
  expected: LiveSourceRecord,
): void {
  if (
    payload.bindingId !== expected.bindingId ||
    payload.sourceType !== expected.sourceType ||
    payload.sourceId !== expected.sourceId ||
    payload.filename !== expected.filename ||
    payload.sha256 !== expected.sha256 ||
    payload.sizeBytes !== expected.sizeBytes ||
    payload.courseId !== expected.courseId ||
    payload.classId !== expected.classId
  ) {
    throw new Error("live fixture retained source mismatch");
  }
}

function verifyEnrollment(
  payload: Record<string, unknown>,
  expected: LiveEnrollmentRecord,
): void {
  if (
    payload.classId !== expected.classId ||
    payload.userId !== expected.userId ||
    payload.status !== "active"
  ) {
    throw new Error("live fixture retained enrollment mismatch");
  }
}

async function ensureCourse(
  context: LiveFixtureContext,
  expected: LiveCourseRecord,
): Promise<LiveCourseRecord> {
  const secret = privateContext(context);
  const response = await guardedRequest("course creation", () =>
    secret.request.post("/api/v1/teaching/courses", {
      headers: adminHeaders(context),
      data: { id: expected.id, title: expected.title },
    }),
  );
  let payload: Record<string, unknown>;
  if (response.status() === 201) {
    payload = await responseRecord(response, "course creation");
  } else if (response.status() === 409) {
    const read = await guardedRequest("retained course", () =>
      secret.request.get("/api/v1/teaching/courses", {
        headers: adminHeaders(context),
      }),
    );
    if (read.status() !== 200) {
      throw new Error("live fixture retained course read failed");
    }
    const body = await responseRecord(read, "retained course");
    const items = requiredArray(body, "items", "retained course");
    const exact = items
      .map((item) => objectItem(item, "retained course"))
      .find((item) => item.id === expected.id);
    if (!exact) throw new Error("live fixture retained course mismatch");
    payload = exact;
  } else {
    throw new Error("live fixture course creation failed");
  }
  verifyCourse(payload, expected);
  retainByKey(context.records.courses, (item) => `${item.tenantId}\0${item.id}`, expected);
  return expected;
}

export async function ensureLiveCourseGenerationPolicy(
  context: LiveFixtureContext,
  course: Pick<LiveCourseRecord, "tenantId" | "id">,
): Promise<LiveCourseGenerationPolicyRecord> {
  const secret = privateContext(context);
  const path = `/api/v1/teaching/courses/${encodeURIComponent(course.id)}/generation-policy`;
  const written = await requestCourseGenerationPolicy(
    () =>
      secret.request.put(path, {
        headers: adminHeaders(context),
        data: STUDENT_COURSE_GENERATION_POLICY,
      }),
    course,
  );
  const retained = await requestCourseGenerationPolicy(
    () => secret.request.get(path, { headers: adminHeaders(context) }),
    course,
  );
  if (!sameCourseGenerationPolicy(written, retained)) {
    return retainedCourseGenerationPolicyMismatch();
  }
  return retained;
}

async function ensureClass(
  context: LiveFixtureContext,
  expected: LiveClassRecord,
): Promise<LiveClassRecord> {
  const secret = privateContext(context);
  const base = `/api/v1/teaching/courses/${encodeURIComponent(expected.courseId)}/classes`;
  const response = await guardedRequest("class creation", () =>
    secret.request.post(base, {
      headers: adminHeaders(context),
      data: { id: expected.id, name: expected.name },
    }),
  );
  let payload: Record<string, unknown>;
  if (response.status() === 201) {
    payload = await responseRecord(response, "class creation");
  } else if (response.status() === 409) {
    const read = await guardedRequest("retained class", () =>
      secret.request.get(base, { headers: adminHeaders(context) }),
    );
    if (read.status() !== 200) {
      throw new Error("live fixture retained class read failed");
    }
    const body = await responseRecord(read, "retained class");
    const items = requiredArray(body, "items", "retained class");
    const exact = items
      .map((item) => objectItem(item, "retained class"))
      .find((item) => item.id === expected.id);
    if (!exact) throw new Error("live fixture retained class mismatch");
    payload = exact;
  } else {
    throw new Error("live fixture class creation failed");
  }
  verifyClass(payload, expected);
  retainByKey(context.records.classes, (item) => `${item.tenantId}\0${item.id}`, expected);
  return expected;
}

async function ensureSource(
  context: LiveFixtureContext,
  expected: LiveSourceRecord,
  pdf: Buffer,
): Promise<LiveSourceRecord> {
  const secret = privateContext(context);
  const response = await guardedRequest("source creation", () =>
    secret.request.post("/api/v1/teaching/sources/pdf", {
      headers: adminHeaders(context),
      multipart: {
        file: { name: expected.filename, mimeType: "application/pdf", buffer: pdf },
        courseId: expected.courseId,
        classId: expected.classId,
      },
    }),
  );
  let payload: Record<string, unknown>;
  if (response.status() === 201) {
    payload = await responseRecord(response, "source creation");
  } else if (response.status() === 409) {
    const read = await guardedRequest("retained source", () =>
      secret.request.get("/api/v1/teaching/sources", {
        headers: adminHeaders(context),
      }),
    );
    if (read.status() !== 200) {
      throw new Error("live fixture retained source read failed");
    }
    const body = await responseRecord(read, "retained source");
    const items = requiredArray(body, "items", "retained source");
    const exact = items
      .map((item) => objectItem(item, "retained source"))
      .find((item) => item.bindingId === expected.bindingId);
    if (!exact) throw new Error("live fixture retained source mismatch");
    payload = exact;
  } else {
    throw new Error("live fixture source creation failed");
  }
  verifySource(payload, expected);
  retainByKey(
    context.records.sources,
    (item) => `${item.tenantId}\0${item.bindingId}`,
    expected,
  );
  return expected;
}

async function ensureEnrollment(
  context: LiveFixtureContext,
  expected: LiveEnrollmentRecord,
): Promise<LiveEnrollmentRecord> {
  const secret = privateContext(context);
  const base = `/api/v1/teaching/classes/${encodeURIComponent(expected.classId)}/enrollments`;
  const response = await guardedRequest("enrollment creation", () =>
    secret.request.post(base, {
      headers: adminHeaders(context),
      data: { userId: expected.userId },
    }),
  );
  let payload: Record<string, unknown>;
  if (response.status() === 201) {
    payload = await responseRecord(response, "enrollment creation");
  } else if (response.status() === 409) {
    const read = await guardedRequest("retained enrollment", () =>
      secret.request.get(base, { headers: adminHeaders(context) }),
    );
    if (read.status() !== 200) {
      throw new Error("live fixture retained enrollment read failed");
    }
    const body = await responseRecord(read, "retained enrollment");
    const items = requiredArray(body, "items", "retained enrollment");
    const exact = items
      .map((item) => objectItem(item, "retained enrollment"))
      .find((item) => item.userId === expected.userId);
    if (!exact) throw new Error("live fixture retained enrollment mismatch");
    payload = exact;
  } else {
    throw new Error("live fixture enrollment creation failed");
  }
  verifyEnrollment(payload, expected);
  retainByKey(
    context.records.enrollments,
    (item) => `${item.tenantId}\0${item.classId}\0${item.userId}`,
    expected,
  );
  return expected;
}

export type EnsureLiveCatalogOptions = {
  controlledSource: boolean;
  enroll: readonly LiveIdentity[];
};

export interface LiveCatalogRecords {
  course: LiveCourseRecord;
  teachingClass: LiveClassRecord;
  source: LiveSourceRecord | null;
  enrollments: LiveEnrollmentRecord[];
}

export async function ensureLiveCatalog(
  context: LiveFixtureContext,
  tenant: LiveTenantRecord,
  options: EnsureLiveCatalogOptions,
): Promise<LiveCatalogRecords> {
  const deterministic = deterministicCatalog(context, tenant.tenantId);
  const course = await ensureCourse(context, {
    tenantId: tenant.tenantId,
    id: deterministic.courseId,
    title: deterministic.title,
    status: "active",
  });
  const teachingClass = await ensureClass(context, {
    tenantId: tenant.tenantId,
    id: deterministic.classId,
    courseId: deterministic.courseId,
    name: deterministic.name,
    status: "active",
  });
  const source = options.controlledSource
    ? await ensureSource(
        context,
        {
          tenantId: tenant.tenantId,
          bindingId: deterministic.bindingId,
          sourceType: "pdf",
          sourceId: deterministic.uploadId,
          filename: deterministic.filename,
          sha256: deterministic.contentSha256,
          sizeBytes: deterministic.pdf.length,
          courseId: deterministic.courseId,
          classId: deterministic.classId,
        },
        deterministic.pdf,
      )
    : null;
  const enrollments: LiveEnrollmentRecord[] = [];
  for (const identity of options.enroll) {
    if (!identity.userId) throw new Error("live fixture user is not provisioned");
    enrollments.push(
      await ensureEnrollment(context, {
        tenantId: tenant.tenantId,
        classId: deterministic.classId,
        userId: identity.userId,
        status: "active",
      }),
    );
  }
  return { course, teachingClass, source, enrollments };
}

export type ProvisionLiveFixtureOptions = {
  roles: readonly LiveRole[];
  catalog?: {
    controlledSource: boolean;
    enrollRoles: readonly LiveRole[];
  };
};

export interface LiveProvisionedFixture {
  tenant: LiveTenantRecord;
  identities: LiveIdentity[];
  grants: LiveGrantRecord[];
  catalog: LiveCatalogRecords | null;
}

export async function provisionLiveFixture(
  context: LiveFixtureContext,
  options: ProvisionLiveFixtureOptions,
): Promise<LiveProvisionedFixture> {
  const roles = [...options.roles];
  if (!roles.length || new Set(roles).size !== roles.length) {
    throw new Error("live fixture roles must be unique and non-empty");
  }
  const identities: LiveIdentity[] = [];
  for (const role of roles) identities.push(await ensureLiveUser(context, role));
  const tenant = await ensureLiveTenant(context);
  const grants: LiveGrantRecord[] = [];
  for (const identity of identities) {
    grants.push(await ensureLiveTenantMembership(context, tenant, identity));
  }
  let catalog: LiveCatalogRecords | null = null;
  if (options.catalog) {
    const enrolled = options.catalog.enrollRoles.map((role) => {
      const identity = identities.find((candidate) => candidate.role === role);
      if (!identity) throw new Error("live fixture enrollment role is not provisioned");
      return identity;
    });
    catalog = await ensureLiveCatalog(context, tenant, {
      controlledSource: options.catalog.controlledSource,
      enroll: enrolled,
    });
  }
  return { tenant, identities, grants, catalog };
}

export async function loginLiveIdentity(
  page: LivePage,
  identity: LiveIdentity,
  nextPath = "/",
): Promise<void> {
  if (!nextPath.startsWith("/") || nextPath.startsWith("//")) {
    throw new Error("live fixture login destination is invalid");
  }
  const password = identityPassword(identity);
  try {
    await page.goto(`/login?next=${encodeURIComponent(nextPath)}`);
    await page.locator("#username").fill(identity.username);
    await page.locator("#password").fill(password);
    await Promise.all([
      page.waitForURL(nextPath),
      page.locator('button[type="submit"]').click(),
    ]);
  } catch {
    throw new Error("live fixture browser login failed");
  }
}
