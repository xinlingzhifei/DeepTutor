export interface EditorHistory<T> {
  readonly past: readonly T[];
  readonly present: T;
  readonly future: readonly T[];
}

export function createHistory<T>(initial: T): EditorHistory<T> {
  return { past: [], present: initial, future: [] };
}

export function pushHistory<T>(
  history: EditorHistory<T>,
  next: T,
  limit = 100,
): EditorHistory<T> {
  if (!Number.isInteger(limit) || limit < 1) {
    throw new RangeError("history limit must be a positive integer");
  }
  if (Object.is(history.present, next)) return history;
  const past = [...history.past, history.present];
  return {
    past: past.slice(Math.max(0, past.length - limit)),
    present: next,
    future: [],
  };
}

export function undo<T>(history: EditorHistory<T>): EditorHistory<T> {
  if (history.past.length === 0) return history;
  const present = history.past.at(-1) as T;
  return {
    past: history.past.slice(0, -1),
    present,
    future: [history.present, ...history.future],
  };
}

export function redo<T>(history: EditorHistory<T>): EditorHistory<T> {
  if (history.future.length === 0) return history;
  const [present, ...future] = history.future;
  return {
    past: [...history.past, history.present],
    present,
    future,
  };
}

export function replacePresent<T>(
  history: EditorHistory<T>,
  present: T,
): EditorHistory<T> {
  return { past: [...history.past], present, future: [] };
}
