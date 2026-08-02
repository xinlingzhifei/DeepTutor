export * from "./contracts";
export * from "./dsl";
export * from "./edit-intents";
export * from "./editor-history";
export * from "./scene-operations";
export * from "./playback/action-reducer";
export * from "./playback/controller";
export * from "./playback/events";
export * from "./playback/interactive-bridge";
export * from "./playback/quiz-submission";
export * from "./playback/types";
export * from "./sanitize";

export {
  HighlightOverlay as ClassroomHighlightOverlay,
  LaserOverlay as ClassroomLaserOverlay,
  SlideCanvas as ClassroomSlideCanvas,
  SpotlightOverlay as ClassroomSpotlightOverlay,
} from "@openmaic/renderer";

export type {
  SlideCanvasProps as ClassroomSlideCanvasProps,
} from "@openmaic/renderer";

export {
  EditableSlideCanvas as EditableClassroomCanvas,
} from "@openmaic/renderer/editing";

export type {
  EditableSlideCanvasProps as EditableClassroomCanvasProps,
} from "@openmaic/renderer/editing";

import type { EditIntent as RendererEditIntent } from "@openmaic/renderer/editing";
import type { ClassroomEditIntent } from "./edit-intents";

type ExactIntentUnion =
  RendererEditIntent extends ClassroomEditIntent
    ? ClassroomEditIntent extends RendererEditIntent
      ? true
      : false
    : false;
type AssertTrue<T extends true> = T;

/** Compile-time guard: adapter and renderer L1 intent unions must stay exact. */
export type ClassroomEditIntentContract = AssertTrue<ExactIntentUnion>;
