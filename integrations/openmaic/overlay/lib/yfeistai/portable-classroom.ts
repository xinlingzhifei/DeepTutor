import type { GenerationMetadata, SourceReference } from "./contracts";

const SHA256_HEX = /^[0-9a-f]{64}$/;
const FORBIDDEN_RUNTIME_KEYS = new Set([
  ["api", "Key"].join(""),
  ["base", "Url"].join(""),
  ["provider", "Id"].join(""),
  ["provider", "Key"].join(""),
  "credential",
  "secret",
  "absolutePath",
  "sourceLocation",
  "browserStorageKey",
  "browserStorage",
  "indexedDbName",
  "indexedDB",
  "localPath",
]);

type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  | JsonPrimitive
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface SlideContent {
  type: "slide";
  canvas: Record<string, JsonValue>;
}

export interface QuizContent {
  type: "quiz";
  questions: Array<{
    id: string;
    prompt: string;
    questionType: "single_choice" | "multiple_choice" | "short_answer";
    options: Array<{ id: string; label: string }>;
    correctOptionIds: string[];
    explanation: string;
  }>;
}

export type PortableSceneContent =
  | SlideContent
  | QuizContent
  | {
      type: "interactive";
      html: string;
      bridgeVersion: "1.0";
      sandbox: { allowScripts: true; allowSameOrigin: false };
    }
  | {
      type: "pbl";
      scenario: string;
      roles: Array<{ id: string; name: string; brief: string }>;
      milestones: Array<{ id: string; title: string; rubric: string }>;
    };

export interface PortableScene {
  id: string;
  stageId: string;
  title: string;
  order: number;
  type: PortableSceneContent["type"];
  content: PortableSceneContent;
  actions: Array<Record<string, JsonValue>>;
}

export interface PortableMediaEntry {
  mediaId: string;
  relativePath: string;
  mimeType: string;
  sha256: string;
  sizeBytes: number;
  temporaryDownloadPath: string;
  expiresAt: string;
}

export interface PortableClassroomDocument {
  schemaVersion: "1.0";
  classroomId: string;
  classroomVersionId: string;
  contentMode: "source_grounded" | "open_creation";
  openCreation: boolean;
  openmaic: {
    dslVersion: "0.1.0";
    stage: {
      id: string;
      name: string;
      createdAt: string;
      updatedAt: string;
    };
    scenes: PortableScene[];
  };
  interactionIds: string[];
  sourceRefs: SourceReference[];
  knowledgePointMappings: Array<{
    knowledgePointId: string;
    sceneIds: string[];
    sourceRefs: SourceReference[];
  }>;
  mediaManifest: PortableMediaEntry[];
  fileSha256: string;
  exportManifest: Array<{
    format: "classroom_zip" | "pptx" | "offline_html" | "mp4";
    relativePath: string;
    sha256: string;
    sizeBytes: number;
    mimeType: string;
    temporaryDownloadPath: string;
    expiresAt: string;
  }>;
  generationMetadata: GenerationMetadata;
  auditMetadata: {
    templateId: string;
    templateVersion: string;
    teachingBriefId: string;
    teachingBriefSha256: string;
    parentClassroomVersionId: string | null;
  };
  validationResult: {
    valid: boolean;
    issues: Array<{
      severity: "error" | "warning";
      code: string;
      message: string;
      path: string;
    }>;
    validatedAt: string;
  };
  migrationRecords: Array<{
    fromDslVersion: string;
    toDslVersion: string;
    migratedAt: string;
    migrationId: string;
  }>;
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
  keys: readonly string[],
): void {
  const expected = new Set(keys);
  if (
    Object.keys(value).length !== keys.length ||
    Object.keys(value).some((key) => !expected.has(key)) ||
    keys.some((key) => !(key in value))
  ) {
    throw new Error(`${label} has an invalid field set`);
  }
}

function nonEmptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value;
}

function awareTimestamp(value: unknown, label: string): string {
  const text = nonEmptyString(value, label);
  if (
    !/(?:Z|[+-]\d{2}:\d{2})$/.test(text) ||
    !Number.isFinite(Date.parse(text))
  ) {
    throw new Error(`${label} must be an aware ISO timestamp`);
  }
  return text;
}

function sha256(value: unknown, label: string): string {
  const text = nonEmptyString(value, label);
  if (!SHA256_HEX.test(text)) {
    throw new Error(`${label} must be a lowercase SHA-256 digest`);
  }
  return text;
}

function hasForbiddenLocation(value: string): boolean {
  const text = value.trim();
  return (
    /(?:^|[\s"'(=])(?:[a-z][a-z0-9+.-]*:|\/\/|\\\\)/i.test(text) ||
    /(?:src|href|srcset|action|poster)\s*=\s*["']?\s*(?:[a-z][a-z0-9+.-]*:|\/\/|\\\\|\/)/i.test(
      text,
    ) ||
    /url\s*\(\s*["']?\s*(?:[a-z][a-z0-9+.-]*:|\/\/|\\\\|\/)/i.test(text) ||
    /(?:^|[\/\\])\.\.(?:[\/\\]|$)/.test(text) ||
    /^[\/\\]/.test(text) ||
    /[A-Za-z]:[\\/]/.test(text)
  );
}

export function assertPortableValue(
  value: unknown,
  label: string,
): asserts value is JsonValue {
  if (value === null || typeof value === "boolean") {
    return;
  }
  if (typeof value === "string") {
    if (hasForbiddenLocation(value)) {
      throw new Error(`${label} contains a non-portable location`);
    }
    return;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error(`${label} contains a non-finite number`);
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      assertPortableValue(item, `${label}[${index}]`),
    );
    return;
  }
  if (typeof value !== "object") {
    throw new Error(`${label} contains a non-JSON value`);
  }
  for (const [key, item] of Object.entries(value)) {
    if (FORBIDDEN_RUNTIME_KEYS.has(key)) {
      throw new Error(`${label} contains a forbidden runtime field`);
    }
    assertPortableValue(item, `${label}.${key}`);
  }
}

function validateSourceReference(value: unknown, label: string): void {
  const reference = asRecord(value, label);
  exactKeys(reference, label, ["citationId", "sourceId", "fragmentId"]);
  nonEmptyString(reference.citationId, `${label}.citationId`);
  nonEmptyString(reference.sourceId, `${label}.sourceId`);
  nonEmptyString(reference.fragmentId, `${label}.fragmentId`);
}

export function validateMediaManifest(
  value: unknown,
): asserts value is PortableMediaEntry[] {
  if (!Array.isArray(value)) {
    throw new Error("media manifest must be an array");
  }
  for (const [index, raw] of value.entries()) {
    const label = `media manifest entry ${index}`;
    const entry = asRecord(raw, label);
    exactKeys(entry, label, [
      "mediaId",
      "relativePath",
      "mimeType",
      "sha256",
      "sizeBytes",
      "temporaryDownloadPath",
      "expiresAt",
    ]);
    nonEmptyString(entry.mediaId, `${label}.mediaId`);
    nonEmptyString(entry.relativePath, `${label}.relativePath`);
    if (
      typeof entry.mimeType !== "string" ||
      entry.mimeType.length === 0 ||
      !Number.isInteger(entry.sizeBytes) ||
      (entry.sizeBytes as number) < 0 ||
      typeof entry.temporaryDownloadPath !== "string" ||
      entry.temporaryDownloadPath.length === 0
    ) {
      throw new Error(`${label} metadata is invalid`);
    }
    sha256(entry.sha256, `${label}.sha256`);
    awareTimestamp(entry.expiresAt, `${label}.expiresAt`);
  }
}

function validateQuiz(content: Record<string, unknown>): void {
  exactKeys(content, "quiz content", ["type", "questions"]);
  if (!Array.isArray(content.questions) || content.questions.length === 0) {
    throw new Error("quiz content requires questions");
  }
  for (const raw of content.questions) {
    const question = asRecord(raw, "quiz question");
    exactKeys(question, "quiz question", [
      "id",
      "prompt",
      "questionType",
      "options",
      "correctOptionIds",
      "explanation",
    ]);
    nonEmptyString(question.id, "quiz question id");
    nonEmptyString(question.prompt, "quiz prompt");
    nonEmptyString(question.explanation, "quiz explanation");
    if (
      !["single_choice", "multiple_choice", "short_answer"].includes(
        question.questionType as string,
      ) ||
      !Array.isArray(question.options) ||
      !Array.isArray(question.correctOptionIds)
    ) {
      throw new Error("quiz question shape is invalid");
    }
    for (const rawOption of question.options) {
      const option = asRecord(rawOption, "quiz option");
      exactKeys(option, "quiz option", ["id", "label"]);
      nonEmptyString(option.id, "quiz option id");
      nonEmptyString(option.label, "quiz option label");
    }
    if (
      question.correctOptionIds.some(
        (optionId) => typeof optionId !== "string" || optionId.length === 0,
      )
    ) {
      throw new Error("quiz answer contract is invalid");
    }
  }
}

function validateScene(
  value: unknown,
  index: number,
  _stageId: string,
  _sceneIds: Set<string>,
  _interactiveIds: Set<string>,
): void {
  const scene = asRecord(value, `classroom scene ${index}`);
  if (!("actions" in scene)) {
    scene.actions = [];
  }
  exactKeys(scene, `classroom scene ${index}`, [
    "id",
    "stageId",
    "title",
    "order",
    "type",
    "content",
    "actions",
  ]);
  nonEmptyString(scene.id, "scene id");
  if (
    typeof scene.stageId !== "string" ||
    scene.stageId.length === 0 ||
    !Number.isInteger(scene.order) ||
    (scene.order as number) < 0
  ) {
    throw new Error("classroom scene identity or order is invalid");
  }
  nonEmptyString(scene.title, "scene title");
  if (!["slide", "quiz", "interactive", "pbl"].includes(scene.type as string)) {
    throw new Error("classroom scene type is unsupported");
  }
  const content = asRecord(scene.content, "scene content");
  if (content.type !== scene.type) {
    throw new Error("classroom scene content type is inconsistent");
  }
  if (scene.type === "slide") {
    exactKeys(content, "slide content", ["type", "canvas"]);
    asRecord(content.canvas, "slide canvas");
  } else if (scene.type === "quiz") {
    validateQuiz(content);
  } else if (scene.type === "interactive") {
    exactKeys(content, "interactive content", [
      "type",
      "html",
      "bridgeVersion",
      "sandbox",
    ]);
    nonEmptyString(content.html, "interactive HTML");
    const sandbox = asRecord(content.sandbox, "interactive sandbox");
    exactKeys(sandbox, "interactive sandbox", [
      "allowScripts",
      "allowSameOrigin",
    ]);
    if (
      content.bridgeVersion !== "1.0" ||
      sandbox.allowScripts !== true ||
      sandbox.allowSameOrigin !== false
    ) {
      throw new Error("interactive sandbox contract is invalid");
    }
  } else {
    exactKeys(content, "PBL content", [
      "type",
      "scenario",
      "roles",
      "milestones",
    ]);
    nonEmptyString(content.scenario, "PBL scenario");
    if (
      !Array.isArray(content.roles) ||
      content.roles.length === 0 ||
      !Array.isArray(content.milestones) ||
      content.milestones.length === 0
    ) {
      throw new Error("PBL content shape is invalid");
    }
    for (const rawRole of content.roles) {
      const role = asRecord(rawRole, "PBL role");
      exactKeys(role, "PBL role", ["id", "name", "brief"]);
      nonEmptyString(role.id, "PBL role id");
      nonEmptyString(role.name, "PBL role name");
      nonEmptyString(role.brief, "PBL role brief");
    }
    for (const rawMilestone of content.milestones) {
      const milestone = asRecord(rawMilestone, "PBL milestone");
      exactKeys(milestone, "PBL milestone", ["id", "title", "rubric"]);
      nonEmptyString(milestone.id, "PBL milestone id");
      nonEmptyString(milestone.title, "PBL milestone title");
      nonEmptyString(milestone.rubric, "PBL milestone rubric");
    }
  }
  if (!Array.isArray(scene.actions)) {
    throw new Error("classroom scene actions must be an array");
  }
  scene.actions.forEach((action) => asRecord(action, "scene action"));
  assertPortableValue(content, "scene content");
  assertPortableValue(scene.actions, "scene actions");
}

export function asPortableDocument(value: unknown): PortableClassroomDocument {
  const document = asRecord(
    JSON.parse(JSON.stringify(value)) as unknown,
    "classroom document",
  );
  exactKeys(document, "classroom document", [
    "schemaVersion",
    "classroomId",
    "classroomVersionId",
    "contentMode",
    "openCreation",
    "openmaic",
    "interactionIds",
    "sourceRefs",
    "knowledgePointMappings",
    "mediaManifest",
    "fileSha256",
    "exportManifest",
    "generationMetadata",
    "auditMetadata",
    "validationResult",
    "migrationRecords",
  ]);
  if (
    document.schemaVersion !== "1.0" ||
    !["source_grounded", "open_creation"].includes(
      document.contentMode as string,
    ) ||
    typeof document.openCreation !== "boolean" ||
    document.openCreation !== (document.contentMode === "open_creation")
  ) {
    throw new Error(
      "classroom document is not a portable version 1.0 document",
    );
  }
  nonEmptyString(document.classroomId, "classroom id");
  nonEmptyString(document.classroomVersionId, "classroom version id");
  sha256(document.fileSha256, "classroom file hash");

  const openmaic = asRecord(document.openmaic, "classroom openmaic");
  exactKeys(openmaic, "classroom openmaic", ["dslVersion", "stage", "scenes"]);
  if (openmaic.dslVersion !== "0.1.0") {
    throw new Error("classroom DSL version is unsupported");
  }
  const stage = asRecord(openmaic.stage, "classroom stage");
  exactKeys(stage, "classroom stage", ["id", "name", "createdAt", "updatedAt"]);
  const stageId = nonEmptyString(stage.id, "stage id");
  nonEmptyString(stage.name, "stage name");
  awareTimestamp(stage.createdAt, "stage createdAt");
  awareTimestamp(stage.updatedAt, "stage updatedAt");
  if (!Array.isArray(openmaic.scenes) || openmaic.scenes.length === 0) {
    throw new Error("classroom scenes must be a non-empty array");
  }
  const sceneIds = new Set<string>();
  const interactiveIds = new Set<string>();
  openmaic.scenes.forEach((scene, index) =>
    validateScene(scene, index, stageId, sceneIds, interactiveIds),
  );

  if (
    !Array.isArray(document.interactionIds) ||
    document.interactionIds.some(
      (id) => typeof id !== "string" || id.length === 0,
    )
  ) {
    throw new Error("classroom interaction identifiers are invalid");
  }
  if (!Array.isArray(document.sourceRefs)) {
    throw new Error("classroom source references must be an array");
  }
  document.sourceRefs.forEach((reference, index) =>
    validateSourceReference(reference, `source reference ${index}`),
  );
  if (
    document.contentMode === "source_grounded" &&
    document.sourceRefs.length === 0
  ) {
    throw new Error(
      "source-grounded classroom requires at least one source ref",
    );
  }
  if (
    !Array.isArray(document.knowledgePointMappings) ||
    document.knowledgePointMappings.length === 0
  ) {
    throw new Error("classroom knowledge mappings must be non-empty");
  }
  for (const rawMapping of document.knowledgePointMappings) {
    const mapping = asRecord(rawMapping, "knowledge mapping");
    exactKeys(mapping, "knowledge mapping", [
      "knowledgePointId",
      "sceneIds",
      "sourceRefs",
    ]);
    nonEmptyString(mapping.knowledgePointId, "knowledge point id");
    if (
      !Array.isArray(mapping.sceneIds) ||
      mapping.sceneIds.length === 0 ||
      mapping.sceneIds.some(
        (id) => typeof id !== "string" || id.length === 0,
      ) ||
      !Array.isArray(mapping.sourceRefs)
    ) {
      throw new Error("classroom knowledge mapping is invalid");
    }
    mapping.sourceRefs.forEach((reference, index) =>
      validateSourceReference(
        reference,
        `knowledge mapping source reference ${index}`,
      ),
    );
  }
  validateMediaManifest(document.mediaManifest);
  if (!Array.isArray(document.exportManifest)) {
    throw new Error("classroom exportManifest must be an array");
  }
  for (const [index, rawExport] of document.exportManifest.entries()) {
    const exported = asRecord(rawExport, `export manifest entry ${index}`);
    exactKeys(exported, `export manifest entry ${index}`, [
      "format",
      "relativePath",
      "sha256",
      "sizeBytes",
      "mimeType",
      "temporaryDownloadPath",
      "expiresAt",
    ]);
    if (
      !["classroom_zip", "pptx", "offline_html", "mp4"].includes(
        exported.format as string,
      ) ||
      !Number.isInteger(exported.sizeBytes) ||
      (exported.sizeBytes as number) < 0 ||
      typeof exported.mimeType !== "string" ||
      exported.mimeType.length === 0
    ) {
      throw new Error("export manifest entry is invalid");
    }
    nonEmptyString(exported.relativePath, "export path");
    nonEmptyString(exported.temporaryDownloadPath, "export download path");
    sha256(exported.sha256, "export hash");
    awareTimestamp(exported.expiresAt, "export expiry");
  }
  if (!Array.isArray(document.migrationRecords)) {
    throw new Error("classroom migrationRecords must be an array");
  }
  for (const [index, rawMigration] of document.migrationRecords.entries()) {
    const migration = asRecord(rawMigration, `migration record ${index}`);
    exactKeys(migration, `migration record ${index}`, [
      "fromDslVersion",
      "toDslVersion",
      "migratedAt",
      "migrationId",
    ]);
    nonEmptyString(migration.fromDslVersion, "migration source DSL version");
    nonEmptyString(migration.toDslVersion, "migration target DSL version");
    awareTimestamp(migration.migratedAt, "migration timestamp");
    nonEmptyString(migration.migrationId, "migration id");
  }

  const generation = asRecord(
    document.generationMetadata,
    "generation metadata",
  );
  exactKeys(generation, "generation metadata", [
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
  ]) {
    nonEmptyString(generation[field], `generation metadata ${field}`);
  }
  awareTimestamp(generation.generatedAt, "generation generatedAt");
  sha256(generation.teachingBriefSha256, "generation teaching brief hash");

  const audit = asRecord(document.auditMetadata, "audit metadata");
  if (!("parentClassroomVersionId" in audit)) {
    audit.parentClassroomVersionId = null;
  }
  exactKeys(audit, "audit metadata", [
    "templateId",
    "templateVersion",
    "teachingBriefId",
    "teachingBriefSha256",
    "parentClassroomVersionId",
  ]);
  for (const field of ["templateId", "templateVersion", "teachingBriefId"]) {
    nonEmptyString(audit[field], `audit metadata ${field}`);
  }
  sha256(audit.teachingBriefSha256, "audit teaching brief hash");
  if (
    audit.parentClassroomVersionId !== null &&
    (typeof audit.parentClassroomVersionId !== "string" ||
      audit.parentClassroomVersionId.length === 0)
  ) {
    throw new Error("parent classroom version id is invalid");
  }

  const validation = asRecord(document.validationResult, "validation result");
  exactKeys(validation, "validation result", [
    "valid",
    "issues",
    "validatedAt",
  ]);
  if (
    typeof validation.valid !== "boolean" ||
    !Array.isArray(validation.issues)
  ) {
    throw new Error("classroom validation result is invalid");
  }
  for (const rawIssue of validation.issues) {
    const issue = asRecord(rawIssue, "validation issue");
    exactKeys(issue, "validation issue", [
      "severity",
      "code",
      "message",
      "path",
    ]);
    if (!["error", "warning"].includes(issue.severity as string)) {
      throw new Error("validation issue severity is invalid");
    }
    nonEmptyString(issue.code, "validation issue code");
    nonEmptyString(issue.message, "validation issue message");
    nonEmptyString(issue.path, "validation issue path");
  }
  awareTimestamp(validation.validatedAt, "validation timestamp");
  return document as unknown as PortableClassroomDocument;
}

export function assertOfflineHtmlSelfContained(html: string): void {
  if (
    !/^\s*<!doctype html>/i.test(html) ||
    /<(?:base|iframe|object|embed)\b/i.test(html) ||
    /<meta\b[^>]*http-equiv\s*=\s*["']?refresh/i.test(html) ||
    /\b(?:src|href|srcset|action|poster)\s*=\s*["']?\s*(?!data:|#)[^\s>"']+/i.test(
      html,
    ) ||
    /url\s*\(\s*["']?\s*(?!data:|#)/i.test(html) ||
    /(?:^|[\s"'(=])(?:https?:|file:|javascript:|blob:|\/\/|\\\\)/im.test(html)
  ) {
    throw new Error("offline HTML output contains an external resource");
  }
}
