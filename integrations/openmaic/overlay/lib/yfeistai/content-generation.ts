import { createHash } from "node:crypto";
import path from "node:path";

import {
  OPENMAIC_APP_VERSION,
  type GenerationRequest,
  type GenerationMetadata,
  type OutlineBundle,
  type OutlineScene,
} from "./contracts";
import {
  type ArtifactEntry,
  type ArtifactStore,
  assertArtifactMimeBytes,
  createArtifactEntry,
  normalizeArtifactPath,
} from "./artifact-manifest";
import {
  OUTLINE_BUNDLE_CONTRACT_SHA256,
  canonicalJson,
  validateGenerationRequest,
  validateOutlineBundle,
} from "./outline-generation";
import {
  type JsonValue,
  type PortableClassroomDocument,
  type PortableScene,
  type PortableSceneContent,
  asPortableDocument,
  assertPortableValue,
} from "./portable-classroom";
import {
  type JobRouteContext,
  type ServiceBoundaryDependencies,
  authenticateServiceRequest,
  hasSignedBodyBinding,
  serviceError,
} from "./service-boundary";
import {
  type DurableLeaseClaim,
  claimDurableLease,
  configuredOpenMaicStateRoot,
  durableFile,
  durableLeaseMatches,
  exactDurableRecord,
  isolatedOpenMaicStateRoot,
  readDurableLease,
  readDurableJson,
  renewDurableLease,
  withDurableLock,
  writeDurableJsonExclusive,
} from "./durable-state";

const SHA256_HEX = /^[0-9a-f]{64}$/;

export type {
  JsonValue,
  PortableClassroomDocument,
  PortableScene,
  PortableSceneContent,
} from "./portable-classroom";

export type ContentGenerationRequest = GenerationRequest & {
  phase: "content" | "micro";
};

export interface GeneratedSceneResult {
  scene: PortableScene | Omit<PortableScene, "stageId" | "order">;
  media?: Array<{
    mediaId: string;
    relativePath: string;
    bytes: Uint8Array;
    mime: string;
  }>;
}

export interface ContentGenerationDependencies {
  generateScenes(
    scene: OutlineScene,
    context: {
      tenantId: string;
      jobId: string;
      stageId: string;
      order: number;
      outline: OutlineBundle;
      phase: "content" | "micro";
      sceneType: PortableScene["type"];
      sourceFragments: GenerationRequest["teachingBrief"]["sourceFragments"];
      mediaPolicy: GenerationRequest["teachingBrief"]["mediaPolicy"];
    },
  ): Promise<PortableScene | GeneratedSceneResult>;
  isCanceled?: () => boolean | Promise<boolean>;
  now?: () => Date;
  writeArtifact?: (input: {
    relativePath: string;
    bytes: Uint8Array;
    mime: string;
    expiresAt: string;
  }) => Promise<ArtifactEntry>;
  assertPublicationActive?: () => void;
  artifactTtlMilliseconds?: number;
}

export function buildOpenMaicSourcePrompt(
  userPrompt: string,
  sourceFragments: Array<{
    fragmentId: string;
    sourceId: string;
    text: string;
  }>,
): string {
  if (sourceFragments.length === 0) {
    return userPrompt;
  }
  const evidence = sourceFragments
    .map(
      (fragment) =>
        `[${fragment.sourceId}/${fragment.fragmentId}]\n${fragment.text}`,
    )
    .join("\n\n");
  return [
    userPrompt,
    "Authorized source fragments (ground course-specific claims only in this evidence):",
    evidence,
  ].join("\n\n");
}

function portableClone(value: unknown, label: string): JsonValue {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) {
    throw new Error(`${label} is not portable JSON`);
  }
  const cloned = JSON.parse(serialized) as JsonValue;
  assertPortableValue(cloned, label);
  return cloned;
}

function optionalNonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function toPortableOpenMaicSceneContent(
  sceneType: PortableScene["type"],
  generated: unknown,
): PortableSceneContent {
  const record = asRecord(generated, "OpenMAIC generated scene content");
  if (sceneType === "slide") {
    if (!Array.isArray(record.elements)) {
      throw new Error("OpenMAIC slide content is invalid");
    }
    const canvas: Record<string, JsonValue> = {
      elements: portableClone(record.elements, "OpenMAIC slide elements"),
    };
    if (record.background !== undefined) {
      canvas.background = portableClone(
        record.background,
        "OpenMAIC slide background",
      );
    }
    return { type: "slide", canvas };
  }
  if (sceneType === "quiz") {
    if (!Array.isArray(record.questions) || record.questions.length === 0) {
      throw new Error("OpenMAIC quiz content is invalid");
    }
    return {
      type: "quiz",
      questions: record.questions.map((value, index) => {
        const question = asRecord(value, `OpenMAIC quiz question ${index}`);
        const upstreamType = nonEmptyString(
          question.type,
          `OpenMAIC quiz question ${index} type`,
        );
        const questionType =
          upstreamType === "single"
            ? ("single_choice" as const)
            : upstreamType === "multiple"
              ? ("multiple_choice" as const)
              : upstreamType === "short_answer"
                ? ("short_answer" as const)
                : null;
        if (!questionType) {
          throw new Error("OpenMAIC quiz question type is unsupported");
        }
        const options = Array.isArray(question.options)
          ? question.options.map((value, optionIndex) => {
              const option = asRecord(
                value,
                `OpenMAIC quiz option ${optionIndex}`,
              );
              return {
                id: nonEmptyString(
                  option.value,
                  `OpenMAIC quiz option ${optionIndex} value`,
                ),
                label: nonEmptyString(
                  option.label,
                  `OpenMAIC quiz option ${optionIndex} label`,
                ),
              };
            })
          : [];
        const answers = Array.isArray(question.answer)
          ? question.answer.map((answer, answerIndex) =>
              nonEmptyString(answer, `OpenMAIC quiz answer ${answerIndex}`),
            )
          : [];
        return {
          id: nonEmptyString(question.id, `OpenMAIC quiz question ${index} id`),
          prompt: nonEmptyString(
            question.question,
            `OpenMAIC quiz question ${index} prompt`,
          ),
          questionType,
          options,
          correctOptionIds: answers,
          explanation:
            optionalNonEmptyString(question.analysis) ??
            optionalNonEmptyString(question.commentPrompt) ??
            "No explanation was supplied by OpenMAIC.",
        };
      }),
    };
  }
  if (sceneType === "interactive") {
    return {
      type: "interactive",
      html: nonEmptyString(record.html, "OpenMAIC interactive HTML"),
      bridgeVersion: "1.0",
      sandbox: { allowScripts: true, allowSameOrigin: false },
    };
  }

  const projectConfig = asRecord(
    record.projectConfig,
    "OpenMAIC PBL project config",
  );
  const projectInfo = asRecord(
    projectConfig.projectInfo,
    "OpenMAIC PBL project info",
  );
  const issueboard = asRecord(
    projectConfig.issueboard,
    "OpenMAIC PBL issue board",
  );
  if (
    !Array.isArray(projectConfig.agents) ||
    projectConfig.agents.length === 0 ||
    !Array.isArray(issueboard.issues) ||
    issueboard.issues.length === 0
  ) {
    throw new Error("OpenMAIC PBL roles and milestones are required");
  }
  return {
    type: "pbl",
    scenario: nonEmptyString(projectInfo.description, "OpenMAIC PBL scenario"),
    roles: projectConfig.agents.map((value, index) => {
      const agent = asRecord(value, `OpenMAIC PBL agent ${index}`);
      const id = nonEmptyString(agent.name, `OpenMAIC PBL agent ${index} name`);
      return {
        id,
        name: optionalNonEmptyString(agent.actor_role) ?? id,
        brief:
          optionalNonEmptyString(agent.system_prompt) ??
          optionalNonEmptyString(agent.actor_role) ??
          id,
      };
    }),
    milestones: issueboard.issues.map((value, index) => {
      const issue = asRecord(value, `OpenMAIC PBL issue ${index}`);
      return {
        id: nonEmptyString(issue.id, `OpenMAIC PBL issue ${index} id`),
        title: nonEmptyString(issue.title, `OpenMAIC PBL issue ${index} title`),
        rubric:
          optionalNonEmptyString(issue.notes) ??
          nonEmptyString(
            issue.description,
            `OpenMAIC PBL issue ${index} description`,
          ),
      };
    }),
  };
}

export interface ContentGenerationResult {
  classroomId: string;
  classroomDocument: PortableClassroomDocument;
  classroomDocumentSha256: string;
  mediaManifestSha256: string;
  artifacts: ArtifactEntry[];
}

export interface ControlledContentOutput {
  classroomDocument: PortableClassroomDocument;
  mediaManifest: PortableClassroomDocument["mediaManifest"];
  sourceJobId: string | null;
}

export class ContentOutputRegistry {
  constructor(
    private readonly stateRoot = isolatedOpenMaicStateRoot("content-outputs"),
  ) {}

  private payloadPath(
    tenantId: string,
    classroomDocumentSha256: string,
    mediaManifestSha256: string,
  ): string {
    return durableFile(
      this.stateRoot,
      "content-outputs",
      "payloads",
      [tenantId, classroomDocumentSha256, mediaManifestSha256],
      "payload.json",
    );
  }

  registerPayload(
    tenantId: string,
    classroomDocument: PortableClassroomDocument,
    mediaManifest: PortableClassroomDocument["mediaManifest"],
    sourceJobId: string | null = null,
  ): {
    classroomDocumentSha256: string;
    mediaManifestSha256: string;
  } {
    const portableDocument = asPortableDocument(classroomDocument);
    if (
      canonicalJson(portableDocument.mediaManifest) !==
      canonicalJson(mediaManifest)
    ) {
      throw new Error(
        "registered media manifest does not match the classroom document",
      );
    }
    const classroomDocumentSha256 = sha256Bytes(
      canonicalJson(portableDocument),
    );
    const mediaManifestSha256 = sha256Bytes(canonicalJson(mediaManifest));
    const record = {
      version: 1,
      tenantId,
      classroomDocumentSha256,
      mediaManifestSha256,
      output: {
        classroomDocument: portableDocument,
        mediaManifest,
        sourceJobId,
      },
    };
    const target = this.payloadPath(
      tenantId,
      classroomDocumentSha256,
      mediaManifestSha256,
    );
    if (!writeDurableJsonExclusive(target, record)) {
      const existing = readDurableJson(target);
      if (canonicalJson(existing) !== canonicalJson(record)) {
        throw new Error(
          "content output registration conflicts with durable state",
        );
      }
    }
    return { classroomDocumentSha256, mediaManifestSha256 };
  }

  register(
    tenantId: string,
    result: ContentGenerationResult,
    sourceJobId: string | null = null,
  ): void {
    if (
      sha256Bytes(canonicalJson(result.classroomDocument)) !==
        result.classroomDocumentSha256 ||
      sha256Bytes(canonicalJson(result.classroomDocument.mediaManifest)) !==
        result.mediaManifestSha256
    ) {
      throw new Error("content output hashes do not match their payloads");
    }
    this.registerPayload(
      tenantId,
      result.classroomDocument,
      result.classroomDocument.mediaManifest,
      sourceJobId,
    );
  }

  resolve(
    tenantId: string,
    classroomDocumentSha256: string,
    mediaManifestSha256: string,
  ): ControlledContentOutput | null {
    if (
      !SHA256_HEX.test(classroomDocumentSha256) ||
      !SHA256_HEX.test(mediaManifestSha256)
    ) {
      throw new Error("content output hash is invalid");
    }
    const value = readDurableJson(
      this.payloadPath(tenantId, classroomDocumentSha256, mediaManifestSha256),
    );
    if (!value) {
      return null;
    }
    const record = exactDurableRecord(value, "content output record", [
      "version",
      "tenantId",
      "classroomDocumentSha256",
      "mediaManifestSha256",
      "output",
    ]);
    if (
      record.version !== 1 ||
      record.tenantId !== tenantId ||
      record.classroomDocumentSha256 !== classroomDocumentSha256 ||
      record.mediaManifestSha256 !== mediaManifestSha256
    ) {
      throw new Error("content output record binding is invalid");
    }
    const output = exactDurableRecord(record.output, "content output payload", [
      "classroomDocument",
      "mediaManifest",
      "sourceJobId",
    ]);
    const classroomDocument = asPortableDocument(output.classroomDocument);
    if (
      (output.sourceJobId !== null &&
        (typeof output.sourceJobId !== "string" ||
          output.sourceJobId.length === 0)) ||
      canonicalJson(classroomDocument.mediaManifest) !==
        canonicalJson(output.mediaManifest) ||
      sha256Bytes(canonicalJson(classroomDocument)) !==
        classroomDocumentSha256 ||
      sha256Bytes(canonicalJson(output.mediaManifest)) !== mediaManifestSha256
    ) {
      throw new Error("content output record integrity validation failed");
    }
    return JSON.parse(
      canonicalJson({
        classroomDocument,
        mediaManifest: output.mediaManifest,
        sourceJobId: output.sourceJobId,
      }),
    ) as ControlledContentOutput;
  }
}

export type EngineJobStatus = "running" | "succeeded" | "failed" | "canceled";

export interface EngineJob<Result = unknown> {
  tenantId: string;
  jobId: string;
  idempotencyKey: string;
  phase: string;
  status: EngineJobStatus;
  createdAt: string;
  updatedAt: string;
  result?: Result;
  error?: { code: string; message: string };
}

interface JobSubmission {
  tenantId: string;
  jobId: string;
  idempotencyKey: string;
  canonicalBody: string;
  phase?: string;
  failureCode?: string;
}

export interface JobPublicationGuard {
  assertActive(): void;
}

export class ContentIdempotencyConflictError extends Error {
  constructor() {
    super("idempotency key conflicts with an existing job");
    this.name = "ContentIdempotencyConflictError";
  }
}

export class ContentCanceledError extends Error {
  constructor() {
    super("content generation was canceled");
    this.name = "ContentCanceledError";
  }
}

export function canonicalConfirmedOutlineJson(value: unknown): string {
  return canonicalJson(value);
}

function sha256Bytes(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function nonEmptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value;
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

export function validateConfirmedOutlineBinding(
  request: Pick<
    ContentGenerationRequest,
    "phase" | "confirmedOutline" | "confirmedOutlineSha256"
  >,
): void {
  if (request.phase !== "content" && request.phase !== "micro") {
    throw new Error("content generation phase is invalid");
  }
  if (
    request.confirmedOutline === undefined ||
    request.confirmedOutline === null ||
    request.confirmedOutlineSha256 === undefined ||
    request.confirmedOutlineSha256 === null
  ) {
    if (request.phase === "micro") {
      return;
    }
    throw new Error("content generation requires a confirmed outline");
  }
  if (!SHA256_HEX.test(request.confirmedOutlineSha256)) {
    throw new Error("confirmed outline hash is invalid");
  }
  const actual = sha256Bytes(
    canonicalConfirmedOutlineJson(request.confirmedOutline),
  );
  if (actual !== request.confirmedOutlineSha256) {
    throw new Error("confirmed outline hash mismatch");
  }
  if (request.confirmedOutline.confirmationMetadata?.status !== "confirmed") {
    throw new Error("content generation requires a confirmed outline");
  }
  if (
    !Array.isArray(request.confirmedOutline.scenes) ||
    request.confirmedOutline.scenes.length === 0
  ) {
    throw new Error("confirmed outline must contain at least one scene");
  }
}

function buildMicroOutline(
  request: ContentGenerationRequest,
  generatedAt: string,
): OutlineBundle {
  const sceneCount = Math.min(
    5,
    request.sceneBudget,
    Math.max(1, request.teachingBrief.objectives.length),
  );
  const knowledgePoints = request.teachingBrief.knowledgePoints;
  const sourceRefs = request.teachingBrief.sourceRefs.map((item) => ({
    ...item,
  }));
  const scenes: OutlineScene[] = Array.from(
    { length: sceneCount },
    (_, index) => {
      const objective =
        request.teachingBrief.objectives[
          index % request.teachingBrief.objectives.length
        ];
      const assignedKnowledge = knowledgePoints
        .filter(
          (_item, knowledgeIndex) => knowledgeIndex % sceneCount === index,
        )
        .map((item) => item.knowledgePointId);
      const fallback =
        knowledgePoints[index % knowledgePoints.length].knowledgePointId;
      return {
        sceneId: `${request.jobId}-micro-scene-${index + 1}`,
        title: knowledgePoints[index % knowledgePoints.length].title,
        summary: objective.description,
        knowledgePointIds:
          assignedKnowledge.length > 0 ? assignedKnowledge : [fallback],
        sourceRefs: sourceRefs.map((item) => ({ ...item })),
      };
    },
  );
  const outline: OutlineBundle = {
    schemaVersion: "1.0",
    outlineId: `micro-outline-${request.jobId}`,
    outlineVersion: 1,
    confirmationMetadata: {
      status: "confirmed",
      confirmedAt: generatedAt,
      confirmedBy: "yfeistai-micro-direct",
    },
    title: knowledgePoints[0].title,
    language: inferTeachingBriefLanguage(request),
    scenes,
    knowledgeCoverage: knowledgePoints.map((point) => ({
      knowledgePointId: point.knowledgePointId,
      sceneIds: scenes
        .filter((scene) =>
          scene.knowledgePointIds.includes(point.knowledgePointId),
        )
        .map((scene) => scene.sceneId),
    })),
    sourceRefs,
    estimatedSceneCount: scenes.length,
    generationMetadata: {
      generator: "openmaic",
      generatorVersion: OPENMAIC_APP_VERSION,
      modelId: "server-selected-model",
      generatedAt,
      teachingBriefId: request.teachingBriefId,
      teachingBriefSha256: request.teachingBriefSha256,
      templateId: request.templateId,
      templateVersion: request.templateVersion,
    },
    contractSha256: OUTLINE_BUNDLE_CONTRACT_SHA256,
  };
  return validateOutlineBundle(outline, request, {
    confirmationStatus: "confirmed",
  });
}

function inferTeachingBriefLanguage(request: ContentGenerationRequest): string {
  const sample = [
    request.teachingBrief.gradeBand,
    request.teachingBrief.audienceLevel,
    ...request.teachingBrief.objectives.map((item) => item.description),
    ...request.teachingBrief.knowledgePoints.flatMap((item) => [
      item.title,
      item.description,
    ]),
    ...request.teachingBrief.sourceFragments.map((item) => item.text),
  ].join("\n");
  if (/\p{Script=Han}/u.test(sample)) {
    return "zh-CN";
  }
  if (/\p{Script=Hiragana}|\p{Script=Katakana}/u.test(sample)) {
    return "ja-JP";
  }
  if (/\p{Script=Hangul}/u.test(sample)) {
    return "ko-KR";
  }
  return "en-US";
}

function resolveContentOutline(
  request: ContentGenerationRequest,
  generatedAt: string,
): OutlineBundle {
  validateConfirmedOutlineBinding(request);
  if (request.confirmedOutline) {
    return request.confirmedOutline;
  }
  return buildMicroOutline(request, generatedAt);
}

function sceneTypeFor(
  request: ContentGenerationRequest,
  scene: OutlineScene,
  order: number,
  sceneCount: number,
): PortableScene["type"] {
  const description = `${scene.title}\n${scene.summary}`.toLowerCase();
  if (/\b(?:quiz|test|assessment)\b|测验|测试|练习题/u.test(description)) {
    return "quiz";
  }
  if (/\b(?:project|pbl|case study)\b|项目|课题/u.test(description)) {
    return "pbl";
  }
  if (
    /\b(?:interactive|simulation|discussion)\b|互动|模拟|讨论/u.test(
      description,
    )
  ) {
    return "interactive";
  }
  if (sceneCount > 1 && order === sceneCount - 1) {
    const methods = request.teachingBrief.assessment.methods;
    if (methods.includes("quiz")) {
      return "quiz";
    }
    if (methods.includes("project")) {
      return "pbl";
    }
    if (
      methods.some((method) =>
        ["discussion", "observation", "self_assessment"].includes(method),
      )
    ) {
      return "interactive";
    }
  }
  return "slide";
}

async function assertNotCanceled(
  isCanceled?: () => boolean | Promise<boolean>,
): Promise<void> {
  if (isCanceled && (await isCanceled())) {
    throw new ContentCanceledError();
  }
}

function normalizePortableScene(
  generated: PortableScene | GeneratedSceneResult,
  outlineScene: OutlineScene,
  stageId: string,
  order: number,
): { scene: PortableScene; media: GeneratedSceneResult["media"] } {
  const wrapped =
    "scene" in generated ? generated : { scene: generated, media: undefined };
  const candidate = asRecord(wrapped.scene, "generated scene");
  const type = nonEmptyString(candidate.type, "generated scene type");
  if (!["slide", "quiz", "interactive", "pbl"].includes(type)) {
    throw new Error("generated scene type is unsupported");
  }
  const content = asRecord(candidate.content, "generated scene content");
  if (content.type !== type) {
    throw new Error("generated scene content type does not match its scene");
  }
  if (type === "slide") {
    asRecord(content.canvas, "slide canvas");
  } else if (type === "quiz") {
    if (!Array.isArray(content.questions) || content.questions.length === 0) {
      throw new Error("quiz scene must contain questions");
    }
  } else if (type === "interactive") {
    nonEmptyString(content.html, "interactive HTML");
    content.bridgeVersion = "1.0";
    content.sandbox = { allowScripts: true, allowSameOrigin: false };
  } else {
    nonEmptyString(content.scenario, "PBL scenario");
    if (!Array.isArray(content.roles) || content.roles.length === 0) {
      throw new Error("PBL scene must contain roles");
    }
    if (!Array.isArray(content.milestones) || content.milestones.length === 0) {
      throw new Error("PBL scene must contain milestones");
    }
  }

  const actions = candidate.actions ?? [];
  if (!Array.isArray(actions)) {
    throw new Error("generated scene actions must be an array");
  }
  assertPortableValue(content, "generated scene content");
  assertPortableValue(actions, "generated scene actions");

  const scene: PortableScene = {
    id:
      typeof candidate.id === "string" && candidate.id.length > 0
        ? candidate.id
        : outlineScene.sceneId,
    stageId,
    title:
      typeof candidate.title === "string" && candidate.title.length > 0
        ? candidate.title
        : outlineScene.title,
    order,
    type: type as PortableScene["type"],
    content: content as unknown as PortableSceneContent,
    actions: actions as Array<Record<string, JsonValue>>,
  };
  return { scene, media: wrapped.media };
}

async function addMediaManifest(
  target: PortableClassroomDocument["mediaManifest"],
  generated: GeneratedSceneResult["media"],
  seenMediaIds: Set<string>,
  seenPaths: Set<string>,
  policy: GenerationRequest["teachingBrief"]["mediaPolicy"],
  expiresAt: string,
  writeArtifact?: ContentGenerationDependencies["writeArtifact"],
  assertPublicationActive?: () => void,
): Promise<ArtifactEntry[]> {
  const artifacts: ArtifactEntry[] = [];
  for (const item of generated ?? []) {
    if (!policy.allowGeneration) {
      throw new Error("teaching brief forbids generated media");
    }
    const mediaId = nonEmptyString(item.mediaId, "media id");
    const relativePath = normalizeArtifactPath(
      nonEmptyString(item.relativePath, "media relative path"),
    );
    if (seenMediaIds.has(mediaId) || seenPaths.has(relativePath)) {
      throw new Error("generated media identifiers and paths must be unique");
    }
    if (!(item.bytes instanceof Uint8Array) || item.bytes.byteLength === 0) {
      throw new Error("generated media bytes are missing");
    }
    const mime = nonEmptyString(item.mime, "generated media MIME type");
    const allowedMimeTypes = new Set(
      policy.allowedMimeTypes.map((value) => value.toLowerCase()),
    );
    if (!allowedMimeTypes.has(mime.toLowerCase())) {
      throw new Error("generated media MIME type is not allowed");
    }
    assertArtifactMimeBytes(item.bytes, mime);
    const expected = createArtifactEntry({
      relativePath,
      bytes: item.bytes,
      mime,
      expiresAt,
    });
    if (writeArtifact) {
      assertPublicationActive?.();
    }
    const written = writeArtifact
      ? await writeArtifact({
          relativePath,
          bytes: item.bytes,
          mime,
          expiresAt,
        })
      : expected;
    if (
      written.relativePath !== expected.relativePath ||
      written.sha256 !== expected.sha256 ||
      written.bytes !== expected.bytes ||
      written.mime !== expected.mime ||
      written.expiresAt !== expected.expiresAt ||
      typeof written.downloadPath !== "string" ||
      written.downloadPath.length === 0
    ) {
      throw new Error("generated media writer integrity binding failed");
    }
    seenMediaIds.add(mediaId);
    seenPaths.add(relativePath);
    const entry = { ...written };
    artifacts.push(entry);
    target.push({
      mediaId,
      relativePath,
      mimeType: entry.mime,
      sha256: entry.sha256,
      sizeBytes: entry.bytes,
      temporaryDownloadPath: entry.downloadPath,
      expiresAt: entry.expiresAt,
    });
  }
  return artifacts;
}

const MEDIA_REFERENCE_KEY =
  /(?:^|_)(?:src|href|url|path)$|(?:audio|video|image|media)(?:src|url|path)$/i;

function collectMediaReferences(
  value: unknown,
  target: Set<string>,
  key = "",
): void {
  if (typeof value === "string") {
    if (/^(?:blob|data):/i.test(value)) {
      throw new Error(
        "generated media must be materialized before publication",
      );
    }
    if (value.startsWith("media/") || MEDIA_REFERENCE_KEY.test(key)) {
      target.add(normalizeArtifactPath(value));
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectMediaReferences(item, target, key));
    return;
  }
  if (value === null || typeof value !== "object") {
    return;
  }
  for (const [childKey, child] of Object.entries(value)) {
    collectMediaReferences(child, target, childKey);
  }
}

export async function generateContentJob(
  request: ContentGenerationRequest,
  dependencies: ContentGenerationDependencies,
): Promise<ContentGenerationResult> {
  validateGenerationRequest(request, "classroom");
  await assertNotCanceled(dependencies.isCanceled);

  const now = (dependencies.now ?? (() => new Date()))();
  const generatedAt = now.toISOString();
  const expiresAt = new Date(
    now.getTime() +
      (dependencies.artifactTtlMilliseconds ?? 24 * 60 * 60 * 1_000),
  ).toISOString();
  const outline = resolveContentOutline(request, generatedAt);
  const stageId = `stage-${request.jobId}`;
  const classroomId = `classroom-${request.jobId}`;
  const classroomVersionId = `${classroomId}-v1`;
  const scenes: PortableScene[] = [];
  const mediaManifest: PortableClassroomDocument["mediaManifest"] = [];
  const mediaArtifacts: ArtifactEntry[] = [];
  const mediaIds = new Set<string>();
  const mediaPaths = new Set<string>();

  for (const [order, outlineScene] of outline.scenes.entries()) {
    await assertNotCanceled(dependencies.isCanceled);
    const generated = await dependencies.generateScenes(outlineScene, {
      tenantId: request.tenantId,
      jobId: request.jobId,
      stageId,
      order,
      outline,
      phase: request.phase,
      mediaPolicy: {
        allowGeneration: request.teachingBrief.mediaPolicy.allowGeneration,
        allowedMimeTypes: [
          ...request.teachingBrief.mediaPolicy.allowedMimeTypes,
        ],
      },
      sceneType: sceneTypeFor(
        request,
        outlineScene,
        order,
        outline.scenes.length,
      ),
      sourceFragments: request.teachingBrief.sourceFragments.map(
        (fragment) => ({
          ...fragment,
        }),
      ),
    });
    const normalized = normalizePortableScene(
      generated,
      outlineScene,
      stageId,
      order,
    );
    scenes.push(normalized.scene);
    mediaArtifacts.push(
      ...(await addMediaManifest(
        mediaManifest,
        normalized.media,
        mediaIds,
        mediaPaths,
        request.teachingBrief.mediaPolicy,
        expiresAt,
        dependencies.writeArtifact,
        dependencies.assertPublicationActive,
      )),
    );
    await assertNotCanceled(dependencies.isCanceled);
  }
  if (scenes.length !== outline.scenes.length) {
    throw new Error("not every confirmed outline scene was generated");
  }
  const referencedMediaPaths = new Set<string>();
  for (const scene of scenes) {
    collectMediaReferences(scene.content, referencedMediaPaths);
    collectMediaReferences(scene.actions, referencedMediaPaths);
  }
  for (const referencedPath of referencedMediaPaths) {
    if (!mediaPaths.has(referencedPath)) {
      throw new Error(
        "generated media reference is missing from the artifact manifest",
      );
    }
  }

  const generationMetadata: GenerationMetadata = {
    ...outline.generationMetadata,
    generator: "openmaic",
    generatorVersion: OPENMAIC_APP_VERSION,
    generatedAt,
  };
  const contentMode = request.teachingBrief.contentMode;
  if (contentMode === "source_grounded" && outline.sourceRefs.length === 0) {
    throw new Error("source-grounded classroom requires source references");
  }
  const documentWithoutHash = {
    schemaVersion: "1.0" as const,
    classroomId,
    classroomVersionId,
    contentMode,
    openCreation: contentMode === "open_creation",
    openmaic: {
      dslVersion: "0.1.0" as const,
      stage: {
        id: stageId,
        name: outline.title,
        createdAt: generatedAt,
        updatedAt: generatedAt,
      },
      scenes,
    },
    interactionIds: scenes
      .filter((scene) => scene.type !== "slide")
      .map((scene) => scene.id),
    sourceRefs: outline.sourceRefs.map((ref) => ({ ...ref })),
    knowledgePointMappings: outline.knowledgeCoverage.map((item) => ({
      knowledgePointId: item.knowledgePointId,
      sceneIds: [...item.sceneIds],
      sourceRefs: outline.sourceRefs.map((ref) => ({ ...ref })),
    })),
    mediaManifest,
    exportManifest: [] as [],
    generationMetadata,
    auditMetadata: {
      templateId: request.templateId,
      templateVersion: request.templateVersion,
      teachingBriefId: request.teachingBriefId,
      teachingBriefSha256: request.teachingBriefSha256,
      parentClassroomVersionId: null,
    },
    validationResult: {
      valid: true as const,
      issues: [] as [],
      validatedAt: generatedAt,
    },
    migrationRecords: [] as [],
  };
  if (documentWithoutHash.knowledgePointMappings.length === 0) {
    throw new Error("classroom document requires knowledge point mappings");
  }
  assertPortableValue(documentWithoutHash as unknown, "classroom document");
  const classroomDocumentCandidate: PortableClassroomDocument = {
    ...documentWithoutHash,
    fileSha256: sha256Bytes(canonicalJson(documentWithoutHash)),
  };
  const classroomDocument = asPortableDocument(classroomDocumentCandidate);
  const classroomDocumentSha256 = sha256Bytes(canonicalJson(classroomDocument));
  const mediaManifestSha256 = sha256Bytes(canonicalJson(mediaManifest));
  const bytes = new TextEncoder().encode(canonicalJson(classroomDocument));
  const expectedDocumentArtifact = createArtifactEntry({
    relativePath: "classroom/classroom.json",
    bytes,
    mime: "application/json",
    expiresAt,
  });
  let documentArtifact = expectedDocumentArtifact;
  if (dependencies.writeArtifact) {
    dependencies.assertPublicationActive?.();
    documentArtifact = await dependencies.writeArtifact({
      relativePath: "classroom/classroom.json",
      bytes,
      mime: "application/json",
      expiresAt,
    });
  }
  if (
    documentArtifact.relativePath !== expectedDocumentArtifact.relativePath ||
    documentArtifact.sha256 !== expectedDocumentArtifact.sha256 ||
    documentArtifact.bytes !== expectedDocumentArtifact.bytes ||
    documentArtifact.mime !== expectedDocumentArtifact.mime ||
    documentArtifact.expiresAt !== expectedDocumentArtifact.expiresAt ||
    typeof documentArtifact.downloadPath !== "string" ||
    documentArtifact.downloadPath.length === 0
  ) {
    throw new Error("classroom document writer integrity binding failed");
  }
  await assertNotCanceled(dependencies.isCanceled);
  return {
    classroomId,
    classroomDocument,
    classroomDocumentSha256,
    mediaManifestSha256,
    artifacts: [documentArtifact, ...mediaArtifacts],
  };
}

function jobKey(tenantId: string, jobId: string): string {
  return `${tenantId}\0${jobId}`;
}

function resultFailure(
  value: unknown,
): { code: string; message: string } | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (
    record.status !== "failed" ||
    record.error === null ||
    typeof record.error !== "object" ||
    Array.isArray(record.error)
  ) {
    return null;
  }
  const error = record.error as Record<string, unknown>;
  return typeof error.code === "string" &&
    error.code.length > 0 &&
    typeof error.message === "string" &&
    error.message.length > 0
    ? { code: error.code, message: error.message }
    : null;
}

export class ContentJobStore<Result = unknown> {
  private readonly completions = new Map<string, Promise<EngineJob<Result>>>();

  constructor(
    private readonly stateRoot = isolatedOpenMaicStateRoot("content-jobs"),
    private readonly namespace = "content-jobs",
    private readonly leaseMilliseconds = 60_000,
    private readonly nowMilliseconds: () => number = Date.now,
    private readonly heartbeatEnabled = true,
  ) {}

  private submissionPath(tenantId: string, jobId: string): string {
    return durableFile(
      this.stateRoot,
      this.namespace,
      "jobs",
      [tenantId, jobId],
      "submission.json",
    );
  }

  private terminalPath(tenantId: string, jobId: string): string {
    return durableFile(
      this.stateRoot,
      this.namespace,
      "jobs",
      [tenantId, jobId],
      "terminal.json",
    );
  }

  private leasePath(tenantId: string, jobId: string): string {
    return durableFile(
      this.stateRoot,
      this.namespace,
      "jobs",
      [tenantId, jobId],
      "lease.json",
    );
  }

  private jobDirectory(tenantId: string, jobId: string): string {
    return path.dirname(this.submissionPath(tenantId, jobId));
  }

  private bindingPath(tenantId: string, idempotency: string): string {
    return durableFile(
      this.stateRoot,
      this.namespace,
      "bindings",
      [tenantId, idempotency],
      "binding.json",
    );
  }

  private readSubmission(
    tenantId: string,
    jobId: string,
  ): Record<string, unknown> | null {
    const value = readDurableJson(this.submissionPath(tenantId, jobId));
    if (!value) {
      return null;
    }
    const record = exactDurableRecord(value, "job submission record", [
      "version",
      "tenantId",
      "jobId",
      "idempotencyKey",
      "phase",
      "bodySha256",
      "createdAt",
    ]);
    if (
      record.version !== 1 ||
      record.tenantId !== tenantId ||
      record.jobId !== jobId ||
      typeof record.idempotencyKey !== "string" ||
      typeof record.phase !== "string" ||
      typeof record.bodySha256 !== "string" ||
      !SHA256_HEX.test(record.bodySha256) ||
      typeof record.createdAt !== "string" ||
      !Number.isFinite(Date.parse(record.createdAt))
    ) {
      throw new Error("job submission record binding is invalid");
    }
    return record;
  }

  private readTerminal(
    tenantId: string,
    jobId: string,
  ): EngineJob<Result> | null {
    const value = readDurableJson(this.terminalPath(tenantId, jobId));
    if (!value) {
      return null;
    }
    const record = exactDurableRecord(value, "job terminal record", [
      "version",
      "job",
    ]);
    const job = record.job;
    if (
      record.version !== 1 ||
      job === null ||
      typeof job !== "object" ||
      Array.isArray(job)
    ) {
      throw new Error("job terminal record is corrupt");
    }
    const candidate = job as EngineJob<Result>;
    if (
      candidate.tenantId !== tenantId ||
      candidate.jobId !== jobId ||
      !["succeeded", "failed", "canceled"].includes(candidate.status) ||
      typeof candidate.idempotencyKey !== "string" ||
      typeof candidate.phase !== "string" ||
      !Number.isFinite(Date.parse(candidate.createdAt)) ||
      !Number.isFinite(Date.parse(candidate.updatedAt))
    ) {
      throw new Error("job terminal record binding is invalid");
    }
    return JSON.parse(canonicalJson(candidate)) as EngineJob<Result>;
  }

  private readLease(tenantId: string, jobId: string) {
    return readDurableLease(
      this.leasePath(tenantId, jobId),
      jobKey(tenantId, jobId),
    );
  }

  private claimExecution(
    tenantId: string,
    jobId: string,
  ): DurableLeaseClaim | null {
    return claimDurableLease({
      directory: this.jobDirectory(tenantId, jobId),
      target: this.leasePath(tenantId, jobId),
      binding: jobKey(tenantId, jobId),
      leaseMilliseconds: this.leaseMilliseconds,
      now: this.nowMilliseconds(),
      mayClaim: () => !this.readTerminal(tenantId, jobId),
    });
  }

  private renewLease(
    tenantId: string,
    jobId: string,
    claim: DurableLeaseClaim,
  ): boolean {
    return renewDurableLease({
      directory: this.jobDirectory(tenantId, jobId),
      target: this.leasePath(tenantId, jobId),
      binding: jobKey(tenantId, jobId),
      claim,
      leaseMilliseconds: this.leaseMilliseconds,
      now: this.nowMilliseconds(),
      mayRenew: () => !this.readTerminal(tenantId, jobId),
    });
  }

  private assertPublicationActive(
    tenantId: string,
    jobId: string,
    claim: DurableLeaseClaim,
  ): void {
    withDurableLock(this.jobDirectory(tenantId, jobId), () => {
      if (
        this.readTerminal(tenantId, jobId) ||
        !durableLeaseMatches(
          this.leasePath(tenantId, jobId),
          jobKey(tenantId, jobId),
          claim,
          this.nowMilliseconds(),
        )
      ) {
        throw new Error("job execution lease was fenced");
      }
    });
  }

  private persistTerminal(
    job: EngineJob<Result>,
    claim?: DurableLeaseClaim,
    publishSucceeded?: (result: Result) => void,
  ): EngineJob<Result> {
    return withDurableLock(this.jobDirectory(job.tenantId, job.jobId), () => {
      const existing = this.readTerminal(job.tenantId, job.jobId);
      if (existing) {
        return existing;
      }
      if (claim) {
        const lease = this.readLease(job.tenantId, job.jobId);
        if (
          !durableLeaseMatches(
            this.leasePath(job.tenantId, job.jobId),
            jobKey(job.tenantId, job.jobId),
            claim,
            this.nowMilliseconds(),
          )
        ) {
          const submission = this.readSubmission(job.tenantId, job.jobId);
          if (!submission) {
            throw new Error("job submission state disappeared");
          }
          return this.runningJob(submission, lease);
        }
      }
      if (job.status === "succeeded" && publishSucceeded) {
        if (job.result === undefined) {
          throw new Error("succeeded job result is missing");
        }
        publishSucceeded(job.result);
      }
      const target = this.terminalPath(job.tenantId, job.jobId);
      if (!writeDurableJsonExclusive(target, { version: 1, job })) {
        const winner = this.readTerminal(job.tenantId, job.jobId);
        if (!winner) {
          throw new Error("job terminal state disappeared");
        }
        return winner;
      }
      return JSON.parse(canonicalJson(job)) as EngineJob<Result>;
    });
  }

  private runningJob(
    record: Record<string, unknown>,
    lease = this.readLease(record.tenantId as string, record.jobId as string),
  ): EngineJob<Result> {
    return {
      tenantId: record.tenantId as string,
      jobId: record.jobId as string,
      idempotencyKey: record.idempotencyKey as string,
      phase: record.phase as string,
      status: "running",
      createdAt: record.createdAt as string,
      updatedAt: lease?.updatedAt ?? (record.createdAt as string),
    };
  }

  start(
    submission: JobSubmission,
    run: (publication: JobPublicationGuard) => Promise<Result>,
    publishSucceeded?: (result: Result) => void,
  ): Promise<EngineJob<Result>> {
    const key = jobKey(submission.tenantId, submission.jobId);
    const bodySha256 = sha256Bytes(submission.canonicalBody);
    const phase = submission.phase ?? "content";
    const binding = {
      version: 1,
      tenantId: submission.tenantId,
      idempotencyKey: submission.idempotencyKey,
      jobId: submission.jobId,
      phase,
      bodySha256,
    };
    const bindingPath = this.bindingPath(
      submission.tenantId,
      submission.idempotencyKey,
    );
    if (!writeDurableJsonExclusive(bindingPath, binding)) {
      const existingBinding = readDurableJson(bindingPath);
      if (canonicalJson(existingBinding) !== canonicalJson(binding)) {
        throw new ContentIdempotencyConflictError();
      }
    }

    const createdAt = new Date(this.nowMilliseconds()).toISOString();
    const persistedSubmission = {
      version: 1,
      tenantId: submission.tenantId,
      jobId: submission.jobId,
      idempotencyKey: submission.idempotencyKey,
      phase,
      bodySha256,
      createdAt,
    };
    const submissionPath = this.submissionPath(
      submission.tenantId,
      submission.jobId,
    );
    let durableSubmission: Record<string, unknown> = persistedSubmission;
    if (!writeDurableJsonExclusive(submissionPath, persistedSubmission)) {
      const existing = this.readSubmission(
        submission.tenantId,
        submission.jobId,
      );
      if (
        !existing ||
        existing.idempotencyKey !== submission.idempotencyKey ||
        existing.bodySha256 !== bodySha256 ||
        existing.phase !== phase
      ) {
        throw new ContentIdempotencyConflictError();
      }
      const active = this.completions.get(key);
      if (active) {
        return active;
      }
      const terminal = this.readTerminal(submission.tenantId, submission.jobId);
      if (terminal) {
        return Promise.resolve(terminal);
      }
      durableSubmission = existing;
    }

    const claim = this.claimExecution(submission.tenantId, submission.jobId);
    if (!claim) {
      const terminal = this.readTerminal(submission.tenantId, submission.jobId);
      return Promise.resolve(terminal ?? this.runningJob(durableSubmission));
    }
    const running = this.runningJob(durableSubmission);
    const heartbeat = this.heartbeatEnabled
      ? setInterval(
          () => {
            try {
              this.renewLease(submission.tenantId, submission.jobId, claim);
            } catch {
              // The fenced terminal write below remains authoritative.
            }
          },
          Math.max(100, Math.floor(this.leaseMilliseconds / 3)),
        )
      : null;
    const completion = (async (): Promise<EngineJob<Result>> => {
      let terminal: EngineJob<Result>;
      try {
        const result = await run({
          assertActive: () =>
            this.assertPublicationActive(
              submission.tenantId,
              submission.jobId,
              claim,
            ),
        });
        const failure = resultFailure(result);
        terminal = failure
          ? {
              ...running,
              status: failure.code === "JOB_CANCELED" ? "canceled" : "failed",
              updatedAt: new Date(this.nowMilliseconds()).toISOString(),
              error: failure,
            }
          : {
              ...running,
              status: "succeeded",
              updatedAt: new Date(this.nowMilliseconds()).toISOString(),
              result,
            };
      } catch (error) {
        terminal = {
          ...running,
          status: error instanceof ContentCanceledError ? "canceled" : "failed",
          updatedAt: new Date(this.nowMilliseconds()).toISOString(),
          error:
            error instanceof ContentCanceledError
              ? { code: "JOB_CANCELED", message: "The job was canceled." }
              : {
                  code: submission.failureCode ?? "CONTENT_GENERATION_FAILED",
                  message:
                    submission.failureCode === "EXPORT_FAILED"
                      ? "Export generation failed."
                      : "Content generation failed.",
                },
        };
      }
      if (heartbeat) {
        clearInterval(heartbeat);
      }
      return this.persistTerminal(terminal, claim, publishSucceeded);
    })();
    this.completions.set(key, completion);
    void completion.then(
      () => this.completions.delete(key),
      () => this.completions.delete(key),
    );
    return completion;
  }

  async read(
    tenantId: string,
    jobId: string,
  ): Promise<EngineJob<Result> | null> {
    const submission = this.readSubmission(tenantId, jobId);
    if (!submission) {
      return null;
    }
    return this.readTerminal(tenantId, jobId) ?? this.runningJob(submission);
  }

  isCanceled(tenantId: string, jobId: string): boolean {
    return this.readTerminal(tenantId, jobId)?.status === "canceled";
  }

  async cancel(
    tenantId: string,
    jobId: string,
  ): Promise<EngineJobStatus | null> {
    const submission = this.readSubmission(tenantId, jobId);
    if (!submission) {
      return null;
    }
    const terminal = this.readTerminal(tenantId, jobId);
    if (terminal) {
      return terminal.status;
    }
    const canceled = this.persistTerminal({
      ...this.runningJob(submission),
      status: "canceled",
      updatedAt: new Date(this.nowMilliseconds()).toISOString(),
      error: {
        code: "JOB_CANCELED",
        message: "The job was canceled.",
      },
    });
    return canceled.status;
  }
}

const CONTENT_STORE_KEY = Symbol.for("yfeistai.openmaic.content-job-store");
const contentGlobal = globalThis as typeof globalThis & {
  [CONTENT_STORE_KEY]?: ContentJobStore<ContentGenerationResult>;
};

export const contentJobStore =
  contentGlobal[CONTENT_STORE_KEY] ??
  (contentGlobal[CONTENT_STORE_KEY] =
    new ContentJobStore<ContentGenerationResult>(
      configuredOpenMaicStateRoot(),
      "content-jobs",
    ));

const CONTENT_OUTPUT_REGISTRY_KEY = Symbol.for(
  "yfeistai.openmaic.content-output-registry",
);
const contentOutputGlobal = globalThis as typeof globalThis & {
  [CONTENT_OUTPUT_REGISTRY_KEY]?: ContentOutputRegistry;
};

export const contentOutputRegistry =
  contentOutputGlobal[CONTENT_OUTPUT_REGISTRY_KEY] ??
  (contentOutputGlobal[CONTENT_OUTPUT_REGISTRY_KEY] = new ContentOutputRegistry(
    configuredOpenMaicStateRoot(),
  ));

function parseContentRequest(value: unknown): ContentGenerationRequest {
  const request = validateGenerationRequest(value, "classroom");
  if (request.phase !== "content" && request.phase !== "micro") {
    throw new Error("classroom endpoint phase is invalid");
  }
  const contentRequest = request as ContentGenerationRequest;
  validateConfirmedOutlineBinding(contentRequest);
  return contentRequest;
}

export interface ClassroomPostHandlerDependencies
  extends ContentGenerationDependencies, ServiceBoundaryDependencies {
  store: ContentJobStore<ContentGenerationResult>;
  artifactStore?: ArtifactStore;
  outputRegistry?: ContentOutputRegistry;
}

export function createClassroomPostHandler(
  dependencies: ClassroomPostHandlerDependencies,
): (request: Request) => Promise<Response> {
  return async (request: Request): Promise<Response> => {
    const body = await request.text();
    const signed = authenticateServiceRequest(request, body, dependencies);
    if (!signed) {
      return serviceError(
        401,
        "AUTHENTICATION_FAILED",
        "Service authentication failed.",
      );
    }
    if (
      request.method !== "POST" ||
      new URL(request.url).pathname !== "/api/yfeistai/v1/classrooms"
    ) {
      return serviceError(404, "ROUTE_NOT_FOUND", "Route not found.");
    }

    let parsed: ContentGenerationRequest;
    try {
      parsed = parseContentRequest(JSON.parse(body));
    } catch {
      return serviceError(400, "INVALID_REQUEST", "Request body is invalid.");
    }
    if (!hasSignedBodyBinding(signed, parsed)) {
      return serviceError(
        403,
        "REQUEST_BINDING_MISMATCH",
        "Signed request metadata does not match the request body.",
      );
    }

    try {
      const completion = dependencies.store.start(
        {
          tenantId: parsed.tenantId,
          jobId: parsed.jobId,
          idempotencyKey: parsed.idempotencyKey,
          canonicalBody: canonicalJson(parsed),
          phase: parsed.phase,
        },
        async (publication) => {
          const result = await generateContentJob(parsed, {
            ...dependencies,
            assertPublicationActive: publication.assertActive,
            isCanceled: () =>
              dependencies.store.isCanceled(parsed.tenantId, parsed.jobId),
            writeArtifact: dependencies.artifactStore
              ? (input) =>
                  dependencies.artifactStore!.put({
                    tenantId: parsed.tenantId,
                    jobId: parsed.jobId,
                    assertPublicationActive: publication.assertActive,
                    ...input,
                  })
              : dependencies.writeArtifact,
          });
          return result;
        },
        (result) => {
          (dependencies.outputRegistry ?? contentOutputRegistry).register(
            parsed.tenantId,
            result,
            parsed.jobId,
          );
        },
      );
      void completion;
      const job = await dependencies.store.read(parsed.tenantId, parsed.jobId);
      return Response.json(job, { status: 202 });
    } catch (error) {
      if (error instanceof ContentIdempotencyConflictError) {
        return serviceError(
          409,
          "IDEMPOTENCY_CONFLICT",
          "The idempotency key conflicts with an existing job.",
        );
      }
      return serviceError(
        500,
        "CONTENT_GENERATION_FAILED",
        "Content generation failed.",
      );
    }
  };
}

export function createClassroomGetHandler(
  dependencies: ServiceBoundaryDependencies & {
    store: ContentJobStore<ContentGenerationResult>;
  },
): (request: Request, context: JobRouteContext) => Promise<Response> {
  return async (
    request: Request,
    context: JobRouteContext,
  ): Promise<Response> => {
    const signed = authenticateServiceRequest(request, "", dependencies);
    if (!signed) {
      return serviceError(
        401,
        "AUTHENTICATION_FAILED",
        "Service authentication failed.",
      );
    }
    const { jobId } = await context.params;
    const expectedPath = `/api/yfeistai/v1/classrooms/${encodeURIComponent(jobId)}`;
    if (
      request.method !== "GET" ||
      new URL(request.url).pathname !== expectedPath
    ) {
      return serviceError(404, "ROUTE_NOT_FOUND", "Route not found.");
    }
    if (signed.jobId !== jobId) {
      return serviceError(
        403,
        "REQUEST_BINDING_MISMATCH",
        "Signed request metadata does not match the requested job.",
      );
    }
    const job = await dependencies.store.read(signed.tenantId, jobId);
    if (!job) {
      return serviceError(404, "JOB_NOT_FOUND", "Content job was not found.");
    }
    return Response.json(job, { status: 200 });
  };
}

export function createJobCancelHandler(
  dependencies: ServiceBoundaryDependencies & {
    stores: Array<{
      cancel(tenantId: string, jobId: string): Promise<EngineJobStatus | null>;
    }>;
    onCanceled?: (tenantId: string, jobId: string) => Promise<void>;
  },
): (request: Request, context: JobRouteContext) => Promise<Response> {
  return async (
    request: Request,
    context: JobRouteContext,
  ): Promise<Response> => {
    const body = await request.text();
    const signed = authenticateServiceRequest(request, body, dependencies);
    if (!signed) {
      return serviceError(
        401,
        "AUTHENTICATION_FAILED",
        "Service authentication failed.",
      );
    }
    const { jobId } = await context.params;
    const expectedPath = `/api/yfeistai/v1/jobs/${encodeURIComponent(jobId)}/cancel`;
    if (
      request.method !== "POST" ||
      new URL(request.url).pathname !== expectedPath
    ) {
      return serviceError(404, "ROUTE_NOT_FOUND", "Route not found.");
    }
    if (signed.jobId !== jobId) {
      return serviceError(
        403,
        "REQUEST_BINDING_MISMATCH",
        "Signed request metadata does not match the requested job.",
      );
    }
    for (const store of dependencies.stores) {
      const status = await store.cancel(signed.tenantId, jobId);
      if (status) {
        if (status === "canceled" && dependencies.onCanceled) {
          try {
            await dependencies.onCanceled(signed.tenantId, jobId);
          } catch {
            // Durable cancellation remains authoritative if external cleanup fails.
          }
        }
        return Response.json(
          { jobId, status },
          { status: status === "canceled" ? 202 : 200 },
        );
      }
    }
    return serviceError(404, "JOB_NOT_FOUND", "Job was not found.");
  };
}
