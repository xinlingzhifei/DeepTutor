import type { PPTElement, Slide } from "@openmaic/dsl";

import {
  mapClassroomTheme,
  type ClassroomDocument,
  type ClassroomThemeId,
  type JsonObject,
  type SlideScene,
} from "./contracts";

export type ClassroomReorderCommand = "front" | "back" | "forward" | "backward";
export type ClassroomAlignCommand =
  | "left"
  | "center"
  | "right"
  | "top"
  | "middle"
  | "bottom";

export interface ClassroomSelection {
  readonly elementIds: readonly string[];
  readonly primaryId?: string;
  readonly groupId?: string;
  readonly editingId?: string;
}

export const EMPTY_CLASSROOM_SELECTION: ClassroomSelection = Object.freeze({
  elementIds: Object.freeze([] as string[]),
});

// Mirrors @openmaic/renderer/editing's public L1 union. The adapter owns this
// local name so business components never import an OpenMAIC package directly.
export type ClassroomEditIntent =
  | { type: "element.update"; id: string; props: Partial<PPTElement> }
  | {
      type: "element.updateMany";
      updates: Array<{ id: string; props: Partial<PPTElement> }>;
    }
  | { type: "element.add"; element: PPTElement; index?: number }
  | { type: "element.delete"; ids: string[] }
  | { type: "element.reorder"; id: string; command: ClassroomReorderCommand }
  | { type: "element.align"; ids: string[]; command: ClassroomAlignCommand }
  | { type: "element.removeProps"; id: string; props: string[] }
  | {
      type: "text.updateContent";
      id: string;
      content: string;
      target: "text" | "shape";
    };

export type ClassroomEditErrorCode =
  | "INVALID_EDIT_INTENT"
  | "UNKNOWN_SLIDE"
  | "UNKNOWN_ELEMENT"
  | "DUPLICATE_ELEMENT_ID"
  | "IMMUTABLE_ELEMENT_FIELD"
  | "INVALID_ELEMENT"
  | "ELEMENT_OUT_OF_BOUNDS"
  | "ELEMENT_IN_USE";

export class ClassroomEditError extends Error {
  readonly code: ClassroomEditErrorCode;

  constructor(code: ClassroomEditErrorCode, message: string) {
    super(`${code}: ${message}`);
    this.name = "ClassroomEditError";
    this.code = code;
  }
}

const ELEMENT_TYPES = new Set([
  "text",
  "image",
  "shape",
  "line",
  "chart",
  "table",
  "latex",
  "video",
  "audio",
  "code",
]);

const BASE_REQUIRED = ["id", "type", "left", "top", "width"] as const;
const BOX_REQUIRED = ["height", "rotate"] as const;
const TYPE_REQUIRED: Readonly<Record<string, readonly string[]>> = {
  text: ["content", "defaultFontName", "defaultColor"],
  image: ["fixedRatio", "src"],
  shape: ["viewBox", "path", "fixedRatio", "fill"],
  line: ["start", "end", "style", "color", "points"],
  chart: ["chartType", "data", "themeColors"],
  table: ["outline", "colWidths", "cellMinHeight", "data"],
  latex: ["latex"],
  video: ["autoplay"],
  audio: ["fixedRatio", "color", "loop", "autoplay", "src"],
  code: ["language", "lines"],
};

function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function tupleOfFiniteNumbers(value: unknown, length: number): boolean {
  return Array.isArray(value) && value.length === length && value.every(finite);
}

function requireElement(elements: readonly PPTElement[], id: string): number {
  if (typeof id !== "string" || id.trim().length === 0) {
    throw new ClassroomEditError("INVALID_EDIT_INTENT", "element id must be non-empty");
  }
  const index = elements.findIndex(element => element.id === id);
  if (index < 0) {
    throw new ClassroomEditError("UNKNOWN_ELEMENT", `element ${JSON.stringify(id)} does not exist`);
  }
  return index;
}

function assertUniqueIds(ids: readonly string[], label: string): void {
  if (ids.length === 0) {
    throw new ClassroomEditError("INVALID_EDIT_INTENT", `${label} cannot be empty`);
  }
  if (new Set(ids).size !== ids.length) {
    throw new ClassroomEditError("INVALID_EDIT_INTENT", `${label} cannot contain duplicate ids`);
  }
}

function assertRequiredFields(element: Record<string, unknown>): void {
  const type = element.type;
  if (typeof type !== "string" || !ELEMENT_TYPES.has(type)) {
    throw new ClassroomEditError("INVALID_ELEMENT", `unknown element type ${JSON.stringify(type)}`);
  }
  const required = [
    ...BASE_REQUIRED,
    ...(type === "line" ? [] : BOX_REQUIRED),
    ...TYPE_REQUIRED[type],
  ];
  for (const field of required) {
    if (element[field] === undefined || element[field] === null) {
      throw new ClassroomEditError("INVALID_ELEMENT", `${type} element requires ${field}`);
    }
  }
}

function assertElementTypeFields(element: Record<string, unknown>): void {
  const type = element.type;
  if (type === "text") {
    if (
      typeof element.content !== "string" ||
      typeof element.defaultFontName !== "string" ||
      typeof element.defaultColor !== "string"
    ) {
      throw new ClassroomEditError("INVALID_ELEMENT", "text fields must be strings");
    }
  } else if (type === "image") {
    if (typeof element.fixedRatio !== "boolean" || typeof element.src !== "string") {
      throw new ClassroomEditError("INVALID_ELEMENT", "image fields have invalid types");
    }
  } else if (type === "shape") {
    if (
      !tupleOfFiniteNumbers(element.viewBox, 2) ||
      typeof element.path !== "string" ||
      typeof element.fixedRatio !== "boolean" ||
      typeof element.fill !== "string"
    ) {
      throw new ClassroomEditError("INVALID_ELEMENT", "shape fields have invalid types");
    }
  } else if (type === "line") {
    if (
      !tupleOfFiniteNumbers(element.start, 2) ||
      !tupleOfFiniteNumbers(element.end, 2) ||
      typeof element.style !== "string" ||
      typeof element.color !== "string" ||
      !Array.isArray(element.points) ||
      element.points.length !== 2
    ) {
      throw new ClassroomEditError("INVALID_ELEMENT", "line fields have invalid types");
    }
  } else if (type === "chart") {
    if (
      typeof element.chartType !== "string" ||
      record(element.data) === null ||
      !Array.isArray(element.themeColors)
    ) {
      throw new ClassroomEditError("INVALID_ELEMENT", "chart fields have invalid types");
    }
  } else if (type === "table") {
    if (
      record(element.outline) === null ||
      !Array.isArray(element.colWidths) ||
      !element.colWidths.every(finite) ||
      !finite(element.cellMinHeight) ||
      !Array.isArray(element.data)
    ) {
      throw new ClassroomEditError("INVALID_ELEMENT", "table fields have invalid types");
    }
  } else if (type === "latex") {
    if (typeof element.latex !== "string") {
      throw new ClassroomEditError("INVALID_ELEMENT", "latex must be a string");
    }
  } else if (type === "video") {
    if (
      typeof element.autoplay !== "boolean" ||
      (typeof element.src !== "string" && typeof element.mediaRef !== "string")
    ) {
      throw new ClassroomEditError("INVALID_ELEMENT", "video requires autoplay and a media reference");
    }
  } else if (type === "audio") {
    if (
      typeof element.fixedRatio !== "boolean" ||
      typeof element.color !== "string" ||
      typeof element.loop !== "boolean" ||
      typeof element.autoplay !== "boolean" ||
      typeof element.src !== "string"
    ) {
      throw new ClassroomEditError("INVALID_ELEMENT", "audio fields have invalid types");
    }
  } else if (type === "code") {
    if (typeof element.language !== "string" || !Array.isArray(element.lines)) {
      throw new ClassroomEditError("INVALID_ELEMENT", "code fields have invalid types");
    }
  }
}

function assertElementWithinSlide(element: PPTElement, slide: Slide): void {
  const candidate = element as unknown as Record<string, unknown>;
  assertRequiredFields(candidate);
  assertElementTypeFields(candidate);
  if (typeof element.id !== "string" || element.id.trim().length === 0) {
    throw new ClassroomEditError("INVALID_ELEMENT", "element id must be non-empty");
  }
  if (!finite(element.left) || !finite(element.top) || !finite(element.width)) {
    throw new ClassroomEditError("INVALID_ELEMENT", `element ${element.id} has non-finite geometry`);
  }
  const canvasWidth = slide.viewportSize;
  const canvasHeight = slide.viewportSize / slide.viewportRatio;
  const height = element.type === "line" ? 0 : element.height;
  const minimumWidth = element.type === "line" ? 0 : Number.EPSILON;
  const minimumHeight = element.type === "line" ? 0 : Number.EPSILON;
  if (
    !finite(height) ||
    element.left < 0 ||
    element.top < 0 ||
    element.width < minimumWidth ||
    height < minimumHeight ||
    element.left + element.width > canvasWidth ||
    element.top + height > canvasHeight
  ) {
    throw new ClassroomEditError(
      "ELEMENT_OUT_OF_BOUNDS",
      `element ${element.id} must stay inside ${canvasWidth}x${canvasHeight}`,
    );
  }
  if (element.type !== "line" && !finite(element.rotate)) {
    throw new ClassroomEditError("INVALID_ELEMENT", `element ${element.id} has an invalid rotation`);
  }
  if (element.type === "line") {
    for (const point of [element.start, element.end]) {
      if (
        point[0] < 0 ||
        point[1] < 0 ||
        point[0] > canvasWidth ||
        point[1] > canvasHeight
      ) {
        throw new ClassroomEditError("ELEMENT_OUT_OF_BOUNDS", `line ${element.id} endpoint is outside the slide`);
      }
    }
  }
}

function assertValidSlide(slide: Slide): void {
  if (
    !finite(slide.viewportSize) ||
    slide.viewportSize <= 0 ||
    !finite(slide.viewportRatio) ||
    slide.viewportRatio <= 0 ||
    !Array.isArray(slide.elements)
  ) {
    throw new ClassroomEditError("INVALID_ELEMENT", "slide viewport and elements are required");
  }
  const ids = new Set<string>();
  for (const element of slide.elements) {
    assertElementWithinSlide(element, slide);
    if (ids.has(element.id)) {
      throw new ClassroomEditError("DUPLICATE_ELEMENT_ID", `duplicate element id ${element.id}`);
    }
    ids.add(element.id);
  }
}

function selectSlide(
  document: ClassroomDocument,
  sceneId?: string,
): { scene: SlideScene; slide: Slide } {
  const candidates = document.openmaic.scenes.filter(
    (scene): scene is SlideScene => scene.type === "slide" && (!sceneId || scene.id === sceneId),
  );
  if (candidates.length !== 1) {
    throw new ClassroomEditError(
      "UNKNOWN_SLIDE",
      sceneId
        ? `slide scene ${JSON.stringify(sceneId)} does not exist`
        : "sceneId is required unless the document has exactly one slide",
    );
  }
  const canvas = cloneJson(candidates[0].content.canvas) as Record<string, unknown>;
  if (canvas.elements !== undefined && !Array.isArray(canvas.elements)) {
    throw new ClassroomEditError("INVALID_ELEMENT", "slide elements must be an array");
  }
  const slide = {
    ...canvas,
    id:
      typeof canvas.id === "string" && canvas.id.length > 0
        ? canvas.id
        : candidates[0].id,
    viewportSize:
      finite(canvas.viewportSize) && canvas.viewportSize > 0
        ? canvas.viewportSize
        : 1_000,
    viewportRatio:
      finite(canvas.viewportRatio) && canvas.viewportRatio > 0
        ? canvas.viewportRatio
        : 16 / 9,
    theme: record(canvas.theme) ?? mapClassroomTheme("snow"),
    elements: Array.isArray(canvas.elements) ? canvas.elements : [],
  } as unknown as Slide;
  assertValidSlide(slide);
  return { scene: candidates[0], slide };
}

export function classroomSlideForEditing(
  document: ClassroomDocument,
  sceneId: string,
  theme: ClassroomThemeId = "snow",
): Slide {
  const slide = selectSlide(document, sceneId).slide;
  return { ...slide, theme: mapClassroomTheme(theme) };
}

function assertMutableProps(props: unknown): Record<string, unknown> {
  const value = record(props);
  if (!value) {
    throw new ClassroomEditError("INVALID_EDIT_INTENT", "element props must be an object");
  }
  for (const field of ["id", "type"]) {
    if (Object.prototype.hasOwnProperty.call(value, field)) {
      throw new ClassroomEditError("IMMUTABLE_ELEMENT_FIELD", `${field} cannot be changed`);
    }
  }
  return cloneJson(value);
}

function updateElement(
  elements: PPTElement[],
  id: string,
  props: unknown,
  slide: Slide,
): void {
  const index = requireElement(elements, id);
  const next = { ...elements[index], ...assertMutableProps(props) } as PPTElement;
  assertElementWithinSlide(next, slide);
  elements[index] = next;
}

function elementHeight(element: PPTElement): number {
  return element.type === "line" ? 0 : element.height;
}

function alignElements(
  elements: PPTElement[],
  ids: readonly string[],
  command: ClassroomAlignCommand,
  slide: Slide,
): void {
  assertUniqueIds(ids, "alignment ids");
  const indexes = ids.map(id => requireElement(elements, id));
  const selected = indexes.map(index => elements[index]);
  const left = Math.min(...selected.map(element => element.left));
  const right = Math.max(...selected.map(element => element.left + element.width));
  const top = Math.min(...selected.map(element => element.top));
  const bottom = Math.max(...selected.map(element => element.top + elementHeight(element)));
  indexes.forEach(index => {
    const element = elements[index];
    let nextLeft = element.left;
    let nextTop = element.top;
    if (command === "left") nextLeft = left;
    if (command === "center") nextLeft = (left + right - element.width) / 2;
    if (command === "right") nextLeft = right - element.width;
    if (command === "top") nextTop = top;
    if (command === "middle") nextTop = (top + bottom - elementHeight(element)) / 2;
    if (command === "bottom") nextTop = bottom - elementHeight(element);
    const next = { ...element, left: nextLeft, top: nextTop } as PPTElement;
    assertElementWithinSlide(next, slide);
    elements[index] = next;
  });
}

function requiredFieldsFor(element: PPTElement): Set<string> {
  return new Set([
    ...BASE_REQUIRED,
    ...(element.type === "line" ? [] : BOX_REQUIRED),
    ...TYPE_REQUIRED[element.type],
  ]);
}

function actionElementId(action: JsonObject): string | null {
  if (typeof action.elementId === "string") return action.elementId;
  const payload = record(action.payload);
  return payload && typeof payload.elementId === "string" ? payload.elementId : null;
}

function applyIntent(
  scene: SlideScene,
  slide: Slide,
  intent: ClassroomEditIntent,
): void {
  if (!record(intent) || typeof intent.type !== "string") {
    throw new ClassroomEditError("INVALID_EDIT_INTENT", "edit intent must be an object with a type");
  }
  const elements = slide.elements;
  if (intent.type === "element.update") {
    updateElement(elements, intent.id, intent.props, slide);
    return;
  }
  if (intent.type === "element.updateMany") {
    if (!Array.isArray(intent.updates)) {
      throw new ClassroomEditError("INVALID_EDIT_INTENT", "updates must be an array");
    }
    if (
      !intent.updates.every(
        update =>
          record(update) !== null &&
          typeof update.id === "string" &&
          record(update.props) !== null,
      )
    ) {
      throw new ClassroomEditError("INVALID_EDIT_INTENT", "each update requires an id and props");
    }
    assertUniqueIds(intent.updates.map(update => update.id), "update ids");
    intent.updates.forEach(update => updateElement(elements, update.id, update.props, slide));
    return;
  }
  if (intent.type === "element.add") {
    const index = intent.index ?? elements.length;
    if (!Number.isInteger(index) || index < 0 || index > elements.length) {
      throw new ClassroomEditError("INVALID_EDIT_INTENT", "element insertion index is out of bounds");
    }
    if (!record(intent.element)) {
      throw new ClassroomEditError("INVALID_ELEMENT", "added element must be an object");
    }
    const element = cloneJson(intent.element);
    assertElementWithinSlide(element, slide);
    if (elements.some(candidate => candidate.id === element.id)) {
      throw new ClassroomEditError("DUPLICATE_ELEMENT_ID", `duplicate element id ${element.id}`);
    }
    elements.splice(index, 0, element);
    return;
  }
  if (intent.type === "element.delete") {
    if (!Array.isArray(intent.ids)) {
      throw new ClassroomEditError("INVALID_EDIT_INTENT", "delete ids must be an array");
    }
    assertUniqueIds(intent.ids, "delete ids");
    intent.ids.forEach(id => requireElement(elements, id));
    const referenced = scene.actions.find(action => {
      const elementId = actionElementId(action);
      return elementId !== null && intent.ids.includes(elementId);
    });
    if (referenced) {
      throw new ClassroomEditError("ELEMENT_IN_USE", "an action still references the deleted element");
    }
    slide.elements = elements.filter(element => !intent.ids.includes(element.id));
    if (slide.animations) {
      slide.animations = slide.animations.filter(animation => !intent.ids.includes(animation.elId));
    }
    return;
  }
  if (intent.type === "element.reorder") {
    const index = requireElement(elements, intent.id);
    const commands = ["front", "back", "forward", "backward"];
    if (!commands.includes(intent.command)) {
      throw new ClassroomEditError("INVALID_EDIT_INTENT", "unknown reorder command");
    }
    let target = index;
    if (intent.command === "front") target = elements.length - 1;
    if (intent.command === "back") target = 0;
    if (intent.command === "forward") target = Math.min(elements.length - 1, index + 1);
    if (intent.command === "backward") target = Math.max(0, index - 1);
    if (target !== index) {
      const [element] = elements.splice(index, 1);
      elements.splice(target, 0, element);
    }
    return;
  }
  if (intent.type === "element.align") {
    if (!Array.isArray(intent.ids)) {
      throw new ClassroomEditError("INVALID_EDIT_INTENT", "alignment ids must be an array");
    }
    if (!["left", "center", "right", "top", "middle", "bottom"].includes(intent.command)) {
      throw new ClassroomEditError("INVALID_EDIT_INTENT", "unknown alignment command");
    }
    alignElements(elements, intent.ids, intent.command, slide);
    return;
  }
  if (intent.type === "element.removeProps") {
    if (!Array.isArray(intent.props) || !intent.props.every(property => typeof property === "string")) {
      throw new ClassroomEditError("INVALID_EDIT_INTENT", "property names must be strings");
    }
    assertUniqueIds(intent.props, "property names");
    const index = requireElement(elements, intent.id);
    const element = elements[index];
    const required = requiredFieldsFor(element);
    const next = { ...element } as unknown as Record<string, unknown>;
    for (const property of intent.props) {
      if (required.has(property)) {
        throw new ClassroomEditError("IMMUTABLE_ELEMENT_FIELD", `${property} is required by ${element.type}`);
      }
      if (!Object.prototype.hasOwnProperty.call(next, property)) {
        throw new ClassroomEditError("INVALID_EDIT_INTENT", `${property} does not exist on ${element.id}`);
      }
      delete next[property];
    }
    assertElementWithinSlide(next as unknown as PPTElement, slide);
    elements[index] = next as unknown as PPTElement;
    return;
  }
  if (intent.type === "text.updateContent") {
    if (typeof intent.content !== "string") {
      throw new ClassroomEditError("INVALID_EDIT_INTENT", "text content must be a string");
    }
    const index = requireElement(elements, intent.id);
    const element = elements[index];
    if (intent.target === "text") {
      if (element.type !== "text") {
        throw new ClassroomEditError("INVALID_EDIT_INTENT", `${element.id} is not a text element`);
      }
      elements[index] = { ...element, content: intent.content };
      return;
    }
    if (intent.target === "shape") {
      if (element.type !== "shape" || !element.text) {
        throw new ClassroomEditError("INVALID_EDIT_INTENT", `${element.id} is not a shape with text`);
      }
      elements[index] = { ...element, text: { ...element.text, content: intent.content } };
      return;
    }
    throw new ClassroomEditError("INVALID_EDIT_INTENT", "unknown text edit target");
  }
  const exhaustive: never = intent;
  throw new ClassroomEditError(
    "INVALID_EDIT_INTENT",
    `unsupported edit intent ${(exhaustive as { type?: unknown }).type as string}`,
  );
}

export function applyEditIntents(
  input: ClassroomDocument,
  intents: readonly ClassroomEditIntent[],
  sceneId?: string,
): ClassroomDocument {
  if (!Array.isArray(intents) || intents.length === 0) {
    throw new ClassroomEditError("INVALID_EDIT_INTENT", "a completed gesture must contain an intent batch");
  }
  const document = cloneJson(input);
  const { scene, slide } = selectSlide(document, sceneId);
  intents.forEach(intent => applyIntent(scene, slide, intent));
  assertValidSlide(slide);
  scene.content = {
    type: "slide",
    canvas: cloneJson(slide) as unknown as JsonObject,
  };
  return document;
}
