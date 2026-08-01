export * from "./contracts";
export * from "./dsl";

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
