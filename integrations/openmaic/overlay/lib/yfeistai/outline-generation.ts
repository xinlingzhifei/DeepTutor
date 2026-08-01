import {
  OPENMAIC_APP_VERSION,
  type GenerationRequest,
  type OutlineBundle,
  type OutlineJob,
  type SourceReference,
  type TeachingBrief,
} from "./contracts";
import { IdempotencyConflictError, type OutlineJobStore } from "./job-store";
import {
  type SignedServiceRequest,
  verifyServiceRequest,
} from "./service-auth";

export const OUTLINE_BUNDLE_CONTRACT_SHA256 =
  "f8ddb7c11138f402ed048c4af2010714b2bfd456e5c38122920c689e4a2b3ddf" as const;

const SHA256_HEX = /^[0-9a-f]{64}$/;
const OPAQUE_IDENTIFIER = /^[^:\s]+$/;
const ROUTING_ALIASES = new Set([
  ["api", "key"].join(""),
  ["base", "url"].join(""),
  ["provider", "id"].join(""),
  ["provider", "key"].join(""),
  ["provider", "api", "key"].join(""),
  ["provider", "base", "url"].join(""),
]);

type JsonPrimitive = string | number | boolean | null;
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export interface OutlineGenerationDependencies {
  generateOutlines(request: GenerationRequest): Promise<unknown>;
  generateScenes?: (...args: unknown[]) => unknown;
  now?: () => Date;
}

export interface UpstreamOutlineResult {
  languageDirective: string;
  courseTitle?: string;
  outlines: Array<{
    id?: string;
    title?: string;
    description?: string;
  }>;
}

export interface OutlinePostHandlerDependencies extends OutlineGenerationDependencies {
  readSecret(): string;
  store: OutlineJobStore;
  nowSeconds?: () => number;
}

export interface OutlineGetHandlerDependencies {
  readSecret(): string;
  store: OutlineJobStore;
  nowSeconds?: () => number;
}

interface RouteContext {
  params: { jobId: string } | Promise<{ jobId: string }>;
}

function canonicalize(value: unknown): JsonValue {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("canonical JSON numbers must be finite");
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(canonicalize);
  }
  if (typeof value === "object") {
    const result: { [key: string]: JsonValue } = {};
    for (const key of Object.keys(value).sort()) {
      const item = (value as Record<string, unknown>)[key];
      if (item !== undefined) {
        result[key] = canonicalize(item);
      }
    }
    return result;
  }
  throw new Error("unsupported canonical JSON value");
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  label: string,
  allowed: readonly string[],
  required: readonly string[] = allowed,
): void {
  const allowedSet = new Set(allowed);
  for (const key of Object.keys(value)) {
    if (!allowedSet.has(key)) {
      throw new Error(`${label} contains an unsupported field`);
    }
  }
  for (const key of required) {
    if (!(key in value)) {
      throw new Error(`${label} is missing a required field`);
    }
  }
}

function nonEmptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value;
}

function positiveInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) {
    throw new Error(`${label} must be a positive integer`);
  }
  return value as number;
}

function sha256(value: unknown, label: string): string {
  const text = nonEmptyString(value, label);
  if (!SHA256_HEX.test(text)) {
    throw new Error(`${label} must be a lowercase SHA-256 digest`);
  }
  return text;
}

function opaqueIdentifier(value: unknown, label: string): string {
  const text = nonEmptyString(value, label);
  if (!OPAQUE_IDENTIFIER.test(text)) {
    throw new Error(`${label} must be an opaque identifier`);
  }
  return text;
}

function stringArray(
  value: unknown,
  label: string,
  options: { min?: number; unique?: boolean } = {},
): string[] {
  if (!Array.isArray(value) || value.length < (options.min ?? 0)) {
    throw new Error(`${label} must be an array`);
  }
  const result = value.map((item, index) =>
    nonEmptyString(item, `${label}[${index}]`),
  );
  if (options.unique && new Set(result).size !== result.length) {
    throw new Error(`${label} must contain unique values`);
  }
  return result;
}

function objectArray(
  value: unknown,
  label: string,
  options: { min?: number } = {},
): Record<string, unknown>[] {
  if (!Array.isArray(value) || value.length < (options.min ?? 0)) {
    throw new Error(`${label} must be an array`);
  }
  return value.map((item, index) => asRecord(item, `${label}[${index}]`));
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`${label} must be a boolean`);
  }
  return value;
}

function dateTime(value: unknown, label: string): string {
  const text = nonEmptyString(value, label);
  if (
    !/(?:Z|[+-]\d{2}:\d{2})$/.test(text) ||
    !Number.isFinite(Date.parse(text))
  ) {
    throw new Error(`${label} must be an RFC 3339 timestamp with a timezone`);
  }
  return text;
}

function assertNoClientRoutingAliases(value: unknown): void {
  if (Array.isArray(value)) {
    for (const item of value) {
      assertNoClientRoutingAliases(item);
    }
    return;
  }
  if (value === null || typeof value !== "object") {
    return;
  }
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    const normalized = key.toLowerCase().replace(/[-_\s]/g, "");
    if (ROUTING_ALIASES.has(normalized)) {
      throw new Error("client-controlled routing fields are forbidden");
    }
    assertNoClientRoutingAliases(item);
  }
}

function validateSourceReference(
  value: unknown,
  label: string,
): SourceReference {
  const record = asRecord(value, label);
  exactKeys(record, label, ["citationId", "sourceId", "fragmentId"]);
  return {
    citationId: nonEmptyString(record.citationId, `${label}.citationId`),
    sourceId: nonEmptyString(record.sourceId, `${label}.sourceId`),
    fragmentId: nonEmptyString(record.fragmentId, `${label}.fragmentId`),
  };
}

function refKey(ref: SourceReference): string {
  return JSON.stringify([ref.citationId, ref.sourceId, ref.fragmentId]);
}

function validateTeachingBrief(value: unknown): TeachingBrief {
  const brief = asRecord(value, "teachingBrief");
  exactKeys(brief, "teachingBrief", [
    "schemaVersion",
    "briefId",
    "briefVersion",
    "tenantId",
    "courseId",
    "targetClassId",
    "gradeBand",
    "audienceLevel",
    "classroomMode",
    "objectives",
    "durationMinutes",
    "knowledgePoints",
    "prerequisites",
    "assessment",
    "sourceSnapshot",
    "sourceFragments",
    "citations",
    "sourceRefs",
    "permissionSummary",
    "contentMode",
    "networkPolicy",
    "mediaPolicy",
    "templatePolicy",
    "safetyPolicy",
    "contentSha256",
  ]);
  if (brief.schemaVersion !== "1.0") {
    throw new Error("teachingBrief.schemaVersion is unsupported");
  }
  positiveInteger(brief.briefVersion, "teachingBrief.briefVersion");
  for (const field of [
    "briefId",
    "tenantId",
    "courseId",
    "targetClassId",
    "gradeBand",
    "audienceLevel",
  ] as const) {
    nonEmptyString(brief[field], `teachingBrief.${field}`);
  }
  if (brief.classroomMode !== "micro" && brief.classroomMode !== "full") {
    throw new Error("teachingBrief.classroomMode is unsupported");
  }

  const objectives = objectArray(brief.objectives, "teachingBrief.objectives", {
    min: 1,
  });
  for (const [index, objective] of objectives.entries()) {
    const label = `teachingBrief.objectives[${index}]`;
    exactKeys(objective, label, [
      "objectiveId",
      "description",
      "knowledgePointIds",
    ]);
    nonEmptyString(objective.objectiveId, `${label}.objectiveId`);
    nonEmptyString(objective.description, `${label}.description`);
    stringArray(objective.knowledgePointIds, `${label}.knowledgePointIds`, {
      min: 1,
    });
  }

  positiveInteger(brief.durationMinutes, "teachingBrief.durationMinutes");
  const knowledgePoints = objectArray(
    brief.knowledgePoints,
    "teachingBrief.knowledgePoints",
    { min: 1 },
  );
  for (const [index, point] of knowledgePoints.entries()) {
    const label = `teachingBrief.knowledgePoints[${index}]`;
    exactKeys(point, label, ["knowledgePointId", "title", "description"]);
    nonEmptyString(point.knowledgePointId, `${label}.knowledgePointId`);
    nonEmptyString(point.title, `${label}.title`);
    nonEmptyString(point.description, `${label}.description`);
  }

  const prerequisites = objectArray(
    brief.prerequisites,
    "teachingBrief.prerequisites",
  );
  for (const [index, prerequisite] of prerequisites.entries()) {
    const label = `teachingBrief.prerequisites[${index}]`;
    exactKeys(prerequisite, label, [
      "knowledgePointId",
      "prerequisiteKnowledgePointIds",
    ]);
    nonEmptyString(prerequisite.knowledgePointId, `${label}.knowledgePointId`);
    stringArray(
      prerequisite.prerequisiteKnowledgePointIds,
      `${label}.prerequisiteKnowledgePointIds`,
      { min: 1 },
    );
  }

  const assessment = asRecord(brief.assessment, "teachingBrief.assessment");
  exactKeys(assessment, "teachingBrief.assessment", [
    "methods",
    "successCriteria",
  ]);
  const methods = stringArray(
    assessment.methods,
    "teachingBrief.assessment.methods",
    { min: 1 },
  );
  const allowedMethods = new Set([
    "quiz",
    "discussion",
    "project",
    "observation",
    "self_assessment",
  ]);
  if (methods.some((method) => !allowedMethods.has(method))) {
    throw new Error("teachingBrief.assessment.methods is unsupported");
  }
  stringArray(
    assessment.successCriteria,
    "teachingBrief.assessment.successCriteria",
    { min: 1 },
  );

  if (brief.sourceSnapshot !== null) {
    const snapshot = asRecord(
      brief.sourceSnapshot,
      "teachingBrief.sourceSnapshot",
    );
    exactKeys(snapshot, "teachingBrief.sourceSnapshot", [
      "snapshotId",
      "createdAt",
      "contentSha256",
    ]);
    nonEmptyString(
      snapshot.snapshotId,
      "teachingBrief.sourceSnapshot.snapshotId",
    );
    dateTime(snapshot.createdAt, "teachingBrief.sourceSnapshot.createdAt");
    sha256(
      snapshot.contentSha256,
      "teachingBrief.sourceSnapshot.contentSha256",
    );
  }

  const fragments = objectArray(
    brief.sourceFragments,
    "teachingBrief.sourceFragments",
  );
  for (const [index, fragment] of fragments.entries()) {
    const label = `teachingBrief.sourceFragments[${index}]`;
    exactKeys(fragment, label, [
      "fragmentId",
      "sourceId",
      "text",
      "contentSha256",
    ]);
    nonEmptyString(fragment.fragmentId, `${label}.fragmentId`);
    nonEmptyString(fragment.sourceId, `${label}.sourceId`);
    nonEmptyString(fragment.text, `${label}.text`);
    sha256(fragment.contentSha256, `${label}.contentSha256`);
  }

  const citations = objectArray(brief.citations, "teachingBrief.citations");
  for (const [index, citation] of citations.entries()) {
    const label = `teachingBrief.citations[${index}]`;
    exactKeys(citation, label, [
      "citationId",
      "sourceId",
      "fragmentId",
      "label",
    ]);
    nonEmptyString(citation.citationId, `${label}.citationId`);
    nonEmptyString(citation.sourceId, `${label}.sourceId`);
    nonEmptyString(citation.fragmentId, `${label}.fragmentId`);
    nonEmptyString(citation.label, `${label}.label`);
  }
  const sourceRefs = objectArray(
    brief.sourceRefs,
    "teachingBrief.sourceRefs",
  ).map((ref, index) =>
    validateSourceReference(ref, `teachingBrief.sourceRefs[${index}]`),
  );

  const permission = asRecord(
    brief.permissionSummary,
    "teachingBrief.permissionSummary",
  );
  exactKeys(permission, "teachingBrief.permissionSummary", [
    "allowedSourceIds",
    "allowedFragmentIds",
    "usageScope",
    "attributionRequired",
  ]);
  const allowedSourceIds = stringArray(
    permission.allowedSourceIds,
    "teachingBrief.permissionSummary.allowedSourceIds",
    { unique: true },
  );
  const allowedFragmentIds = stringArray(
    permission.allowedFragmentIds,
    "teachingBrief.permissionSummary.allowedFragmentIds",
    { unique: true },
  );
  nonEmptyString(
    permission.usageScope,
    "teachingBrief.permissionSummary.usageScope",
  );
  booleanValue(
    permission.attributionRequired,
    "teachingBrief.permissionSummary.attributionRequired",
  );

  if (
    brief.contentMode !== "source_grounded" &&
    brief.contentMode !== "open_creation"
  ) {
    throw new Error("teachingBrief.contentMode is unsupported");
  }
  if (brief.contentMode === "source_grounded") {
    if (
      brief.sourceSnapshot === null ||
      fragments.length === 0 ||
      citations.length === 0 ||
      sourceRefs.length === 0 ||
      allowedSourceIds.length === 0 ||
      allowedFragmentIds.length === 0
    ) {
      throw new Error("source-grounded teaching brief requires source lineage");
    }
    const referencedSourceIds = new Set<string>();
    for (const item of [...fragments, ...citations, ...sourceRefs]) {
      referencedSourceIds.add(item.sourceId as string);
    }
    if (
      [...referencedSourceIds].some(
        (sourceId) => !allowedSourceIds.includes(sourceId),
      )
    ) {
      throw new Error("source-grounded source identifier is not authorized");
    }
    const fragmentIds = fragments.map(
      (fragment) => fragment.fragmentId as string,
    );
    if (
      new Set(fragmentIds).size !== fragmentIds.length ||
      fragmentIds.length !== allowedFragmentIds.length ||
      fragmentIds.some((fragmentId) => !allowedFragmentIds.includes(fragmentId))
    ) {
      throw new Error(
        "source-grounded fragments must exactly match the fragment allowlist",
      );
    }
    const fragmentsById = new Map(
      fragments.map((fragment) => [
        fragment.fragmentId as string,
        fragment.sourceId as string,
      ]),
    );
    const citationIds = citations.map(
      (citation) => citation.citationId as string,
    );
    if (new Set(citationIds).size !== citationIds.length) {
      throw new Error("source-grounded citation identifiers must be unique");
    }
    const citationsById = new Map(
      citations.map((citation) => [
        citation.citationId as string,
        {
          sourceId: citation.sourceId as string,
          fragmentId: citation.fragmentId as string,
        },
      ]),
    );
    for (const citation of citations) {
      if (
        fragmentsById.get(citation.fragmentId as string) !== citation.sourceId
      ) {
        throw new Error(
          "source-grounded citation does not match an authorized fragment",
        );
      }
    }
    const refKeys = sourceRefs.map(refKey);
    if (new Set(refKeys).size !== refKeys.length) {
      throw new Error("source-grounded source references must be unique");
    }
    for (const ref of sourceRefs) {
      const citation = citationsById.get(ref.citationId);
      if (
        !citation ||
        citation.sourceId !== ref.sourceId ||
        citation.fragmentId !== ref.fragmentId
      ) {
        throw new Error(
          "source-grounded source reference does not match a citation",
        );
      }
    }
  }

  const network = asRecord(brief.networkPolicy, "teachingBrief.networkPolicy");
  exactKeys(network, "teachingBrief.networkPolicy", [
    "allowWebAccess",
    "allowedDomains",
  ]);
  booleanValue(
    network.allowWebAccess,
    "teachingBrief.networkPolicy.allowWebAccess",
  );
  stringArray(
    network.allowedDomains,
    "teachingBrief.networkPolicy.allowedDomains",
  );

  const media = asRecord(brief.mediaPolicy, "teachingBrief.mediaPolicy");
  exactKeys(media, "teachingBrief.mediaPolicy", [
    "allowGeneration",
    "allowedMimeTypes",
  ]);
  booleanValue(
    media.allowGeneration,
    "teachingBrief.mediaPolicy.allowGeneration",
  );
  stringArray(
    media.allowedMimeTypes,
    "teachingBrief.mediaPolicy.allowedMimeTypes",
  );

  const template = asRecord(
    brief.templatePolicy,
    "teachingBrief.templatePolicy",
  );
  exactKeys(template, "teachingBrief.templatePolicy", [
    "templateId",
    "templateVersion",
  ]);
  nonEmptyString(
    template.templateId,
    "teachingBrief.templatePolicy.templateId",
  );
  nonEmptyString(
    template.templateVersion,
    "teachingBrief.templatePolicy.templateVersion",
  );

  const safety = asRecord(brief.safetyPolicy, "teachingBrief.safetyPolicy");
  exactKeys(safety, "teachingBrief.safetyPolicy", [
    "policyId",
    "blockedCategories",
  ]);
  nonEmptyString(safety.policyId, "teachingBrief.safetyPolicy.policyId");
  stringArray(
    safety.blockedCategories,
    "teachingBrief.safetyPolicy.blockedCategories",
  );
  sha256(brief.contentSha256, "teachingBrief.contentSha256");

  return brief as unknown as TeachingBrief;
}

export function validateGenerationRequest(
  value: unknown,
  endpoint: "outline" | "classroom" = "outline",
): GenerationRequest {
  assertNoClientRoutingAliases(value);
  const request = asRecord(value, "generation request");
  const required = [
    "schemaVersion",
    "tenantId",
    "requestId",
    "jobId",
    "idempotencyKey",
    "phase",
    "classroomMode",
    "teachingBriefId",
    "teachingBriefSha256",
    "teachingBrief",
    "templateId",
    "templateVersion",
    "sceneBudget",
    "durationMinutes",
    "requestedExports",
    "callbackContext",
    "dataPlaneRouteId",
    "priority",
  ] as const;
  exactKeys(
    request,
    "generation request",
    [...required, "confirmedOutline", "confirmedOutlineSha256"],
    required,
  );
  if (request.schemaVersion !== "1.0") {
    throw new Error("generation request schemaVersion is unsupported");
  }
  for (const field of [
    "tenantId",
    "requestId",
    "jobId",
    "idempotencyKey",
    "teachingBriefId",
    "templateId",
    "templateVersion",
  ] as const) {
    nonEmptyString(request[field], `generation request.${field}`);
  }
  opaqueIdentifier(
    request.callbackContext,
    "generation request.callbackContext",
  );
  opaqueIdentifier(
    request.dataPlaneRouteId,
    "generation request.dataPlaneRouteId",
  );
  sha256(request.teachingBriefSha256, "generation request.teachingBriefSha256");
  positiveInteger(request.sceneBudget, "generation request.sceneBudget");
  positiveInteger(
    request.durationMinutes,
    "generation request.durationMinutes",
  );
  const requestedExports = stringArray(
    request.requestedExports,
    "generation request.requestedExports",
    { min: 1 },
  );
  const exportFormats = new Set([
    "classroom_zip",
    "pptx",
    "offline_html",
    "mp4",
  ]);
  if (requestedExports.some((format) => !exportFormats.has(format))) {
    throw new Error("generation request requestedExports is unsupported");
  }
  const priorities = new Set([
    "student_micro",
    "interaction",
    "teacher",
    "full",
    "batch",
  ]);
  if (
    typeof request.priority !== "string" ||
    !priorities.has(request.priority)
  ) {
    throw new Error("generation request priority is unsupported");
  }
  const teachingBrief = validateTeachingBrief(request.teachingBrief);
  if (
    teachingBrief.tenantId !== request.tenantId ||
    teachingBrief.briefId !== request.teachingBriefId ||
    teachingBrief.contentSha256 !== request.teachingBriefSha256 ||
    teachingBrief.classroomMode !== request.classroomMode
  ) {
    throw new Error("generation request teaching brief binding is invalid");
  }
  const generationRequest = request as unknown as GenerationRequest;
  const outlineFieldDeclared = "confirmedOutline" in request;
  const hashFieldDeclared = "confirmedOutlineSha256" in request;
  if (outlineFieldDeclared !== hashFieldDeclared) {
    throw new Error(
      "generation request confirmed outline and hash must be declared together",
    );
  }
  const outlinePresent =
    request.confirmedOutline !== undefined &&
    request.confirmedOutline !== null;
  const hashPresent =
    request.confirmedOutlineSha256 !== undefined &&
    request.confirmedOutlineSha256 !== null;
  if (outlinePresent !== hashPresent) {
    throw new Error(
      "generation request confirmed outline and hash must be provided together",
    );
  }

  if (endpoint === "outline") {
    if (request.phase !== "outline" || request.classroomMode !== "full") {
      throw new Error("outline endpoint accepts only full outline requests");
    }
    if (outlinePresent) {
      throw new Error("outline requests cannot include a confirmed outline");
    }
    return generationRequest;
  }

  if (
    !(
      (request.phase === "content" && request.classroomMode === "full") ||
      (request.phase === "micro" && request.classroomMode === "micro")
    )
  ) {
    throw new Error(
      "classroom endpoint accepts only full content or micro requests",
    );
  }
  if (request.phase === "content" && !outlinePresent) {
    throw new Error("content requests require a confirmed outline");
  }
  if (outlinePresent) {
    sha256(
      request.confirmedOutlineSha256,
      "generation request.confirmedOutlineSha256",
    );
    validateOutlineBundle(request.confirmedOutline, generationRequest, {
      confirmationStatus: "confirmed",
    });
  }
  return generationRequest;
}

function sameReferenceSet(
  actual: SourceReference[],
  expected: SourceReference[],
): boolean {
  const actualKeys = actual.map(refKey).sort();
  const expectedKeys = expected.map(refKey).sort();
  return (
    actualKeys.length === expectedKeys.length &&
    actualKeys.every((key, index) => key === expectedKeys[index])
  );
}

export function validateOutlineBundle(
  value: unknown,
  request: GenerationRequest,
  options: { confirmationStatus?: "draft" | "confirmed" } = {},
): OutlineBundle {
  const outline = asRecord(value, "outline");
  exactKeys(outline, "outline", [
    "schemaVersion",
    "outlineId",
    "outlineVersion",
    "confirmationMetadata",
    "title",
    "language",
    "scenes",
    "knowledgeCoverage",
    "sourceRefs",
    "estimatedSceneCount",
    "generationMetadata",
    "contractSha256",
  ]);
  if (outline.schemaVersion !== "1.0") {
    throw new Error("outline schemaVersion is unsupported");
  }
  nonEmptyString(outline.outlineId, "outline.outlineId");
  positiveInteger(outline.outlineVersion, "outline.outlineVersion");
  nonEmptyString(outline.title, "outline.title");
  nonEmptyString(outline.language, "outline.language");
  if (outline.contractSha256 !== OUTLINE_BUNDLE_CONTRACT_SHA256) {
    throw new Error("outline contract hash is invalid");
  }

  const confirmation = asRecord(
    outline.confirmationMetadata,
    "outline.confirmationMetadata",
  );
  exactKeys(
    confirmation,
    "outline.confirmationMetadata",
    ["status", "confirmedAt", "confirmedBy"],
    ["status"],
  );
  const confirmationStatus = options.confirmationStatus ?? "draft";
  if (confirmation.status !== confirmationStatus) {
    throw new Error(
      confirmationStatus === "draft"
        ? "generated outline must be an unconfirmed draft"
        : "content generation requires a confirmed outline",
    );
  }
  if (confirmationStatus === "draft") {
    if (
      (confirmation.confirmedAt !== undefined &&
        confirmation.confirmedAt !== null) ||
      (confirmation.confirmedBy !== undefined &&
        confirmation.confirmedBy !== null)
    ) {
      throw new Error("generated outline must be an unconfirmed draft");
    }
  } else {
    dateTime(confirmation.confirmedAt, "outline.confirmationMetadata.confirmedAt");
    nonEmptyString(
      confirmation.confirmedBy,
      "outline.confirmationMetadata.confirmedBy",
    );
  }

  const expectedKnowledgeIds = request.teachingBrief.knowledgePoints.map(
    (point) => point.knowledgePointId,
  );
  const expectedKnowledgeSet = new Set(expectedKnowledgeIds);
  const expectedRefs = request.teachingBrief.sourceRefs;
  const topRefs = objectArray(outline.sourceRefs, "outline.sourceRefs").map(
    (ref, index) =>
      validateSourceReference(ref, `outline.sourceRefs[${index}]`),
  );
  if (!sameReferenceSet(topRefs, expectedRefs)) {
    throw new Error(
      "outline source references do not match the teaching brief",
    );
  }

  const scenes = objectArray(outline.scenes, "outline.scenes", { min: 1 });
  const sceneIds = new Set<string>();
  const sceneKnowledge = new Map<string, Set<string>>();
  for (const [index, scene] of scenes.entries()) {
    const label = `outline.scenes[${index}]`;
    exactKeys(scene, label, [
      "sceneId",
      "title",
      "summary",
      "knowledgePointIds",
      "sourceRefs",
    ]);
    const sceneId = nonEmptyString(scene.sceneId, `${label}.sceneId`);
    if (sceneIds.has(sceneId)) {
      throw new Error("outline scene identifiers must be unique");
    }
    sceneIds.add(sceneId);
    nonEmptyString(scene.title, `${label}.title`);
    nonEmptyString(scene.summary, `${label}.summary`);
    const knowledgeIds = stringArray(
      scene.knowledgePointIds,
      `${label}.knowledgePointIds`,
      { min: 1, unique: true },
    );
    if (knowledgeIds.some((id) => !expectedKnowledgeSet.has(id))) {
      throw new Error("outline scene references an unknown knowledge point");
    }
    sceneKnowledge.set(sceneId, new Set(knowledgeIds));
    const refs = objectArray(scene.sourceRefs, `${label}.sourceRefs`).map(
      (ref, refIndex) =>
        validateSourceReference(ref, `${label}.sourceRefs[${refIndex}]`),
    );
    if (
      request.teachingBrief.contentMode === "source_grounded" &&
      (refs.length === 0 || !sameReferenceSet(refs, expectedRefs))
    ) {
      throw new Error("source-grounded outline scene lost source references");
    }
    if (
      request.teachingBrief.contentMode === "open_creation" &&
      refs.length !== 0
    ) {
      throw new Error(
        "open-creation outline cannot introduce source references",
      );
    }
  }
  if (
    positiveInteger(
      outline.estimatedSceneCount,
      "outline.estimatedSceneCount",
    ) !== scenes.length ||
    scenes.length > request.sceneBudget
  ) {
    throw new Error("outline scene count is invalid");
  }

  const coverage = objectArray(
    outline.knowledgeCoverage,
    "outline.knowledgeCoverage",
    { min: 1 },
  );
  const coveredKnowledge = new Set<string>();
  for (const [index, item] of coverage.entries()) {
    const label = `outline.knowledgeCoverage[${index}]`;
    exactKeys(item, label, ["knowledgePointId", "sceneIds"]);
    const knowledgeId = nonEmptyString(
      item.knowledgePointId,
      `${label}.knowledgePointId`,
    );
    if (
      !expectedKnowledgeSet.has(knowledgeId) ||
      coveredKnowledge.has(knowledgeId)
    ) {
      throw new Error("outline knowledge coverage is invalid");
    }
    coveredKnowledge.add(knowledgeId);
    const coveredSceneIds = stringArray(item.sceneIds, `${label}.sceneIds`, {
      min: 1,
      unique: true,
    });
    for (const sceneId of coveredSceneIds) {
      if (
        !sceneIds.has(sceneId) ||
        !sceneKnowledge.get(sceneId)?.has(knowledgeId)
      ) {
        throw new Error("outline knowledge coverage does not match its scenes");
      }
    }
  }
  if (
    expectedKnowledgeIds.some(
      (knowledgeId) => !coveredKnowledge.has(knowledgeId),
    )
  ) {
    throw new Error("outline does not cover every teaching knowledge point");
  }

  const metadata = asRecord(
    outline.generationMetadata,
    "outline.generationMetadata",
  );
  exactKeys(metadata, "outline.generationMetadata", [
    "generator",
    "generatorVersion",
    "modelId",
    "generatedAt",
    "teachingBriefId",
    "teachingBriefSha256",
    "templateId",
    "templateVersion",
  ]);
  for (const field of [
    "generator",
    "generatorVersion",
    "modelId",
    "teachingBriefId",
    "templateId",
    "templateVersion",
  ] as const) {
    nonEmptyString(metadata[field], `outline.generationMetadata.${field}`);
  }
  dateTime(metadata.generatedAt, "outline.generationMetadata.generatedAt");
  sha256(
    metadata.teachingBriefSha256,
    "outline.generationMetadata.teachingBriefSha256",
  );
  if (
    metadata.generator !== "openmaic" ||
    metadata.generatorVersion !== OPENMAIC_APP_VERSION ||
    metadata.teachingBriefId !== request.teachingBriefId ||
    metadata.teachingBriefSha256 !== request.teachingBriefSha256 ||
    metadata.templateId !== request.templateId ||
    metadata.templateVersion !== request.templateVersion
  ) {
    throw new Error("outline generation metadata binding is invalid");
  }
  return outline as unknown as OutlineBundle;
}

function knowledgePointIdsForScene(
  request: GenerationRequest,
  sceneIndex: number,
  sceneCount: number,
): string[] {
  const knowledgeIds = request.teachingBrief.knowledgePoints.map(
    (point) => point.knowledgePointId,
  );
  const assigned = knowledgeIds.filter(
    (_knowledgeId, index) => index % sceneCount === sceneIndex,
  );
  return assigned.length > 0
    ? assigned
    : [knowledgeIds[sceneIndex % knowledgeIds.length]];
}

export function normalizeUpstreamOutlineBundle(
  request: GenerationRequest,
  value: UpstreamOutlineResult,
  options: { modelId: string; generatedAt: string },
): OutlineBundle {
  nonEmptyString(value.languageDirective, "upstream language directive");
  if (!Array.isArray(value.outlines) || value.outlines.length === 0) {
    throw new Error("upstream outline result is empty");
  }
  const upstreamScenes = value.outlines.slice(0, request.sceneBudget);
  const sourceRefs = request.teachingBrief.sourceRefs.map((ref) => ({
    ...ref,
  }));
  const scenes = upstreamScenes.map((scene, index) => ({
    sceneId:
      typeof scene.id === "string" && scene.id.length > 0
        ? scene.id
        : `${request.jobId}-scene-${index + 1}`,
    title:
      typeof scene.title === "string" && scene.title.length > 0
        ? scene.title
        : `Scene ${index + 1}`,
    summary:
      typeof scene.description === "string" && scene.description.length > 0
        ? scene.description
        : typeof scene.title === "string" && scene.title.length > 0
          ? scene.title
          : `Scene ${index + 1}`,
    knowledgePointIds: knowledgePointIdsForScene(
      request,
      index,
      upstreamScenes.length,
    ),
    sourceRefs: sourceRefs.map((ref) => ({ ...ref })),
  }));
  const knowledgeCoverage = request.teachingBrief.knowledgePoints.map(
    (point) => ({
      knowledgePointId: point.knowledgePointId,
      sceneIds: scenes
        .filter((scene) =>
          scene.knowledgePointIds.includes(point.knowledgePointId),
        )
        .map((scene) => scene.sceneId),
    }),
  );
  const firstKnowledgePoint = request.teachingBrief.knowledgePoints[0];
  const outline: OutlineBundle = {
    schemaVersion: "1.0",
    outlineId: `outline-${request.jobId}`,
    outlineVersion: 1,
    confirmationMetadata: {
      status: "draft",
      confirmedAt: null,
      confirmedBy: null,
    },
    title:
      typeof value.courseTitle === "string" && value.courseTitle.length > 0
        ? value.courseTitle
        : firstKnowledgePoint.title,
    language: value.languageDirective,
    scenes,
    knowledgeCoverage,
    sourceRefs,
    estimatedSceneCount: scenes.length,
    generationMetadata: {
      generator: "openmaic",
      generatorVersion: OPENMAIC_APP_VERSION,
      modelId: nonEmptyString(options.modelId, "resolved model identifier"),
      generatedAt: dateTime(options.generatedAt, "generated timestamp"),
      teachingBriefId: request.teachingBriefId,
      teachingBriefSha256: request.teachingBriefSha256,
      templateId: request.templateId,
      templateVersion: request.templateVersion,
    },
    contractSha256: OUTLINE_BUNDLE_CONTRACT_SHA256,
  };
  return validateOutlineBundle(outline, request);
}

export function buildUpstreamRequirement(request: GenerationRequest): string {
  const brief = request.teachingBrief;
  return [
    `Audience: ${brief.gradeBand}, ${brief.audienceLevel}`,
    `Duration: ${request.durationMinutes} minutes`,
    `Maximum scenes: ${request.sceneBudget}`,
    `Objectives: ${brief.objectives.map((item) => item.description).join("; ")}`,
    `Knowledge: ${brief.knowledgePoints
      .map((item) => `${item.title}: ${item.description}`)
      .join("; ")}`,
  ].join("\n");
}

export function buildSourceText(
  request: GenerationRequest,
): string | undefined {
  if (request.teachingBrief.sourceFragments.length === 0) {
    return undefined;
  }
  return request.teachingBrief.sourceFragments
    .map((fragment) => fragment.text)
    .join("\n\n");
}

export async function generateOutlineJob(
  input: unknown,
  dependencies: OutlineGenerationDependencies,
): Promise<OutlineJob> {
  const request = validateGenerationRequest(input);
  const now = (dependencies.now ?? (() => new Date()))().toISOString();
  const base = {
    tenantId: request.tenantId,
    jobId: request.jobId,
    idempotencyKey: request.idempotencyKey,
    phase: "outline" as const,
    createdAt: now,
    updatedAt: now,
  };
  try {
    const candidate = await dependencies.generateOutlines(request);
    const outline = validateOutlineBundle(candidate, request);
    return {
      ...base,
      status: "succeeded",
      result: { outline },
    };
  } catch {
    return {
      ...base,
      status: "failed",
      error: {
        code: "OUTLINE_GENERATION_FAILED",
        message: "Outline generation failed.",
      },
    };
  }
}

function errorResponse(
  status: number,
  code: string,
  message: string,
): Response {
  return Response.json({ error: { code, message } }, { status });
}

function signedRequestFromHeaders(
  request: Request,
  body: string,
): SignedServiceRequest {
  return {
    method: request.method,
    path: new URL(request.url).pathname,
    tenantId: request.headers.get("x-yfeistai-tenant-id") ?? "",
    jobId: request.headers.get("x-yfeistai-job-id") ?? "",
    timestamp: Number(request.headers.get("x-yfeistai-timestamp")),
    idempotencyKey: request.headers.get("x-yfeistai-idempotency-key") ?? "",
    signature: request.headers.get("x-yfeistai-signature") ?? "",
  };
}

function authenticate(
  request: Request,
  body: string,
  dependencies: {
    readSecret(): string;
    nowSeconds?: () => number;
  },
): SignedServiceRequest | null {
  let secret: string;
  try {
    secret = dependencies.readSecret();
  } catch {
    return null;
  }
  const signed = signedRequestFromHeaders(request, body);
  const result = verifyServiceRequest(signed, {
    secret,
    nowSeconds: dependencies.nowSeconds?.() ?? Math.floor(Date.now() / 1_000),
    body,
  });
  return result.ok ? signed : null;
}

export function createOutlinePostHandler(
  dependencies: OutlinePostHandlerDependencies,
): (request: Request) => Promise<Response> {
  return async (request: Request): Promise<Response> => {
    const body = await request.text();
    const signed = authenticate(request, body, dependencies);
    if (!signed) {
      return errorResponse(
        401,
        "AUTHENTICATION_FAILED",
        "Service authentication failed.",
      );
    }
    if (
      request.method !== "POST" ||
      new URL(request.url).pathname !== "/api/yfeistai/v1/outlines"
    ) {
      return errorResponse(404, "ROUTE_NOT_FOUND", "Route not found.");
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(body);
    } catch {
      return errorResponse(400, "INVALID_REQUEST", "Request body is invalid.");
    }

    let generationRequest: GenerationRequest;
    try {
      generationRequest = validateGenerationRequest(parsed);
    } catch {
      return errorResponse(400, "INVALID_REQUEST", "Request body is invalid.");
    }
    if (
      generationRequest.tenantId !== signed.tenantId ||
      generationRequest.jobId !== signed.jobId ||
      generationRequest.idempotencyKey !== signed.idempotencyKey
    ) {
      return errorResponse(
        403,
        "REQUEST_BINDING_MISMATCH",
        "Signed request metadata does not match the request body.",
      );
    }

    try {
      const job = await dependencies.store.submit(
        {
          tenantId: generationRequest.tenantId,
          jobId: generationRequest.jobId,
          idempotencyKey: generationRequest.idempotencyKey,
          action: "outline",
          canonicalBody: canonicalJson(generationRequest),
        },
        () => generateOutlineJob(generationRequest, dependencies),
      );
      return Response.json(job, { status: 202 });
    } catch (error) {
      if (error instanceof IdempotencyConflictError) {
        return errorResponse(
          409,
          "IDEMPOTENCY_CONFLICT",
          "The idempotency key conflicts with an existing job.",
        );
      }
      return errorResponse(
        500,
        "OUTLINE_GENERATION_FAILED",
        "Outline generation failed.",
      );
    }
  };
}

export function createOutlineGetHandler(
  dependencies: OutlineGetHandlerDependencies,
): (request: Request, context: RouteContext) => Promise<Response> {
  return async (request: Request, context: RouteContext): Promise<Response> => {
    const signed = authenticate(request, "", dependencies);
    if (!signed) {
      return errorResponse(
        401,
        "AUTHENTICATION_FAILED",
        "Service authentication failed.",
      );
    }
    const { jobId } = await context.params;
    const expectedPath = `/api/yfeistai/v1/outlines/${encodeURIComponent(jobId)}`;
    if (
      request.method !== "GET" ||
      new URL(request.url).pathname !== expectedPath
    ) {
      return errorResponse(404, "ROUTE_NOT_FOUND", "Route not found.");
    }
    if (signed.jobId !== jobId) {
      return errorResponse(
        403,
        "REQUEST_BINDING_MISMATCH",
        "Signed request metadata does not match the requested job.",
      );
    }
    const stored = dependencies.store.read(signed.tenantId, jobId);
    if (!stored) {
      return errorResponse(404, "JOB_NOT_FOUND", "Outline job was not found.");
    }
    const job = await stored;
    return Response.json(job, { status: 200 });
  };
}
