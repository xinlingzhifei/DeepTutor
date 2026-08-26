import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
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
