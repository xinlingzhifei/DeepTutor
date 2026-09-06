import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import * as liveFixtureModule from "./e2e/support/classroom-first-release-live-fixture";
import {
  LIVE_FIXTURE_POLICY_VERSION,
  LiveFixtureContext,
  deriveLiveIdentity,
  ensureLiveCatalog,
  ensureLiveCourseGenerationPolicy,
  ensureLiveTenant,
  ensureLiveTenantMembership,
  ensureLiveUser,
  loginLiveIdentity,
  provisionLiveFixture,
  type LiveApiRequestContext,
  type LiveApiRequestOptions,
  type LiveApiResponse,
  type LiveEvidence,
  type LiveIdentity,
  type LiveTenantRecord,
} from "./e2e/support/classroom-first-release-live-fixture";

const ADMIN_TOKEN = "admin-token-never-serialize";

type StudentClassroomPollState = {
  assetId: string;
  generationJobId: string;
  status: string;
  courseId: string;
  classId: string;
  mode: "micro" | "full";
  ownerId: string;
  classroomVersionId: string | null;
};

type StudentGenerationJobPollState = {
  jobId: string;
  jobKind: string;
  phase: "outline" | "content";
  status: string;
  progressPercent: number;
};

type PollLiveStudentClassroomOptions = {
  expected: {
    assetId: string;
    generationJobId: string;
    courseId: string;
    classId: string;
    mode: "micro" | "full";
    ownerId: string;
  };
  pollAttempts: number;
  pollIntervalMs: number;
  pause: (milliseconds: number) => Promise<void>;
  readClassroom: () => Promise<StudentClassroomPollState>;
  readGenerationJob: (
    jobId: string,
  ) => Promise<StudentGenerationJobPollState>;
  onAwaitingConfirmation?: (
    classroom: StudentClassroomPollState,
  ) => Promise<void>;
};

type PollLiveStudentClassroom = (
  options: PollLiveStudentClassroomOptions,
) => Promise<{
  classroom: StudentClassroomPollState & { classroomVersionId: string };
  generationJob: StudentGenerationJobPollState;
}>;

type StudentCourseGenerationPolicyRecord = {
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
};

type EnsureLiveCourseGenerationPolicy = (
  context: LiveFixtureContext,
  course: { tenantId: string; id: string },
) => Promise<StudentCourseGenerationPolicyRecord>;

type ActualPollLiveStudentClassroom = typeof liveFixtureModule extends {
  pollLiveStudentClassroom: infer Candidate;
}
  ? Candidate extends (...args: never[]) => unknown
    ? Candidate
    : PollLiveStudentClassroom
  : PollLiveStudentClassroom;

type ActualPollLiveStudentClassroomOptions =
  Parameters<ActualPollLiveStudentClassroom>[0];

const POLLER_OPTIONS_REJECT_CALLER_VERSION_ID: "versionId" extends keyof ActualPollLiveStudentClassroomOptions
  ? never
  : true = true;

function studentPoller(): PollLiveStudentClassroom {
  const candidate = (
    liveFixtureModule as unknown as Record<string, unknown>
  ).pollLiveStudentClassroom;
  assert.equal(
    typeof candidate,
    "function",
    "pollLiveStudentClassroom must be implemented",
  );
  return candidate as PollLiveStudentClassroom;
}

function classroomState(
  overrides: Partial<StudentClassroomPollState> = {},
): StudentClassroomPollState {
  return {
    assetId: "asset-student-live",
    generationJobId: "job-student-live",
    status: "queued",
    courseId: "course-student-live",
    classId: "class-student-live",
    mode: "micro",
    ownerId: "student-live",
    classroomVersionId: null,
    ...overrides,
  };
}

function generationJobState(
  overrides: Partial<StudentGenerationJobPollState> = {},
): StudentGenerationJobPollState {
  return {
    jobId: "job-student-live",
    jobKind: "generation",
    phase: "content",
    status: "running",
    progressPercent: 50,
    ...overrides,
  };
}

function fakeSequence<T>(values: readonly T[]) {
  assert.ok(values.length > 0);
  let calls = 0;
  return {
    async read(): Promise<T> {
      const value = values[Math.min(calls, values.length - 1)];
      calls += 1;
      return value;
    },
    calls(): number {
      return calls;
    },
  };
}

function studentPollOptions(
  overrides: Partial<PollLiveStudentClassroomOptions> = {},
): PollLiveStudentClassroomOptions {
  return {
    expected: {
      assetId: "asset-student-live",
      generationJobId: "job-student-live",
      courseId: "course-student-live",
      classId: "class-student-live",
      mode: "micro",
      ownerId: "student-live",
    },
    pollAttempts: 4,
    pollIntervalMs: 0,
    pause: async () => undefined,
    readClassroom: async () => classroomState(),
    readGenerationJob: async () => generationJobState(),
    ...overrides,
  };
}

type Call = {
  method: "GET" | "POST" | "PUT";
  url: string;
  requestUrl: string;
  options: LiveApiRequestOptions | undefined;
};

const CANONICAL_HTTPS_ORIGIN = "https://candidate.example.test";

type LiveCandidateIdentity = {
  sourceHead: string;
  releaseTag: string;
  candidateSha256: string;
};

type CanonicalCandidate = {
  imageDigests: {
    deeptutor: string;
    openmaic: string;
    openmaic_render: string;
  };
  openmaicHead: string;
  releaseTag: string;
  sourceHead: string;
  sourceRepository: string;
};

function candidateIdentity(
  sourceHead: string,
  releaseTag: string,
): LiveCandidateIdentity {
  return {
    sourceHead,
    releaseTag,
    candidateSha256: sha256(JSON.stringify({ releaseTag, sourceHead })),
  };
}

function fullCandidateIdentity(
  candidate: CanonicalCandidate,
): LiveCandidateIdentity {
  const canonical: CanonicalCandidate = {
    imageDigests: {
      deeptutor: candidate.imageDigests.deeptutor,
      openmaic: candidate.imageDigests.openmaic,
      openmaic_render: candidate.imageDigests.openmaic_render,
    },
    openmaicHead: candidate.openmaicHead,
    releaseTag: candidate.releaseTag,
    sourceHead: candidate.sourceHead,
    sourceRepository: candidate.sourceRepository,
  };
  return {
    sourceHead: canonical.sourceHead,
    releaseTag: canonical.releaseTag,
    candidateSha256: sha256(JSON.stringify(canonical)),
  };
}

const DEFAULT_CANDIDATE = candidateIdentity(
  "a".repeat(40),
  "yfeistai-first-release-20260826-aaaaaaaa",
);

class FakeResponse implements LiveApiResponse {
  constructor(
    private readonly statusCode: number,
    private readonly body: unknown,
    private readonly responseUrl = `${CANONICAL_HTTPS_ORIGIN}/api/v1/test`,
  ) {}

  status(): number {
    return this.statusCode;
  }

  url(): string {
    return this.responseUrl;
  }

  async json(): Promise<unknown> {
    if (this.body instanceof Error) throw this.body;
    return this.body;
  }
}

class FakeRequest implements LiveApiRequestContext {
  readonly calls: Call[] = [];

  constructor(
    private readonly respond: (
      call: Call,
    ) => LiveApiResponse | Promise<LiveApiResponse>,
  ) {}

  get(url: string, options?: LiveApiRequestOptions): Promise<LiveApiResponse> {
    return this.send("GET", url, options);
  }

  post(url: string, options?: LiveApiRequestOptions): Promise<LiveApiResponse> {
    return this.send("POST", url, options);
  }

  put(url: string, options?: LiveApiRequestOptions): Promise<LiveApiResponse> {
    return this.send("PUT", url, options);
  }

  private async send(
    method: Call["method"],
    url: string,
    options: LiveApiRequestOptions | undefined,
  ): Promise<LiveApiResponse> {
    let routeUrl = url;
    try {
      const parsed = new URL(url);
      routeUrl = `${parsed.pathname}${parsed.search}${parsed.hash}`;
    } catch {
      // Existing fixture responders intentionally match relative API paths.
    }
    const call = { method, url: routeUrl, requestUrl: url, options };
    this.calls.push(call);
    return this.respond(call);
  }
}

function response(
  status: number,
  body: unknown,
  responseUrl?: string,
): FakeResponse {
  return new FakeResponse(status, body, responseUrl);
}

type GuardedLiveApiRequestWithOrigin = (
  request: LiveApiRequestContext,
  label: string,
  method: "get" | "post" | "put",
  path: string,
  canonicalOrigin: string,
  options?: LiveApiRequestOptions,
) => Promise<LiveApiResponse>;

function guardedLiveApiRequestWithOrigin(): GuardedLiveApiRequestWithOrigin {
  const candidate = (
    liveFixtureModule as unknown as Record<string, unknown>
  ).guardedLiveApiRequest;
  assert.equal(
    typeof candidate,
    "function",
    "guardedLiveApiRequest must be implemented",
  );
  return candidate as GuardedLiveApiRequestWithOrigin;
}

type FakeLoginRequest = {
  url(): string;
  method(): string;
  isNavigationRequest(): boolean;
};

type FakeLoginRequestPredicate = (
  request: FakeLoginRequest,
) => boolean | Promise<boolean>;

type FakeLoginRequestWaiter = {
  predicate: FakeLoginRequestPredicate;
  resolve(request: FakeLoginRequest): void;
  reject(error: unknown): void;
};

function fakeLoginPage(
  options: {
    origin?: string;
    formCount?: number;
    formCountAfterWait?: number;
    urlAfterFormWait?: string;
    submissionUrl?: string;
  } = {},
) {
  const origin = options.origin ?? CANONICAL_HTTPS_ORIGIN;
  let currentUrl = `${origin}/login`;
  let requestWaiter: FakeLoginRequestWaiter | null = null;
  let credentialWrites = 0;
  let observedSubmissions = 0;
  let ignoredSubmissions = 0;
  const filled: Record<string, string> = {};
  const actions: string[] = [];
  let formCount = options.formCount ?? 1;
  const submissionUrl =
    options.submissionUrl ?? `${origin}/api/v1/auth/login`;

  const page = {
    url() {
      return currentUrl;
    },
    async goto(url: string) {
      actions.push(`goto:${url}`);
      currentUrl = new URL(url, origin).href;
    },
    locator(selector: string) {
      if (selector.includes("form")) {
        return {
          async waitFor(waitOptions: { state: string; timeout: number }) {
            actions.push(
              `form:wait:${waitOptions.state}:${waitOptions.timeout}`,
            );
            await Promise.resolve();
            formCount = options.formCountAfterWait ?? formCount;
            currentUrl = options.urlAfterFormWait ?? currentUrl;
            if (formCount === 0) {
              actions.push("form:wait:timeout");
              throw new Error("login form wait timed out");
            }
            actions.push("form:visible");
          },
          async count() {
            actions.push("form:count");
            return formCount;
          },
          async getAttribute(name: string) {
            actions.push(`form:${name}`);
            return null;
          },
          async fill(_value: string) {
            throw new Error(
              "the login form itself must not receive credentials",
            );
          },
          async click() {
            throw new Error("the login form itself must not be clicked");
          },
        };
      }
      return {
        async count() {
          return 1;
        },
        async getAttribute(_name: string) {
          return null;
        },
        async fill(value: string) {
          credentialWrites += 1;
          filled[selector] = value;
          actions.push(`fill:${selector}`);
        },
        async click() {
          actions.push(`click:${selector}`);
          const waiter = requestWaiter;
          if (!waiter) return;
          const request: FakeLoginRequest = {
            url: () => submissionUrl,
            method: () => "POST",
            isNavigationRequest: () => false,
          };
          try {
            if (await waiter.predicate(request)) {
              observedSubmissions += 1;
              waiter.resolve(request);
            } else {
              ignoredSubmissions += 1;
              actions.push("waitForRequest:ignored");
            }
          } catch (error) {
            waiter.reject(error);
          }
        },
      };
    },
    async waitForRequest(predicate: FakeLoginRequestPredicate) {
      actions.push("waitForRequest");
      if (requestWaiter) throw new Error("login request waiter already exists");
      return new Promise<FakeLoginRequest>((resolve, reject) => {
        // Accelerate the browser timeout; nonmatching requests keep waiting.
        const timeout = setTimeout(() => {
          requestWaiter = null;
          actions.push("waitForRequest:timeout");
          reject(new Error("login request wait timed out"));
        }, 0);
        requestWaiter = {
          predicate,
          resolve(request) {
            clearTimeout(timeout);
            requestWaiter = null;
            resolve(request);
          },
          reject(error) {
            clearTimeout(timeout);
            requestWaiter = null;
            reject(error);
          },
        };
      });
    },
    async waitForURL(url: string) {
      actions.push(`wait:${url}`);
      await Promise.resolve();
      currentUrl = new URL(url, origin).href;
    },
  };

  return {
    page,
    filled,
    actions,
    credentialWrites: () => credentialWrites,
    observedSubmissions: () => observedSubmissions,
    ignoredSubmissions: () => ignoredSubmissions,
  };
}

function data(call: Call): Record<string, unknown> {
  assert.ok(call.options?.data && typeof call.options.data === "object");
  return call.options.data as Record<string, unknown>;
}

function header(call: Call, name: string): string {
  const value = call.options?.headers?.[name];
  assert.ok(typeof value === "string");
  return value;
}

function makeContext(
  request: LiveApiRequestContext,
  options: {
    evidence?: LiveEvidence;
    releaseRun?: string;
    environment?: string;
    apiOrigin?: string;
    candidate?: LiveCandidateIdentity;
    provisioningPollAttempts?: number;
    pause?: (milliseconds: number) => Promise<void>;
  } = {},
): LiveFixtureContext {
  const contextOptions = {
    request,
    adminToken: ADMIN_TOKEN,
    releaseRun: options.releaseRun ?? "release-20260826-a",
    environment: options.environment ?? "candidate-prod-a",
    evidence: options.evidence ?? "teacher_flow",
    apiOrigin: options.apiOrigin ?? CANONICAL_HTTPS_ORIGIN,
    candidate: options.candidate ?? DEFAULT_CANDIDATE,
    provisioningPollAttempts: options.provisioningPollAttempts,
    provisioningPollIntervalMs: 0,
    pause: options.pause ?? (async () => undefined),
  } as ConstructorParameters<typeof LiveFixtureContext>[0] & {
    apiOrigin: string;
    candidate: LiveCandidateIdentity;
  };
  return new LiveFixtureContext(contextOptions);
}

function userCreated(call: Call, userId?: string) {
  const username = String(data(call).username);
  return {
    ok: true,
    user_id: userId ?? `u-${sha256(username).slice(0, 12)}`,
    username,
    role: "user",
    is_admin: false,
  };
}

function tenantRecord(): LiveTenantRecord {
  return {
    tenantId: "t-live-a",
    name: "yFeiSTAI live fixture",
    status: "active",
    jobId: "j-live-a",
  };
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function digestId(prefix: string, ...values: string[]): string {
  return `${prefix}-${sha256(values.join("\0"))}`;
}

function sourceFromUpload(call: Call, tenantId: string) {
  const multipart = call.options?.multipart;
  assert.ok(multipart);
  const file = multipart.file;
  assert.ok(file && typeof file === "object" && "buffer" in file);
  const upload = file as { name: string; mimeType: string; buffer: Buffer };
  const courseId = String(multipart.courseId);
  const classId = String(multipart.classId);
  const contentSha256 = sha256(upload.buffer);
  const uploadId = digestId("upload", tenantId, contentSha256);
  const snapshotId = digestId(
    "pdf-source",
    tenantId,
    uploadId,
    courseId,
    classId,
    upload.name,
  );
  const bindingId = `source-binding-${sha256(
    [tenantId, snapshotId, courseId, classId].join("\0"),
  )}`;
  return {
    bindingId,
    sourceType: "pdf",
    sourceId: uploadId,
    filename: upload.name,
    sha256: contentSha256,
    sizeBytes: upload.buffer.length,
    courseId,
    classId,
    createdAt: "2026-08-26T00:00:00Z",
  };
}

function successfulCatalogWrite(
  call: Call,
  tenantId: string,
): FakeResponse | null {
  if (call.method !== "POST") return null;
  if (call.url === "/api/v1/teaching/courses") {
    const body = data(call);
    return response(201, { id: body.id, title: body.title, status: "active" });
  }
  if (call.url.endsWith("/classes")) {
    const body = data(call);
    return response(201, {
      id: body.id,
      courseId: call.url.split("/")[5],
      name: body.name,
      status: "active",
    });
  }
  if (call.url === "/api/v1/teaching/sources/pdf") {
    return response(201, sourceFromUpload(call, tenantId));
  }
  if (call.url.endsWith("/enrollments")) {
    const body = data(call);
    return response(201, {
      classId: call.url.split("/")[5],
      userId: body.userId,
      status: "active",
    });
  }
  return null;
}

test("policy v2 HMAC identities are stable and differ by role and evidence", () => {
  const request = new FakeRequest(() => response(500, {}));
  const teacher = deriveLiveIdentity(makeContext(request), "teacher");
  const teacherReplay = deriveLiveIdentity(makeContext(request), "teacher");
  const student = deriveLiveIdentity(makeContext(request), "student");
  const otherEvidenceTeacher = deriveLiveIdentity(
    makeContext(request, { evidence: "content_operations_flow" }),
    "teacher",
  );
  const author = deriveLiveIdentity(
    makeContext(request, { evidence: "content_operations_flow" }),
    "author",
  );
  const reviewer = deriveLiveIdentity(
    makeContext(request, { evidence: "content_operations_flow" }),
    "reviewer",
  );
  const publisher = deriveLiveIdentity(
    makeContext(request, { evidence: "content_operations_flow" }),
    "publisher",
  );

  assert.equal(LIVE_FIXTURE_POLICY_VERSION, 2);
  assert.equal(teacher.username, teacherReplay.username);
  assert.equal(teacher.suffix, teacherReplay.suffix);
  assert.notEqual(teacher.username, student.username);
  assert.notEqual(teacher.username, otherEvidenceTeacher.username);
  assert.equal(teacher.role, "teacher");
  assert.equal(student.role, "student");
  assert.equal(author.tenantRole, "content_author");
  assert.equal(reviewer.tenantRole, "content_reviewer");
  assert.equal(publisher.tenantRole, "teacher");
});

test("candidate identity namespaces accounts tenants and catalog deterministically", async () => {
  async function namespace(candidate: LiveCandidateIdentity) {
    let tenantId = "";
    let tenantKey = "";
    const request = new FakeRequest((call) => {
      if (call.method === "POST" && call.url === "/api/v1/tenants") {
        tenantKey = header(call, "Idempotency-Key");
        tenantId = `tenant-${sha256(tenantKey).slice(0, 12)}`;
        return response(202, {
          tenant_id: tenantId,
          job_id: `job-${sha256(tenantKey).slice(12, 24)}`,
          status: "active",
        });
      }
      if (call.method === "PUT" && call.url === "/api/v1/tenants/active") {
        assert.equal(data(call).tenant_id, tenantId);
        return response(200, { active_tenant_id: tenantId });
      }
      return successfulCatalogWrite(call, tenantId) ?? response(500, {});
    });
    const context = makeContext(request, { candidate });
    const identity = deriveLiveIdentity(context, "teacher");
    const tenant = await ensureLiveTenant(context);
    const catalog = await ensureLiveCatalog(context, tenant, {
      controlledSource: true,
      enroll: [],
    });
    assert.ok(catalog.source);
    return {
      username: identity.username,
      tenantKey,
      tenantId: tenant.tenantId,
      courseId: catalog.course.id,
      classId: catalog.teachingClass.id,
      sourceBindingId: catalog.source.bindingId,
    };
  }

  function assertSeparated(
    reference: Awaited<ReturnType<typeof namespace>>,
    other: Awaited<ReturnType<typeof namespace>>,
    label: string,
  ): void {
    for (const key of Object.keys(reference) as (keyof typeof reference)[]) {
      assert.notEqual(other[key], reference[key], `${label} reused ${key}`);
    }
  }

  const canonicalCandidate: CanonicalCandidate = {
    imageDigests: {
      deeptutor: `sha256:${"1".repeat(64)}`,
      openmaic: `sha256:${"2".repeat(64)}`,
      openmaic_render: `sha256:${"3".repeat(64)}`,
    },
    openmaicHead: "0cf2a330411681190e89f48e20f305345ff99f87",
    releaseTag: "yfeistai-first-release-20260826-aaaaaaaa",
    sourceHead: "a".repeat(40),
    sourceRepository: "xinlingzhifei/DeepTutor",
  };
  const candidateA = fullCandidateIdentity(canonicalCandidate);
  const changedSourceHead = fullCandidateIdentity({
    ...canonicalCandidate,
    sourceHead: "b".repeat(40),
  });
  const changedReleaseTag = fullCandidateIdentity({
    ...canonicalCandidate,
    releaseTag: "yfeistai-first-release-20260827-aaaaaaaa",
  });
  const changedImageDigest = fullCandidateIdentity({
    ...canonicalCandidate,
    imageDigests: {
      ...canonicalCandidate.imageDigests,
      deeptutor: `sha256:${"4".repeat(64)}`,
    },
  });
  const changedOpenmaicHead = fullCandidateIdentity({
    ...canonicalCandidate,
    openmaicHead: "b".repeat(40),
  });
  const candidateAFirst = await namespace(candidateA);
  const candidateAReplay = await namespace(candidateA);
  const changedSourceHeadFirst = await namespace(changedSourceHead);
  const changedReleaseTagFirst = await namespace(changedReleaseTag);
  const changedImageDigestFirst = await namespace(changedImageDigest);
  const changedOpenmaicHeadFirst = await namespace(changedOpenmaicHead);

  assert.deepEqual(candidateAReplay, candidateAFirst);
  assertSeparated(
    candidateAFirst,
    changedSourceHeadFirst,
    "changed source head candidate",
  );
  assertSeparated(
    candidateAFirst,
    changedReleaseTagFirst,
    "changed release tag candidate",
  );
  assertSeparated(
    candidateAFirst,
    changedImageDigestFirst,
    "changed image digest candidate",
  );
  assertSeparated(
    candidateAFirst,
    changedOpenmaicHeadFirst,
    "changed OpenMAIC head candidate",
  );
});

test("derived usernames are reserved and strong passwords remain non-enumerable", async () => {
  let capturedPassword = "";
  const request = new FakeRequest((call) => {
    assert.equal(call.url, "/api/v1/auth/users");
    const body = data(call);
    capturedPassword = String(body.password);
    return response(201, {
      ok: true,
      user_id: "u-teacher",
      username: body.username,
      role: "user",
      is_admin: false,
    });
  });
  const context = makeContext(request, {
    releaseRun: "Release 2026/08/26 A",
  });

  const identity = await ensureLiveUser(context, "teacher");

  assert.match(identity.username, /^[a-z0-9-]+@example\.invalid$/);
  assert.match(capturedPassword, /[A-Z]/);
  assert.match(capturedPassword, /[a-z]/);
  assert.match(capturedPassword, /[0-9]/);
  assert.match(capturedPassword, /[^A-Za-z0-9]/);
  assert.ok(capturedPassword.length >= 24);
  const serialized = JSON.stringify({ context, identity, records: context.records });
  assert.equal(serialized.includes(ADMIN_TOKEN), false);
  assert.equal(serialized.includes(capturedPassword), false);
  assert.equal(identity.username.includes(ADMIN_TOKEN), false);
  assert.equal(header(request.calls[0], "Authorization"), `Bearer ${ADMIN_TOKEN}`);
});

test("a user create conflict is adopted only after derived-password login succeeds", async () => {
  let derivedPassword = "";
  const request = new FakeRequest((call) => {
    if (call.url === "/api/v1/auth/users") {
      derivedPassword = String(data(call).password);
      return response(409, { detail: "Username already taken" });
    }
    assert.equal(call.url, "/api/v1/auth/login");
    assert.equal(call.options?.headers, undefined);
    const body = data(call);
    assert.equal(body.password, derivedPassword);
    return response(200, {
      ok: true,
      user_id: "u-owned",
      username: body.username,
      role: "user",
      is_admin: false,
    });
  });

  const identity = await ensureLiveUser(makeContext(request), "teacher");

  assert.equal(identity.userId, "u-owned");
  assert.deepEqual(
    request.calls.map((call) => [call.method, call.url]),
    [
      ["POST", "/api/v1/auth/users"],
      ["POST", "/api/v1/auth/login"],
    ],
  );
});

test("a user create conflict followed by 401 fails closed without leaking credentials", async () => {
  let derivedPassword = "";
  const request = new FakeRequest((call) => {
    if (call.url === "/api/v1/auth/users") {
      derivedPassword = String(data(call).password);
      return response(409, { detail: ADMIN_TOKEN });
    }
    return response(401, { detail: derivedPassword });
  });

  await assert.rejects(
    ensureLiveUser(makeContext(request), "teacher"),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /ownership/i);
      assert.equal(error.message.includes(ADMIN_TOKEN), false);
      assert.equal(error.message.includes(derivedPassword), false);
      return true;
    },
  );
});

test("tenant setup uses a stable idempotency key, bounded active polling, and admin switch", async () => {
  const keys: string[] = [];
  const pauses: number[] = [];
  let poll = 0;
  const request = new FakeRequest((call) => {
    assert.equal(header(call, "Authorization"), `Bearer ${ADMIN_TOKEN}`);
    if (call.method === "POST") {
      assert.equal(call.url, "/api/v1/tenants");
      keys.push(header(call, "Idempotency-Key"));
      return response(202, {
        tenant_id: "t-live-a",
        status: "provisioning",
        job_id: "j-live-a",
      });
    }
    if (call.method === "GET") {
      poll += 1;
      return response(200, {
        tenant_id: "t-live-a",
        status: poll === 2 ? "active" : "provisioning",
        job_id: "j-live-a",
        job_status: poll === 2 ? "completed" : "running",
        attempt_count: 1,
      });
    }
    assert.equal(call.url, "/api/v1/tenants/active");
    assert.deepEqual(data(call), { tenant_id: "t-live-a" });
    return response(200, { active_tenant_id: "t-live-a" });
  });
  const context = makeContext(request, {
    pause: async (milliseconds) => {
      pauses.push(milliseconds);
    },
  });

  const tenant = await ensureLiveTenant(context);

  assert.equal(tenant.status, "active");
  assert.equal(keys.length, 1);
  assert.match(keys[0], /^yfeistai-live-v2-[a-f0-9]{32}$/);
  assert.deepEqual(pauses, [0]);

  const replayRequest = new FakeRequest((call) => {
    if (call.method === "POST") {
      keys.push(header(call, "Idempotency-Key"));
      return response(202, {
        tenant_id: "t-live-a",
        status: "active",
        job_id: "j-live-a",
      });
    }
    return response(200, { active_tenant_id: "t-live-a" });
  });
  await ensureLiveTenant(makeContext(replayRequest));
  assert.equal(keys[1], keys[0]);
});

test("tenant polling is bounded and never switches an inactive tenant", async () => {
  const request = new FakeRequest((call) => {
    if (call.method === "POST") {
      return response(202, {
        tenant_id: "t-pending",
        status: "provisioning",
        job_id: "j-pending",
      });
    }
    return response(200, {
      tenant_id: "t-pending",
      status: "provisioning",
      job_id: "j-pending",
      job_status: "running",
      attempt_count: 1,
    });
  });

  await assert.rejects(
    ensureLiveTenant(
      makeContext(request, { provisioningPollAttempts: 2 }),
    ),
    /did not become active/i,
  );
  assert.equal(request.calls.filter((call) => call.method === "GET").length, 2);
  assert.equal(request.calls.some((call) => call.method === "PUT"), false);
});

test("tenant membership grants the exact scoped formal role and verifies the response", async () => {
  const userRequest = new FakeRequest((call) =>
    response(201, userCreated(call, "u-publisher")),
  );
  const context = makeContext(userRequest, {
    evidence: "content_operations_flow",
  });
  const publisher = await ensureLiveUser(context, "publisher");
  const membershipRequest = new FakeRequest((call) => {
    assert.equal(call.url, "/api/v1/tenants/t-live-a/members");
    assert.deepEqual(data(call), {
      user_id: "u-publisher",
      grants: [
        { role: "teacher", scope_type: "tenant", scope_id: "t-live-a" },
      ],
    });
    return response(200, {
      tenant_id: "t-live-a",
      user_id: "u-publisher",
      roles: ["teacher"],
      grants: [
        { role: "teacher", scope_type: "tenant", scope_id: "t-live-a" },
      ],
    });
  });
  const membershipContext = makeContext(membershipRequest, {
    evidence: "content_operations_flow",
  });

  const grant = await ensureLiveTenantMembership(
    membershipContext,
    tenantRecord(),
    publisher,
  );

  assert.equal(grant.role, "teacher");
  assert.equal(grant.scopeType, "tenant");
  assert.equal(grant.scopeId, "t-live-a");
});

test("catalog setup creates and verifies course, class, PDF source, and enrollment", async () => {
  const tenant = tenantRecord();
  let identity: LiveIdentity;
  const request = new FakeRequest((call) => {
    if (call.url === "/api/v1/auth/users") {
      return response(201, userCreated(call, "u-student"));
    }
    return successfulCatalogWrite(call, tenant.tenantId) ?? response(500, {});
  });
  const context = makeContext(request, { evidence: "student_micro_flow" });
  identity = await ensureLiveUser(context, "student");

  const catalog = await ensureLiveCatalog(context, tenant, {
    controlledSource: true,
    enroll: [identity],
  });

  assert.equal(catalog.course.status, "active");
  assert.equal(catalog.teachingClass.courseId, catalog.course.id);
  assert.equal(catalog.source?.sourceType, "pdf");
  assert.equal(catalog.source?.courseId, catalog.course.id);
  assert.equal(catalog.source?.classId, catalog.teachingClass.id);
  assert.deepEqual(catalog.enrollments, [
    {
      tenantId: tenant.tenantId,
      classId: catalog.teachingClass.id,
      userId: "u-student",
      status: "active",
    },
  ]);
  const sourceCall = request.calls.find(
    (call: Call) => call.url === "/api/v1/teaching/sources/pdf",
  );
  assert.ok(sourceCall?.options?.multipart);
  const file = sourceCall.options.multipart.file;
  assert.ok(file && typeof file === "object" && "mimeType" in file);
  assert.equal(file.mimeType, "application/pdf");
});

test("student fixture PUTs and GETs the exact retained generation policy with the platform-admin token", async () => {
  const course = { tenantId: "t-student-live", id: "course-student-live" };
  const path = `/api/v1/teaching/courses/${course.id}/generation-policy`;
  const policy = {
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
  const retainedPolicy = {
    tenantId: course.tenantId,
    courseId: course.id,
    ...policy,
    updatedBy: "platform-admin-live",
    updatedAt: "2026-08-26T08:00:00Z",
  } as const;
  const expectedMethods = ["PUT", "GET"] as const;
  let callIndex = 0;
  const request = new FakeRequest((call) => {
    assert.ok(callIndex < expectedMethods.length, "unexpected policy request");
    assert.equal(call.method, expectedMethods[callIndex]);
    assert.equal(call.url, path);
    assert.equal(header(call, "Authorization"), `Bearer ${ADMIN_TOKEN}`);
    if (call.method === "PUT") {
      assert.deepEqual(data(call), policy);
    } else {
      assert.equal(call.options?.data, undefined);
    }
    callIndex += 1;
    return response(200, retainedPolicy);
  });
  const candidate = (
    liveFixtureModule as unknown as Record<string, unknown>
  ).ensureLiveCourseGenerationPolicy;
  assert.equal(
    typeof candidate,
    "function",
    "ensureLiveCourseGenerationPolicy must be implemented",
  );
  const ensurePolicy = candidate as EnsureLiveCourseGenerationPolicy;

  const result = await ensurePolicy(
    makeContext(request, { evidence: "student_micro_flow" }),
    course,
  );

  assert.equal(callIndex, 2);
  assert.ok(result.updatedBy.trim());
  assert.equal(Number.isNaN(Date.parse(result.updatedAt)), false);
  assert.deepEqual(result, retainedPolicy);

  const isStaticPolicyMismatch = (error: unknown): boolean => {
    assert.ok(error instanceof Error);
    assert.equal(
      error.message,
      "live fixture retained course generation policy mismatch",
    );
    assert.equal(error.message.includes(ADMIN_TOKEN), false);
    return true;
  };

  let mismatchedPutCallIndex = 0;
  const mismatchedPutRequest = new FakeRequest((call) => {
    assert.equal(call.method, expectedMethods[mismatchedPutCallIndex]);
    assert.equal(call.url, path);
    assert.equal(header(call, "Authorization"), `Bearer ${ADMIN_TOKEN}`);
    if (call.method === "PUT") {
      assert.deepEqual(data(call), policy);
      mismatchedPutCallIndex += 1;
      return response(200, {
        ...retainedPolicy,
        tenantId: `${course.tenantId}:${ADMIN_TOKEN}`,
      });
    }
    assert.equal(call.options?.data, undefined);
    mismatchedPutCallIndex += 1;
    return response(200, retainedPolicy);
  });

  await assert.rejects(
    ensurePolicy(
      makeContext(mismatchedPutRequest, { evidence: "student_micro_flow" }),
      course,
    ),
    isStaticPolicyMismatch,
  );
  assert.equal(mismatchedPutCallIndex, 1);
  assert.deepEqual(
    mismatchedPutRequest.calls.map((call) => call.method),
    ["PUT"],
  );

  let driftedGetCallIndex = 0;
  const driftedGetRequest = new FakeRequest((call) => {
    assert.equal(call.method, expectedMethods[driftedGetCallIndex]);
    assert.equal(call.url, path);
    assert.equal(header(call, "Authorization"), `Bearer ${ADMIN_TOKEN}`);
    if (call.method === "PUT") {
      assert.deepEqual(data(call), policy);
      driftedGetCallIndex += 1;
      return response(200, retainedPolicy);
    }
    assert.equal(call.options?.data, undefined);
    driftedGetCallIndex += 1;
    return response(200, {
      ...retainedPolicy,
      updatedAt: "2026-08-26T08:00:01Z",
    });
  });

  await assert.rejects(
    ensurePolicy(
      makeContext(driftedGetRequest, { evidence: "student_micro_flow" }),
      course,
    ),
    isStaticPolicyMismatch,
  );
  assert.equal(driftedGetCallIndex, 2);
  assert.deepEqual(
    driftedGetRequest.calls.map((call) => call.method),
    ["PUT", "GET"],
  );
});

test("retained conflicts select exact deterministic records instead of the first list item", async () => {
  const tenant = tenantRecord();
  let expectedSource: ReturnType<typeof sourceFromUpload> | undefined;
  let courseId = "";
  let courseTitle = "";
  let classId = "";
  let className = "";
  const request = new FakeRequest((call) => {
    if (call.url === "/api/v1/auth/users") {
      return response(201, userCreated(call, "u-owned-student"));
    }
    if (call.url === "/api/v1/teaching/courses" && call.method === "POST") {
      courseId = String(data(call).id);
      courseTitle = String(data(call).title);
      return response(409, {});
    }
    if (call.url === "/api/v1/teaching/courses" && call.method === "GET") {
      return response(200, {
        items: [
          { id: "decoy", title: courseTitle, status: "active" },
          { id: courseId, title: courseTitle, status: "active" },
        ],
      });
    }
    if (call.url.endsWith("/classes") && call.method === "POST") {
      classId = String(data(call).id);
      className = String(data(call).name);
      return response(409, {});
    }
    if (call.url.endsWith("/classes") && call.method === "GET") {
      return response(200, {
        items: [
          { id: "decoy", courseId, name: className, status: "active" },
          { id: classId, courseId, name: className, status: "active" },
        ],
      });
    }
    if (call.url === "/api/v1/teaching/sources/pdf") {
      expectedSource = sourceFromUpload(call, tenant.tenantId);
      return response(409, {});
    }
    if (call.url === "/api/v1/teaching/sources") {
      assert.ok(expectedSource);
      return response(200, {
        items: [
          { ...expectedSource, bindingId: "source-binding-decoy" },
          expectedSource,
        ],
      });
    }
    if (call.url.endsWith("/enrollments") && call.method === "POST") {
      return response(409, {});
    }
    if (call.url.endsWith("/enrollments") && call.method === "GET") {
      return response(200, {
        items: [
          { classId, userId: "u-decoy", status: "active" },
          { classId, userId: "u-owned-student", status: "active" },
        ],
      });
    }
    return response(500, {});
  });
  const context = makeContext(request, { evidence: "student_full_flow" });
  const student = await ensureLiveUser(context, "student");

  const catalog = await ensureLiveCatalog(context, tenant, {
    controlledSource: true,
    enroll: [student],
  });

  assert.equal(catalog.course.id, courseId);
  assert.equal(catalog.teachingClass.id, classId);
  assert.equal(catalog.source?.bindingId, expectedSource?.bindingId);
  assert.equal(catalog.enrollments[0].userId, "u-owned-student");
});

const mismatches = ["course", "class", "source", "enrollment"] as const;

for (const mismatch of mismatches) {
  test(`a retained ${mismatch} mismatch fails closed`, async () => {
    const tenant = tenantRecord();
    let expectedSource: ReturnType<typeof sourceFromUpload> | undefined;
    let courseId = "";
    let courseTitle = "";
    let classId = "";
    let className = "";
    const request = new FakeRequest((call) => {
      if (call.url === "/api/v1/auth/users") {
        return response(201, userCreated(call, "u-student"));
      }
      if (call.url === "/api/v1/teaching/courses" && call.method === "POST") {
        courseId = String(data(call).id);
        courseTitle = String(data(call).title);
        if (mismatch === "course") return response(409, {});
        return response(201, { id: courseId, title: courseTitle, status: "active" });
      }
      if (call.url === "/api/v1/teaching/courses" && call.method === "GET") {
        return response(200, {
          items: [{ id: courseId, title: `${courseTitle}-wrong`, status: "active" }],
        });
      }
      if (call.url.endsWith("/classes") && call.method === "POST") {
        classId = String(data(call).id);
        className = String(data(call).name);
        if (mismatch === "class") return response(409, {});
        return response(201, { id: classId, courseId, name: className, status: "active" });
      }
      if (call.url.endsWith("/classes") && call.method === "GET") {
        return response(200, {
          items: [{ id: classId, courseId: "course-wrong", name: className, status: "active" }],
        });
      }
      if (call.url === "/api/v1/teaching/sources/pdf") {
        expectedSource = sourceFromUpload(call, tenant.tenantId);
        if (mismatch === "source") return response(409, {});
        return response(201, expectedSource);
      }
      if (call.url === "/api/v1/teaching/sources") {
        assert.ok(expectedSource);
        return response(200, {
          items: [{ ...expectedSource, sourceType: "knowledge_base" }],
        });
      }
      if (call.url.endsWith("/enrollments") && call.method === "POST") {
        if (mismatch === "enrollment") return response(409, {});
        return response(201, {
          classId,
          userId: "u-student",
          status: "active",
        });
      }
      if (call.url.endsWith("/enrollments") && call.method === "GET") {
        return response(200, {
          items: [{ classId, userId: "u-other", status: "active" }],
        });
      }
      return response(500, {});
    });
    const context = makeContext(request, { evidence: "student_micro_flow" });
    const student = await ensureLiveUser(context, "student");

    await assert.rejects(
      ensureLiveCatalog(context, tenant, {
        controlledSource: true,
        enroll: [student],
      }),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.match(error.message, new RegExp(mismatch, "i"));
        assert.equal(error.message.includes(ADMIN_TOKEN), false);
        return true;
      },
    );
  });
}

test("provisioning retains records and never sends delete or cleanup requests", async () => {
  const tenantId = "t-live-a";
  const request = new FakeRequest((call) => {
    if (call.url === "/api/v1/auth/users") {
      return response(201, userCreated(call));
    }
    if (call.url === "/api/v1/tenants") {
      return response(202, { tenant_id: tenantId, status: "active", job_id: "j-live-a" });
    }
    if (call.url === "/api/v1/tenants/active") {
      return response(200, { active_tenant_id: tenantId });
    }
    if (call.url.endsWith("/members")) {
      const body = data(call);
      const grants = body.grants as Array<Record<string, unknown>>;
      return response(200, {
        tenant_id: tenantId,
        user_id: body.user_id,
        roles: [grants[0].role],
        grants,
      });
    }
    return successfulCatalogWrite(call, tenantId) ?? response(500, {});
  });
  const context = makeContext(request, { evidence: "student_micro_flow" });

  const fixture = await provisionLiveFixture(context, {
    roles: ["teacher", "student"],
    catalog: { controlledSource: false, enrollRoles: ["student"] },
  });

  assert.equal(fixture.identities.length, 2);
  assert.equal(context.records.identities.length, 2);
  assert.equal(context.records.tenants.length, 1);
  assert.equal(context.records.courses.length, 1);
  assert.equal(context.records.classes.length, 1);
  assert.equal(context.records.enrollments.length, 1);
  assert.equal(
    request.calls.some(
      (call) => /delete|cleanup/i.test(call.url) || call.method === ("DELETE" as Call["method"]),
    ),
    false,
  );
});

test("live fixture pins every API request to a relative no-redirect transport", async () => {
  const tenantId = "https://attacker.invalid//tenant";
  const course = {
    tenantId,
    id: "https://attacker.invalid//course",
  };
  const retainedPolicy = {
    tenantId,
    courseId: course.id,
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
    updatedBy: "platform-admin-live",
    updatedAt: "2026-08-26T08:00:00Z",
  } as const;
  const request = new FakeRequest((call) => {
    if (call.url === "/api/v1/auth/users") {
      return response(201, userCreated(call, "u-teacher"));
    }
    if (call.url === "/api/v1/tenants") {
      return response(202, {
        tenant_id: tenantId,
        status: "active",
        job_id: "j-live-transport",
      });
    }
    if (call.url === "/api/v1/tenants/active") {
      return response(200, { active_tenant_id: tenantId });
    }
    if (call.url.endsWith("/members")) {
      const body = data(call);
      const grants = body.grants as Array<Record<string, unknown>>;
      return response(200, {
        tenant_id: tenantId,
        user_id: body.user_id,
        roles: [grants[0].role],
        grants,
      });
    }
    if (call.url.endsWith("/generation-policy")) {
      return response(200, retainedPolicy);
    }
    return response(500, {});
  });
  const context = makeContext(request, { evidence: "student_micro_flow" });

  const teacher = await ensureLiveUser(context, "teacher");
  const tenant = await ensureLiveTenant(context);
  await ensureLiveTenantMembership(context, tenant, teacher);
  await ensureLiveCourseGenerationPolicy(context, course);

  assert.equal(request.calls.length, 6);
  for (const call of request.calls) {
    assert.equal(call.url.startsWith("/") && !call.url.startsWith("//"), true);
    assert.equal(
      (call.options as LiveApiRequestOptions & { maxRedirects?: unknown })
        ?.maxRedirects,
      0,
      `${call.method} ${call.url} did not disable redirects`,
    );
  }
});

test("live API transport rejects non-relative paths before invoking the request context", async () => {
  const guardedRequest = guardedLiveApiRequestWithOrigin();
  const request = new FakeRequest(() => response(200, {}));

  for (const path of [
    "https://attacker.invalid/api/v1/escape",
    "//attacker.invalid/api/v1/escape",
    "api/v1/missing-leading-slash",
  ]) {
    await assert.rejects(
      guardedRequest(
        request,
        "transport contract",
        "get",
        path,
        CANONICAL_HTTPS_ORIGIN,
      ),
      /relative API path/i,
    );
  }
  assert.equal(request.calls.length, 0);
});

test("live API transport binds normalized requests and responses to one canonical HTTPS origin", async () => {
  const guardedRequest = guardedLiveApiRequestWithOrigin();
  const dynamicId = encodeURIComponent(
    "course/https://attacker.invalid/lesson?draft=true",
  );
  const path = `/api/v1/teaching/courses/${dynamicId}/generation-policy`;
  const expectedUrl = new URL(path, CANONICAL_HTTPS_ORIGIN).href;
  const request = new FakeRequest(() =>
    response(200, { ok: true }, expectedUrl),
  );

  const result = await guardedRequest(
    request,
    "encoded course read",
    "get",
    path,
    CANONICAL_HTTPS_ORIGIN,
    { headers: { Authorization: `Bearer ${ADMIN_TOKEN}` } },
  );

  assert.equal(result.status(), 200);
  assert.equal(request.calls.length, 1);
  assert.equal(request.calls[0].requestUrl, expectedUrl);
  assert.equal(request.calls[0].url, path);
  assert.equal(request.calls[0].options?.maxRedirects, 0);
});

test("live API transport rejects ambiguous origins paths and fragments before request dispatch", async () => {
  const guardedRequest = guardedLiveApiRequestWithOrigin();
  const cases = [
    {
      origin: "http://candidate.example.test",
      path: "/api/v1/tenants",
    },
    {
      origin: "https://user:password@candidate.example.test",
      path: "/api/v1/tenants",
    },
    {
      origin: `${CANONICAL_HTTPS_ORIGIN}/api`,
      path: "/api/v1/tenants",
    },
    {
      origin: `${CANONICAL_HTTPS_ORIGIN}#release-fragment`,
      path: "/api/v1/tenants",
    },
    {
      origin: CANONICAL_HTTPS_ORIGIN,
      path: "/\\attacker.invalid/api/v1/escape",
    },
    {
      origin: CANONICAL_HTTPS_ORIGIN,
      path: "/api/v1/tenants\t/active",
    },
    {
      origin: CANONICAL_HTTPS_ORIGIN,
      path: "/api/v1/tenants\r\n/active",
    },
    {
      origin: CANONICAL_HTTPS_ORIGIN,
      path: "/api/v1/tenants#active",
    },
  ] as const;

  for (const candidate of cases) {
    const request = new FakeRequest(() => response(200, {}));
    await assert.rejects(
      guardedRequest(
        request,
        "transport normalization",
        "get",
        candidate.path,
        candidate.origin,
      ),
      Error,
    );
    assert.equal(
      request.calls.length,
      0,
      `${candidate.origin} ${JSON.stringify(candidate.path)} was dispatched`,
    );
  }
});

test("live API transport rejects cross-origin successful responses before body access", async () => {
  const guardedRequest = guardedLiveApiRequestWithOrigin();
  let bodyReads = 0;
  const request = new FakeRequest(() => ({
    status: () => 200,
    url: () => "https://attacker.example.test/api/v1/tenants",
    async json() {
      bodyReads += 1;
      return { detail: ADMIN_TOKEN };
    },
  }));

  await assert.rejects(
    guardedRequest(
      request,
      "cross-origin response",
      "get",
      "/api/v1/tenants",
      CANONICAL_HTTPS_ORIGIN,
    ),
    Error,
  );
  assert.equal(request.calls.length, 1);
  assert.equal(bodyReads, 0);
});

test("live fixture rejects every 3xx API response before reading its body", async () => {
  for (let status = 300; status <= 399; status += 1) {
    let responseReads = 0;
    const request = new FakeRequest(() => ({
      status: () => status,
      url: () => `${CANONICAL_HTTPS_ORIGIN}/api/v1/auth/users`,
      async json() {
        responseReads += 1;
        return { detail: ADMIN_TOKEN };
      },
    }));

    await assert.rejects(
      ensureLiveUser(makeContext(request), "teacher"),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.match(error.message, /redirect/i);
        assert.equal(error.message.includes(ADMIN_TOKEN), false);
        return true;
      },
    );
    assert.equal(responseReads, 0, `HTTP ${status} response body was read`);
  }
});

test("browser login follows the React onSubmit fetch without native form attributes", async () => {
  const request = new FakeRequest((call) =>
    response(201, userCreated(call, "u-teacher")),
  );
  const context = makeContext(request);
  const identity = await ensureLiveUser(context, "teacher");
  const login = fakeLoginPage();

  await (
    loginLiveIdentity as unknown as (
      candidatePage: typeof login.page,
      identity: LiveIdentity,
      nextPath: string,
      expectedOrigin: string,
    ) => Promise<void>
  )(
    login.page,
    identity,
    "/teaching/classrooms/new",
    CANONICAL_HTTPS_ORIGIN,
  );

  assert.equal(login.filled["#username"], identity.username);
  assert.notEqual(login.filled["#password"], "");
  assert.equal(
    JSON.stringify(identity).includes(login.filled["#password"]),
    false,
  );
  assert.ok(
    login.actions.indexOf("form:count") <
      login.actions.indexOf("fill:#username"),
  );
  assert.equal(login.observedSubmissions(), 1);
  assert.equal(login.ignoredSubmissions(), 0);
  assert.ok(
    login.actions.indexOf("waitForRequest") <
      login.actions.indexOf('click:button[type="submit"]'),
  );
});

test("browser login waits for a delayed form before completing the React fetch login", async () => {
  const request = new FakeRequest((call) =>
    response(201, userCreated(call, "u-teacher")),
  );
  const identity = await ensureLiveUser(makeContext(request), "teacher");
  const login = fakeLoginPage({ formCount: 0, formCountAfterWait: 1 });

  await (
    loginLiveIdentity as unknown as (
      page: typeof login.page,
      identity: LiveIdentity,
      nextPath: string,
      expectedOrigin: string,
    ) => Promise<void>
  )(login.page, identity, "/home", CANONICAL_HTTPS_ORIGIN);

  assert.ok(login.actions.includes("form:wait:visible:10000"));
  assert.ok(
    login.actions.indexOf("form:visible") <
      login.actions.indexOf("fill:#username"),
  );
  assert.equal(login.credentialWrites(), 2);
  assert.equal(login.filled["#username"], identity.username);
  assert.equal(login.observedSubmissions(), 1);
  assert.equal(login.page.url(), `${CANONICAL_HTTPS_ORIGIN}/home`);
});

test("browser login fails closed when waiting for the form times out", async () => {
  const request = new FakeRequest((call) =>
    response(201, userCreated(call, "u-teacher")),
  );
  const identity = await ensureLiveUser(makeContext(request), "teacher");
  const login = fakeLoginPage({ formCount: 0 });

  await assert.rejects(
    (
      loginLiveIdentity as unknown as (
        page: typeof login.page,
        identity: LiveIdentity,
        nextPath: string,
        expectedOrigin: string,
      ) => Promise<void>
    )(login.page, identity, "/home", CANONICAL_HTTPS_ORIGIN),
    /browser login form is invalid/,
  );

  assert.ok(login.actions.includes("form:wait:visible:10000"));
  assert.ok(login.actions.includes("form:wait:timeout"));
  assert.equal(login.credentialWrites(), 0);
  assert.equal(login.observedSubmissions(), 0);
  assert.equal(login.actions.includes("waitForRequest"), false);
});

test("browser login rejects an origin change during the form wait before credential entry", async () => {
  const request = new FakeRequest((call) =>
    response(201, userCreated(call, "u-teacher")),
  );
  const identity = await ensureLiveUser(makeContext(request), "teacher");
  const login = fakeLoginPage({
    formCount: 0,
    formCountAfterWait: 1,
    urlAfterFormWait: "https://attacker.example.test/login",
  });

  await assert.rejects(
    (
      loginLiveIdentity as unknown as (
        page: typeof login.page,
        identity: LiveIdentity,
        nextPath: string,
        expectedOrigin: string,
      ) => Promise<void>
    )(login.page, identity, "/home", CANONICAL_HTTPS_ORIGIN),
    /HTTPS origin/i,
  );

  assert.ok(login.actions.includes("form:wait:visible:10000"));
  assert.ok(login.actions.includes("form:visible"));
  assert.equal(login.credentialWrites(), 0);
  assert.equal(login.observedSubmissions(), 0);
  assert.equal(login.actions.includes("waitForRequest"), false);
});

test("browser login rejects non-HTTPS or cross-origin pages before filling credentials", async () => {
  const request = new FakeRequest((call) =>
    response(201, userCreated(call, "u-teacher")),
  );
  const identity = await ensureLiveUser(makeContext(request), "teacher");
  const secureLogin = loginLiveIdentity as unknown as (
    page: {
      url(): string;
      goto(url: string): Promise<unknown>;
      locator(selector: string): {
        fill(value: string): Promise<unknown>;
        click(): Promise<unknown>;
      };
      waitForURL(url: string): Promise<unknown>;
    },
    identity: LiveIdentity,
    nextPath: string,
    expectedOrigin: string,
  ) => Promise<void>;

  for (const currentUrl of [
    "http://candidate.example.test/login",
    "https://attacker.example.test/login",
  ]) {
    let credentialWrites = 0;
    const page = {
      url: () => currentUrl,
      async goto(_url: string) {},
      locator(_selector: string) {
        return {
          async fill(_value: string) {
            credentialWrites += 1;
          },
          async click() {},
        };
      },
      async waitForURL(_url: string) {},
    };

    await assert.rejects(
      secureLogin(page, identity, "/", "https://candidate.example.test"),
      /HTTPS origin/i,
    );
    assert.equal(credentialWrites, 0, currentUrl);
  }
});

test("browser login rejects missing or ambiguous form surfaces before credential entry", async () => {
  const request = new FakeRequest((call) =>
    response(201, userCreated(call, "u-teacher")),
  );
  const identity = await ensureLiveUser(makeContext(request), "teacher");
  const secureLogin = loginLiveIdentity as unknown as (
    page: ReturnType<typeof fakeLoginPage>["page"],
    identity: LiveIdentity,
    nextPath: string,
    expectedOrigin: string,
  ) => Promise<void>;
  const cases = [{ formCount: 0 }, { formCount: 2 }] as const;

  for (const options of cases) {
    const login = fakeLoginPage(options);
    await assert.rejects(
      secureLogin(login.page, identity, "/", CANONICAL_HTTPS_ORIGIN),
      Error,
    );
    assert.equal(login.credentialWrites(), 0, JSON.stringify(options));
    assert.equal(login.observedSubmissions(), 0, JSON.stringify(options));
    assert.equal(login.ignoredSubmissions(), 0, JSON.stringify(options));
  }
});

test("browser login rejects untrusted submission evidence after the request wait times out", async () => {
  const request = new FakeRequest((call) =>
    response(201, userCreated(call, "u-teacher")),
  );
  const identity = await ensureLiveUser(makeContext(request), "teacher");
  const login = fakeLoginPage({
    submissionUrl: "https://attacker.example.test/capture-login",
  });

  await assert.rejects(
    (
      loginLiveIdentity as unknown as (
        page: typeof login.page,
        identity: LiveIdentity,
        nextPath: string,
        expectedOrigin: string,
      ) => Promise<void>
    )(login.page, identity, "/", CANONICAL_HTTPS_ORIGIN),
    Error,
  );

  assert.equal(login.credentialWrites(), 2);
  assert.equal(login.observedSubmissions(), 0);
  assert.equal(login.ignoredSubmissions(), 1);
  assert.ok(
    login.actions.indexOf("waitForRequest:ignored") <
      login.actions.indexOf("waitForRequest:timeout"),
  );
});

test("browser login rejects WHATWG-ambiguous next paths before navigation", async () => {
  const request = new FakeRequest((call) =>
    response(201, userCreated(call, "u-teacher")),
  );
  const identity = await ensureLiveUser(makeContext(request), "teacher");
  const secureLogin = loginLiveIdentity as unknown as (
    page: ReturnType<typeof fakeLoginPage>["page"],
    identity: LiveIdentity,
    nextPath: string,
    expectedOrigin: string,
  ) => Promise<void>;

  for (const nextPath of [
    "/\\attacker.example.test/escape",
    "/\t\\attacker.example.test/escape",
    "/\r\n\\attacker.example.test/escape",
    "/teaching/classrooms#credential-fragment",
  ]) {
    const login = fakeLoginPage();
    await assert.rejects(
      secureLogin(login.page, identity, nextPath, CANONICAL_HTTPS_ORIGIN),
      Error,
    );
    assert.equal(
      login.actions.some((action) => action.startsWith("goto:")),
      false,
    );
    assert.equal(login.credentialWrites(), 0);
  }
});

test("role-session GET transports disable redirects and validate response origins", () => {
  const source = readFileSync(
    "tests/e2e/support/classroom-first-release-live-flows.ts",
    "utf8",
  );
  const pollStart = source.indexOf("async function pollRecord(");
  const sameSessionStart = source.indexOf(
    "async function sameSessionGetRecord(",
  );
  const safeOptionStart = source.indexOf("function safeOptionValue(");
  assert.ok(
    pollStart >= 0 &&
      sameSessionStart > pollStart &&
      safeOptionStart > sameSessionStart,
  );

  const bodies = [
    ["pollRecord", source.slice(pollStart, sameSessionStart)],
    ["sameSessionGetRecord", source.slice(sameSessionStart, safeOptionStart)],
  ] as const;
  const contract = bodies.map(([name, body]) => ({
    name,
    disablesRedirects: /maxRedirects\s*:\s*0/.test(body),
    readsResponseUrl: /response\.url\(\)/.test(body),
    avoidsMutablePageOrigin: !/page\.url\(\)/.test(body),
    bindsExpectedOrigin: /expectedOrigin\s*:\s*string/.test(body),
    usesSharedOriginValidator:
      /sameOriginHttpsUrl\s*\(\s*response\.url\(\)\s*,\s*expectedOrigin\s*,/.test(
        body,
      ),
  }));

  assert.deepEqual(contract, [
    {
      name: "pollRecord",
      disablesRedirects: true,
      readsResponseUrl: true,
      avoidsMutablePageOrigin: true,
      bindsExpectedOrigin: true,
      usesSharedOriginValidator: true,
    },
    {
      name: "sameSessionGetRecord",
      disablesRedirects: true,
      readsResponseUrl: true,
      avoidsMutablePageOrigin: true,
      bindsExpectedOrigin: true,
      usesSharedOriginValidator: true,
    },
  ]);
});

test("live spec normalizes WEB_BASE_URL to an exact HTTPS origin and binds every browser login", () => {
  const spec = readFileSync(
    "tests/e2e/classroom-first-release.live.spec.ts",
    "utf8",
  );
  const flows = readFileSync(
    "tests/e2e/support/classroom-first-release-live-flows.ts",
    "utf8",
  );

  assert.match(spec, /parsed\.protocol\s*!==\s*["']https:["']/);
  assert.equal(spec.includes('["http:", "https:"]'), false);
  assert.match(
    spec,
    /function liveBaseUrl\(\): string \{[\s\S]*?const parsed = new URL\(value\);[\s\S]*?return parsed\.origin;/,
  );
  assert.match(spec, /loginLiveIdentity\(page, identity, "\/home", baseUrl\)/);
  assert.match(flows, /loginLiveIdentity\(page, identity, "\/home", baseUrl\)/);
});

test("live browser evidence binds every response and route landmark to the candidate origin", () => {
  const spec = readFileSync(
    "tests/e2e/classroom-first-release.live.spec.ts",
    "utf8",
  );
  const flows = readFileSync(
    "tests/e2e/support/classroom-first-release-live-flows.ts",
    "utf8",
  );

  for (const source of [spec, flows]) {
    assert.match(
      source,
      /sameOriginHttpsUrl\s*\(\s*response\.url\(\)\s*,\s*expectedOrigin\s*,/,
    );
    assert.equal(source.includes("new URL(response.url()).pathname"), false);
  }
  assert.match(spec, /expect\(currentUrl\.origin\)\.toBe\(expectedOrigin\)/);
});

test("live sources observe the real login submission without request interception", () => {
  const liveSources = [
    [
      "spec",
      readFileSync("tests/e2e/classroom-first-release.live.spec.ts", "utf8"),
    ],
    [
      "fixture",
      readFileSync(
        "tests/e2e/support/classroom-first-release-live-fixture.ts",
        "utf8",
      ),
    ],
    [
      "flows",
      readFileSync(
        "tests/e2e/support/classroom-first-release-live-flows.ts",
        "utf8",
      ),
    ],
  ] as const;
  const fixture = liveSources.find(([name]) => name === "fixture")?.[1] ?? "";
  const loginStart = fixture.indexOf("export async function loginLiveIdentity(");
  assert.ok(loginStart >= 0, "live login helper is missing");
  const login = fixture.slice(loginStart);

  assert.deepEqual(
    {
      observesSubmission: /page\.waitForRequest\(/.test(login),
      validatesHttpsOrigin:
        /sameOriginHttpsUrl\([\s\S]*?["']login submission["']/.test(login),
      validatesLoginEndpoint: /["']\/api\/v1\/auth\/login["']/.test(login),
      validatesPost: /method\s*===\s*["']POST["']/.test(login),
      validatesRedirectBoundary:
        /page\.waitForURL\(destination\.href\)/.test(login) &&
        /sameOriginHttpsUrl\([\s\S]*?["']browser login destination["']/.test(login),
    },
    {
      observesSubmission: true,
      validatesHttpsOrigin: true,
      validatesLoginEndpoint: true,
      validatesPost: true,
      validatesRedirectBoundary: true,
    },
  );

  const forbidden = [
    [".route(", /\.\s*route\s*\(/],
    [".unroute(", /\.\s*unroute\s*\(/],
    ["route.fulfill(", /\broute\s*\.\s*fulfill\s*\(/],
    ["route.continue(", /\broute\s*\.\s*continue\s*\(/],
    ["route.fallback(", /\broute\s*\.\s*fallback\s*\(/],
    ["route.abort(", /\broute\s*\.\s*abort\s*\(/],
    [".routeFromHAR(", /\.\s*routeFromHAR\s*\(/],
  ] as const;
  const violations = liveSources.flatMap(([name, source]) =>
    forbidden
      .filter(([, pattern]) => pattern.test(source))
      .map(([operation]) => `${name}:${operation}`),
  );

  assert.deepEqual(
    violations,
    [],
    "live sources must observe real responses or requests without browser/context interception",
  );
});

test("live fixture keeps resource-specific API setup explicit", () => {
  const source = readFileSync(
    "tests/e2e/support/classroom-first-release-live-fixture.ts",
    "utf8",
  );

  assert.deepEqual(
    {
      genericOptionsType: source.includes("type CreateOrReadOptions<T>"),
      genericCreateOrRead: source.includes("async function createOrRead<T>"),
    },
    { genericOptionsType: false, genericCreateOrRead: false },
  );
});

test("live spec consumes the fixed candidate digest without rereading the mutable lock", () => {
  const source = readFileSync(
    "tests/e2e/classroom-first-release.live.spec.ts",
    "utf8",
  );

  assert.equal(source.includes("YFEISTAI_CANDIDATE_SHA256"), true);
  assert.equal(source.includes("YFEISTAI_CANDIDATE_ROOT"), false);
  assert.equal(source.includes("image-lock.json"), false);
});

test("malformed API errors stay static and redact the admin token and password", async () => {
  let capturedPassword = "";
  const request = new FakeRequest((call) => {
    capturedPassword = String(data(call).password);
    return response(201, new Error(`${ADMIN_TOKEN}:${capturedPassword}`));
  });

  await assert.rejects(
    ensureLiveUser(makeContext(request), "reviewer"),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.message.includes(ADMIN_TOKEN), false);
      assert.equal(error.message.includes(capturedPassword), false);
      assert.match(error.message, /response/i);
      return true;
    },
  );
});

test("bounded student poller requires awaiting_confirmation before completing a full flow", async () => {
  const poll = studentPoller();
  const skippedConfirmation = fakeSequence([
    classroomState({
      mode: "full",
      status: "succeeded",
      classroomVersionId: "version-too-early",
    }),
  ]);
  let confirmations = 0;

  await assert.rejects(
    poll(
      studentPollOptions({
        expected: {
          assetId: "asset-student-live",
          generationJobId: "job-student-live",
          courseId: "course-student-live",
          classId: "class-student-live",
          mode: "full",
          ownerId: "student-live",
        },
        readClassroom: skippedConfirmation.read,
        readGenerationJob: async () =>
          generationJobState({ status: "succeeded", progressPercent: 100 }),
        onAwaitingConfirmation: async () => {
          confirmations += 1;
        },
      }),
    ),
    /awaiting_confirmation/i,
  );
  assert.equal(confirmations, 0);

  const unauthorizedDrift = fakeSequence([
    classroomState({ mode: "full", status: "awaiting_confirmation" }),
    classroomState({
      generationJobId: "job-student-content-drifted",
      mode: "full",
      status: "generating",
    }),
  ]);
  const unauthorizedJobReads: string[] = [];
  await assert.rejects(
    poll(
      studentPollOptions({
        expected: {
          assetId: "asset-student-live",
          generationJobId: "job-student-live",
          courseId: "course-student-live",
          classId: "class-student-live",
          mode: "full",
          ownerId: "student-live",
        },
        readClassroom: unauthorizedDrift.read,
        readGenerationJob: async (jobId) => {
          unauthorizedJobReads.push(jobId);
          return generationJobState({
            jobId,
            phase: "outline",
            status: "awaiting_confirmation",
            progressPercent: 100,
          });
        },
        onAwaitingConfirmation: async () => {
          confirmations += 1;
        },
      }),
    ),
    /job|binding/i,
  );
  assert.equal(confirmations, 1);
  assert.deepEqual(unauthorizedJobReads, ["job-student-live"]);
  confirmations = 0;

  await assert.rejects(
    poll(
      studentPollOptions({
        expected: {
          assetId: "asset-student-live",
          generationJobId: "job-student-live",
          courseId: "course-student-live",
          classId: "class-student-live",
          mode: "full",
          ownerId: "student-live",
        },
        readClassroom: async () =>
          classroomState({ mode: "full", status: "awaiting_confirmation" }),
        readGenerationJob: async () =>
          generationJobState({ phase: "content", status: "generating_content" }),
        onAwaitingConfirmation: async () => {
          confirmations += 1;
        },
      }),
    ),
    /phase|generation job/i,
  );
  assert.equal(confirmations, 0);
  confirmations = 0;

  const stalledAfterConfirmation = fakeSequence([
    classroomState({ mode: "full", status: "awaiting_confirmation" }),
    classroomState({ mode: "full", status: "generating" }),
  ]);
  await assert.rejects(
    poll(
      studentPollOptions({
        expected: {
          assetId: "asset-student-live",
          generationJobId: "job-student-live",
          courseId: "course-student-live",
          classId: "class-student-live",
          mode: "full",
          ownerId: "student-live",
        },
        pollAttempts: 2,
        readClassroom: stalledAfterConfirmation.read,
        readGenerationJob: async () =>
          generationJobState({
            phase: "outline",
            status: "awaiting_confirmation",
            progressPercent: 100,
          }),
        onAwaitingConfirmation: async () => {
          confirmations += 1;
        },
      }),
    ),
    /phase|generation job/i,
  );
  assert.equal(confirmations, 1);
  confirmations = 0;

  const finalClassroom = classroomState({
    generationJobId: "job-student-live",
    mode: "full",
    status: "succeeded",
    classroomVersionId: "version-full-live",
  });
  const finalJob = generationJobState({
    jobId: "job-student-live",
    phase: "content",
    status: "succeeded",
    progressPercent: 100,
  });
  const classrooms = fakeSequence([
    classroomState({ mode: "full", status: "awaiting_confirmation" }),
    classroomState({
      generationJobId: "job-student-live",
      mode: "full",
      status: "generating",
    }),
    finalClassroom,
  ]);
  const jobs = fakeSequence([
    generationJobState({
      jobId: "job-student-live",
      phase: "outline",
      status: "awaiting_confirmation",
      progressPercent: 100,
    }),
    generationJobState({
      jobId: "job-student-live",
      phase: "content",
      progressPercent: 80,
    }),
    finalJob,
  ]);

  const result = await poll(
    studentPollOptions({
      expected: {
        assetId: "asset-student-live",
        generationJobId: "job-student-live",
        courseId: "course-student-live",
        classId: "class-student-live",
        mode: "full",
        ownerId: "student-live",
      },
      readClassroom: classrooms.read,
      readGenerationJob: async (jobId) => {
        assert.equal(jobId, "job-student-live");
        return jobs.read();
      },
      onAwaitingConfirmation: async (classroom) => {
        assert.equal(classroom.status, "awaiting_confirmation");
        assert.equal(classroom.classroomVersionId, null);
        assert.equal(classroom.generationJobId, "job-student-live");
        confirmations += 1;
      },
    }),
  );

  assert.equal(confirmations, 1);
  assert.deepEqual(result, {
    classroom: finalClassroom,
    generationJob: finalJob,
  });
});

test("bounded student poller rejects a pre-seeded immutable version", async () => {
  assert.equal(POLLER_OPTIONS_REJECT_CALLER_VERSION_ID, true);

  await assert.rejects(
    studentPoller()(
      studentPollOptions({
        expected: {
          assetId: "asset-student-live",
          generationJobId: "job-student-live",
          courseId: "course-student-live",
          classId: "class-student-live",
          mode: "full",
          ownerId: "student-live",
        },
        readClassroom: async () =>
          classroomState({
            mode: "full",
            status: "awaiting_confirmation",
            classroomVersionId: "version-before-confirmation",
          }),
        onAwaitingConfirmation: async () => undefined,
      }),
    ),
    /version/i,
  );
});

test("bounded student poller redacts timeout diagnostics", async () => {
  const secret = `${ADMIN_TOKEN}:student-password-never-log`;
  const classrooms = fakeSequence([
    { ...classroomState(), diagnostic: secret },
  ]);
  const jobs = fakeSequence([
    { ...generationJobState(), diagnostic: secret },
  ]);
  let pauses = 0;

  await assert.rejects(
    studentPoller()(
      studentPollOptions({
        pollAttempts: 2,
        readClassroom: classrooms.read,
        readGenerationJob: jobs.read,
        pause: async () => {
          pauses += 1;
        },
      }),
    ),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.message, "live student classroom completion timed out");
      assert.equal(error.message.includes(secret), false);
      return true;
    },
  );
  assert.equal(classrooms.calls(), 2);
  assert.equal(jobs.calls(), 2);
  assert.equal(pauses, 1);

  const assertStaticFailure =
    (message: string) =>
    (error: unknown): boolean => {
      assert.ok(error instanceof Error);
      assert.equal(error.message, message);
      assert.equal(error.message.includes(secret), false);
      return true;
    };
  await assert.rejects(
    studentPoller()(
      studentPollOptions({
        readClassroom: async () => {
          throw new Error(secret);
        },
      }),
    ),
    assertStaticFailure("live student classroom synchronization failed"),
  );
  await assert.rejects(
    studentPoller()(
      studentPollOptions({
        readGenerationJob: async () => {
          throw new Error(secret);
        },
      }),
    ),
    assertStaticFailure(
      "live student classroom generation synchronization failed",
    ),
  );
  await assert.rejects(
    studentPoller()(
      studentPollOptions({
        expected: {
          ...studentPollOptions().expected,
          mode: "full",
        },
        readClassroom: async () =>
          classroomState({ mode: "full", status: "awaiting_confirmation" }),
        readGenerationJob: async () =>
          generationJobState({
            phase: "outline",
            status: "awaiting_confirmation",
            progressPercent: 100,
          }),
        onAwaitingConfirmation: async () => {
          throw new Error(secret);
        },
      }),
    ),
    assertStaticFailure("live student classroom confirmation failed"),
  );
});

test("bounded student poller returns only after the expected generation job and immutable version", async () => {
  const poll = studentPoller();
  const expected = studentPollOptions().expected;

  for (const mismatch of [
    { assetId: "other-asset" },
    { ownerId: "other-student" },
    { courseId: "other-course" },
    { classId: "other-class" },
    { mode: "full" as const },
    { generationJobId: "other-job" },
  ]) {
    await assert.rejects(
      poll(
        studentPollOptions({
          readClassroom: async () =>
            classroomState({
              ...mismatch,
              status: "succeeded",
              classroomVersionId: "version-mismatched",
            }),
          readGenerationJob: async () =>
            generationJobState({ status: "succeeded", progressPercent: 100 }),
        }),
      ),
      /binding/i,
    );
  }

  await assert.rejects(
    poll(
      studentPollOptions({
        readClassroom: async () => classroomState({ status: "failed" }),
        readGenerationJob: async () => generationJobState({ status: "failed" }),
      }),
    ),
    /failed/i,
  );

  await assert.rejects(
    poll(
      studentPollOptions({
        readClassroom: async () =>
          classroomState({
            status: "succeeded",
            classroomVersionId: "version-wrong-job",
          }),
        readGenerationJob: async () =>
          generationJobState({
            jobId: "other-job",
            status: "succeeded",
            progressPercent: 100,
          }),
      }),
    ),
    /job|binding/i,
  );

  await assert.rejects(
    poll(
      studentPollOptions({
        readClassroom: async () =>
          classroomState({
            status: "succeeded",
            classroomVersionId: "version-from-non-generation-job",
          }),
        readGenerationJob: async () =>
          generationJobState({
            jobKind: "export",
            status: "succeeded",
            progressPercent: 100,
          }),
      }),
    ),
    /generation/i,
  );

  await assert.rejects(
    poll(
      studentPollOptions({
        readClassroom: async () =>
          classroomState({
            status: "succeeded",
            classroomVersionId: "version-from-outline-job",
          }),
        readGenerationJob: async () =>
          generationJobState({
            phase: "outline",
            status: "succeeded",
            progressPercent: 100,
          }),
      }),
    ),
    /phase|generation job/i,
  );

  for (const classroomVersionId of ["", "   "]) {
    await assert.rejects(
      poll(
        studentPollOptions({
          readClassroom: async () =>
            classroomState({ status: "succeeded", classroomVersionId }),
          readGenerationJob: async () =>
            generationJobState({ status: "succeeded", progressPercent: 100 }),
        }),
      ),
      /version/i,
    );
  }

  const finalClassroom = classroomState({
    status: "succeeded",
    classroomVersionId: "version-student-live",
  });
  const finalJob = generationJobState({
    status: "succeeded",
    progressPercent: 100,
  });
  const classrooms = fakeSequence([finalClassroom, finalClassroom]);
  const jobs = fakeSequence([
    generationJobState({ progressPercent: 99 }),
    finalJob,
  ]);

  const result = await poll(
    studentPollOptions({
      expected,
      readClassroom: classrooms.read,
      readGenerationJob: async (jobId) => {
        assert.equal(jobId, expected.generationJobId);
        return jobs.read();
      },
    }),
  );

  assert.deepEqual(result, {
    classroom: finalClassroom,
    generationJob: finalJob,
  });
  assert.ok(classrooms.calls() >= 2);
  assert.ok(jobs.calls() >= 2);
});
