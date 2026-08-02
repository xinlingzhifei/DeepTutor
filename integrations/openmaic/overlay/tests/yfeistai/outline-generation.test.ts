import { createHash } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  statSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { describe, expect, test, vi } from "vitest";

import {
  OUTLINE_BUNDLE_CONTRACT_SHA256,
  canonicalJson,
  createOutlineGetHandler,
  createOutlinePostHandler,
  generateOutlineJob,
  normalizeUpstreamOutlineBundle,
  validateGenerationRequest,
} from "../../lib/yfeistai/outline-generation";
import { runOutlineRouteAdapter } from "../../lib/yfeistai/generation-adapter";
import {
  durableFile,
  writeDurableJsonExclusive,
} from "../../lib/yfeistai/durable-state";
import { OutlineJobStore } from "../../lib/yfeistai/job-store";
import { signServiceRequest } from "../../lib/yfeistai/service-auth";
import type {
  GenerationRequest,
  OutlineBundle,
  SourceReference,
} from "../../lib/yfeistai/contracts";

const SHA256 = "a".repeat(64);
const GENERATED_AT = "2026-07-30T00:00:00.000Z";
const NOW_SECONDS = 1_800_000_000;
const SECRET = "outline-test-secret";

function validRequest(): GenerationRequest {
  return {
    schemaVersion: "1.0",
    tenantId: "tenant-a",
    requestId: "request-a",
    jobId: "job-a",
    idempotencyKey: "idem-a",
    phase: "outline",
    classroomMode: "full",
    teachingBriefId: "brief-a",
    teachingBriefSha256: SHA256,
    teachingBrief: {
      schemaVersion: "1.0",
      briefId: "brief-a",
      briefVersion: 1,
      tenantId: "tenant-a",
      courseId: "course-a",
      targetClassId: "class-a",
      gradeBand: "middle-school",
      audienceLevel: "introductory",
      classroomMode: "full",
      objectives: [
        {
          objectiveId: "objective-a",
          description: "理解并应用勾股定理",
          knowledgePointIds: ["kp-a"],
        },
      ],
      durationMinutes: 20,
      knowledgePoints: [
        {
          knowledgePointId: "kp-a",
          title: "勾股定理",
          description: "直角三角形三边关系",
        },
      ],
      prerequisites: [
        {
          knowledgePointId: "kp-a",
          prerequisiteKnowledgePointIds: ["kp-prerequisite"],
        },
      ],
      assessment: {
        methods: ["quiz"],
        successCriteria: ["能够计算未知边长"],
      },
      sourceSnapshot: null,
      sourceFragments: [],
      citations: [],
      sourceRefs: [],
      permissionSummary: {
        allowedSourceIds: [],
        allowedFragmentIds: [],
        usageScope: "classroom_generation",
        attributionRequired: false,
      },
      contentMode: "open_creation",
      networkPolicy: {
        allowWebAccess: false,
        allowedDomains: [],
      },
      mediaPolicy: {
        allowGeneration: false,
        allowedMimeTypes: [],
      },
      templatePolicy: {
        templateId: "template-a",
        templateVersion: "1",
      },
      safetyPolicy: {
        policyId: "school-default",
        blockedCategories: [],
      },
      contentSha256: SHA256,
    },
    confirmedOutline: null,
    confirmedOutlineSha256: null,
    templateId: "template-a",
    templateVersion: "1",
    sceneBudget: 4,
    durationMinutes: 20,
    requestedExports: ["classroom_zip"],
    callbackContext: "callback-a",
    dataPlaneRouteId: "shared-primary",
    priority: "teacher",
  };
}

function validOutlineBundle(sourceRefs: SourceReference[] = []): OutlineBundle {
  const sceneIds = Array.from(
    { length: 4 },
    (_, index) => `scene-${index + 1}`,
  );
  return {
    schemaVersion: "1.0",
    outlineId: "outline-job-a",
    outlineVersion: 1,
    confirmationMetadata: {
      status: "draft",
      confirmedAt: null,
      confirmedBy: null,
    },
    title: "勾股定理",
    language: "zh-CN",
    scenes: sceneIds.map((sceneId, index) => ({
      sceneId,
      title: `场景 ${index + 1}`,
      summary: `讲解 ${index + 1}`,
      knowledgePointIds: ["kp-a"],
      sourceRefs,
    })),
    knowledgeCoverage: [
      {
        knowledgePointId: "kp-a",
        sceneIds,
      },
    ],
    sourceRefs,
    estimatedSceneCount: 4,
    generationMetadata: {
      generator: "openmaic",
      generatorVersion: "0.3.1",
      modelId: "server-selected-model",
      generatedAt: GENERATED_AT,
      teachingBriefId: "brief-a",
      teachingBriefSha256: SHA256,
      templateId: "template-a",
      templateVersion: "1",
    },
    contractSha256: OUTLINE_BUNDLE_CONTRACT_SHA256,
  };
}

function groundedRequest(): GenerationRequest {
  const request = validRequest();
  const sourceRef = {
    citationId: "citation-a",
    sourceId: "source-a",
    fragmentId: "fragment-a",
  };
  return {
    ...request,
    teachingBrief: {
      ...request.teachingBrief,
      sourceSnapshot: {
        snapshotId: "snapshot-a",
        createdAt: GENERATED_AT,
        contentSha256: SHA256,
      },
      sourceFragments: [
        {
          fragmentId: "fragment-a",
          sourceId: "source-a",
          text: "勾股定理描述直角三角形三边关系。",
          contentSha256: SHA256,
        },
      ],
      citations: [
        {
          ...sourceRef,
          label: "教材第一章",
        },
      ],
      sourceRefs: [sourceRef],
      permissionSummary: {
        allowedSourceIds: ["source-a"],
        allowedFragmentIds: ["fragment-a"],
        usageScope: "classroom_generation",
        attributionRequired: true,
      },
      contentMode: "source_grounded",
    },
  };
}

function signedHttpRequest(options: {
  method: "GET" | "POST";
  path: string;
  body?: string;
  tenantId?: string;
  jobId?: string;
  idempotencyKey?: string;
  secret?: string;
  signedPath?: string;
}): Request {
  const body = options.body ?? "";
  const signed = signServiceRequest({
    secret: options.secret ?? SECRET,
    method: options.method,
    path: options.signedPath ?? options.path,
    tenantId: options.tenantId ?? "tenant-a",
    jobId: options.jobId ?? "job-a",
    timestamp: NOW_SECONDS,
    idempotencyKey: options.idempotencyKey,
    body,
  });
  const headers = new Headers({
    "x-yfeistai-tenant-id": signed.tenantId,
    "x-yfeistai-job-id": signed.jobId,
    "x-yfeistai-timestamp": String(signed.timestamp),
    "x-yfeistai-signature": signed.signature,
  });
  if (signed.idempotencyKey) {
    headers.set("x-yfeistai-idempotency-key", signed.idempotencyKey);
  }
  return new Request(`http://openmaic.internal${options.path}`, {
    method: options.method,
    headers,
    body: options.method === "POST" ? body : undefined,
  });
}

function handlerDependencies(store = new OutlineJobStore()) {
  const generateOutlines = vi.fn(async () => validOutlineBundle());
  return {
    store,
    generateOutlines,
    readSecret: () => SECRET,
    nowSeconds: () => NOW_SECONDS,
    now: () => new Date(GENERATED_AT),
  };
}

async function waitForOutlineTerminal(
  store: OutlineJobStore,
  tenantId = "tenant-a",
  jobId = "job-a",
) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const job = await store.read(tenantId, jobId);
    if (job && job.status !== "running") {
      return job;
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }
  throw new Error("outline job did not become terminal");
}

describe("outline-only generation boundary", () => {
  test.each([
    ["provider_429", { statusCode: 429 }, "lastError"],
    ["provider_5xx", { response: { status: 503 } }, "errors"],
    ["connect_timeout", { code: "UND_ERR_CONNECT_TIMEOUT" }, "lastErrorCause"],
    ["read_timeout", { code: "UND_ERR_HEADERS_TIMEOUT" }, "errorsCause"],
    ["read_timeout", { response: { status: 408 } }, "lastError"],
    ["read_timeout", { code: "ECONNRESET" }, "errorsCause"],
    ["read_timeout", { code: "UND_ERR_SOCKET" }, "lastErrorCause"],
  ] as const)(
    "classifies a swallowed AI RetryError as %s without leaking details",
    async (expectedCode, providerShape, placement) => {
      const leaked = `sensitive-outline-route-${expectedCode}`;
      const providerError = Object.assign(new Error(leaked), providerShape);
      const neutralError = new Error("retry attempt failed");
      const retryError = Object.assign(new Error(`retry failed: ${leaked}`), {
        name: "AI_RetryError",
        lastError: neutralError as unknown,
        errors: [] as unknown[],
      });
      if (placement === "lastError") {
        retryError.lastError = providerError;
        retryError.errors = [neutralError];
        providerError.cause = retryError;
      } else if (placement === "errors") {
        retryError.errors = [providerError];
        neutralError.cause = retryError;
      } else if (placement === "lastErrorCause") {
        retryError.lastError = Object.assign(new Error("last attempt"), {
          cause: providerError,
        });
        providerError.cause = retryError;
      } else {
        retryError.errors = [
          Object.assign(new Error("recorded attempt"), {
            cause: providerError,
          }),
        ];
        neutralError.cause = retryError;
      }

      let upstreamLogged = "";
      const result = await generateOutlineJob(validRequest(), {
        generateOutlines: async () =>
          runOutlineRouteAdapter({
            callProvider: async () => {
              throw retryError;
            },
            generate: async (callProvider) => {
              try {
                await callProvider("system", "user");
              } catch (error) {
                // The pinned upstream helper converts this to success=false.
                upstreamLogged = String(error);
              }
              return { success: false, data: null };
            },
          }),
        now: () => new Date(GENERATED_AT),
      });

      expect(result).toMatchObject({
        status: "failed",
        error: {
          code: expectedCode,
          message: expect.stringMatching(/^Provider/),
        },
      });
      expect(JSON.stringify(result)).not.toContain(leaked);
      expect(upstreamLogged).toContain("OpenMAIC provider request failed.");
      expect(upstreamLogged).not.toContain(leaked);
    },
  );

  test("keeps an outline route contract failure generic", async () => {
    const result = await generateOutlineJob(validRequest(), {
      generateOutlines: async () =>
        runOutlineRouteAdapter({
          callProvider: async () => "unused",
          generate: async () => ({ success: false, data: null }),
        }),
      now: () => new Date(GENERATED_AT),
    });

    expect(result).toMatchObject({
      status: "failed",
      error: {
        code: "OUTLINE_GENERATION_FAILED",
        message: "Outline generation failed.",
      },
    });
  });

  test("uses the frozen GenerationRequest and OutlineBundle field sets", () => {
    const generationSchema = JSON.parse(
      readFileSync(
        resolve(
          process.cwd(),
          "../../../contracts/classroom/generation-request.schema.json",
        ),
        "utf8",
      ),
    ) as { required: string[] };
    const outlineSchema = JSON.parse(
      readFileSync(
        resolve(
          process.cwd(),
          "../../../contracts/classroom/outline-bundle.schema.json",
        ),
        "utf8",
      ),
    ) as { required: string[] };

    expect(Object.keys(validRequest()).sort()).toEqual(
      [
        ...generationSchema.required,
        "confirmedOutline",
        "confirmedOutlineSha256",
      ].sort(),
    );
    expect(Object.keys(validOutlineBundle()).sort()).toEqual(
      [...outlineSchema.required].sort(),
    );
    expect(OUTLINE_BUNDLE_CONTRACT_SHA256).toBe(
      createHash("sha256")
        .update(
          readFileSync(
            resolve(
              process.cwd(),
              "../../../contracts/classroom/outline-bundle.schema.json",
            ),
          ),
        )
        .digest("hex"),
    );
  });

  test("outline job stops before scene generation", async () => {
    const generateScenes = vi.fn();
    const result = await generateOutlineJob(validRequest(), {
      generateOutlines: async () => validOutlineBundle(),
      generateScenes,
    });

    expect(result.status).toBe("succeeded");
    expect(result.result?.outline.scenes).toHaveLength(4);
    expect(generateScenes).not.toHaveBeenCalled();
  });

  test("source-grounded request keeps source refs", async () => {
    const request = groundedRequest();
    const bundle = validOutlineBundle(request.teachingBrief.sourceRefs);

    const result = await generateOutlineJob(request, {
      generateOutlines: async () => bundle,
    });

    expect(
      result.result?.outline.scenes.every(
        (scene) => scene.sourceRefs.length > 0,
      ),
    ).toBe(true);
    expect(result.result?.outline.sourceRefs).toEqual(
      request.teachingBrief.sourceRefs,
    );
  });

  test("rejects routing aliases anywhere in the signed contract", async () => {
    const request = structuredClone(validRequest()) as GenerationRequest &
      Record<string, unknown>;
    const forbiddenField = ["provider", "Id"].join("");
    (request.teachingBrief.networkPolicy as unknown as Record<string, unknown>)[
      forbiddenField
    ] = "client-selected";
    const generateOutlines = vi.fn();

    await expect(
      generateOutlineJob(request, { generateOutlines }),
    ).rejects.toThrow(/routing fields/i);
    expect(generateOutlines).not.toHaveBeenCalled();
  });

  test.each([
    [
      "source outside the allowlist",
      (request: GenerationRequest) => {
        request.teachingBrief.sourceFragments[0].sourceId = "source-b";
      },
    ],
    [
      "duplicate fragment identifier",
      (request: GenerationRequest) => {
        request.teachingBrief.sourceFragments.push({
          ...request.teachingBrief.sourceFragments[0],
        });
      },
    ],
    [
      "fragment allowlist mismatch",
      (request: GenerationRequest) => {
        request.teachingBrief.permissionSummary.allowedFragmentIds.push(
          "fragment-b",
        );
      },
    ],
    [
      "duplicate citation identifier",
      (request: GenerationRequest) => {
        request.teachingBrief.citations.push({
          ...request.teachingBrief.citations[0],
        });
      },
    ],
    [
      "citation-to-fragment mismatch",
      (request: GenerationRequest) => {
        request.teachingBrief.citations[0].sourceId = "source-b";
        request.teachingBrief.permissionSummary.allowedSourceIds.push(
          "source-b",
        );
      },
    ],
    [
      "duplicate source-reference triple",
      (request: GenerationRequest) => {
        request.teachingBrief.sourceRefs.push({
          ...request.teachingBrief.sourceRefs[0],
        });
      },
    ],
    [
      "source reference not matching its citation",
      (request: GenerationRequest) => {
        request.teachingBrief.sourceRefs[0].citationId = "citation-b";
      },
    ],
  ] as Array<[string, (request: GenerationRequest) => void]>)(
    "rejects source-grounded lineage with %s",
    (_caseName, mutate) => {
      const request = structuredClone(groundedRequest());
      mutate(request);

      expect(() => validateGenerationRequest(request)).toThrow(/source/i);
    },
  );

  test.each(["callbackContext", "dataPlaneRouteId"] as const)(
    "enforces the frozen opaque identifier pattern for %s",
    (field) => {
      const request = validRequest();
      request[field] = "invalid:value";

      expect(() => validateGenerationRequest(request)).toThrow(/opaque/i);
    },
  );

  test("fails closed on a wrong outline contract hash without leaking details", async () => {
    const outline = validOutlineBundle();
    outline.contractSha256 = "b".repeat(64);

    const result = await generateOutlineJob(validRequest(), {
      generateOutlines: async () => outline,
      now: () => new Date(GENERATED_AT),
    });

    expect(result).toMatchObject({
      status: "failed",
      error: {
        code: "OUTLINE_GENERATION_FAILED",
        message: "Outline generation failed.",
      },
    });
    expect(result).not.toHaveProperty("result");
  });
});

describe("outline service API security and idempotency", () => {
  test("persists a stable public model id without exposing provider routing", async () => {
    const root = mkdtempSync(join(tmpdir(), "openmaic-outline-public-model-"));
    const store = new OutlineJobStore(root);
    const providerRoute = "openai/gpt-4o-mini";
    const request = validRequest();
    const handler = createOutlinePostHandler({
      store,
      readSecret: () => SECRET,
      nowSeconds: () => NOW_SECONDS,
      now: () => new Date(GENERATED_AT),
      generateOutlines: async (boundRequest) =>
        normalizeUpstreamOutlineBundle(
          boundRequest,
          {
            languageDirective: "en-US",
            outlines: [
              {
                id: "scene-a",
                title: "Public scene",
                description: "Provider-neutral outline.",
              },
            ],
          },
          { modelId: providerRoute, generatedAt: GENERATED_AT },
        ),
    });

    const response = await handler(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: JSON.stringify(request),
        idempotencyKey: request.idempotencyKey,
      }),
    );
    const submitted = await response.json();
    const terminal =
      submitted.status === "running"
        ? await waitForOutlineTerminal(store)
        : submitted;

    expect(terminal.result.outline.generationMetadata).toMatchObject({
      modelId: "server-selected-model",
      teachingBriefId: request.teachingBriefId,
      teachingBriefSha256: request.teachingBriefSha256,
      templateId: request.templateId,
      templateVersion: request.templateVersion,
    });
    expect(JSON.stringify(terminal)).not.toContain(providerRoute);
    const restarted = new OutlineJobStore(root);
    const persisted = await restarted.read(request.tenantId, request.jobId);
    expect(persisted).toEqual(terminal);
    expect(JSON.stringify(persisted)).not.toContain(providerRoute);
  });

  test("keeps legacy outline submission timestamps stable across restarts", async () => {
    const root = mkdtempSync(join(tmpdir(), "openmaic-outline-legacy-time-"));
    const tenantId = "tenant-legacy";
    const jobId = "job-legacy";
    const submissionPath = durableFile(
      root,
      "outline-jobs",
      "jobs",
      [tenantId, jobId],
      "submission.json",
    );
    writeDurableJsonExclusive(submissionPath, {
      version: 1,
      tenantId,
      jobId,
      idempotencyKey: "idem-legacy",
      action: "outline",
      bodySha256: createHash("sha256").update("{}", "utf8").digest("hex"),
    });
    const expectedCreatedAt = new Date(statSync(submissionPath).mtimeMs).toISOString();

    const first = await new OutlineJobStore(root, 60_000, () => 1_000).read(
      tenantId,
      jobId,
    );
    const restarted = await new OutlineJobStore(
      root,
      60_000,
      () => 9_000,
    ).read(tenantId, jobId);

    expect(first?.createdAt).toBe(expectedCreatedAt);
    expect(restarted?.createdAt).toBe(expectedCreatedAt);
  });

  test("persists unexpected outline rejection and prevents lease replay after restart", async () => {
    const root = mkdtempSync(join(tmpdir(), "openmaic-outline-rejection-"));
    const now = Date.parse("2026-08-02T01:00:00.000Z");
    const store = new OutlineJobStore(root, 60_000, () => now, false);
    const leaked = "sensitive-unexpected-outline-rejection";
    const dependencies = {
      ...handlerDependencies(store),
      now: () => {
        throw new Error(leaked);
      },
    };
    const request = validRequest();
    const firstResponse = await createOutlinePostHandler(dependencies)(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: JSON.stringify(request),
        idempotencyKey: request.idempotencyKey,
      }),
    );

    await expect(firstResponse.json()).resolves.toMatchObject({
      status: "running",
      createdAt: "2026-08-02T01:00:00.000Z",
    });
    const terminal = await waitForOutlineTerminal(store);
    expect(terminal).toMatchObject({
      status: "failed",
      createdAt: "2026-08-02T01:00:00.000Z",
      error: {
        code: "OUTLINE_GENERATION_FAILED",
        message: "Outline generation failed.",
      },
    });
    expect(JSON.stringify(terminal)).not.toContain(leaked);

    const restarted = new OutlineJobStore(root, 60_000, () => now, false);
    await expect(restarted.read(request.tenantId, request.jobId)).resolves.toEqual(
      terminal,
    );
    const replayDependencies = handlerDependencies(restarted);
    const replayResponse = await createOutlinePostHandler(replayDependencies)(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: JSON.stringify(request),
        idempotencyKey: request.idempotencyKey,
      }),
    );
    await expect(replayResponse.json()).resolves.toEqual(terminal);
    expect(replayDependencies.generateOutlines).not.toHaveBeenCalled();
  });

  test("logs a fixed message when rejected outline terminal persistence fails", async () => {
    const root = mkdtempSync(join(tmpdir(), "openmaic-outline-persist-log-"));
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const store = new OutlineJobStore(root, 60_000, Date.now, false);
    const request = validRequest();
    const submission = {
      tenantId: request.tenantId,
      jobId: request.jobId,
      idempotencyKey: request.idempotencyKey,
      action: "outline" as const,
      canonicalBody: canonicalJson(request),
    };
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    store.start(submission, async () => {
      await blocked;
      throw new Error("sensitive-persistence-source-error");
    });
    mkdirSync(
      durableFile(
        root,
        "outline-jobs",
        "jobs",
        [request.tenantId, request.jobId],
        "terminal.json",
      ),
    );
    release();

    await vi.waitFor(() => {
      expect(consoleError).toHaveBeenCalledWith(
        "OpenMAIC outline terminal persistence failed.",
      );
    });
    expect(JSON.stringify(consoleError.mock.calls)).not.toContain("sensitive");
    consoleError.mockRestore();
  });

  test("reclaims an expired durable outline lease and fences the old owner", async () => {
    const root = mkdtempSync(join(tmpdir(), "openmaic-outline-reclaim-"));
    let now = 1_000;
    let release!: () => void;
    const blocked = new Promise<void>((resolveBlocked) => {
      release = resolveBlocked;
    });
    const request = validRequest();
    const submission = {
      tenantId: request.tenantId,
      jobId: request.jobId,
      idempotencyKey: request.idempotencyKey,
      action: "outline" as const,
      canonicalBody: JSON.stringify(request),
    };
    const staleOutline = validOutlineBundle();
    staleOutline.title = "stale";
    const recoveredOutline = validOutlineBundle();
    recoveredOutline.title = "recovered";
    const staleJob = await generateOutlineJob(request, {
      generateOutlines: async () => staleOutline,
      now: () => new Date(GENERATED_AT),
    });
    const recoveredJob = await generateOutlineJob(request, {
      generateOutlines: async () => recoveredOutline,
      now: () => new Date(GENERATED_AT),
    });
    const expectedRecoveredJob = {
      ...recoveredJob,
      createdAt: "1970-01-01T00:00:01.000Z",
      updatedAt: "1970-01-01T00:00:01.200Z",
    };
    const abandoned = new OutlineJobStore(root, 100, () => now, false);
    const oldCompletion = abandoned.submit(submission, async () => {
      await blocked;
      return staleJob;
    });

    now = 1_200;
    const recovered = new OutlineJobStore(root, 100, () => now, false);
    await expect(
      recovered.submit(submission, async () => recoveredJob),
    ).resolves.toEqual(expectedRecoveredJob);
    release();
    await expect(oldCompletion).resolves.toEqual(expectedRecoveredJob);

    const restarted = new OutlineJobStore(root, 100, () => now, false);
    await expect(
      restarted.read(request.tenantId, request.jobId),
    ).resolves.toEqual(expectedRecoveredJob);
  });

  test("shares the in-process store across isolated route module loads", async () => {
    const firstModule = await import("../../lib/yfeistai/job-store");
    const contractSuffix = OUTLINE_BUNDLE_CONTRACT_SHA256.slice(0, 12);
    const sharedTenantId = `shared-tenant-${contractSuffix}`;
    const sharedJobId = `shared-job-${contractSuffix}`;
    const sharedIdempotencyKey = `shared-idempotency-${contractSuffix}`;
    const request = validRequest();
    const job = await generateOutlineJob(request, {
      generateOutlines: async () => validOutlineBundle(),
      now: () => new Date(GENERATED_AT),
    });
    const sharedJob = {
      ...job,
      tenantId: sharedTenantId,
      jobId: sharedJobId,
      idempotencyKey: sharedIdempotencyKey,
    };
    await firstModule.outlineJobStore.submit(
      {
        tenantId: sharedTenantId,
        jobId: sharedJobId,
        idempotencyKey: sharedIdempotencyKey,
        action: "outline",
        canonicalBody: "{}",
      },
      async () => sharedJob,
    );

    vi.resetModules();
    const secondModule = await import("../../lib/yfeistai/job-store");

    await expect(
      secondModule.outlineJobStore.read(sharedTenantId, sharedJobId),
    ).resolves.toEqual(sharedJob);
  });

  test("authenticates before parsing malformed JSON", async () => {
    const dependencies = handlerDependencies();
    const handler = createOutlinePostHandler(dependencies);
    const response = await handler(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: "{",
        idempotencyKey: "idem-a",
        secret: "wrong-secret",
      }),
    );

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({
      error: {
        code: "AUTHENTICATION_FAILED",
        message: "Service authentication failed.",
      },
    });
    expect(dependencies.generateOutlines).not.toHaveBeenCalled();

    const authenticated = await handler(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: "{",
        idempotencyKey: "idem-a",
      }),
    );
    expect(authenticated.status).toBe(400);
  });

  test("binds tenant, job, idempotency key, action, and body", async () => {
    const dependencies = handlerDependencies();
    const handler = createOutlinePostHandler(dependencies);
    const body = JSON.stringify(validRequest());

    const wrongTenant = await handler(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body,
        tenantId: "tenant-b",
        idempotencyKey: "idem-a",
      }),
    );
    expect(wrongTenant.status).toBe(403);

    const wrongAction = await handler(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        signedPath: "/api/yfeistai/v1/classrooms",
        body,
        idempotencyKey: "idem-a",
      }),
    );
    expect(wrongAction.status).toBe(401);

    const changedBody = JSON.stringify({
      ...validRequest(),
      sceneBudget: 3,
    });
    const bodyMismatch = signedHttpRequest({
      method: "POST",
      path: "/api/yfeistai/v1/outlines",
      body: changedBody,
      idempotencyKey: "idem-a",
    });
    bodyMismatch.headers.set(
      "x-yfeistai-signature",
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body,
        idempotencyKey: "idem-a",
      }).headers.get("x-yfeistai-signature")!,
    );
    expect((await handler(bodyMismatch)).status).toBe(401);
    expect(dependencies.generateOutlines).not.toHaveBeenCalled();
  });

  test("deduplicates canonical bodies and rejects conflicting reuse", async () => {
    const dependencies = handlerDependencies();
    const handler = createOutlinePostHandler(dependencies);
    const request = validRequest();
    const firstBody = JSON.stringify(request);
    const reorderedBody = JSON.stringify(
      Object.fromEntries(Object.entries(request).reverse()),
    );

    const [first, duplicate] = await Promise.all([
      handler(
        signedHttpRequest({
          method: "POST",
          path: "/api/yfeistai/v1/outlines",
          body: firstBody,
          idempotencyKey: request.idempotencyKey,
        }),
      ),
      handler(
        signedHttpRequest({
          method: "POST",
          path: "/api/yfeistai/v1/outlines",
          body: reorderedBody,
          idempotencyKey: request.idempotencyKey,
        }),
      ),
    ]);

    expect(first.status).toBe(202);
    expect(duplicate.status).toBe(202);
    expect(await duplicate.json()).toEqual(await first.json());
    expect(dependencies.generateOutlines).toHaveBeenCalledTimes(1);

    const conflicting = await handler(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: JSON.stringify({ ...request, sceneBudget: 3 }),
        idempotencyKey: request.idempotencyKey,
      }),
    );
    expect(conflicting.status).toBe(409);
  });

  test("rejects one idempotency binding reused for another job", async () => {
    const dependencies = handlerDependencies();
    const handler = createOutlinePostHandler(dependencies);
    const first = validRequest();
    expect(
      (
        await handler(
          signedHttpRequest({
            method: "POST",
            path: "/api/yfeistai/v1/outlines",
            body: JSON.stringify(first),
            idempotencyKey: first.idempotencyKey,
          }),
        )
      ).status,
    ).toBe(202);

    const second = {
      ...validRequest(),
      requestId: "request-b",
      jobId: "job-b",
    };
    const response = await handler(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: JSON.stringify(second),
        jobId: second.jobId,
        idempotencyKey: second.idempotencyKey,
      }),
    );

    expect(response.status).toBe(409);
    expect(dependencies.generateOutlines).toHaveBeenCalledTimes(1);
  });

  test("polling cannot cross tenant or job boundaries", async () => {
    const store = new OutlineJobStore();
    const dependencies = handlerDependencies(store);
    const post = createOutlinePostHandler(dependencies);
    const get = createOutlineGetHandler({
      store,
      readSecret: () => SECRET,
      nowSeconds: () => NOW_SECONDS,
    });
    const request = validRequest();
    await post(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: JSON.stringify(request),
        idempotencyKey: request.idempotencyKey,
      }),
    );

    const crossTenant = await get(
      signedHttpRequest({
        method: "GET",
        path: "/api/yfeistai/v1/outlines/job-a",
        tenantId: "tenant-b",
        jobId: "job-a",
      }),
      { params: { jobId: "job-a" } },
    );
    expect(crossTenant.status).toBe(404);

    const crossJob = await get(
      signedHttpRequest({
        method: "GET",
        path: "/api/yfeistai/v1/outlines/job-a",
        tenantId: "tenant-a",
        jobId: "job-b",
      }),
      { params: { jobId: "job-a" } },
    );
    expect(crossJob.status).toBe(403);

    const ownJob = await get(
      signedHttpRequest({
        method: "GET",
        path: "/api/yfeistai/v1/outlines/job-a",
        tenantId: "tenant-a",
        jobId: "job-a",
      }),
      { params: Promise.resolve({ jobId: "job-a" }) },
    );
    expect(ownJob.status).toBe(200);
  });

  test("durably starts outline generation and returns running without awaiting it", async () => {
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const store = new OutlineJobStore();
    const dependencies = handlerDependencies(store);
    dependencies.generateOutlines.mockImplementation(async () => {
      await blocked;
      return validOutlineBundle();
    });
    const handler = createOutlinePostHandler(dependencies);
    const request = validRequest();
    const responsePromise = handler(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: JSON.stringify(request),
        idempotencyKey: request.idempotencyKey,
      }),
    );

    const settledBeforeGeneration = await Promise.race([
      responsePromise.then(() => true),
      new Promise<false>((resolve) => setTimeout(() => resolve(false), 500)),
    ]);
    if (!settledBeforeGeneration) {
      release();
    }

    expect(settledBeforeGeneration).toBe(true);
    const response = await responsePromise;
    expect(response.status).toBe(202);
    await expect(response.json()).resolves.toMatchObject({
      tenantId: request.tenantId,
      jobId: request.jobId,
      idempotencyKey: request.idempotencyKey,
      phase: "outline",
      status: "running",
    });

    release();
    await expect(waitForOutlineTerminal(store)).resolves.toMatchObject({
      status: "succeeded",
    });
  });

  test.each([
    ["provider_429", { status: 429 }],
    ["provider_5xx", { response: { status: 503 } }],
    ["connect_timeout", { code: "UND_ERR_CONNECT_TIMEOUT" }],
    ["read_timeout", { code: "UND_ERR_HEADERS_TIMEOUT" }],
  ])(
    "maps upstream %s failures to stable secret-free codes",
    async (code, shape) => {
      const leaked = `sensitive-upstream-credential-${code}`;
      const store = new OutlineJobStore();
      const handler = createOutlinePostHandler({
        ...handlerDependencies(store),
        generateOutlines: async () => {
          throw Object.assign(new Error(leaked), shape);
        },
      });
      const request = validRequest();
      const response = await handler(
        signedHttpRequest({
          method: "POST",
          path: "/api/yfeistai/v1/outlines",
          body: JSON.stringify(request),
          idempotencyKey: request.idempotencyKey,
        }),
      );
      const submitted = await response.json();
      const payload =
        submitted.status === "running"
          ? await waitForOutlineTerminal(store)
          : submitted;

      expect(response.status).toBe(202);
      expect(payload).toMatchObject({
        status: "failed",
        error: {
          code,
          message: expect.stringMatching(/^Provider/),
        },
      });
      expect(JSON.stringify(payload)).not.toContain(leaked);
    },
  );

  test("keeps outline contract failures non-retryable and secret-free", async () => {
    const leaked = "sensitive-contract-detail";
    const store = new OutlineJobStore();
    const handler = createOutlinePostHandler({
      ...handlerDependencies(store),
      generateOutlines: async () => {
        throw new Error(leaked);
      },
    });
    const request = validRequest();
    const response = await handler(
      signedHttpRequest({
        method: "POST",
        path: "/api/yfeistai/v1/outlines",
        body: JSON.stringify(request),
        idempotencyKey: request.idempotencyKey,
      }),
    );
    const submitted = await response.json();
    const payload =
      submitted.status === "running"
        ? await waitForOutlineTerminal(store)
        : submitted;

    expect(payload).toMatchObject({
      status: "failed",
      error: {
        code: "OUTLINE_GENERATION_FAILED",
        message: "Outline generation failed.",
      },
    });
    expect(JSON.stringify(payload)).not.toContain(leaked);
  });
});
