import {
  assertPortableInteractiveHtml,
  parseYFeClassroomDocument,
  type ClassroomDocument,
  type ClassroomScene,
  type PblMilestone,
  type PblRole,
  type QuizOption,
  type QuizQuestion,
  type SourceReference,
} from "./contracts";

export interface SceneAddOperation {
  type: "scene.add";
  scene: ClassroomScene;
  toIndex?: number;
}

export interface SceneDuplicateOperation {
  type: "scene.duplicate";
  sceneId: string;
  newSceneId: string;
  title?: string;
  toIndex?: number;
}

export interface SceneDeleteOperation {
  type: "scene.delete";
  sceneId: string;
}

export interface SceneReorderOperation {
  type: "scene.reorder";
  sceneId: string;
  toIndex: number;
}

export interface SceneUpdateOperation {
  type: "scene.update";
  sceneId: string;
  title: string;
}

export interface QuizUpdateOperation {
  type: "quiz.update";
  sceneId: string;
  questionId?: string;
  question?: string;
  prompt?: string;
  questionType?: QuizQuestion["questionType"];
  options?: readonly (string | QuizOption)[];
  correctOption?: number;
  correctOptionIds?: readonly string[];
  explanation?: string;
  knowledgePointIds?: readonly string[];
}

export interface InteractiveUpdateOperation {
  type: "interactive.update";
  sceneId: string;
  html: string;
  config?: {
    bridgeVersion: "1.0";
    sandbox: { allowScripts: true; allowSameOrigin: false };
  };
}

export interface PblUpdateOperation {
  type: "pbl.update";
  sceneId: string;
  scenario?: string;
  roles?: readonly PblRole[];
  milestones?: readonly PblMilestone[];
}

export type SceneOperation =
  | SceneAddOperation
  | SceneDuplicateOperation
  | SceneDeleteOperation
  | SceneReorderOperation
  | SceneUpdateOperation
  | QuizUpdateOperation
  | InteractiveUpdateOperation
  | PblUpdateOperation;

export type SceneOperationErrorCode =
  | "INVALID_SCENE_OPERATION"
  | "UNKNOWN_SCENE"
  | "DUPLICATE_SCENE_ID"
  | "SCENE_TYPE_MISMATCH"
  | "INVALID_QUIZ"
  | "INVALID_INTERACTIVE"
  | "INVALID_PBL"
  | "INVALID_DRAFT_RESPONSE";

export class SceneOperationError extends Error {
  readonly code: SceneOperationErrorCode;

  constructor(code: SceneOperationErrorCode, message: string) {
    super(`${code}: ${message}`);
    this.name = "SceneOperationError";
    this.code = code;
  }
}

function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function nonEmpty(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new SceneOperationError("INVALID_SCENE_OPERATION", `${label} must be non-empty`);
  }
  return value;
}

function uniqueNonEmpty(values: readonly string[], label: string): string[] {
  const normalized = values.map((value, index) => nonEmpty(value, `${label}[${index}]`));
  if (new Set(normalized).size !== normalized.length) {
    throw new SceneOperationError("INVALID_SCENE_OPERATION", `${label} must be unique`);
  }
  return normalized;
}

function sceneIndex(document: ClassroomDocument, id: string): number {
  nonEmpty(id, "scene id");
  const index = document.openmaic.scenes.findIndex(scene => scene.id === id);
  if (index < 0) {
    throw new SceneOperationError("UNKNOWN_SCENE", `scene ${JSON.stringify(id)} does not exist`);
  }
  return index;
}

function insertionIndex(value: number | undefined, length: number): number {
  const index = value ?? length;
  if (!Number.isInteger(index) || index < 0 || index > length) {
    throw new SceneOperationError("INVALID_SCENE_OPERATION", "scene insertion index is out of bounds");
  }
  return index;
}

function reorderIndex(value: number, length: number): number {
  if (!Number.isInteger(value) || value < 0 || value >= length) {
    throw new SceneOperationError("INVALID_SCENE_OPERATION", "scene reorder index is out of bounds");
  }
  return value;
}

function normalizeOrders(document: ClassroomDocument): void {
  document.openmaic.scenes.forEach((scene, order) => {
    scene.order = order;
  });
}

function updateInteractionIdsForDuplicate(
  document: ClassroomDocument,
  sourceId: string,
  newId: string,
): void {
  if (document.interactionIds.includes(sourceId)) {
    document.interactionIds = [...document.interactionIds, newId];
  }
}

function updateMappingsForDuplicate(
  document: ClassroomDocument,
  sourceId: string,
  newId: string,
): void {
  document.knowledgePointMappings = document.knowledgePointMappings.map(mapping =>
    mapping.sceneIds.includes(sourceId)
      ? { ...mapping, sceneIds: [...mapping.sceneIds, newId] }
      : mapping,
  );
}

function removeSceneReferences(document: ClassroomDocument, sceneId: string): void {
  document.interactionIds = document.interactionIds.filter(id => id !== sceneId);
  document.knowledgePointMappings = document.knowledgePointMappings
    .map(mapping => ({
      ...mapping,
      sceneIds: mapping.sceneIds.filter(id => id !== sceneId),
    }))
    .filter(mapping => mapping.sceneIds.length > 0);
}

function normalizeQuizOptions(
  question: QuizQuestion,
  input: readonly (string | QuizOption)[],
): QuizOption[] {
  const options = input.map((option, index) => {
    if (typeof option === "string") {
      return {
        id: question.options[index]?.id ?? `${question.id}-option-${index + 1}`,
        label: nonEmpty(option, `option ${index + 1}`),
      };
    }
    return {
      id: nonEmpty(option.id, `option ${index + 1} id`),
      label: nonEmpty(option.label, `option ${index + 1} label`),
    };
  });
  if (new Set(options.map(option => option.id)).size !== options.length) {
    throw new SceneOperationError("INVALID_QUIZ", "quiz option ids must be unique");
  }
  return options;
}

function updateKnowledgePoints(
  document: ClassroomDocument,
  sceneId: string,
  desiredIds: readonly string[],
): void {
  const desired = uniqueNonEmpty(desiredIds, "knowledge point ids");
  const desiredSet = new Set(desired);
  const existing = new Set(document.knowledgePointMappings.map(mapping => mapping.knowledgePointId));
  document.knowledgePointMappings = document.knowledgePointMappings
    .map(mapping => {
      const contains = mapping.sceneIds.includes(sceneId);
      if (desiredSet.has(mapping.knowledgePointId) && !contains) {
        return { ...mapping, sceneIds: [...mapping.sceneIds, sceneId] };
      }
      if (!desiredSet.has(mapping.knowledgePointId) && contains) {
        return { ...mapping, sceneIds: mapping.sceneIds.filter(id => id !== sceneId) };
      }
      return mapping;
    })
    .filter(mapping => mapping.sceneIds.length > 0);
  for (const knowledgePointId of desired) {
    if (!existing.has(knowledgePointId)) {
      const sourceRefs: SourceReference[] = [];
      document.knowledgePointMappings.push({
        knowledgePointId,
        sceneIds: [sceneId],
        sourceRefs,
      });
    }
  }
}

function applyQuizUpdate(document: ClassroomDocument, operation: QuizUpdateOperation): void {
  const scene = document.openmaic.scenes[sceneIndex(document, operation.sceneId)];
  if (scene.type !== "quiz") {
    throw new SceneOperationError("SCENE_TYPE_MISMATCH", `${scene.id} is not a quiz scene`);
  }
  if (operation.question !== undefined && operation.prompt !== undefined) {
    throw new SceneOperationError("INVALID_QUIZ", "use either question or prompt, not both");
  }
  if (operation.options !== undefined && !Array.isArray(operation.options)) {
    throw new SceneOperationError("INVALID_QUIZ", "quiz options must be an array");
  }
  if (operation.correctOptionIds !== undefined && !Array.isArray(operation.correctOptionIds)) {
    throw new SceneOperationError("INVALID_QUIZ", "correct option ids must be an array");
  }
  if (operation.knowledgePointIds !== undefined && !Array.isArray(operation.knowledgePointIds)) {
    throw new SceneOperationError("INVALID_QUIZ", "knowledge point ids must be an array");
  }
  const question = operation.questionId
    ? scene.content.questions.find(candidate => candidate.id === operation.questionId)
    : scene.content.questions.length === 1
      ? scene.content.questions[0]
      : undefined;
  if (!question) {
    throw new SceneOperationError("INVALID_QUIZ", "questionId is required and must exist");
  }
  const prompt = operation.question ?? operation.prompt;
  if (prompt !== undefined) question.prompt = nonEmpty(prompt, "question prompt");
  if (operation.explanation !== undefined) {
    question.explanation = nonEmpty(operation.explanation, "question explanation");
  }
  if (operation.questionType !== undefined) question.questionType = operation.questionType;
  if (operation.options !== undefined) {
    question.options = normalizeQuizOptions(question, operation.options);
  }
  if (question.questionType === "short_answer") {
    if (
      (operation.options !== undefined && operation.options.length > 0) ||
      operation.correctOption !== undefined ||
      (operation.correctOptionIds !== undefined && operation.correctOptionIds.length > 0)
    ) {
      throw new SceneOperationError("INVALID_QUIZ", "short-answer questions cannot have options or option ids");
    }
    question.options = [];
    question.correctOptionIds = [];
  } else if (operation.correctOption !== undefined) {
    if (
      !Number.isInteger(operation.correctOption) ||
      operation.correctOption < 0 ||
      operation.correctOption >= question.options.length
    ) {
      throw new SceneOperationError("INVALID_QUIZ", "correct option index is out of bounds");
    }
    question.correctOptionIds = [question.options[operation.correctOption].id];
  } else if (operation.correctOptionIds !== undefined) {
    const ids = uniqueNonEmpty(operation.correctOptionIds, "correct option ids");
    const optionIds = new Set(question.options.map(option => option.id));
    if (ids.some(id => !optionIds.has(id))) {
      throw new SceneOperationError("INVALID_QUIZ", "correct option ids must reference current options");
    }
    question.correctOptionIds = ids;
  }
  if (question.questionType === "single_choice" && question.correctOptionIds.length !== 1) {
    throw new SceneOperationError("INVALID_QUIZ", "single-choice questions require one answer");
  }
  if (question.questionType === "multiple_choice" && question.correctOptionIds.length === 0) {
    throw new SceneOperationError("INVALID_QUIZ", "multiple-choice questions require an answer");
  }
  if (operation.knowledgePointIds !== undefined) {
    updateKnowledgePoints(document, scene.id, operation.knowledgePointIds);
  }
}

function assertPortablePblFields(
  scenario: string,
  roles: readonly PblRole[],
  milestones: readonly PblMilestone[],
): void {
  nonEmpty(scenario, "PBL scenario");
  if (roles.length === 0 || milestones.length === 0) {
    throw new SceneOperationError("INVALID_PBL", "PBL requires roles and milestones");
  }
  const roleIds = uniqueNonEmpty(roles.map(role => role.id), "PBL role ids");
  roles.forEach((role, index) => {
    nonEmpty(roleIds[index], `role ${index + 1} id`);
    nonEmpty(role.name, `role ${index + 1} name`);
    nonEmpty(role.brief, `role ${index + 1} brief`);
  });
  const milestoneIds = uniqueNonEmpty(
    milestones.map(milestone => milestone.id),
    "PBL milestone ids",
  );
  milestones.forEach((milestone, index) => {
    nonEmpty(milestoneIds[index], `milestone ${index + 1} id`);
    nonEmpty(milestone.title, `milestone ${index + 1} title`);
    nonEmpty(milestone.rubric, `milestone ${index + 1} rubric`);
  });
}

function applyOperation(document: ClassroomDocument, operation: SceneOperation): void {
  if (!record(operation) || typeof operation.type !== "string") {
    throw new SceneOperationError(
      "INVALID_SCENE_OPERATION",
      "scene operation must be an object with a type",
    );
  }
  if (operation.type === "scene.add") {
    if (!record(operation.scene)) {
      throw new SceneOperationError("INVALID_SCENE_OPERATION", "added scene must be an object");
    }
    const scene = cloneJson(operation.scene);
    nonEmpty(scene.id, "scene id");
    if (document.openmaic.scenes.some(candidate => candidate.id === scene.id)) {
      throw new SceneOperationError("DUPLICATE_SCENE_ID", `duplicate scene id ${scene.id}`);
    }
    if (scene.stageId !== document.openmaic.stage.id || scene.content.type !== scene.type) {
      throw new SceneOperationError("SCENE_TYPE_MISMATCH", "scene stage/type binding is invalid");
    }
    const index = insertionIndex(operation.toIndex, document.openmaic.scenes.length);
    document.openmaic.scenes.splice(index, 0, scene);
    if (scene.type === "interactive") {
      document.interactionIds = [...document.interactionIds, scene.id];
    }
    return;
  }
  if (operation.type === "scene.duplicate") {
    const sourceIndex = sceneIndex(document, operation.sceneId);
    const newSceneId = nonEmpty(operation.newSceneId, "new scene id");
    if (document.openmaic.scenes.some(scene => scene.id === newSceneId)) {
      throw new SceneOperationError("DUPLICATE_SCENE_ID", `duplicate scene id ${newSceneId}`);
    }
    const duplicate = cloneJson(document.openmaic.scenes[sourceIndex]);
    duplicate.id = newSceneId;
    if (operation.title !== undefined) duplicate.title = nonEmpty(operation.title, "scene title");
    const target = insertionIndex(operation.toIndex ?? sourceIndex + 1, document.openmaic.scenes.length);
    document.openmaic.scenes.splice(target, 0, duplicate);
    updateInteractionIdsForDuplicate(document, operation.sceneId, newSceneId);
    updateMappingsForDuplicate(document, operation.sceneId, newSceneId);
    return;
  }
  if (operation.type === "scene.delete") {
    if (document.openmaic.scenes.length === 1) {
      throw new SceneOperationError("INVALID_SCENE_OPERATION", "the last classroom scene cannot be deleted");
    }
    const index = sceneIndex(document, operation.sceneId);
    document.openmaic.scenes.splice(index, 1);
    removeSceneReferences(document, operation.sceneId);
    return;
  }
  if (operation.type === "scene.reorder") {
    const index = sceneIndex(document, operation.sceneId);
    const target = reorderIndex(operation.toIndex, document.openmaic.scenes.length);
    if (index !== target) {
      const [scene] = document.openmaic.scenes.splice(index, 1);
      document.openmaic.scenes.splice(target, 0, scene);
    }
    return;
  }
  if (operation.type === "scene.update") {
    const scene = document.openmaic.scenes[sceneIndex(document, operation.sceneId)];
    scene.title = nonEmpty(operation.title, "scene title");
    return;
  }
  if (operation.type === "quiz.update") {
    applyQuizUpdate(document, operation);
    return;
  }
  if (operation.type === "interactive.update") {
    const scene = document.openmaic.scenes[sceneIndex(document, operation.sceneId)];
    if (scene.type !== "interactive") {
      throw new SceneOperationError("SCENE_TYPE_MISMATCH", `${scene.id} is not interactive`);
    }
    nonEmpty(operation.html, "interactive HTML");
    assertPortableInteractiveHtml(operation.html, `/openmaic/scenes/${scene.id}/content/html`);
    if (operation.config) {
      const config = record(operation.config);
      const sandbox = config ? record(config.sandbox) : null;
      if (
        !config ||
        !sandbox ||
        config.bridgeVersion !== "1.0" ||
        sandbox.allowScripts !== true ||
        sandbox.allowSameOrigin !== false
      ) {
        throw new SceneOperationError("INVALID_INTERACTIVE", "interactive sandbox policy cannot be weakened");
      }
    }
    scene.content = {
      type: "interactive",
      html: operation.html,
      bridgeVersion: "1.0",
      sandbox: { allowScripts: true, allowSameOrigin: false },
    };
    return;
  }
  if (operation.type === "pbl.update") {
    const scene = document.openmaic.scenes[sceneIndex(document, operation.sceneId)];
    if (scene.type !== "pbl") {
      throw new SceneOperationError("SCENE_TYPE_MISMATCH", `${scene.id} is not PBL`);
    }
    if (operation.roles !== undefined && !Array.isArray(operation.roles)) {
      throw new SceneOperationError("INVALID_PBL", "PBL roles must be an array");
    }
    if (operation.milestones !== undefined && !Array.isArray(operation.milestones)) {
      throw new SceneOperationError("INVALID_PBL", "PBL milestones must be an array");
    }
    const scenario = operation.scenario ?? scene.content.scenario;
    const roles: PblRole[] = cloneJson([...(operation.roles ?? scene.content.roles)]);
    const milestones: PblMilestone[] = cloneJson([
      ...(operation.milestones ?? scene.content.milestones),
    ]);
    assertPortablePblFields(scenario, roles, milestones);
    scene.content = { type: "pbl", scenario, roles, milestones };
    return;
  }
  const exhaustive: never = operation;
  throw new SceneOperationError(
    "INVALID_SCENE_OPERATION",
    `unsupported operation ${(exhaustive as { type?: unknown }).type as string}`,
  );
}

function validateDraftAggregate(document: ClassroomDocument): ClassroomDocument {
  document.openmaic.scenes.forEach((scene, index) => {
    if (scene.type === "interactive") {
      assertPortableInteractiveHtml(
        scene.content.html,
        `/openmaic/scenes/${index}/content/html`,
      );
    }
  });
  return parseYFeClassroomDocument(document);
}

export function applySceneOperations(
  input: ClassroomDocument,
  operations: readonly SceneOperation[],
): ClassroomDocument {
  if (!Array.isArray(operations) || operations.length === 0) {
    throw new SceneOperationError("INVALID_SCENE_OPERATION", "an operation batch is required");
  }
  const document = cloneJson(input);
  operations.forEach(operation => applyOperation(document, operation));
  normalizeOrders(document);
  return validateDraftAggregate(document);
}

export interface SaveClassroomDraftRequest {
  classroomId: string;
  revision: string;
  document: ClassroomDocument;
  fetch?: typeof globalThis.fetch;
}

export interface SavedClassroomDraft {
  status: "saved";
  revision: string;
  document: ClassroomDocument;
}

export interface ConflictingClassroomDraft {
  status: "conflict";
  responseStatus: 409 | 412;
  clientRevision: string;
  serverRevision: string;
  serverDocument: ClassroomDocument;
}

export type SaveClassroomDraftResult =
  | SavedClassroomDraft
  | ConflictingClassroomDraft;

function readDraftEnvelope(input: unknown): {
  revision: string;
  document: ClassroomDocument;
} {
  const envelope = record(input);
  const revision =
    envelope?.revision ?? envelope?.serverRevision ?? envelope?.server_revision;
  const document =
    envelope?.document ?? envelope?.serverDocument ?? envelope?.server_document;
  if (typeof revision !== "string" || revision.trim().length === 0 || !document) {
    throw new SceneOperationError(
      "INVALID_DRAFT_RESPONSE",
      "draft response must include a revision and classroom document",
    );
  }
  return {
    revision,
    document: validateDraftAggregate(cloneJson(document) as ClassroomDocument),
  };
}

async function responseJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new SceneOperationError("INVALID_DRAFT_RESPONSE", "draft response must be JSON");
  }
}

export async function saveClassroomDraft(
  request: SaveClassroomDraftRequest,
): Promise<SaveClassroomDraftResult> {
  const classroomId = nonEmpty(request.classroomId, "classroom id");
  const revision = nonEmpty(request.revision, "draft revision");
  const document = validateDraftAggregate(cloneJson(request.document));
  const fetcher = request.fetch ?? globalThis.fetch;
  if (typeof fetcher !== "function") {
    throw new SceneOperationError("INVALID_SCENE_OPERATION", "fetch is unavailable");
  }
  const response = await fetcher(
    `/api/v1/classrooms/${encodeURIComponent(classroomId)}/draft`,
    {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "If-Match": revision,
      },
      body: JSON.stringify({ document }),
    },
  );
  if (response.status === 409 || response.status === 412) {
    const server = readDraftEnvelope(await responseJson(response));
    return {
      status: "conflict",
      responseStatus: response.status,
      clientRevision: revision,
      serverRevision: server.revision,
      serverDocument: server.document,
    };
  }
  if (!response.ok) {
    throw new SceneOperationError(
      "INVALID_DRAFT_RESPONSE",
      `draft save failed with status ${response.status}`,
    );
  }
  const saved = readDraftEnvelope(await responseJson(response));
  return { status: "saved", revision: saved.revision, document: saved.document };
}
