import type { ClassroomLearningEvent } from "./openmaic-adapter/playback/events";

type JsonPrimitive = boolean | number | string | null;
const MAX_EVENT_BATCH_COUNT = 100;
const MAX_EVENT_BATCH_BYTES = 256 * 1024;
export type JsonValue =
  | JsonPrimitive
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue };

interface LearningEventBase {
  schema_version: "1.0";
  event_id: string;
  occurred_at: string;
}

export type LearningEvent =
  | (LearningEventBase & { event_type: "classroom.started" })
  | (LearningEventBase & {
      event_type: "scene.completed";
      scene_id: string;
      knowledge_point_id?: string;
    })
  | (LearningEventBase & {
      event_type: "quiz.graded";
      scene_id: string;
      knowledge_point_id: string;
      assessment_id: string;
      question_id: string;
      answer: JsonValue;
    })
  | (LearningEventBase & {
      event_type: "hint.used";
      scene_id: string;
      knowledge_point_id?: string;
      hint_id: string;
    })
  | (LearningEventBase & {
      event_type: "pbl.milestone_completed";
      scene_id: string;
      knowledge_point_id?: string;
      milestone_id: string;
    })
  | (LearningEventBase & { event_type: "classroom.completed" });

export interface ClassroomKnowledgePointMapping {
  knowledgePointId: string;
  sceneIds: readonly string[];
}

export interface EventAcceptedResult {
  event_id: string;
  seq: number;
}

export interface EventQuarantinedResult {
  event_id: string;
  reason: string;
}

export interface EventIngestionResult {
  accepted: readonly EventAcceptedResult[];
  duplicate: readonly EventAcceptedResult[];
  quarantined: readonly EventQuarantinedResult[];
}

export interface LearningEventQueueStorage {
  load(): readonly LearningEvent[];
  save(events: readonly LearningEvent[]): void;
}

export interface EventQueueOptions {
  storage?: LearningEventQueueStorage;
  onQuarantined?(item: EventQuarantinedResult): void;
  onChange?(size: number): void;
}

export interface ClassroomEventDispatcherOptions extends EventQueueOptions {
  send(events: readonly LearningEvent[]): Promise<EventIngestionResult>;
  schedule?(run: () => Promise<void>, delayMs: number): unknown;
  cancelSchedule?(handle: unknown): void;
  onError?(error: Error): void;
}

function required(value: string | undefined, label: string): string {
  if (
    !value?.trim() ||
    value.length > 128 ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new Error(`${label} is invalid`);
  }
  return value;
}

function jsonValue(value: unknown, label: string): JsonValue {
  if (
    value === null ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value)) ||
    typeof value === "string"
  ) {
    return value;
  }
  if (Array.isArray(value)) return value.map((item) => jsonValue(item, label));
  if (typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, jsonValue(item, label)]),
    );
  }
  throw new Error(`${label} must be JSON`);
}

function isJsonValue(value: unknown): value is JsonValue {
  if (
    value === null ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value)) ||
    typeof value === "string"
  ) {
    return true;
  }
  if (Array.isArray(value)) return value.every(isJsonValue);
  return (
    typeof value === "object" &&
    Object.values(value as Record<string, unknown>).every(isJsonValue)
  );
}

function storedLearningEvent(value: unknown): LearningEvent | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (
    item.schema_version !== "1.0" ||
    typeof item.event_id !== "string" ||
    !item.event_id ||
    item.event_id.length > 128 ||
    typeof item.occurred_at !== "string" ||
    !Number.isFinite(Date.parse(item.occurred_at)) ||
    !/(?:[zZ]|[+-]\d{2}:\d{2})$/.test(item.occurred_at) ||
    typeof item.event_type !== "string"
  ) {
    return null;
  }
  const optionalKnowledgePoint =
    item.knowledge_point_id === undefined ||
    (typeof item.knowledge_point_id === "string" && Boolean(item.knowledge_point_id));
  const exactKeys = (...allowed: string[]) =>
    Object.keys(item).every(key => allowed.includes(key));
  const baseKeys = ["schema_version", "event_id", "event_type", "occurred_at"];
  if (item.event_type === "classroom.started" || item.event_type === "classroom.completed") {
    return exactKeys(...baseKeys) ? (item as unknown as LearningEvent) : null;
  }
  if (
    item.event_type === "scene.completed" &&
    typeof item.scene_id === "string" &&
    Boolean(item.scene_id) &&
    item.scene_id.length <= 128 &&
    optionalKnowledgePoint &&
    exactKeys(...baseKeys, "scene_id", "knowledge_point_id")
  ) {
    return item as unknown as LearningEvent;
  }
  if (
    item.event_type === "quiz.graded" &&
    typeof item.scene_id === "string" &&
    Boolean(item.scene_id) &&
    item.scene_id.length <= 128 &&
    typeof item.knowledge_point_id === "string" &&
    Boolean(item.knowledge_point_id) &&
    item.knowledge_point_id.length <= 128 &&
    typeof item.assessment_id === "string" &&
    Boolean(item.assessment_id) &&
    item.assessment_id.length <= 128 &&
    typeof item.question_id === "string" &&
    Boolean(item.question_id) &&
    item.question_id.length <= 128 &&
    isJsonValue(item.answer) &&
    exactKeys(
      ...baseKeys,
      "scene_id",
      "knowledge_point_id",
      "assessment_id",
      "question_id",
      "answer",
    )
  ) {
    return item as unknown as LearningEvent;
  }
  const interaction =
    item.event_type === "hint.used"
      ? ["hint_id", item.hint_id]
      : item.event_type === "pbl.milestone_completed"
        ? ["milestone_id", item.milestone_id]
        : null;
  if (
    interaction &&
    typeof item.scene_id === "string" &&
    Boolean(item.scene_id) &&
    item.scene_id.length <= 128 &&
    optionalKnowledgePoint &&
    typeof interaction[1] === "string" &&
    Boolean(interaction[1]) &&
    (interaction[1] as string).length <= 128 &&
    exactKeys(...baseKeys, "scene_id", "knowledge_point_id", interaction[0] as string)
  ) {
    return item as unknown as LearningEvent;
  }
  return null;
}

function batchBytes(events: readonly LearningEvent[]): number {
  return new TextEncoder().encode(JSON.stringify({ events })).byteLength;
}

function transportableEvent(value: unknown): LearningEvent | null {
  const event = storedLearningEvent(value);
  return event && batchBytes([event]) <= MAX_EVENT_BATCH_BYTES ? event : null;
}

async function transportEventId(sessionId: string, runtimeEventId: string): Promise<string> {
  const session = required(sessionId, "learning session ID");
  const runtime = runtimeEventId.trim();
  if (!runtime || /[\u0000-\u001f\u007f]/.test(runtime)) {
    throw new Error("learning event ID is invalid");
  }
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new Error("secure classroom event hashing is unavailable");
  const input = new TextEncoder().encode(
    JSON.stringify(["yfeistai.classroom.event.v1", session, runtime]),
  );
  const digest = await subtle.digest("SHA-256", input);
  return `evt_${[...new Uint8Array(digest)]
    .map(value => value.toString(16).padStart(2, "0"))
    .join("")}`;
}

function knowledgePointIds(
  sceneId: string,
  mappings: readonly ClassroomKnowledgePointMapping[],
): string[] {
  return [
    ...new Set(
      mappings
        .filter((mapping) => mapping.sceneIds.includes(sceneId))
        .map((mapping) => mapping.knowledgePointId),
    ),
  ];
}

function optionalKnowledgePoint(
  sceneId: string,
  mappings: readonly ClassroomKnowledgePointMapping[],
): string | undefined {
  const ids = knowledgePointIds(sceneId, mappings);
  return ids.length === 1 ? required(ids[0], "knowledge point ID") : undefined;
}

function requiredKnowledgePoint(
  sceneId: string,
  mappings: readonly ClassroomKnowledgePointMapping[],
): string {
  const ids = knowledgePointIds(sceneId, mappings);
  if (ids.length !== 1) {
    throw new Error("quiz knowledge point binding is ambiguous");
  }
  return required(ids[0], "knowledge point ID");
}

async function base(
  event: ClassroomLearningEvent,
  sessionId: string,
): Promise<LearningEventBase> {
  if (
    !Number.isFinite(Date.parse(event.occurredAt)) ||
    !/(?:[zZ]|[+-]\d{2}:\d{2})$/.test(event.occurredAt)
  ) {
    throw new Error("learning event time is invalid");
  }
  return {
    schema_version: "1.0",
    event_id: await transportEventId(sessionId, event.eventId),
    occurred_at: event.occurredAt,
  };
}

export async function toLearningEvent(
  event: ClassroomLearningEvent,
  mappings: readonly ClassroomKnowledgePointMapping[],
  sessionId: string,
): Promise<LearningEvent | null> {
  const common = await base(event, sessionId);
  if (event.type === "scene.entered") {
    return { ...common, event_type: "classroom.started" };
  }
  if (event.type === "scene.completed") {
    const sceneId = required(event.sceneId, "scene ID");
    const knowledgePointId = optionalKnowledgePoint(sceneId, mappings);
    return {
      ...common,
      event_type: "scene.completed",
      scene_id: sceneId,
      ...(knowledgePointId ? { knowledge_point_id: knowledgePointId } : {}),
    };
  }
  if (event.type === "quiz.graded") {
    const sceneId = required(event.sceneId, "scene ID");
    const questionId = required(event.interactionId, "question ID");
    return {
      ...common,
      event_type: "quiz.graded",
      scene_id: sceneId,
      knowledge_point_id: requiredKnowledgePoint(sceneId, mappings),
      assessment_id: sceneId,
      question_id: questionId,
      answer: jsonValue(event.payload?.answer, "quiz answer"),
    };
  }
  if (event.type === "hint.used") {
    const sceneId = required(event.sceneId, "scene ID");
    const knowledgePointId = optionalKnowledgePoint(sceneId, mappings);
    return {
      ...common,
      event_type: "hint.used",
      scene_id: sceneId,
      hint_id: required(event.interactionId, "hint ID"),
      ...(knowledgePointId ? { knowledge_point_id: knowledgePointId } : {}),
    };
  }
  if (event.type === "pbl.milestone") {
    const sceneId = required(event.sceneId, "scene ID");
    const knowledgePointId = optionalKnowledgePoint(sceneId, mappings);
    return {
      ...common,
      event_type: "pbl.milestone_completed",
      scene_id: sceneId,
      milestone_id: required(event.interactionId, "milestone ID"),
      ...(knowledgePointId ? { knowledge_point_id: knowledgePointId } : {}),
    };
  }
  if (event.type === "classroom.completed") {
    return { ...common, event_type: "classroom.completed" };
  }
  return null;
}

export function createClassroomEventTranslator(
  mappings: readonly ClassroomKnowledgePointMapping[],
  started: boolean,
  sessionId: string,
) {
  let classroomStarted = started;
  let translationTail: Promise<void> = Promise.resolve();
  return {
    translate(event: ClassroomLearningEvent): Promise<LearningEvent | null> {
      const operation = translationTail.then(async () => {
        if (event.type === "scene.entered") {
          if (classroomStarted) return null;
          const translated = await toLearningEvent(event, mappings, sessionId);
          classroomStarted = true;
          return translated;
        }
        return toLearningEvent(event, mappings, sessionId);
      });
      translationTail = operation.then(
        () => undefined,
        () => undefined,
      );
      return operation;
    },
  };
}

export function createEventQueue(
  initial: readonly LearningEvent[] = [],
  options: EventQueueOptions = {},
) {
  const events: LearningEvent[] = [];
  const ids = new Set<string>();
  const restore = [...(options.storage?.load() ?? []), ...initial];
  for (const rawEvent of restore) {
    const event = transportableEvent(rawEvent);
    if (!event) continue;
    if (events.length === MAX_EVENT_BATCH_COUNT) break;
    if (ids.has(event.event_id)) continue;
    ids.add(event.event_id);
    events.push(event);
  }
  const persist = () => {
    options.storage?.save(events);
    options.onChange?.(events.length);
  };
  persist();

  return {
    get size(): number {
      return events.length;
    },
    enqueue(event: LearningEvent): boolean {
      if (!transportableEvent(event)) {
        throw new Error("learning event exceeds the transport limit");
      }
      if (ids.has(event.event_id)) return true;
      if (events.length >= MAX_EVENT_BATCH_COUNT) return false;
      ids.add(event.event_id);
      events.push(event);
      try {
        persist();
      } catch (reason) {
        events.pop();
        ids.delete(event.event_id);
        throw reason;
      }
      return true;
    },
    pending(): readonly LearningEvent[] {
      return [...events];
    },
    async flush(
      send: (batch: readonly LearningEvent[]) => Promise<EventIngestionResult>,
    ): Promise<number> {
      if (events.length === 0) return 0;
      const batch: LearningEvent[] = [];
      for (const event of events) {
        if (batchBytes([...batch, event]) > MAX_EVENT_BATCH_BYTES) break;
        batch.push(event);
      }
      if (batch.length === 0) throw new Error("learning event exceeds the transport limit");
      const response = await send(batch);
      const batchIds = new Set(batch.map(event => event.event_id));
      const outcomes = [
        ...response.accepted.map(item => item.event_id),
        ...response.duplicate.map(item => item.event_id),
        ...response.quarantined.map(item => item.event_id),
      ];
      if (
        outcomes.some(eventId => !batchIds.has(eventId)) ||
        new Set(outcomes).size !== outcomes.length
      ) {
        throw new Error("learning event response is invalid");
      }
      const settled = new Set(outcomes);
      for (const item of response.quarantined) {
        if (batchIds.has(item.event_id)) options.onQuarantined?.(item);
      }
      for (let index = events.length - 1; index >= 0; index -= 1) {
        const event = events[index];
        if (event && settled.has(event.event_id)) {
          ids.delete(event.event_id);
          events.splice(index, 1);
        }
      }
      persist();
      return settled.size;
    },
  };
}

export function createClassroomEventDispatcher(
  options: ClassroomEventDispatcherOptions,
) {
  const queue = createEventQueue([], options);
  const schedule =
    options.schedule ??
    ((run: () => Promise<void>, delayMs: number) =>
      globalThis.setTimeout(() => void run(), delayMs));
  const cancelSchedule =
    options.cancelSchedule ??
    ((handle: unknown) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>));
  let timer: unknown;
  let disposed = false;
  let activeFlush: Promise<void> | null = null;
  let activeCommit: Promise<void> | null = null;

  const scheduleNext = () => {
    if (disposed) return;
    if (timer !== undefined) cancelSchedule(timer);
    timer = schedule(async () => {
      timer = undefined;
      try {
        await flush();
      } catch (reason) {
        options.onError?.(
          reason instanceof Error ? reason : new Error("classroom event delivery failed"),
        );
      } finally {
        scheduleNext();
      }
    }, 15_000);
  };

  const runFlush = (): Promise<void> => {
    if (disposed || queue.size === 0) return Promise.resolve();
    if (activeFlush) return activeFlush;
    activeFlush = (async () => {
      while (!disposed && queue.size > 0) {
        const settled = await queue.flush(options.send);
        if (settled === 0) {
          throw new Error("classroom event delivery did not settle the pending batch");
        }
      }
    })().finally(() => {
      activeFlush = null;
    });
    return activeFlush;
  };

  const flush = (): Promise<void> =>
    activeCommit ? activeCommit.then(() => runFlush()) : runFlush();

  const commit = (
    events: readonly LearningEvent[],
    persistCursor: () => Promise<void>,
  ): Promise<void> => {
    const previous = activeCommit ?? Promise.resolve();
    const operation = previous.then(async () => {
      const newEvents = events.filter(
        event => !queue.pending().some(pending => pending.event_id === event.event_id),
      );
      if (batchBytes(newEvents) > MAX_EVENT_BATCH_BYTES) {
        throw new Error("learning event checkpoint exceeds the transport limit");
      }
      const pending = queue.pending();
      if (
        queue.size + newEvents.length > MAX_EVENT_BATCH_COUNT ||
        batchBytes([...pending, ...newEvents]) > MAX_EVENT_BATCH_BYTES
      ) {
        await runFlush();
      }
      if (
        queue.size + newEvents.length > MAX_EVENT_BATCH_COUNT ||
        batchBytes([...queue.pending(), ...newEvents]) > MAX_EVENT_BATCH_BYTES
      ) {
        throw new Error("classroom event queue is full");
      }
      for (const event of events) {
        if (!queue.enqueue(event)) {
          throw new Error("classroom event queue is full");
        }
      }
      await persistCursor();
      if (queue.size === MAX_EVENT_BATCH_COUNT) await runFlush();
    });
    const tracked = operation.finally(() => {
      if (activeCommit === tracked) activeCommit = null;
    });
    activeCommit = tracked;
    return tracked;
  };

  scheduleNext();
  return {
    get size(): number {
      return queue.size;
    },
    pending(): readonly LearningEvent[] {
      return queue.pending();
    },
    async enqueue(events: readonly LearningEvent[]): Promise<void> {
      await commit(events, async () => undefined);
    },
    commit,
    flush,
    dispose(): void {
      disposed = true;
      if (timer !== undefined) {
        cancelSchedule(timer);
        timer = undefined;
      }
    },
  };
}
