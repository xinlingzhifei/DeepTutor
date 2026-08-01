import { createHash } from "node:crypto";

import { describe, expect, test, vi } from "vitest";

import {
  canonicalConfirmedOutlineJson,
  buildOpenMaicSourcePrompt,
  createClassroomGetHandler,
  createClassroomPostHandler,
  generateContentJob,
  toPortableOpenMaicSceneContent,
  type ContentGenerationRequest,
  type ContentGenerationResult,
} from "../../lib/yfeistai/content-generation";
import { ContentJobStore } from "../../lib/yfeistai/content-generation";
import { createArtifactEntry } from "../../lib/yfeistai/artifact-manifest";
import type { OutlineBundle } from "../../lib/yfeistai/contracts";
import { asPortableDocument } from "../../lib/yfeistai/portable-classroom";
import { signServiceRequest } from "../../lib/yfeistai/service-auth";

function confirmedOutline(): OutlineBundle {
  return {
    schemaVersion: "1.0",
    outlineId: "outline-a",
    outlineVersion: 1,
    confirmationMetadata: {
      status: "confirmed",
      confirmedAt: "2026-07-30T00:00:00.000Z",
      confirmedBy: "teacher-a",
    },
    title: "Pythagorean theorem",
    language: "en-US",
    scenes: [
      {
        sceneId: "scene-a",
        title: "Triangle",
        summary: "Explain the relation between the three sides.",
        knowledgePointIds: ["kp-a"],
        sourceRefs: [],
      },
      {
        sceneId: "scene-b",
        title: "Practice",
        summary: "Apply the relation.",
        knowledgePointIds: ["kp-a"],
        sourceRefs: [],
      },
    ],
    knowledgeCoverage: [
      { knowledgePointId: "kp-a", sceneIds: ["scene-a", "scene-b"] },
    ],
    sourceRefs: [],
    estimatedSceneCount: 2,
    generationMetadata: {
      generator: "openmaic",
      generatorVersion: "0.3.1",
      modelId: "server-selected-model",
      generatedAt: "2026-07-30T00:00:00.000Z",
      teachingBriefId: "brief-a",
      teachingBriefSha256: "a".repeat(64),
      templateId: "template-a",
      templateVersion: "1",
    },
    contractSha256:
      "f8ddb7c11138f402ed048c4af2010714b2bfd456e5c38122920c689e4a2b3ddf",
  };
}

function boundRequest(
  phase: "content" | "micro" = "content",
): ContentGenerationRequest {
  const outline = confirmedOutline();
  return {
    schemaVersion: "1.0",
    tenantId: "tenant-a",
    requestId: `${phase}-request-a`,
    jobId: `${phase}-a`,
    idempotencyKey: `${phase}-idem-a`,
    phase,
    classroomMode: phase === "micro" ? ("micro" as const) : ("full" as const),
    teachingBriefId: "brief-a",
    teachingBriefSha256: "a".repeat(64),
    teachingBrief: {
      schemaVersion: "1.0",
      briefId: "brief-a",
      briefVersion: 1,
      tenantId: "tenant-a",
      courseId: "course-a",
      targetClassId: "class-a",
      gradeBand: "middle-school",
      audienceLevel: "introductory",
      classroomMode: phase === "micro" ? ("micro" as const) : ("full" as const),
      objectives: [
        {
          objectiveId: "objective-a",
          description: "Understand and apply the theorem.",
          knowledgePointIds: ["kp-a"],
        },
      ],
      durationMinutes: 20,
      knowledgePoints: [
        {
          knowledgePointId: "kp-a",
          title: "Pythagorean theorem",
          description: "The relation between right-triangle sides.",
        },
      ],
      prerequisites: [
        {
          knowledgePointId: "kp-a",
          prerequisiteKnowledgePointIds: ["kp-prerequisite"],
        },
      ],
      assessment: {
        methods: ["quiz" as const],
        successCriteria: ["Can calculate an unknown side."],
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
      contentMode: "open_creation" as const,
      networkPolicy: { allowWebAccess: false, allowedDomains: [] },
      mediaPolicy: { allowGeneration: true, allowedMimeTypes: ["image/png"] },
      templatePolicy: { templateId: "template-a", templateVersion: "1" },
      safetyPolicy: { policyId: "school-default", blockedCategories: [] },
      contentSha256: "a".repeat(64),
    },
    confirmedOutline: outline,
    confirmedOutlineSha256: createHash("sha256")
      .update(canonicalConfirmedOutlineJson(outline), "utf8")
      .digest("hex"),
    templateId: "template-a",
    templateVersion: "1",
    sceneBudget: 4,
    durationMinutes: 20,
    requestedExports: ["classroom_zip"],
    callbackContext: "callback-a",
    dataPlaneRouteId: "shared-primary",
    priority:
      phase === "micro" ? ("student_micro" as const) : ("teacher" as const),
  };
}

function sourceGroundedRequest(): ContentGenerationRequest {
  const request = boundRequest();
  const sourceRef = {
    citationId: "citation-a",
    sourceId: "source-a",
    fragmentId: "fragment-a",
  };
  request.teachingBrief.contentMode = "source_grounded";
  request.teachingBrief.sourceSnapshot = {
    snapshotId: "snapshot-a",
    createdAt: "2026-07-30T00:00:00.000Z",
    contentSha256: "b".repeat(64),
  };
  request.teachingBrief.sourceFragments = [
    {
      fragmentId: "fragment-a",
      sourceId: "source-a",
      text: "The square of the hypotenuse equals the sum of the leg squares.",
      contentSha256: "c".repeat(64),
    },
  ];
  request.teachingBrief.citations = [
    { ...sourceRef, label: "Textbook section 1" },
  ];
  request.teachingBrief.sourceRefs = [{ ...sourceRef }];
  request.teachingBrief.permissionSummary = {
    allowedSourceIds: ["source-a"],
    allowedFragmentIds: ["fragment-a"],
    usageScope: "classroom_generation",
    attributionRequired: true,
  };
  request.confirmedOutline!.sourceRefs = [{ ...sourceRef }];
  request.confirmedOutline!.scenes.forEach((scene) => {
    scene.sourceRefs = [{ ...sourceRef }];
  });
  request.confirmedOutlineSha256 = createHash("sha256")
    .update(canonicalConfirmedOutlineJson(request.confirmedOutline!), "utf8")
    .digest("hex");
  return request;
}

function generatedSlide(
  sceneId: string,
  title: string,
  stageId: string,
  order: number,
) {
  return {
    id: sceneId,
    stageId,
    title,
    order,
    type: "slide" as const,
    content: {
      type: "slide" as const,
      canvas: {
        elements: [{ id: `${sceneId}-text`, type: "text", content: title }],
      },
    },
    actions: [{ id: `${sceneId}-speech`, type: "speech", text: title }],
  };
}

function signedRequest(input: {
  method: string;
  path: string;
  body: string;
  tenantId: string;
  jobId: string;
  idempotencyKey?: string;
  secret?: string;
}) {
  const timestamp = 1_800_000_000;
  const signed = signServiceRequest({
    method: input.method,
    path: input.path,
    body: input.body,
    tenantId: input.tenantId,
    jobId: input.jobId,
    timestamp,
    idempotencyKey: input.idempotencyKey,
    secret: input.secret ?? "service-secret",
  });
  return new Request(`http://openmaic${input.path}`, {
    method: input.method,
    body: input.method === "GET" ? undefined : input.body,
    headers: {
      "x-yfeistai-tenant-id": signed.tenantId,
      "x-yfeistai-job-id": signed.jobId,
      "x-yfeistai-timestamp": String(signed.timestamp),
      "x-yfeistai-idempotency-key": signed.idempotencyKey,
      "x-yfeistai-signature": signed.signature,
    },
  });
}

describe("confirmed outline content boundary", () => {
  test("preserves source fragments and a non-slide scene intent for the production adapter", async () => {
    const request = sourceGroundedRequest();
    const contexts: Array<Record<string, unknown>> = [];

    await generateContentJob(request, {
      generateScenes: async (scene, context) => {
        contexts.push(context as unknown as Record<string, unknown>);
        return generatedSlide(
          scene.sceneId,
          scene.title,
          context.stageId,
          context.order,
        );
      },
    });

    expect(contexts.map((context) => context.sceneType)).toEqual([
      "slide",
      "quiz",
    ]);
    expect(contexts[0].sourceFragments).toEqual(
      request.teachingBrief.sourceFragments,
    );
    expect(contexts[0].sourceFragments).not.toBe(
      request.teachingBrief.sourceFragments,
    );
  });

  test("preserves generated union discriminators in the classroom interaction index", async () => {
    const result = await generateContentJob(boundRequest(), {
      generateScenes: async (scene, context) => {
        if (context.sceneType === "quiz") {
          return {
            id: scene.sceneId,
            stageId: context.stageId,
            title: scene.title,
            order: context.order,
            type: "quiz" as const,
            content: toPortableOpenMaicSceneContent("quiz", {
              questions: [
                {
                  id: "question-a",
                  type: "single",
                  question: "Which relation is correct?",
                  options: [
                    { label: "a²+b²=c²", value: "A" },
                    { label: "a+b=c", value: "B" },
                  ],
                  answer: ["A"],
                  analysis: "This is the Pythagorean theorem.",
                },
              ],
            }),
            actions: [],
          };
        }
        return generatedSlide(
          scene.sceneId,
          scene.title,
          context.stageId,
          context.order,
        );
      },
    });

    expect(
      result.classroomDocument.openmaic.scenes.map((scene) => scene.type),
    ).toEqual(["slide", "quiz"]);
    expect(result.classroomDocument.interactionIds).toEqual(["scene-b"]);
  });

  test("checks the execution fence before invoking an artifact publisher", async () => {
    const writeArtifact = vi.fn(async (input) => ({
      ...createArtifactEntry(input),
      downloadPath:
        "/api/yfeistai/v1/artifacts/content-a/classroom/classroom.json",
    }));

    await expect(
      generateContentJob(boundRequest(), {
        generateScenes: async (scene, context) =>
          generatedSlide(
            scene.sceneId,
            scene.title,
            context.stageId,
            context.order,
          ),
        assertPublicationActive: () => {
          throw new Error("execution lease was fenced");
        },
        writeArtifact,
      }),
    ).rejects.toThrow(/lease was fenced/i);
    expect(writeArtifact).not.toHaveBeenCalled();
  });

  test("rejects a changed outline after confirmation", async () => {
    const original = confirmedOutline();
    const confirmedOutlineSha256 = createHash("sha256")
      .update(canonicalConfirmedOutlineJson(original), "utf8")
      .digest("hex");
    const changed = structuredClone(original);
    changed.scenes[0].summary = "Changed after confirmation.";
    const generateScenes = vi.fn();

    await expect(
      generateContentJob(
        {
          ...boundRequest(),
          confirmedOutline: changed,
          confirmedOutlineSha256,
        },
        { generateScenes },
      ),
    ).rejects.toThrow("confirmed outline hash mismatch");
    expect(generateScenes).not.toHaveBeenCalled();
  });

  test.each(["content", "micro"] as const)(
    "generates every confirmed scene for phase=%s without regenerating the outline",
    async (phase) => {
      const request = boundRequest(phase);
      const generateOutlines = vi.fn();
      const generateScenes = vi.fn(
        async (
          scene: ReturnType<typeof confirmedOutline>["scenes"][number],
          context: { stageId: string; order: number },
        ) =>
          generatedSlide(
            scene.sceneId,
            scene.title,
            context.stageId,
            context.order,
          ),
      );

      const result = await generateContentJob(request, {
        generateScenes,
        now: () => new Date("2026-07-30T01:00:00.000Z"),
        // Deliberately present as a tripwire: the content path must never call it.
        ...({ generateOutlines } as object),
      });

      expect(generateOutlines).not.toHaveBeenCalled();
      expect(generateScenes).toHaveBeenCalledTimes(2);
      expect(result.classroomDocument.openmaic.scenes).toHaveLength(2);
      expect(
        result.classroomDocument.openmaic.scenes.map((scene) => scene.id),
      ).toEqual(["scene-a", "scene-b"]);
      expect(result.classroomDocument.fileSha256).toMatch(/^[0-9a-f]{64}$/);
      expect(result.artifacts).toEqual([
        expect.objectContaining({
          relativePath: "classroom/classroom.json",
          mime: "application/json",
          sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
          bytes: expect.any(Number),
          expiresAt: "2026-07-31T01:00:00.000Z",
        }),
      ]);
      const forbiddenRuntimeSurface = new RegExp(
        [
          ["api", "Key"].join(""),
          ["base", "Url"].join(""),
          ["provider", "Id"].join(""),
          "https?://",
          "file://",
          "[A-Za-z]:\\\\\\\\",
        ].join("|"),
      );
      expect(JSON.stringify(result.classroomDocument)).not.toMatch(
        forbiddenRuntimeSurface,
      );
    },
  );

  test("rejects a source-grounded portable document without source refs", async () => {
    const result = await generateContentJob(boundRequest(), {
      generateScenes: async (scene, context) =>
        generatedSlide(
          scene.sceneId,
          scene.title,
          context.stageId,
          context.order,
        ),
    });
    const invalid = structuredClone(result.classroomDocument);
    invalid.contentMode = "source_grounded";
    invalid.openCreation = false;

    expect(() => asPortableDocument(invalid)).toThrow(
      /source-grounded classroom requires at least one source ref/i,
    );
  });

  test("accepts the frozen micro request without a confirmed outline", async () => {
    const {
      confirmedOutline: _confirmedOutline,
      confirmedOutlineSha256: _confirmedOutlineSha256,
      ...request
    } = boundRequest("micro");
    const generateOutlines = vi.fn();
    const generateScenes = vi.fn(
      async (
        scene: OutlineBundle["scenes"][number],
        context: { stageId: string; order: number },
      ) =>
        generatedSlide(
          scene.sceneId,
          scene.title,
          context.stageId,
          context.order,
        ),
    );

    const result = await generateContentJob(request, {
      generateScenes,
      ...({ generateOutlines } as object),
    });

    expect(generateOutlines).not.toHaveBeenCalled();
    expect(generateScenes).toHaveBeenCalledTimes(1);
    expect(generateScenes.mock.calls[0][1].outline.language).toBe("en-US");
    expect(result.classroomDocument.openmaic.scenes).toHaveLength(1);
  });

  test("checks cancellation before and after every generated scene", async () => {
    const request = boundRequest();
    let canceled = false;
    const generateScenes = vi.fn(
      async (
        scene: ReturnType<typeof confirmedOutline>["scenes"][number],
        context: { stageId: string; order: number },
      ) => {
        canceled = true;
        return generatedSlide(
          scene.sceneId,
          scene.title,
          context.stageId,
          context.order,
        );
      },
    );

    await expect(
      generateContentJob(request, {
        generateScenes,
        isCanceled: () => canceled,
      }),
    ).rejects.toThrow(/canceled/i);
    expect(generateScenes).toHaveBeenCalledTimes(1);
  });

  test("rejects non-portable scene output", async () => {
    const request = boundRequest();
    await expect(
      generateContentJob(request, {
        generateScenes: async (scene, context) => ({
          ...generatedSlide(
            scene.sceneId,
            scene.title,
            context.stageId,
            context.order,
          ),
          actions: [
            {
              id: "speech-a",
              type: "speech",
              audioUrl: "https://untrusted.example/audio.mp3",
            },
          ],
        }),
      }),
    ).rejects.toThrow(/non-portable/i);
  });

  test("requires every generated media reference in both manifests", async () => {
    const mediaBytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
    const entry = createArtifactEntry({
      relativePath: "media/scene-1/asset-1.png",
      bytes: mediaBytes,
      mime: "image/png",
      expiresAt: "2026-07-31T00:00:00.000Z",
    });
    const result = await generateContentJob(boundRequest(), {
      now: () => new Date("2026-07-30T00:00:00.000Z"),
      generateScenes: async (scene, context) => {
        const slide = generatedSlide(
          scene.sceneId,
          scene.title,
          context.stageId,
          context.order,
        );
        if (context.order === 0) {
          slide.content.canvas = {
            elements: [
              { id: "image-a", type: "image", src: entry.relativePath },
            ],
          } as unknown as typeof slide.content.canvas;
          return {
            scene: slide,
            media: [
              {
                mediaId: "media-a",
                relativePath: entry.relativePath,
                bytes: mediaBytes,
                mime: entry.mime,
              },
            ],
          };
        }
        return slide;
      },
    });
    expect(result.artifacts).toContainEqual(entry);
    expect(result.classroomDocument.mediaManifest).toContainEqual({
      mediaId: "media-a",
      relativePath: entry.relativePath,
      mimeType: entry.mime,
      sha256: entry.sha256,
      sizeBytes: entry.bytes,
      temporaryDownloadPath: entry.downloadPath,
      expiresAt: entry.expiresAt,
    });
  });

  test("rejects generated media whose bytes do not match the declared MIME", async () => {
    await expect(
      generateContentJob(boundRequest(), {
        generateScenes: async (scene, context) => ({
          scene: generatedSlide(
            scene.sceneId,
            scene.title,
            context.stageId,
            context.order,
          ),
          media:
            context.order === 0
              ? [
                  {
                    mediaId: "media-spoofed",
                    relativePath: "media/spoofed.png",
                    bytes: new TextEncoder().encode("not a png"),
                    mime: "image/png",
                  },
                ]
              : [],
        }),
      }),
    ).rejects.toThrow(/MIME signature/i);
  });

  test.each([
    {
      name: "brief forbids generation",
      configure(request: ContentGenerationRequest) {
        request.teachingBrief.mediaPolicy.allowGeneration = false;
      },
      media: {
        mediaId: "media-policy",
        relativePath: "media/policy.png",
        bytes: new Uint8Array([1]),
        mime: "image/png",
      },
      expected: /forbids generated media/i,
    },
    {
      name: "MIME is not allowlisted",
      configure() {},
      media: {
        mediaId: "media-mime",
        relativePath: "media/policy.mp3",
        bytes: new Uint8Array([1]),
        mime: "audio/mpeg",
      },
      expected: /MIME type is not allowed/i,
    },
    {
      name: "bytes are absent",
      configure() {},
      media: {
        mediaId: "media-bytes",
        relativePath: "media/policy.png",
        bytes: undefined as unknown as Uint8Array,
        mime: "image/png",
      },
      expected: /bytes are missing/i,
    },
  ])(
    "rejects generated media when $name",
    async ({ configure, media, expected }) => {
      const request = boundRequest();
      configure(request);
      await expect(
        generateContentJob(request, {
          generateScenes: async (scene, context) => ({
            scene: generatedSlide(
              scene.sceneId,
              scene.title,
              context.stageId,
              context.order,
            ),
            media: context.order === 0 ? [media] : [],
          }),
        }),
      ).rejects.toThrow(expected);
    },
  );

  test("rejects a media writer that does not bind the submitted bytes", async () => {
    await expect(
      generateContentJob(boundRequest(), {
        generateScenes: async (scene, context) => ({
          scene: generatedSlide(
            scene.sceneId,
            scene.title,
            context.stageId,
            context.order,
          ),
          media:
            context.order === 0
              ? [
                  {
                    mediaId: "media-writer",
                    relativePath: "media/writer.png",
                    bytes: new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]),
                    mime: "image/png",
                  },
                ]
              : [],
        }),
        writeArtifact: async (input) =>
          createArtifactEntry({
            ...input,
            bytes: new Uint8Array([9, 9, 9]),
          }),
      }),
    ).rejects.toThrow(/writer integrity binding failed/i);
  });

  test("rejects a classroom document writer that does not bind the submitted bytes", async () => {
    await expect(
      generateContentJob(boundRequest(), {
        generateScenes: async (scene, context) =>
          generatedSlide(
            scene.sceneId,
            scene.title,
            context.stageId,
            context.order,
          ),
        writeArtifact: async (input) =>
          createArtifactEntry({
            ...input,
            bytes: new TextEncoder().encode("wrong classroom"),
          }),
      }),
    ).rejects.toThrow(/writer integrity binding failed/i);
  });

  test("rejects protocol-relative HTML and extra union fields", async () => {
    await expect(
      generateContentJob(boundRequest(), {
        generateScenes: async (scene, context) =>
          ({
            ...generatedSlide(
              scene.sceneId,
              scene.title,
              context.stageId,
              context.order,
            ),
            content: {
              type: "interactive",
              html: '<img src="//untrusted.example/pixel.png">',
              bridgeVersion: "1.0",
              sandbox: { allowScripts: true, allowSameOrigin: false },
              upstreamOnly: true,
            },
            type: "interactive",
          }) as never,
      }),
    ).rejects.toThrow(/non-portable|invalid field set/i);
  });
});

describe("OpenMAIC production scene adapter", () => {
  test("preserves all four generated scene content discriminators", () => {
    expect(
      toPortableOpenMaicSceneContent("slide", {
        elements: [{ id: "title", type: "text", content: "Triangle" }],
        background: { color: "#ffffff" },
      }),
    ).toEqual({
      type: "slide",
      canvas: {
        elements: [{ id: "title", type: "text", content: "Triangle" }],
        background: { color: "#ffffff" },
      },
    });
    expect(
      toPortableOpenMaicSceneContent("quiz", {
        questions: [
          {
            id: "question-a",
            type: "single",
            question: "Which side is the hypotenuse?",
            options: [
              { label: "a", value: "A" },
              { label: "c", value: "C" },
            ],
            answer: ["C"],
            analysis: "It is opposite the right angle.",
          },
        ],
      }),
    ).toEqual({
      type: "quiz",
      questions: [
        {
          id: "question-a",
          prompt: "Which side is the hypotenuse?",
          questionType: "single_choice",
          options: [
            { id: "A", label: "a" },
            { id: "C", label: "c" },
          ],
          correctOptionIds: ["C"],
          explanation: "It is opposite the right angle.",
        },
      ],
    });
    expect(
      toPortableOpenMaicSceneContent("interactive", {
        html: "<main>Drag the point</main>",
      }),
    ).toEqual({
      type: "interactive",
      html: "<main>Drag the point</main>",
      bridgeVersion: "1.0",
      sandbox: { allowScripts: true, allowSameOrigin: false },
    });
    expect(
      toPortableOpenMaicSceneContent("pbl", {
        projectConfig: {
          projectInfo: {
            title: "Bridge",
            description: "Design a safe bridge.",
          },
          agents: [
            {
              name: "engineer",
              actor_role: "Structural engineer",
              system_prompt: "Check every load calculation.",
            },
          ],
          issueboard: {
            issues: [
              {
                id: "milestone-a",
                title: "Choose dimensions",
                description: "Justify the dimensions.",
                notes: "Use the theorem as the rubric.",
              },
            ],
          },
        },
      }),
    ).toEqual({
      type: "pbl",
      scenario: "Design a safe bridge.",
      roles: [
        {
          id: "engineer",
          name: "Structural engineer",
          brief: "Check every load calculation.",
        },
      ],
      milestones: [
        {
          id: "milestone-a",
          title: "Choose dimensions",
          rubric: "Use the theorem as the rubric.",
        },
      ],
    });
  });

  test("adds authorized fragment identity and text to every upstream user prompt", () => {
    expect(
      buildOpenMaicSourcePrompt("Generate the scene.", [
        {
          fragmentId: "fragment-a",
          sourceId: "source-a",
          text: "Grounded fact A.",
        },
      ]),
    ).toContain("[source-a/fragment-a]\nGrounded fact A.");
  });
});

describe("signed classroom routes", () => {
  test("authenticates POST before parsing and binds tenant/job/idempotency", async () => {
    const generateScenes = vi.fn();
    const handler = createClassroomPostHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      store: new ContentJobStore<ContentGenerationResult>(),
      generateScenes,
    });
    const malformed = signedRequest({
      method: "POST",
      path: "/api/yfeistai/v1/classrooms",
      body: "{",
      tenantId: "tenant-a",
      jobId: "content-a",
      idempotencyKey: "idem-a",
      secret: "wrong-secret",
    });
    expect((await handler(malformed)).status).toBe(401);

    const unsupportedBody = JSON.stringify({
      ...boundRequest(),
      [["base", "Url"].join("")]: "http://client-selected",
    });
    expect(
      (
        await handler(
          signedRequest({
            method: "POST",
            path: "/api/yfeistai/v1/classrooms",
            body: unsupportedBody,
            tenantId: "tenant-a",
            jobId: "content-a",
            idempotencyKey: "content-idem-a",
          }),
        )
      ).status,
    ).toBe(400);

    const body = JSON.stringify(boundRequest());
    const wrongTenant = signedRequest({
      method: "POST",
      path: "/api/yfeistai/v1/classrooms",
      body,
      tenantId: "tenant-b",
      jobId: "content-a",
      idempotencyKey: "content-idem-a",
    });
    expect((await handler(wrongTenant)).status).toBe(403);
    expect(generateScenes).not.toHaveBeenCalled();
  });

  test("binds classroom polling to the signed tenant and job", async () => {
    const store = new ContentJobStore<ContentGenerationResult>();
    const running = store.start(
      {
        tenantId: "tenant-a",
        jobId: "content-a",
        idempotencyKey: "idem-a",
        canonicalBody: "{}",
      },
      async () =>
        ({ classroomId: "classroom-a" }) as unknown as ContentGenerationResult,
    );
    await running;
    const handler = createClassroomGetHandler({
      readSecret: () => "service-secret",
      nowSeconds: () => 1_800_000_000,
      store,
    });
    const request = signedRequest({
      method: "GET",
      path: "/api/yfeistai/v1/classrooms/content-a",
      body: "",
      tenantId: "tenant-b",
      jobId: "content-a",
    });
    expect(
      (
        await handler(request, {
          params: Promise.resolve({ jobId: "content-a" }),
        })
      ).status,
    ).toBe(404);
  });
});
