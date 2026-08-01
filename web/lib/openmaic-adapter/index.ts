export * from "./contracts";
export * from "./dsl";
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
  EditIntent as ClassroomEditIntent,
  EditableSlideCanvasProps as EditableClassroomCanvasProps,
} from "@openmaic/renderer/editing";
