export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export type JsonObject = { [key: string]: JsonValue };

export interface SourceReference {
  citationId: string;
  sourceId: string;
  fragmentId: string;
}

export interface ClassroomStage {
  id: string;
  name: string;
  createdAt: string;
  updatedAt: string;
}

export interface SlideSceneContent {
  type: "slide";
  canvas: JsonObject;
}

export interface QuizOption {
  id: string;
  label: string;
}

export interface QuizQuestion {
  id: string;
  prompt: string;
  questionType: "single_choice" | "multiple_choice" | "short_answer";
  options: QuizOption[];
  correctOptionIds: string[];
  explanation: string;
}

export interface QuizSceneContent {
  type: "quiz";
  questions: QuizQuestion[];
}

export interface InteractiveSceneContent {
  type: "interactive";
  html: string;
  bridgeVersion: "1.0";
  sandbox: {
    allowScripts: true;
    allowSameOrigin: false;
  };
}

export interface PblRole {
  id: string;
  name: string;
  brief: string;
}

export interface PblMilestone {
  id: string;
  title: string;
  rubric: string;
}

export interface PblSceneContent {
  type: "pbl";
  scenario: string;
  roles: PblRole[];
  milestones: PblMilestone[];
}

export type ClassroomSceneContent =
  | SlideSceneContent
  | QuizSceneContent
  | InteractiveSceneContent
  | PblSceneContent;

interface ClassroomSceneBase {
  id: string;
  stageId: string;
  title: string;
  order: number;
  actions: JsonObject[];
}

export type SlideScene = ClassroomSceneBase & {
  type: "slide";
  content: SlideSceneContent;
};

export type QuizScene = ClassroomSceneBase & {
  type: "quiz";
  content: QuizSceneContent;
};

export type InteractiveScene = ClassroomSceneBase & {
  type: "interactive";
  content: InteractiveSceneContent;
};

export type PblScene = ClassroomSceneBase & {
  type: "pbl";
  content: PblSceneContent;
};

export type ClassroomScene =
  | SlideScene
  | QuizScene
  | InteractiveScene
  | PblScene;

export interface MediaManifestItem {
  mediaId: string;
  relativePath: string;
  mimeType: string;
  sha256: string;
  sizeBytes: number;
  temporaryDownloadPath: string;
  expiresAt: string;
}

export type ExportFormat = "classroom_zip" | "pptx" | "offline_html" | "mp4";

export interface ExportManifestItem {
  format: ExportFormat;
  relativePath: string;
  sha256: string;
  sizeBytes: number;
  mimeType: string;
  temporaryDownloadPath: string;
  expiresAt: string;
}

export interface GenerationMetadata {
  generator: string;
  generatorVersion: string;
  modelId: string;
  generatedAt: string;
  teachingBriefId: string;
  teachingBriefSha256: string;
  templateId: string;
  templateVersion: string;
}

export interface ClassroomDocument {
  schemaVersion: "1.0";
  classroomId: string;
  classroomVersionId: string;
  contentMode: "source_grounded" | "open_creation";
  openCreation: boolean;
  openmaic: {
    dslVersion: string;
    stage: ClassroomStage;
    scenes: ClassroomScene[];
  };
  interactionIds: string[];
  sourceRefs: SourceReference[];
  knowledgePointMappings: Array<{
    knowledgePointId: string;
    sceneIds: string[];
    sourceRefs: SourceReference[];
  }>;
  mediaManifest: MediaManifestItem[];
  fileSha256: string;
  exportManifest: ExportManifestItem[];
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

export type ClassroomCompatibilityCode =
  | "INVALID_CLASSROOM_DOCUMENT"
  | "UNSUPPORTED_DSL_VERSION"
  | "OPENMAIC_VALIDATION_FAILED"
  | "UNSAFE_MEDIA_REFERENCE";

export interface ClassroomCompatibilityIssue {
  path: string;
  code: string;
  message: string;
}

export class ClassroomCompatibilityError extends Error {
  readonly code: ClassroomCompatibilityCode;
  readonly issues: readonly ClassroomCompatibilityIssue[];

  constructor(
    code: ClassroomCompatibilityCode,
    issues: readonly ClassroomCompatibilityIssue[],
  ) {
    super(
      issues.length === 0
        ? code
        : `${code}: ${issues.map(issue => `${issue.path}: ${issue.message}`).join("; ")}`,
    );
    this.name = "ClassroomCompatibilityError";
    this.code = code;
    this.issues = [...issues];
  }
}

const SHA256 = /^[0-9a-f]{64}$/;
const MIME_TYPE = /^[^\s/]+\/[^\s/]+(?:\s*;\s*[^\r\n]+)?$/;
const SCENE_TYPES = new Set(["slide", "quiz", "interactive", "pbl"]);
const QUIZ_TYPES = new Set([
  "single_choice",
  "multiple_choice",
  "short_answer",
]);
const EXPORT_FORMATS = new Set([
  "classroom_zip",
  "pptx",
  "offline_html",
  "mp4",
]);

interface ValidationContext {
  issues: ClassroomCompatibilityIssue[];
}

function issue(
  context: ValidationContext,
  path: string,
  code: string,
  message: string,
): void {
  context.issues.push({ path, code, message });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function objectAt(
  context: ValidationContext,
  value: unknown,
  path: string,
  requiredKeys: readonly string[],
  optionalKeys: readonly string[] = [],
): Record<string, unknown> | null {
  if (!isRecord(value)) {
    issue(context, path, "INVALID_TYPE", "must be an object");
    return null;
  }
  const allowed = new Set([...requiredKeys, ...optionalKeys]);
  for (const key of requiredKeys) {
    if (!(key in value)) {
      issue(context, `${path}/${key}`, "REQUIRED_FIELD", "is required");
    }
  }
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      issue(context, `${path}/${key}`, "UNEXPECTED_FIELD", "is not allowed");
    }
  }
  return value;
}

function arrayAt(
  context: ValidationContext,
  value: unknown,
  path: string,
  minimum = 0,
): unknown[] | null {
  if (!Array.isArray(value)) {
    issue(context, path, "INVALID_TYPE", "must be an array");
    return null;
  }
  if (value.length < minimum) {
    issue(context, path, "INVALID_LENGTH", `must contain at least ${minimum} item(s)`);
  }
  return value;
}

function stringAt(
  context: ValidationContext,
  value: unknown,
  path: string,
): string | null {
  if (typeof value !== "string" || value.length === 0) {
    issue(context, path, "INVALID_STRING", "must be a non-empty string");
    return null;
  }
  return value;
}

function literalAt(
  context: ValidationContext,
  value: unknown,
  expected: unknown,
  path: string,
): void {
  if (value !== expected) {
    issue(context, path, "INVALID_VALUE", `must equal ${JSON.stringify(expected)}`);
  }
}

function integerAt(
  context: ValidationContext,
  value: unknown,
  path: string,
  minimum = Number.MIN_SAFE_INTEGER,
): number | null {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    issue(context, path, "INVALID_INTEGER", `must be an integer >= ${minimum}`);
    return null;
  }
  return value as number;
}

function timestampAt(
  context: ValidationContext,
  value: unknown,
  path: string,
): string | null {
  const text = stringAt(context, value, path);
  if (
    text !== null &&
    (!/(?:Z|[+-]\d{2}:\d{2})$/.test(text) || !Number.isFinite(Date.parse(text)))
  ) {
    issue(context, path, "INVALID_TIMESTAMP", "must be an aware ISO-8601 timestamp");
  }
  return text;
}

function sha256At(
  context: ValidationContext,
  value: unknown,
  path: string,
): void {
  const text = stringAt(context, value, path);
  if (text !== null && !SHA256.test(text)) {
    issue(context, path, "INVALID_SHA256", "must be a lowercase SHA-256 digest");
  }
}

function jsonAt(context: ValidationContext, value: unknown, path: string): void {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      issue(context, path, "INVALID_JSON", "must not contain a non-finite number");
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => jsonAt(context, item, `${path}/${index}`));
    return;
  }
  if (isRecord(value)) {
    Object.entries(value).forEach(([key, item]) =>
      jsonAt(context, item, `${path}/${key}`),
    );
    return;
  }
  issue(context, path, "INVALID_JSON", "must contain JSON values only");
}

function stringArray(
  context: ValidationContext,
  value: unknown,
  path: string,
  minimum = 0,
): string[] {
  const values = arrayAt(context, value, path, minimum) ?? [];
  const result: string[] = [];
  values.forEach((item, index) => {
    const parsed = stringAt(context, item, `${path}/${index}`);
    if (parsed !== null) result.push(parsed);
  });
  return result;
}

function validateSourceReference(
  context: ValidationContext,
  value: unknown,
  path: string,
): void {
  const reference = objectAt(context, value, path, [
    "citationId",
    "sourceId",
    "fragmentId",
  ]);
  if (!reference) return;
  stringAt(context, reference.citationId, `${path}/citationId`);
  stringAt(context, reference.sourceId, `${path}/sourceId`);
  stringAt(context, reference.fragmentId, `${path}/fragmentId`);
}

function validateQuiz(
  context: ValidationContext,
  content: Record<string, unknown>,
  path: string,
): void {
  const questions = arrayAt(context, content.questions, `${path}/questions`, 1) ?? [];
  const questionIds = new Set<string>();
  questions.forEach((rawQuestion, questionIndex) => {
    const questionPath = `${path}/questions/${questionIndex}`;
    const question = objectAt(context, rawQuestion, questionPath, [
      "id",
      "prompt",
      "questionType",
      "options",
      "correctOptionIds",
      "explanation",
    ]);
    if (!question) return;
    const questionId = stringAt(context, question.id, `${questionPath}/id`);
    if (questionId && questionIds.has(questionId)) {
      issue(context, `${questionPath}/id`, "DUPLICATE_ID", "must be unique");
    }
    if (questionId) questionIds.add(questionId);
    stringAt(context, question.prompt, `${questionPath}/prompt`);
    stringAt(context, question.explanation, `${questionPath}/explanation`);
    if (!QUIZ_TYPES.has(question.questionType as string)) {
      issue(context, `${questionPath}/questionType`, "INVALID_VALUE", "is unsupported");
    }
    const options = arrayAt(context, question.options, `${questionPath}/options`) ?? [];
    const optionIds = new Set<string>();
    options.forEach((rawOption, optionIndex) => {
      const optionPath = `${questionPath}/options/${optionIndex}`;
      const option = objectAt(context, rawOption, optionPath, ["id", "label"]);
      if (!option) return;
      const optionId = stringAt(context, option.id, `${optionPath}/id`);
      stringAt(context, option.label, `${optionPath}/label`);
      if (optionId && optionIds.has(optionId)) {
        issue(context, `${optionPath}/id`, "DUPLICATE_ID", "must be unique");
      }
      if (optionId) optionIds.add(optionId);
    });
    const answers = stringArray(
      context,
      question.correctOptionIds,
      `${questionPath}/correctOptionIds`,
    );
    answers.forEach((answer, answerIndex) => {
      if (!optionIds.has(answer)) {
        issue(
          context,
          `${questionPath}/correctOptionIds/${answerIndex}`,
          "UNKNOWN_OPTION",
          "must reference an option in the same question",
        );
      }
    });
    if (question.questionType === "single_choice" && answers.length !== 1) {
      issue(context, `${questionPath}/correctOptionIds`, "INVALID_ANSWER", "must contain one answer");
    }
    if (question.questionType === "multiple_choice" && answers.length === 0) {
      issue(context, `${questionPath}/correctOptionIds`, "INVALID_ANSWER", "must not be empty");
    }
    if (
      question.questionType === "short_answer" &&
      (options.length !== 0 || answers.length !== 0)
    ) {
      issue(context, questionPath, "INVALID_ANSWER", "short-answer questions cannot carry options or option ids");
    }
  });
}

function validateScene(
  context: ValidationContext,
  value: unknown,
  path: string,
  stageId: string | null,
  sceneIds: Set<string>,
  sceneOrders: Set<number>,
): void {
  const scene = objectAt(
    context,
    value,
    path,
    ["id", "stageId", "title", "order", "type", "content"],
    ["actions"],
  );
  if (!scene) return;
  const sceneId = stringAt(context, scene.id, `${path}/id`);
  if (sceneId && sceneIds.has(sceneId)) {
    issue(context, `${path}/id`, "DUPLICATE_ID", "must be unique");
  }
  if (sceneId) sceneIds.add(sceneId);
  const linkedStageId = stringAt(context, scene.stageId, `${path}/stageId`);
  if (stageId && linkedStageId && stageId !== linkedStageId) {
    issue(context, `${path}/stageId`, "UNKNOWN_STAGE", "must reference openmaic.stage.id");
  }
  stringAt(context, scene.title, `${path}/title`);
  const order = integerAt(context, scene.order, `${path}/order`, 0);
  if (order !== null && sceneOrders.has(order)) {
    issue(context, `${path}/order`, "DUPLICATE_ORDER", "must be unique");
  }
  if (order !== null) sceneOrders.add(order);
  if (!SCENE_TYPES.has(scene.type as string)) {
    issue(context, `${path}/type`, "UNSUPPORTED_SCENE", "must be slide, quiz, interactive, or pbl");
  }
  const content = objectAt(context, scene.content, `${path}/content`, ["type"], [
    "canvas",
    "questions",
    "html",
    "bridgeVersion",
    "sandbox",
    "scenario",
    "roles",
    "milestones",
  ]);
  if (content) {
    if (content.type !== scene.type) {
      issue(context, `${path}/content/type`, "MISMATCHED_SCENE_TYPE", "must match scene.type");
    }
    if (scene.type === "slide") {
      objectAt(context, content, `${path}/content`, ["type", "canvas"]);
      if (!isRecord(content.canvas)) {
        issue(context, `${path}/content/canvas`, "INVALID_TYPE", "must be an object");
      } else {
        jsonAt(context, content.canvas, `${path}/content/canvas`);
      }
    } else if (scene.type === "quiz") {
      objectAt(context, content, `${path}/content`, ["type", "questions"]);
      validateQuiz(context, content, `${path}/content`);
    } else if (scene.type === "interactive") {
      objectAt(context, content, `${path}/content`, [
        "type",
        "html",
        "bridgeVersion",
        "sandbox",
      ]);
      stringAt(context, content.html, `${path}/content/html`);
      literalAt(context, content.bridgeVersion, "1.0", `${path}/content/bridgeVersion`);
      const sandbox = objectAt(context, content.sandbox, `${path}/content/sandbox`, [
        "allowScripts",
        "allowSameOrigin",
      ]);
      if (sandbox) {
        literalAt(context, sandbox.allowScripts, true, `${path}/content/sandbox/allowScripts`);
        literalAt(context, sandbox.allowSameOrigin, false, `${path}/content/sandbox/allowSameOrigin`);
      }
    } else if (scene.type === "pbl") {
      objectAt(context, content, `${path}/content`, [
        "type",
        "scenario",
        "roles",
        "milestones",
      ]);
      stringAt(context, content.scenario, `${path}/content/scenario`);
      const roles = arrayAt(context, content.roles, `${path}/content/roles`, 1) ?? [];
      roles.forEach((rawRole, roleIndex) => {
        const rolePath = `${path}/content/roles/${roleIndex}`;
        const role = objectAt(context, rawRole, rolePath, ["id", "name", "brief"]);
        if (!role) return;
        stringAt(context, role.id, `${rolePath}/id`);
        stringAt(context, role.name, `${rolePath}/name`);
        stringAt(context, role.brief, `${rolePath}/brief`);
      });
      const milestones =
        arrayAt(context, content.milestones, `${path}/content/milestones`, 1) ?? [];
      milestones.forEach((rawMilestone, milestoneIndex) => {
        const milestonePath = `${path}/content/milestones/${milestoneIndex}`;
        const milestone = objectAt(context, rawMilestone, milestonePath, [
          "id",
          "title",
          "rubric",
        ]);
        if (!milestone) return;
        stringAt(context, milestone.id, `${milestonePath}/id`);
        stringAt(context, milestone.title, `${milestonePath}/title`);
        stringAt(context, milestone.rubric, `${milestonePath}/rubric`);
      });
    }
  }
  const actions = scene.actions === undefined ? [] : arrayAt(context, scene.actions, `${path}/actions`) ?? [];
  actions.forEach((action, actionIndex) => {
    if (!isRecord(action)) {
      issue(context, `${path}/actions/${actionIndex}`, "INVALID_TYPE", "must be an object");
    } else {
      jsonAt(context, action, `${path}/actions/${actionIndex}`);
    }
  });
}

function validateManifestItem(
  context: ValidationContext,
  value: unknown,
  path: string,
  media: boolean,
): { id: string | null; relativePath: string | null } {
  const required = media
    ? [
        "mediaId",
        "relativePath",
        "mimeType",
        "sha256",
        "sizeBytes",
        "temporaryDownloadPath",
        "expiresAt",
      ]
    : [
        "format",
        "relativePath",
        "sha256",
        "sizeBytes",
        "mimeType",
        "temporaryDownloadPath",
        "expiresAt",
      ];
  const item = objectAt(context, value, path, required);
  if (!item) return { id: null, relativePath: null };
  const id = media ? stringAt(context, item.mediaId, `${path}/mediaId`) : null;
  if (!media && !EXPORT_FORMATS.has(item.format as string)) {
    issue(context, `${path}/format`, "INVALID_VALUE", "is not a supported export format");
  }
  const relativePath = stringAt(context, item.relativePath, `${path}/relativePath`);
  const mime = stringAt(context, item.mimeType, `${path}/mimeType`);
  if (mime && !MIME_TYPE.test(mime)) {
    issue(context, `${path}/mimeType`, "INVALID_MIME", "must be a MIME type");
  }
  sha256At(context, item.sha256, `${path}/sha256`);
  integerAt(context, item.sizeBytes, `${path}/sizeBytes`, 0);
  stringAt(context, item.temporaryDownloadPath, `${path}/temporaryDownloadPath`);
  timestampAt(context, item.expiresAt, `${path}/expiresAt`);
  return { id, relativePath };
}

function validateGenerationMetadata(
  context: ValidationContext,
  value: unknown,
  path: string,
): void {
  const metadata = objectAt(context, value, path, [
    "generator",
    "generatorVersion",
    "modelId",
    "generatedAt",
    "teachingBriefId",
    "teachingBriefSha256",
    "templateId",
    "templateVersion",
  ]);
  if (!metadata) return;
  for (const field of [
    "generator",
    "generatorVersion",
    "modelId",
    "teachingBriefId",
    "templateId",
    "templateVersion",
  ]) {
    stringAt(context, metadata[field], `${path}/${field}`);
  }
  timestampAt(context, metadata.generatedAt, `${path}/generatedAt`);
  sha256At(context, metadata.teachingBriefSha256, `${path}/teachingBriefSha256`);
}

function validateClassroomDocument(value: unknown): ValidationContext {
  const context: ValidationContext = { issues: [] };
  const document = objectAt(context, value, "", [
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
  if (!document) return context;
  literalAt(context, document.schemaVersion, "1.0", "/schemaVersion");
  stringAt(context, document.classroomId, "/classroomId");
  stringAt(context, document.classroomVersionId, "/classroomVersionId");
  if (document.contentMode !== "source_grounded" && document.contentMode !== "open_creation") {
    issue(context, "/contentMode", "INVALID_VALUE", "must be source_grounded or open_creation");
  }
  if (typeof document.openCreation !== "boolean") {
    issue(context, "/openCreation", "INVALID_TYPE", "must be a boolean");
  } else if (document.openCreation !== (document.contentMode === "open_creation")) {
    issue(context, "/openCreation", "INCONSISTENT_MODE", "must agree with contentMode");
  }
  sha256At(context, document.fileSha256, "/fileSha256");

  const openmaic = objectAt(context, document.openmaic, "/openmaic", [
    "dslVersion",
    "stage",
    "scenes",
  ]);
  const sceneIds = new Set<string>();
  if (openmaic) {
    stringAt(context, openmaic.dslVersion, "/openmaic/dslVersion");
    const stage = objectAt(context, openmaic.stage, "/openmaic/stage", [
      "id",
      "name",
      "createdAt",
      "updatedAt",
    ]);
    const stageId = stage ? stringAt(context, stage.id, "/openmaic/stage/id") : null;
    if (stage) {
      stringAt(context, stage.name, "/openmaic/stage/name");
      timestampAt(context, stage.createdAt, "/openmaic/stage/createdAt");
      timestampAt(context, stage.updatedAt, "/openmaic/stage/updatedAt");
    }
    const scenes = arrayAt(context, openmaic.scenes, "/openmaic/scenes", 1) ?? [];
    const sceneOrders = new Set<number>();
    scenes.forEach((scene, index) =>
      validateScene(context, scene, `/openmaic/scenes/${index}`, stageId, sceneIds, sceneOrders),
    );
  }

  const interactionIds = stringArray(context, document.interactionIds, "/interactionIds");
  const seenInteractionIds = new Set<string>();
  interactionIds.forEach((id, index) => {
    if (!sceneIds.has(id)) {
      issue(context, `/interactionIds/${index}`, "UNKNOWN_SCENE", "must reference a classroom scene");
    }
    if (seenInteractionIds.has(id)) {
      issue(context, `/interactionIds/${index}`, "DUPLICATE_ID", "must be unique");
    }
    seenInteractionIds.add(id);
  });
  const sourceRefs = arrayAt(context, document.sourceRefs, "/sourceRefs") ?? [];
  sourceRefs.forEach((reference, index) =>
    validateSourceReference(context, reference, `/sourceRefs/${index}`),
  );
  if (document.contentMode === "source_grounded" && sourceRefs.length === 0) {
    issue(context, "/sourceRefs", "MISSING_SOURCE", "source-grounded content requires source references");
  }
  const mappings =
    arrayAt(context, document.knowledgePointMappings, "/knowledgePointMappings", 1) ?? [];
  mappings.forEach((rawMapping, mappingIndex) => {
    const path = `/knowledgePointMappings/${mappingIndex}`;
    const mapping = objectAt(context, rawMapping, path, [
      "knowledgePointId",
      "sceneIds",
      "sourceRefs",
    ]);
    if (!mapping) return;
    stringAt(context, mapping.knowledgePointId, `${path}/knowledgePointId`);
    const mappedSceneIds = stringArray(context, mapping.sceneIds, `${path}/sceneIds`, 1);
    mappedSceneIds.forEach((id, sceneIndex) => {
      if (!sceneIds.has(id)) {
        issue(context, `${path}/sceneIds/${sceneIndex}`, "UNKNOWN_SCENE", "must reference a classroom scene");
      }
    });
    const refs = arrayAt(context, mapping.sourceRefs, `${path}/sourceRefs`) ?? [];
    refs.forEach((reference, referenceIndex) =>
      validateSourceReference(context, reference, `${path}/sourceRefs/${referenceIndex}`),
    );
  });

  const media = arrayAt(context, document.mediaManifest, "/mediaManifest") ?? [];
  const mediaIds = new Set<string>();
  const mediaPaths = new Set<string>();
  media.forEach((item, index) => {
    const parsed = validateManifestItem(context, item, `/mediaManifest/${index}`, true);
    if (parsed.id) {
      if (mediaIds.has(parsed.id)) {
        issue(context, `/mediaManifest/${index}/mediaId`, "DUPLICATE_ID", "must be unique");
      }
      mediaIds.add(parsed.id);
    }
    if (parsed.relativePath) {
      if (mediaPaths.has(parsed.relativePath)) {
        issue(context, `/mediaManifest/${index}/relativePath`, "DUPLICATE_PATH", "must be unique");
      }
      mediaPaths.add(parsed.relativePath);
    }
  });
  const exports = arrayAt(context, document.exportManifest, "/exportManifest") ?? [];
  exports.forEach((item, index) =>
    validateManifestItem(context, item, `/exportManifest/${index}`, false),
  );
  validateGenerationMetadata(context, document.generationMetadata, "/generationMetadata");

  const audit = objectAt(context, document.auditMetadata, "/auditMetadata", [
    "templateId",
    "templateVersion",
    "teachingBriefId",
    "teachingBriefSha256",
  ], ["parentClassroomVersionId"]);
  if (audit) {
    stringAt(context, audit.templateId, "/auditMetadata/templateId");
    stringAt(context, audit.templateVersion, "/auditMetadata/templateVersion");
    stringAt(context, audit.teachingBriefId, "/auditMetadata/teachingBriefId");
    sha256At(context, audit.teachingBriefSha256, "/auditMetadata/teachingBriefSha256");
    if (
      audit.parentClassroomVersionId !== undefined &&
      audit.parentClassroomVersionId !== null
    ) {
      stringAt(context, audit.parentClassroomVersionId, "/auditMetadata/parentClassroomVersionId");
    }
  }

  const validation = objectAt(context, document.validationResult, "/validationResult", [
    "valid",
    "issues",
    "validatedAt",
  ]);
  if (validation) {
    if (typeof validation.valid !== "boolean") {
      issue(context, "/validationResult/valid", "INVALID_TYPE", "must be a boolean");
    }
    const validationIssues = arrayAt(context, validation.issues, "/validationResult/issues") ?? [];
    validationIssues.forEach((rawValidationIssue, validationIssueIndex) => {
      const path = `/validationResult/issues/${validationIssueIndex}`;
      const validationIssue = objectAt(context, rawValidationIssue, path, [
        "severity",
        "code",
        "message",
        "path",
      ]);
      if (!validationIssue) return;
      if (validationIssue.severity !== "error" && validationIssue.severity !== "warning") {
        issue(context, `${path}/severity`, "INVALID_VALUE", "must be error or warning");
      }
      stringAt(context, validationIssue.code, `${path}/code`);
      stringAt(context, validationIssue.message, `${path}/message`);
      stringAt(context, validationIssue.path, `${path}/path`);
    });
    timestampAt(context, validation.validatedAt, "/validationResult/validatedAt");
  }

  const migrations = arrayAt(context, document.migrationRecords, "/migrationRecords") ?? [];
  migrations.forEach((rawMigration, migrationIndex) => {
    const path = `/migrationRecords/${migrationIndex}`;
    const migration = objectAt(context, rawMigration, path, [
      "fromDslVersion",
      "toDslVersion",
      "migratedAt",
      "migrationId",
    ]);
    if (!migration) return;
    stringAt(context, migration.fromDslVersion, `${path}/fromDslVersion`);
    stringAt(context, migration.toDslVersion, `${path}/toDslVersion`);
    timestampAt(context, migration.migratedAt, `${path}/migratedAt`);
    stringAt(context, migration.migrationId, `${path}/migrationId`);
  });
  jsonAt(context, value, "");
  return context;
}

function cloneDocument(input: ClassroomDocument): ClassroomDocument {
  return JSON.parse(JSON.stringify(input)) as ClassroomDocument;
}

export function parseYFeClassroomDocument(input: unknown): ClassroomDocument {
  const context = validateClassroomDocument(input);
  if (context.issues.length > 0) {
    throw new ClassroomCompatibilityError("INVALID_CLASSROOM_DOCUMENT", context.issues);
  }
  const document = cloneDocument(input as ClassroomDocument);
  document.auditMetadata.parentClassroomVersionId ??= null;
  document.openmaic.scenes = document.openmaic.scenes.map(scene => ({
    ...scene,
    actions: scene.actions ?? [],
  })) as ClassroomScene[];
  return document;
}

function safeRouteSegment(value: string, label: string): string {
  const normalized = value.trim();
  if (
    normalized.length === 0 ||
    normalized !== value ||
    normalized === "." ||
    normalized === ".." ||
    /[\u0000-\u001f\u007f]/.test(normalized)
  ) {
    throw new ClassroomCompatibilityError("UNSAFE_MEDIA_REFERENCE", [
      { path: label, code: "INVALID_ROUTE_SEGMENT", message: "must be a non-empty safe identifier" },
    ]);
  }
  return encodeURIComponent(normalized);
}

export function classroomMediaUrl(
  classroomVersionId: string,
  mediaId: string,
): string {
  return `/api/v1/classrooms/versions/${safeRouteSegment(classroomVersionId, "/classroomVersionId")}/media/${safeRouteSegment(mediaId, "/mediaId")}`;
}

function portableArtifactPath(value: string): boolean {
  return (
    value.length > 0 &&
    !value.includes("\\") &&
    !value.startsWith("/") &&
    !/(?:^|\/)\.(?:\/|$)/.test(value) &&
    !/(?:^|\/)\.\.(?:\/|$)/.test(value) &&
    !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(value) &&
    !/[\u0000-\u001f\u007f]/.test(value)
  );
}

const MEDIA_KEYS = new Set([
  "src",
  "href",
  "poster",
  "audioUrl",
  "videoUrl",
  "imageUrl",
]);

function unsafeExternalReference(value: string): boolean {
  return /^(?:data|blob|file|https?|javascript):/i.test(value) || value.startsWith("//");
}

function rewriteMediaValue(
  value: JsonValue,
  key: string,
  path: string,
  pathToUrl: ReadonlyMap<string, string>,
  controlledUrls: ReadonlySet<string>,
): JsonValue {
  if (typeof value === "string") {
    const resolved = pathToUrl.get(value);
    if (resolved) return resolved;
    const allowedControlledReference =
      controlledUrls.has(value) ||
      (key === "href" && value.startsWith("#"));
    if (
      value.startsWith("media/") ||
      (MEDIA_KEYS.has(key) && !allowedControlledReference)
    ) {
      throw new ClassroomCompatibilityError("UNSAFE_MEDIA_REFERENCE", [
        {
          path,
          code:
            value.startsWith("media/")
              ? "UNKNOWN_MEDIA_PATH"
              : unsafeExternalReference(value)
                ? "EXTERNAL_MEDIA_URL"
                : "UNCONTROLLED_MEDIA_PATH",
          message: "must resolve through the yFeiSTAI classroom media route",
        },
      ]);
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item, index) =>
      rewriteMediaValue(
        item,
        key,
        `${path}/${index}`,
        pathToUrl,
        controlledUrls,
      ),
    );
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([childKey, child]) => [
        childKey,
        rewriteMediaValue(
          child,
          childKey,
          `${path}/${childKey}`,
          pathToUrl,
          controlledUrls,
        ),
      ]),
    );
  }
  return value;
}

function assertInteractiveHtmlIsPortable(html: string, path: string): void {
  if (
    /\b(?:src|srcset|action|poster)\s*=/i.test(html) ||
    /\bhref\s*=\s*["']?\s*(?!#)/i.test(html) ||
    /\b(?:src|href|srcset|action|poster)\s*=\s*["']?\s*(?:[a-z][a-z0-9+.-]*:|\/\/|\/|\\\\)/i.test(html) ||
    /url\s*\(\s*["']?\s*(?:[a-z][a-z0-9+.-]*:|\/\/|\/|\\\\)/i.test(html) ||
    /<(?:base|iframe|object|embed)\b/i.test(html)
  ) {
    throw new ClassroomCompatibilityError("UNSAFE_MEDIA_REFERENCE", [
      {
        path,
        code: "EXTERNAL_INTERACTIVE_RESOURCE",
        message: "interactive HTML must be self-contained and cannot embed nested frames",
      },
    ]);
  }
}

export function resolveClassroomMediaReferences(
  input: ClassroomDocument,
): ClassroomDocument {
  const document = cloneDocument(input);
  const pathToUrl = new Map<string, string>();
  document.mediaManifest.forEach((item, index) => {
    if (!portableArtifactPath(item.relativePath)) {
      throw new ClassroomCompatibilityError("UNSAFE_MEDIA_REFERENCE", [
        {
          path: `/mediaManifest/${index}/relativePath`,
          code: "UNSAFE_MEDIA_PATH",
          message: "must be a portable relative artifact path",
        },
      ]);
    }
    pathToUrl.set(
      item.relativePath,
      classroomMediaUrl(document.classroomVersionId, item.mediaId),
    );
  });
  const controlledUrls = new Set(pathToUrl.values());
  document.openmaic.scenes = document.openmaic.scenes.map((scene, index) => {
    if (scene.type === "interactive") {
      assertInteractiveHtmlIsPortable(
        scene.content.html,
        `/openmaic/scenes/${index}/content/html`,
      );
    }
    const rewritten = rewriteMediaValue(
      scene as unknown as JsonValue,
      "",
      `/openmaic/scenes/${index}`,
      pathToUrl,
      controlledUrls,
    ) as unknown as ClassroomScene;
    return rewritten;
  });
  return document;
}

export const CLASSROOM_THEME_IDS = ["snow", "cream", "dark", "glass"] as const;
export type ClassroomThemeId = (typeof CLASSROOM_THEME_IDS)[number];

export interface ClassroomSlideTheme {
  backgroundColor: string;
  themeColors: string[];
  fontColor: string;
  fontName: string;
}

const CLASSROOM_THEMES: Record<ClassroomThemeId, ClassroomSlideTheme> = {
  snow: {
    backgroundColor: "#ffffff",
    themeColors: ["#2563eb", "#0d0d0d", "#6e6e6e", "#e5e5e5"],
    fontColor: "#0d0d0d",
    fontName: "var(--openmaic-font-sans)",
  },
  cream: {
    backgroundColor: "#fdfcf9",
    themeColors: ["#b0501e", "#1c1816", "#6d645a", "#e6decc"],
    fontColor: "#1c1816",
    fontName: "var(--openmaic-font-serif)",
  },
  dark: {
    backgroundColor: "#1a1918",
    themeColors: ["#d4734b", "#e8e4de", "#9b9590", "#3a3634"],
    fontColor: "#e8e4de",
    fontName: "var(--openmaic-font-sans)",
  },
  glass: {
    backgroundColor: "#0e0d1a",
    themeColors: ["#a855f7", "#ffffff", "#b5b5c7", "#38344a"],
    fontColor: "#ffffff",
    fontName: "var(--openmaic-font-sans)",
  },
};

export function mapClassroomTheme(theme: ClassroomThemeId): ClassroomSlideTheme {
  const mapped = CLASSROOM_THEMES[theme];
  if (!mapped) {
    throw new ClassroomCompatibilityError("OPENMAIC_VALIDATION_FAILED", [
      { path: "/theme", code: "UNSUPPORTED_THEME", message: `unsupported classroom theme ${JSON.stringify(theme)}` },
    ]);
  }
  return { ...mapped, themeColors: [...mapped.themeColors] };
}
