import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { describe, expect, test, vi } from "vitest";

import {
  OUTLINE_BUNDLE_CONTRACT_SHA256,
  createOutlineGetHandler,
  createOutlinePostHandler,
  generateOutlineJob,
  validateGenerationRequest,
} from "../../lib/yfeistai/outline-generation";
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

describe("outline-only generation boundary", () => {
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
    const abandoned = new OutlineJobStore(root, 100, () => now, false);
    const oldCompletion = abandoned.submit(submission, async () => {
      await blocked;
      return staleJob;
    });

    now = 1_200;
    const recovered = new OutlineJobStore(root, 100, () => now, false);
    await expect(
      recovered.submit(submission, async () => recoveredJob),
    ).resolves.toEqual(recoveredJob);
    release();
    await expect(oldCompletion).resolves.toEqual(recoveredJob);

    const restarted = new OutlineJobStore(root, 100, () => now, false);
    await expect(
      restarted.read(request.tenantId, request.jobId),
    ).resolves.toEqual(recoveredJob);
  });

  test("shares the in-process store across isolated route module loads", async () => {
    const firstModule = await import("../../lib/yfeistai/job-store");
    const sharedTenantId = "shared-tenant-durable";
    const sharedJobId = "shared-job-durable";
    const sharedIdempotencyKey = "shared-idempotency-durable";
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

  test("returns stable failures without upstream secrets", async () => {
    const leaked = "sensitive-upstream-credential";
    const handler = createOutlinePostHandler({
      ...handlerDependencies(),
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
    const payload = await response.json();

    expect(response.status).toBe(202);
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
