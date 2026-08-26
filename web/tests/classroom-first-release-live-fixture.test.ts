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
  options: LiveApiRequestOptions | undefined;
};

class FakeResponse implements LiveApiResponse {
  constructor(
    private readonly statusCode: number,
    private readonly body: unknown,
  ) {}

  status(): number {
    return this.statusCode;
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
    ) => FakeResponse | Promise<FakeResponse>,
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
    const call = { method, url, options };
    this.calls.push(call);
    return this.respond(call);
  }
}

function response(status: number, body: unknown): FakeResponse {
  return new FakeResponse(status, body);
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
    provisioningPollAttempts?: number;
    pause?: (milliseconds: number) => Promise<void>;
  } = {},
): LiveFixtureContext {
  return new LiveFixtureContext({
    request,
    adminToken: ADMIN_TOKEN,
    releaseRun: options.releaseRun ?? "release-20260826-a",
    environment: options.environment ?? "candidate-prod-a",
    evidence: options.evidence ?? "teacher_flow",
    provisioningPollAttempts: options.provisioningPollAttempts,
    provisioningPollIntervalMs: 0,
    pause: options.pause ?? (async () => undefined),
  });
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

test("real login helper drives the /login UI through a fake Page without interception", async () => {
  const request = new FakeRequest((call) =>
    response(201, userCreated(call, "u-teacher")),
  );
  const context = makeContext(request);
  const identity = await ensureLiveUser(context, "teacher");
  const filled: Record<string, string> = {};
  const actions: string[] = [];
  const page = {
    async goto(url: string) {
      actions.push(`goto:${url}`);
    },
    locator(selector: string) {
      return {
        async fill(value: string) {
          filled[selector] = value;
        },
        async click() {
          actions.push(`click:${selector}`);
        },
      };
    },
    getByLabel(): never {
      throw new Error("locale-dependent getByLabel must not be used");
    },
    getByRole(): never {
      throw new Error("locale-dependent getByRole must not be used");
    },
    async waitForURL(url: string) {
      actions.push(`wait:${url}`);
    },
  };

  await loginLiveIdentity(page, identity, "/teaching/classrooms/new");

  assert.equal(filled["#username"], identity.username);
  assert.notEqual(filled["#password"], "");
  assert.equal(JSON.stringify(identity).includes(filled["#password"]), false);
  assert.deepEqual(actions, [
    "goto:/login?next=%2Fteaching%2Fclassrooms%2Fnew",
    "wait:/teaching/classrooms/new",
    'click:button[type="submit"]',
  ]);
  assert.equal("route" in page, false);
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
