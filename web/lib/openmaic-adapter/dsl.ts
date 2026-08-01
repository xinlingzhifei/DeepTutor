import {
  DSL_VERSION,
  migrate,
  normalizeScene,
  validateAction,
  validateScene,
  validateStage,
  type Action,
  type QuizContent as OpenMaicQuizContent,
  type Scene as OpenMaicScene,
  type Slide,
  type SlideContent as OpenMaicSlideContent,
  type Stage,
  type ValidationIssue as OpenMaicValidationIssue,
} from "@openmaic/dsl";

import {
  ClassroomCompatibilityError,
  mapClassroomTheme,
  parseYFeClassroomDocument,
  resolveClassroomMediaReferences,
  type ClassroomDocument,
  type ClassroomScene,
  type ClassroomThemeId,
  type InteractiveScene,
  type JsonObject,
  type PblScene,
  type QuizScene,
  type SlideScene,
} from "./contracts";

export interface ReadClassroomDocumentOptions {
  theme?: ClassroomThemeId;
}

export type RenderableSlideScene = OpenMaicScene<Action, OpenMaicSlideContent>;
export type RenderableQuizScene = OpenMaicScene<Action, OpenMaicQuizContent>;
export type RenderableInteractiveScene = Omit<InteractiveScene, "actions"> & {
  actions: Action[];
};
export type RenderablePblScene = Omit<PblScene, "actions"> & {
  actions: Action[];
};

// OpenMAIC 0.4.0 deliberately validates only slide and quiz scene content.
// yFeiSTAI owns the richer interactive/PBL shapes but still validates their
// playback actions against OpenMAIC's real Action contract.
export type RenderableClassroomScene =
  | RenderableSlideScene
  | RenderableQuizScene
  | RenderableInteractiveScene
  | RenderablePblScene;

function compatibilityIssues(
  prefix: string,
  errors: readonly OpenMaicValidationIssue[],
) {
  return errors.map(error => ({
    path: `${prefix}${error.path === "/" ? "" : error.path}` || "/",
    code: "OPENMAIC_CONTRACT",
    message: error.message,
  }));
}

function asObject(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function migratedOpenMaicDocument(
  input: ClassroomDocument["openmaic"],
): ClassroomDocument["openmaic"] {
  let migrated: unknown;
  try {
    migrated = migrate(input);
  } catch (error) {
    throw new ClassroomCompatibilityError("UNSUPPORTED_DSL_VERSION", [
      {
        path: "/openmaic/dslVersion",
        code: "MIGRATION_FAILED",
        message: error instanceof Error ? error.message : "OpenMAIC migration failed",
      },
    ]);
  }
  const record = asObject(migrated);
  if (!record || record.dslVersion !== DSL_VERSION) {
    throw new ClassroomCompatibilityError("UNSUPPORTED_DSL_VERSION", [
      {
        path: "/openmaic/dslVersion",
        code: "UNSUPPORTED_DSL_VERSION",
        message: `requires ${DSL_VERSION}; received ${JSON.stringify(record?.dslVersion)}`,
      },
    ]);
  }
  return migrated as ClassroomDocument["openmaic"];
}

export function migrateOpenMaicDocument(
  input: ClassroomDocument["openmaic"],
): ClassroomDocument["openmaic"] {
  return migratedOpenMaicDocument(input);
}

function openMaicStage(document: ClassroomDocument): Stage {
  const source = document.openmaic.stage;
  const stage: Stage = {
    id: source.id,
    name: source.name,
    createdAt: Date.parse(source.createdAt),
    updatedAt: Date.parse(source.updatedAt),
  };
  const report = validateStage(stage);
  if (!report.valid) {
    throw new ClassroomCompatibilityError(
      "OPENMAIC_VALIDATION_FAILED",
      compatibilityIssues("/openmaic/stage", report.errors),
    );
  }
  return stage;
}

function normalizeClassroomAction(
  sceneId: string,
  source: JsonObject,
  index: number,
): Action {
  const payload = asObject(source.payload);
  const candidate: Record<string, unknown> = {
    ...source,
    ...(payload ?? {}),
    id:
      typeof source.id === "string" && source.id.length > 0
        ? source.id
        : `${sceneId}:action:${index + 1}`,
  };
  delete candidate.payload;
  const report = validateAction(candidate);
  if (!report.valid) {
    throw new ClassroomCompatibilityError(
      "OPENMAIC_VALIDATION_FAILED",
      compatibilityIssues(
        `/openmaic/scenes/${sceneId}/actions/${index}`,
        report.errors,
      ),
    );
  }
  return candidate as unknown as Action;
}

function normalizedActions(scene: ClassroomScene): Action[] {
  return scene.actions.map((action, index) =>
    normalizeClassroomAction(scene.id, action, index),
  );
}

function finitePositive(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function slideCompatibilityIssues(slide: Record<string, unknown>, path: string) {
  const issues: Array<{ path: string; code: string; message: string }> = [];
  if (typeof slide.id !== "string" || slide.id.length === 0) {
    issues.push({ path: `${path}/id`, code: "INVALID_SLIDE", message: "must be a non-empty string" });
  }
  if (!finitePositive(slide.viewportSize)) {
    issues.push({ path: `${path}/viewportSize`, code: "INVALID_SLIDE", message: "must be a positive finite number" });
  }
  if (!finitePositive(slide.viewportRatio)) {
    issues.push({ path: `${path}/viewportRatio`, code: "INVALID_SLIDE", message: "must be a positive finite number" });
  }
  const theme = asObject(slide.theme);
  if (
    !theme ||
    typeof theme.backgroundColor !== "string" ||
    !Array.isArray(theme.themeColors) ||
    theme.themeColors.some(color => typeof color !== "string") ||
    typeof theme.fontColor !== "string" ||
    typeof theme.fontName !== "string"
  ) {
    issues.push({ path: `${path}/theme`, code: "INVALID_SLIDE", message: "must be a complete OpenMAIC slide theme" });
  }
  if (!Array.isArray(slide.elements)) {
    issues.push({ path: `${path}/elements`, code: "INVALID_SLIDE", message: "must be an array" });
  } else {
    slide.elements.forEach((element, index) => {
      const elementPath = `${path}/elements/${index}`;
      const record = asObject(element);
      if (!record) {
        issues.push({ path: elementPath, code: "INVALID_ELEMENT", message: "must be an object" });
        return;
      }
      if (typeof record.id !== "string" || record.id.length === 0) {
        issues.push({ path: `${elementPath}/id`, code: "INVALID_ELEMENT", message: "must be a non-empty string" });
      }
      if (typeof record.type !== "string" || record.type.length === 0) {
        issues.push({ path: `${elementPath}/type`, code: "INVALID_ELEMENT", message: "must be a known element type" });
      }
      for (const field of ["left", "top", "width", "height", "rotate"]) {
        if (typeof record[field] !== "number" || !Number.isFinite(record[field])) {
          issues.push({ path: `${elementPath}/${field}`, code: "INVALID_ELEMENT", message: "must be a finite number" });
        }
      }
    });
  }
  return issues;
}

function slideFromScene(scene: SlideScene, theme: ClassroomThemeId): Slide {
  const source = scene.content.canvas;
  const background = source.background;
  const candidate: Record<string, unknown> = {
    ...source,
    id:
      typeof source.id === "string" && source.id.length > 0
        ? source.id
        : scene.id,
    viewportSize: finitePositive(source.viewportSize) ? source.viewportSize : 1_000,
    viewportRatio: finitePositive(source.viewportRatio)
      ? source.viewportRatio
      : 16 / 9,
    theme: mapClassroomTheme(theme),
    elements: Array.isArray(source.elements) ? source.elements : [],
  };
  if (background === null || background === undefined) {
    delete candidate.background;
  }
  return candidate as unknown as Slide;
}

function renderableSlideScene(
  scene: SlideScene,
  theme: ClassroomThemeId,
): RenderableSlideScene {
  const candidate: RenderableSlideScene = {
    id: scene.id,
    stageId: scene.stageId,
    title: scene.title,
    order: scene.order,
    type: "slide",
    content: { type: "slide", canvas: slideFromScene(scene, theme) },
    actions: normalizedActions(scene),
  };
  let normalized: RenderableSlideScene;
  try {
    normalized = normalizeScene(candidate);
  } catch (error) {
    throw new ClassroomCompatibilityError("OPENMAIC_VALIDATION_FAILED", [
      {
        path: `/openmaic/scenes/${scene.id}/content/canvas`,
        code: "NORMALIZATION_FAILED",
        message: error instanceof Error ? error.message : "slide normalization failed",
      },
    ]);
  }
  const report = validateScene(normalized);
  const slideIssues = slideCompatibilityIssues(
    normalized.content.canvas as unknown as Record<string, unknown>,
    `/openmaic/scenes/${scene.id}/content/canvas`,
  );
  if (!report.valid || slideIssues.length > 0) {
    throw new ClassroomCompatibilityError("OPENMAIC_VALIDATION_FAILED", [
      ...(report.valid
        ? []
        : compatibilityIssues(`/openmaic/scenes/${scene.id}`, report.errors)),
      ...slideIssues,
    ]);
  }
  return normalized;
}

function renderableQuizScene(scene: QuizScene): RenderableQuizScene {
  const candidate: RenderableQuizScene = {
    id: scene.id,
    stageId: scene.stageId,
    title: scene.title,
    order: scene.order,
    type: "quiz",
    content: {
      type: "quiz",
      questions: scene.content.questions.map(question => ({
        id: question.id,
        type:
          question.questionType === "single_choice"
            ? "single"
            : question.questionType === "multiple_choice"
              ? "multiple"
              : "short_answer",
        question: question.prompt,
        options: question.options.map(option => ({
          label: option.label,
          value: option.id,
        })),
        answer: [...question.correctOptionIds],
        analysis: question.explanation,
        hasAnswer: question.correctOptionIds.length > 0,
      })),
    },
    actions: normalizedActions(scene),
  };
  const report = validateScene(candidate);
  if (!report.valid) {
    throw new ClassroomCompatibilityError(
      "OPENMAIC_VALIDATION_FAILED",
      compatibilityIssues(`/openmaic/scenes/${scene.id}`, report.errors),
    );
  }
  return candidate;
}

export function toRenderableClassroomScene(
  scene: ClassroomScene,
  options: ReadClassroomDocumentOptions = {},
): RenderableClassroomScene {
  if (scene.type === "slide") {
    return renderableSlideScene(scene, options.theme ?? "snow");
  }
  if (scene.type === "quiz") return renderableQuizScene(scene);
  return { ...scene, actions: normalizedActions(scene) };
}

export function validateOpenMaicDocument(
  document: ClassroomDocument,
  options: ReadClassroomDocumentOptions = {},
): void {
  openMaicStage(document);
  document.openmaic.scenes.forEach(scene => {
    toRenderableClassroomScene(scene, options);
  });
}

export function readClassroomDocument(
  input: unknown,
  options: ReadClassroomDocumentOptions = {},
): ClassroomDocument {
  const parsed = parseYFeClassroomDocument(input);
  if (!parsed.validationResult.valid) {
    const reportedIssues = parsed.validationResult.issues
      .filter(issue => issue.severity === "error")
      .map(issue => ({
        path: issue.path,
        code: issue.code,
        message: issue.message,
      }));
    throw new ClassroomCompatibilityError(
      "OPENMAIC_VALIDATION_FAILED",
      reportedIssues.length > 0
        ? reportedIssues
        : [
            {
              path: "/validationResult/valid",
              code: "UPSTREAM_VALIDATION_FAILED",
              message: "the classroom was not validated for publication",
            },
          ],
    );
  }
  const migrated: ClassroomDocument = {
    ...parsed,
    openmaic: migratedOpenMaicDocument(parsed.openmaic),
  };
  const resolved = resolveClassroomMediaReferences(migrated);
  validateOpenMaicDocument(resolved, options);
  return resolved;
}
