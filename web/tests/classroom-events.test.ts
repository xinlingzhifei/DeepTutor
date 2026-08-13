import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import {
  createClassroomEventTranslator,
  createClassroomEventDispatcher,
  createEventQueue,
  toLearningEvent,
  type LearningEvent,
  type LearningEventQueueStorage,
} from "../lib/classroom-events";
import type { ClassroomLearningEvent } from "../lib/openmaic-adapter/playback/events";
import { playbackStableId } from "../lib/openmaic-adapter/playback/types";
import {
  appendClassroomEvents,
  classroomSessionNeedsCompletionRecovery,
  completeClassroomLearningSession,
  createClassroomLearningSession,
  fetchClassroomLearningDocument,
  fetchClassroomLearningMedia,
  fetchClassLearningReport,
  getClassroomLearningSession,
  resolveStudentClassroomAuthority,
  restoreOrCreateClassroomLearningSession,
} from "../lib/learning-api";

const VERSION_ID = "version-a";
const OCCURRED_AT = "2026-08-13T01:02:03.000Z";

function runtimeEvent(
  type: ClassroomLearningEvent["type"],
  overrides: Partial<ClassroomLearningEvent> = {},
): ClassroomLearningEvent {
  return {
    eventId: `runtime-${type}`,
    type,
    classroomVersionId: VERSION_ID,
    occurredAt: OCCURRED_AT,
    ...overrides,
  };
}

function sceneCompleted(eventId = "scene-event"): LearningEvent {
  return {
    schema_version: "1.0",
    event_id: eventId,
    event_type: "scene.completed",
    occurred_at: OCCURRED_AT,
    scene_id: "scene-a",
    knowledge_point_id: "kp-a",
  };
}

function largeQuiz(eventId: string, fill: string): LearningEvent {
  return {
    schema_version: "1.0",
    event_id: eventId,
    event_type: "quiz.graded",
    occurred_at: OCCURRED_AT,
    scene_id: "scene-a",
    knowledge_point_id: "kp-a",
    assessment_id: "scene-a",
    question_id: "question-a",
    answer: { text: fill.repeat(140_000) },
  };
}

test("event payload excludes authoritative identity fields", async () => {
  const event = await toLearningEvent(
    runtimeEvent("scene.completed", { sceneId: "scene-a" }),
    [{ knowledgePointId: "kp-a", sceneIds: ["scene-a"] }],
    "session-a",
  );

  assert.ok(event);
  assert.equal("tenant_id" in event, false);
  assert.equal("user_id" in event, false);
  assert.equal("session_id" in event, false);
  assert.equal("classroom_version_id" in event, false);
});

test("translator emits exactly the six standard event types", async () => {
  const translator = createClassroomEventTranslator(
    [{ knowledgePointId: "kp-a", sceneIds: ["scene-a"] }],
    false,
    "session-a",
  );
  const translated = (await Promise.all([
    translator.translate(runtimeEvent("scene.entered", { sceneId: "scene-a" })),
    translator.translate(runtimeEvent("scene.entered", { sceneId: "scene-b" })),
    translator.translate(runtimeEvent("scene.completed", { sceneId: "scene-a" })),
    translator.translate(
      runtimeEvent("quiz.graded", {
        sceneId: "scene-a",
        interactionId: "question-a",
        payload: { answer: { optionIds: ["option-a"] } },
      }),
    ),
    translator.translate(
      runtimeEvent("hint.used", {
        sceneId: "scene-a",
        interactionId: "hint-a",
      }),
    ),
    translator.translate(
      runtimeEvent("pbl.milestone", {
        sceneId: "scene-a",
        interactionId: "milestone-a",
      }),
    ),
    translator.translate(runtimeEvent("classroom.completed")),
    translator.translate(runtimeEvent("action.completed", { sceneId: "scene-a" })),
    translator.translate(runtimeEvent("quiz.answered", { sceneId: "scene-a" })),
    translator.translate(runtimeEvent("interactive.event", { sceneId: "scene-a" })),
  ])).filter((event): event is LearningEvent => event !== null);

  assert.deepEqual(
    translated.map((event) => event.event_type),
    [
      "classroom.started",
      "scene.completed",
      "quiz.graded",
      "hint.used",
      "pbl.milestone_completed",
      "classroom.completed",
    ],
  );
  assert.equal(translated[2]?.event_type, "quiz.graded");
  if (translated[2]?.event_type !== "quiz.graded") throw new Error("quiz event missing");
  assert.equal(translated[2].event_id.length <= 128, true);
  assert.deepEqual(translated[2].answer, { optionIds: ["option-a"] });
});

test("quiz grading fails closed when the scene knowledge point is ambiguous", async () => {
  const event = runtimeEvent("quiz.graded", {
    sceneId: "scene-a",
    interactionId: "question-a",
    payload: { answer: { optionIds: ["option-a"] } },
  });

  await assert.rejects(
    () =>
      toLearningEvent(event, [
        { knowledgePointId: "kp-a", sceneIds: ["scene-a"] },
        { knowledgePointId: "kp-b", sceneIds: ["scene-a"] },
      ], "session-a"),
    /knowledge point binding is ambiguous/i,
  );
});

test("a failed first-scene translation remains retryable", async () => {
  const translator = createClassroomEventTranslator([], false, "session-a");

  await assert.rejects(
    translator.translate(runtimeEvent("scene.entered", { eventId: "", sceneId: "scene-a" })),
    /event ID is invalid/i,
  );
  const retried = await translator.translate(
    runtimeEvent("scene.entered", { eventId: "valid-start", sceneId: "scene-a" }),
  );

  assert.equal(retried?.event_type, "classroom.started");
});

test("knowledge point bindings must fit the server event contract", async () => {
  await assert.rejects(
    toLearningEvent(
      runtimeEvent("scene.completed", { sceneId: "scene-a" }),
      [{ knowledgePointId: "k".repeat(129), sceneIds: ["scene-a"] }],
      "session-a",
    ),
    /knowledge point ID is invalid/i,
  );
});

test("transport event ids are compact stable and scoped to one learning session", async () => {
  const runtimeId = playbackStableId(
    "event",
    playbackStableId(
      "checkpoint",
      VERSION_ID,
      "a".repeat(64),
      1,
      "scene-completed:scene-a",
    ),
    "scene.completed",
  );
  assert.equal(runtimeId.length > 128, true);
  const event = runtimeEvent("scene.completed", { eventId: runtimeId, sceneId: "scene-a" });
  const mappings = [{ knowledgePointId: "kp-a", sceneIds: ["scene-a"] }];

  const first = await toLearningEvent(event, mappings, "session-a");
  const retry = await toLearningEvent(event, mappings, "session-a");
  const otherLearner = await toLearningEvent(event, mappings, "session-b");

  assert.ok(first && retry && otherLearner);
  assert.equal(first.event_id.length <= 128, true);
  assert.equal(first.event_id, retry.event_id);
  assert.notEqual(first.event_id, otherLearner.event_id);
});

test("accepted duplicate and quarantined events all settle without retry", async () => {
  const diagnostics: string[] = [];
  const queue = createEventQueue(
    [sceneCompleted("accepted"), sceneCompleted("duplicate"), sceneCompleted("quarantined")],
    { onQuarantined: item => diagnostics.push(`${item.event_id}:${item.reason}`) },
  );

  await queue.flush(async events => {
    assert.deepEqual(
      events.map(event => event.event_id),
      ["accepted", "duplicate", "quarantined"],
    );
    return {
      accepted: [{ event_id: "accepted", seq: 1 }],
      duplicate: [{ event_id: "duplicate", seq: 2 }],
      quarantined: [{ event_id: "quarantined", reason: "scene_not_in_version" }],
    };
  });

  assert.equal(queue.size, 0);
  assert.deepEqual(diagnostics, ["quarantined:scene_not_in_version"]);
});

test("network failure and unacknowledged events remain queued for recovery", async () => {
  let stored: readonly LearningEvent[] = [];
  const storage: LearningEventQueueStorage = {
    load: () => stored,
    save: events => {
      stored = structuredClone(events);
    },
  };
  const queue = createEventQueue([sceneCompleted("event-a"), sceneCompleted("event-b")], {
    storage,
  });

  await assert.rejects(queue.flush(async () => Promise.reject(new Error("offline"))), /offline/);
  assert.equal(queue.size, 2);
  assert.deepEqual(stored.map(event => event.event_id), ["event-a", "event-b"]);

  await queue.flush(async () => ({
    accepted: [{ event_id: "event-a", seq: 1 }],
    duplicate: [],
    quarantined: [],
  }));
  assert.equal(queue.size, 1);

  const restored = createEventQueue([], { storage });
  assert.equal(restored.size, 1);
  assert.equal(restored.pending()[0]?.event_id, "event-b");
});

test("offline recovery drops locally corrupted or authority-bearing events", () => {
  let stored: readonly LearningEvent[] = [
    {
      ...sceneCompleted("corrupted"),
      tenant_id: "forged-tenant",
    } as never,
    sceneCompleted("valid"),
  ];
  const storage: LearningEventQueueStorage = {
    load: () => stored,
    save: events => {
      stored = structuredClone(events);
    },
  };

  const restored = createEventQueue([], { storage });

  assert.deepEqual(restored.pending().map(event => event.event_id), ["valid"]);
  assert.deepEqual(stored.map(event => event.event_id), ["valid"]);
});

test("queue is bounded to one server batch", () => {
  const queue = createEventQueue(
    Array.from({ length: 100 }, (_, index) => sceneCompleted(`event-${index}`)),
  );

  assert.equal(queue.size, 100);
  assert.equal(queue.enqueue(sceneCompleted("overflow")), false);
  assert.equal(queue.size, 100);
});

test("a failed queue persistence rolls back the in-memory enqueue", () => {
  let saves = 0;
  let persisted: readonly LearningEvent[] = [];
  const storage: LearningEventQueueStorage = {
    load: () => [],
    save: events => {
      saves += 1;
      if (saves === 2) throw new Error("storage quota exceeded");
      persisted = [...events];
    },
  };
  const queue = createEventQueue([], { storage });
  const event = sceneCompleted("persist-retry");

  assert.throws(() => queue.enqueue(event), /storage quota exceeded/);
  assert.equal(queue.size, 0);
  assert.deepEqual(queue.pending(), []);

  assert.equal(queue.enqueue(event), true);
  assert.equal(queue.size, 1);
  assert.deepEqual(persisted.map(item => item.event_id), ["persist-retry"]);
});

test("dispatcher flushes at 100 events and schedules the 15 second boundary", async () => {
  const scheduled: Array<{ delay: number; run: () => Promise<void> }> = [];
  const batches: string[][] = [];
  const dispatcher = createClassroomEventDispatcher({
    send: async events => {
      batches.push(events.map(event => event.event_id));
      return {
        accepted: events.map((event, index) => ({ event_id: event.event_id, seq: index + 1 })),
        duplicate: [],
        quarantined: [],
      };
    },
    schedule: (run, delay) => {
      scheduled.push({ delay, run });
      return scheduled.length;
    },
    cancelSchedule: () => undefined,
  });

  assert.equal(scheduled[0]?.delay, 15_000);
  await dispatcher.enqueue(
    Array.from({ length: 100 }, (_, index) => sceneCompleted(`full-${index}`)),
  );
  assert.equal(dispatcher.size, 0);
  assert.equal(batches.length, 1);
  assert.equal(batches[0]?.length, 100);

  await dispatcher.enqueue([sceneCompleted("timer-event")]);
  await scheduled.at(-1)?.run();
  assert.deepEqual(batches.at(-1), ["timer-event"]);
  assert.equal(dispatcher.size, 0);
  dispatcher.dispose();
});

test("dispatcher serializes overlapping flush requests", async () => {
  let release: (() => void) | undefined;
  let sends = 0;
  const dispatcher = createClassroomEventDispatcher({
    send: async events => {
      sends += 1;
      await new Promise<void>(resolve => {
        release = resolve;
      });
      return {
        accepted: events.map((event, index) => ({ event_id: event.event_id, seq: index + 1 })),
        duplicate: [],
        quarantined: [],
      };
    },
    schedule: () => 1,
    cancelSchedule: () => undefined,
  });
  await dispatcher.enqueue([sceneCompleted("single-flight")]);

  const first = dispatcher.flush();
  const second = dispatcher.flush();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(sends, 1);
  release?.();
  await Promise.all([first, second]);
  assert.equal(dispatcher.size, 0);
  dispatcher.dispose();
});

test("dispatcher reports pending-count changes after timer delivery", async () => {
  const scheduled: Array<() => Promise<void>> = [];
  const sizes: number[] = [];
  const dispatcher = createClassroomEventDispatcher({
    send: async events => ({
      accepted: events.map((event, index) => ({ event_id: event.event_id, seq: index + 1 })),
      duplicate: [],
      quarantined: [],
    }),
    schedule: run => {
      scheduled.push(run);
      return scheduled.length;
    },
    cancelSchedule: () => undefined,
    onChange: size => sizes.push(size),
  });

  await dispatcher.enqueue([sceneCompleted("timer-count")]);
  await scheduled[0]?.();

  assert.deepEqual(sizes, [0, 1, 0]);
  dispatcher.dispose();
});

test("checkpoint commit persists the cursor before a full batch is sent", async () => {
  const order: string[] = [];
  const dispatcher = createClassroomEventDispatcher({
    send: async events => {
      order.push("send");
      return {
        accepted: events.map((event, index) => ({ event_id: event.event_id, seq: index + 1 })),
        duplicate: [],
        quarantined: [],
      };
    },
    schedule: () => 1,
    cancelSchedule: () => undefined,
  });

  await dispatcher.commit(
    Array.from({ length: 100 }, (_, index) => sceneCompleted(`checkpoint-${index}`)),
    async () => {
      order.push("cursor");
    },
  );

  assert.deepEqual(order, ["cursor", "send"]);
  assert.equal(dispatcher.size, 0);
  dispatcher.dispose();
});

test("failed cursor persistence leaves staged events retryable and unsent", async () => {
  let sends = 0;
  const dispatcher = createClassroomEventDispatcher({
    send: async events => {
      sends += 1;
      return {
        accepted: events.map((event, index) => ({ event_id: event.event_id, seq: index + 1 })),
        duplicate: [],
        quarantined: [],
      };
    },
    schedule: () => 1,
    cancelSchedule: () => undefined,
  });

  await assert.rejects(
    dispatcher.commit([sceneCompleted("staged")], async () => {
      throw new Error("cursor failed");
    }),
    /cursor failed/,
  );
  assert.equal(dispatcher.size, 1);
  assert.equal(sends, 0);

  await dispatcher.commit([sceneCompleted("staged")], async () => undefined);
  await dispatcher.flush();
  assert.equal(sends, 1);
  assert.equal(dispatcher.size, 0);
  dispatcher.dispose();
});

test("checkpoint never partially stages when the pending queue lacks capacity", async () => {
  const batches: string[][] = [];
  const dispatcher = createClassroomEventDispatcher({
    send: async events => {
      batches.push(events.map(event => event.event_id));
      return {
        accepted: events.map((event, index) => ({ event_id: event.event_id, seq: index + 1 })),
        duplicate: [],
        quarantined: [],
      };
    },
    schedule: () => 1,
    cancelSchedule: () => undefined,
  });
  await dispatcher.commit(
    Array.from({ length: 99 }, (_, index) => sceneCompleted(`old-${index}`)),
    async () => undefined,
  );

  await dispatcher.commit(
    [sceneCompleted("new-a"), sceneCompleted("new-b")],
    async () => undefined,
  );

  assert.equal(batches.length, 1);
  assert.equal(batches[0]?.length, 99);
  assert.deepEqual(dispatcher.pending().map(event => event.event_id), ["new-a", "new-b"]);
  dispatcher.dispose();
});

test("dispatcher flushes before the JSON body exceeds the server byte limit", async () => {
  const batches: string[][] = [];
  let cursorWrites = 0;
  const dispatcher = createClassroomEventDispatcher({
    send: async events => {
      batches.push(events.map(event => event.event_id));
      return {
        accepted: events.map((event, index) => ({ event_id: event.event_id, seq: index + 1 })),
        duplicate: [],
        quarantined: [],
      };
    },
    schedule: () => 1,
    cancelSchedule: () => undefined,
  });

  await dispatcher.commit([largeQuiz("large-a", "a")], async () => {
    cursorWrites += 1;
  });
  await dispatcher.commit([largeQuiz("large-b", "b")], async () => {
    cursorWrites += 1;
  });

  assert.deepEqual(batches, [["large-a"]]);
  assert.deepEqual(dispatcher.pending().map(event => event.event_id), ["large-b"]);
  assert.equal(cursorWrites, 2);

  await dispatcher.flush();
  assert.deepEqual(dispatcher.pending(), []);

  let oversizedCursorWrite = false;
  await assert.rejects(
    dispatcher.commit(
      [largeQuiz("oversized", "x".repeat(2))],
      async () => {
        oversizedCursorWrite = true;
      },
    ),
    /transport limit/i,
  );
  assert.equal(oversizedCursorWrite, false);
  assert.deepEqual(dispatcher.pending().map(event => event.event_id), []);
  dispatcher.dispose();
});

test("one flush drains every recoverable byte-limited batch", async () => {
  const batches: string[][] = [];
  const restored = [largeQuiz("restored-a", "a"), largeQuiz("restored-b", "b")];
  const dispatcher = createClassroomEventDispatcher({
    storage: {
      load: () => restored,
      save: () => undefined,
    },
    send: async events => {
      batches.push(events.map(event => event.event_id));
      return {
        accepted: events.map((event, index) => ({ event_id: event.event_id, seq: index + 1 })),
        duplicate: [],
        quarantined: [],
      };
    },
    schedule: () => 1,
    cancelSchedule: () => undefined,
  });

  await dispatcher.flush();

  assert.deepEqual(batches, [["restored-a"], ["restored-b"]]);
  assert.equal(dispatcher.size, 0);
  dispatcher.dispose();
});

test("an unacknowledged dispatcher batch rejects instead of silently completing", async () => {
  const dispatcher = createClassroomEventDispatcher({
    send: async () => ({ accepted: [], duplicate: [], quarantined: [] }),
    schedule: () => 1,
    cancelSchedule: () => undefined,
  });
  await dispatcher.enqueue([sceneCompleted("unacknowledged")]);

  await assert.rejects(dispatcher.flush(), /did not settle/i);

  assert.equal(dispatcher.size, 1);
  dispatcher.dispose();
});

async function withFetch<T>(
  implementation: typeof fetch,
  run: () => Promise<T>,
): Promise<T> {
  const original = globalThis.fetch;
  globalThis.fetch = implementation;
  try {
    return await run();
  } finally {
    globalThis.fetch = original;
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sessionResponse() {
  return {
    id: "session-a",
    tenant_id: "tenant-a",
    user_id: "learner-a",
    classroom_version_id: VERSION_ID,
    assignment_id: null,
    student_asset_id: "asset-a",
    status: "active",
    last_cursor: null,
    started_at: OCCURRED_AT,
    completed_at: null,
  };
}

test("the server event-sequence sentinel is not treated as a playback cursor", async () => {
  const session = await withFetch(
    async () => jsonResponse({ ...sessionResponse(), last_cursor: { last_event_seq: 0 } }),
    () => getClassroomLearningSession("session-a"),
  );

  assert.equal(session.lastCursor, null);
});

test("session recovery replaces only an explicitly stale server pointer", async () => {
  const transientRequests: string[] = [];
  await assert.rejects(
    withFetch(
      async input => {
        transientRequests.push(String(input));
        return jsonResponse({ detail: "temporarily unavailable" }, 503);
      },
      () =>
        restoreOrCreateClassroomLearningSession(
          VERSION_ID,
          { studentAssetId: "asset-a" },
          "stored-session",
        ),
    ),
    /503/,
  );
  assert.deepEqual(transientRequests, ["/api/v1/classroom-sessions/stored-session"]);

  const staleRequests: Array<[string, string | undefined]> = [];
  const restored = await withFetch(
    async (input, init) => {
      staleRequests.push([String(input), init?.method]);
      if (String(input).endsWith("/stored-session")) {
        return jsonResponse({ detail: "access denied" }, 403);
      }
      return jsonResponse(sessionResponse(), 201);
    },
    () =>
      restoreOrCreateClassroomLearningSession(
        VERSION_ID,
        { studentAssetId: "asset-a" },
        "stored-session",
      ),
  );

  assert.equal(restored.id, "session-a");
  assert.deepEqual(staleRequests, [
    ["/api/v1/classroom-sessions/stored-session", undefined],
    ["/api/v1/classroom-sessions", "POST"],
  ]);

  const abandonedRequests: string[] = [];
  await withFetch(
    async input => {
      abandonedRequests.push(String(input));
      return abandonedRequests.length === 1
        ? jsonResponse({
            ...sessionResponse(),
            id: "abandoned-session",
            status: "abandoned",
            completed_at: OCCURRED_AT,
          })
        : jsonResponse(sessionResponse(), 201);
    },
    () =>
      restoreOrCreateClassroomLearningSession(
        VERSION_ID,
        { studentAssetId: "asset-a" },
        "abandoned-session",
      ),
  );
  assert.deepEqual(abandonedRequests, [
    "/api/v1/classroom-sessions/abandoned-session",
    "/api/v1/classroom-sessions",
  ]);
});

test("a terminal cursor on an active session requires completion recovery", () => {
  const active = {
    id: "session-a",
    classroomVersionId: VERSION_ID,
    assignmentId: null,
    studentAssetId: "asset-a",
    status: "active" as const,
    lastCursor: { sceneIndex: 3, actionIndex: 0 },
    startedAt: OCCURRED_AT,
    completedAt: null,
  };

  assert.equal(classroomSessionNeedsCompletionRecovery(active, 3), true);
  assert.equal(classroomSessionNeedsCompletionRecovery(active, 4), false);
  assert.equal(
    classroomSessionNeedsCompletionRecovery({ ...active, status: "completed" }, 3),
    false,
  );
});

test("completion response loss converges on the authoritative completed session", async () => {
  const requests: Array<[string, string | undefined]> = [];
  const completed = await withFetch(
    async (input, init) => {
      requests.push([String(input), init?.method]);
      if (init?.method === "POST") {
        return jsonResponse({ detail: "gateway response lost" }, 503);
      }
      return jsonResponse({
        ...sessionResponse(),
        status: "completed",
        completed_at: OCCURRED_AT,
      });
    },
    () => completeClassroomLearningSession("session-a"),
  );

  assert.equal(completed.status, "completed");
  assert.deepEqual(requests, [
    ["/api/v1/classroom-sessions/session-a/complete", "POST"],
    ["/api/v1/classroom-sessions/session-a", undefined],
  ]);
});

test("learning session creation sends only one non-authoritative authority reference", async () => {
  let body: unknown;
  const created = await withFetch(
    async (input, init) => {
      assert.equal(input, "/api/v1/classroom-sessions");
      assert.equal(init?.method, "POST");
      body = JSON.parse(String(init?.body));
      return jsonResponse(sessionResponse(), 201);
    },
    () => createClassroomLearningSession({ studentAssetId: "asset-a" }),
  );

  assert.deepEqual(body, { student_asset_id: "asset-a" });
  assert.equal(created.classroomVersionId, VERSION_ID);
  assert.equal("tenantId" in (body as object), false);
  assert.equal("userId" in (body as object), false);
  assert.equal("classroomVersionId" in (body as object), false);
  await assert.rejects(
    createClassroomLearningSession(
      { assignmentId: "assignment-a", studentAssetId: "asset-a" } as never,
    ),
    /exactly one authority/i,
  );
});

test("each learning event batch obtains one fresh write ticket", async () => {
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  let ticketNumber = 0;
  await withFetch(
    async (input, init) => {
      requests.push({ input: String(input), init });
      if (String(input).endsWith("/event-ticket")) {
        ticketNumber += 1;
        return jsonResponse({ ticket: `ticket-${ticketNumber}`, expires_in: 300 });
      }
      return jsonResponse({
        accepted: [{ event_id: `event-${ticketNumber}`, seq: ticketNumber }],
        duplicate: [],
        quarantined: [],
      }, 202);
    },
    async () => {
      await appendClassroomEvents("session-a", [sceneCompleted("event-1")]);
      await appendClassroomEvents("session-a", [sceneCompleted("event-2")]);
    },
  );

  assert.deepEqual(
    requests.map(request => [request.input, request.init?.method]),
    [
      ["/api/v1/classroom-sessions/session-a/event-ticket", "POST"],
      ["/api/v1/classroom-sessions/session-a/events", "POST"],
      ["/api/v1/classroom-sessions/session-a/event-ticket", "POST"],
      ["/api/v1/classroom-sessions/session-a/events", "POST"],
    ],
  );
  assert.equal(
    (requests[1]?.init?.headers as Record<string, string>)["X-Classroom-Ticket"],
    "ticket-1",
  );
  assert.equal(
    (requests[3]?.init?.headers as Record<string, string>)["X-Classroom-Ticket"],
    "ticket-2",
  );
});

test("student document and media reads use exact scoped tickets", async () => {
  const requests: Array<{ input: string; init?: RequestInit }> = [];
  await withFetch(
    async (input, init) => {
      requests.push({ input: String(input), init });
      if (String(input).endsWith("/read-ticket")) {
        return jsonResponse({ ticket: `read-${requests.length}`, expires_in: 60 });
      }
      if (String(input).endsWith("/document")) return jsonResponse({ schemaVersion: "1.0" });
      return new Response(new Blob(["media"], { type: "image/png" }), { status: 200 });
    },
    async () => {
      await fetchClassroomLearningDocument("session-a", VERSION_ID);
      await fetchClassroomLearningMedia("session-a", VERSION_ID, "media-a");
    },
  );

  assert.deepEqual(JSON.parse(String(requests[0]?.init?.body)), {
    action: "classroom.document.read",
    resource_id: VERSION_ID,
  });
  assert.equal(requests[1]?.input, `/api/v1/classroom-versions/${VERSION_ID}/document`);
  assert.deepEqual(JSON.parse(String(requests[2]?.init?.body)), {
    action: "classroom.media.read",
    resource_id: "media-a",
  });
  assert.equal(
    requests[3]?.input,
    `/api/v1/classroom-versions/${VERSION_ID}/media/media-a`,
  );
});

test("student authority is resolved from the owner-scoped classroom list", async () => {
  await withFetch(
    async input => {
      assert.equal(input, "/api/v1/student-classrooms");
      return jsonResponse({
        items: [
          {
            assetId: "asset-other",
            requestId: "request-other",
            approvalId: null,
            generationJobId: "job-other",
            status: "succeeded",
            courseId: "course-a",
            classId: "class-a",
            mode: "micro",
            ownerId: "learner-a",
            revision: 1,
            outline: null,
            classroomVersionId: "version-other",
          },
          {
            assetId: "asset-a",
            requestId: "request-a",
            approvalId: null,
            generationJobId: "job-a",
            status: "succeeded",
            courseId: "course-a",
            classId: "class-a",
            mode: "micro",
            ownerId: "learner-a",
            revision: 1,
            outline: null,
            classroomVersionId: VERSION_ID,
          },
        ],
      });
    },
    async () => {
      assert.deepEqual(await resolveStudentClassroomAuthority(VERSION_ID), {
        studentAssetId: "asset-a",
      });
      assert.equal(await resolveStudentClassroomAuthority("version-missing"), null);
    },
  );
});

test("teacher reports use only aggregate endpoints and parse mastery", async () => {
  let seenInput: RequestInfo | URL | undefined;
  const report = await withFetch(
    async input => {
      seenInput = input;
      return jsonResponse({
        classId: "class-a",
        sessionCount: 2,
        completedCount: 1,
        completionRate: 0.5,
        completedSceneCount: 4,
        validQuizCount: 3,
        correctQuizCount: 2,
        hintCount: 1,
        pblMilestoneCount: 1,
        mastery: [{ knowledgePointId: "kp-a", level: 0.75, evidenceCount: 2 }],
        projectionLagSeconds: 1.25,
      });
    },
    () => fetchClassLearningReport("class-a"),
  );

  assert.equal(seenInput, "/api/v1/teaching-reports/classes/class-a");
  assert.equal(report.mastery[0]?.knowledgePointId, "kp-a");
  assert.equal(JSON.stringify(report).includes("payload"), false);
  assert.equal(JSON.stringify(report).includes("events"), false);
});

test("teacher reports reject impossible aggregate values", async () => {
  await assert.rejects(
    withFetch(
      async () =>
        jsonResponse({
          classId: "class-a",
          sessionCount: -1,
          completedCount: 2.5,
          completionRate: 1.1,
          completedSceneCount: 1,
          validQuizCount: 1,
          correctQuizCount: 1,
          hintCount: 0,
          pblMilestoneCount: 0,
          mastery: [{ knowledgePointId: "kp-a", level: 2, evidenceCount: -1 }],
          projectionLagSeconds: -1,
        }),
      () => fetchClassLearningReport("class-a"),
    ),
    /learning report .* is invalid/i,
  );
});

test("player keeps authoritative answers and emits hint events", () => {
  const player = readFileSync("components/classroom/ClassroomPlayer.tsx", "utf8");
  const quiz = readFileSync("components/classroom/QuizScene.tsx", "utf8");

  assert.match(player, /answer:\s*pending\.answer/);
  assert.match(player, /learningEvent\(\s*['"]hint\.used['"]/);
  assert.match(quiz, /onHint/);
  assert.match(player, /readOnly\?: boolean/);
  assert.match(player, /controlsDisabled[\s\S]{0,160}readOnly/);
});

test("learning and report pages use scoped clients without raw event downloads", () => {
  const learningPage = readFileSync(
    "app/(workspace)/learn/classrooms/[versionId]/page.tsx",
    "utf8",
  );
  const learningClient = readFileSync(
    "components/classroom/ClassroomLearningClient.tsx",
    "utf8",
  );
  const reportPage = readFileSync("app/(utility)/teaching/reports/page.tsx", "utf8");

  assert.match(learningPage, /ClassroomLearningClient/);
  assert.match(learningClient, /restoreOrCreateClassroomLearningSession/);
  assert.match(learningClient, /fetchClassroomLearningDocument/);
  assert.match(learningClient, /LearningProgressPanel/);
  assert.match(learningClient, /readOnly=\{session\.status !== ['"]active['"]\}/);
  assert.match(learningClient, /classroomSessionNeedsCompletionRecovery/);
  assert.match(
    learningClient,
    /classroomSessionNeedsCompletionRecovery[\s\S]{0,220}queue\.flush\(\)[\s\S]{0,220}completeClassroomLearningSession/,
  );
  assert.doesNotMatch(learningClient, /tenantId|userId/);
  assert.match(reportPage, /fetchClassLearningReport/);
  assert.doesNotMatch(reportPage, /classroom-sessions\/.+\/events|teaching-reports\/quarantine/);
});

test("learning page discards stale loads and releases their media resources", () => {
  const learningClient = readFileSync(
    "components/classroom/ClassroomLearningClient.tsx",
    "utf8",
  );

  assert.match(learningClient, /loadGenerationRef\s*=\s*useRef\(0\)/);
  assert.match(learningClient, /generation\s*=\s*\+\+loadGenerationRef\.current/);
  assert.match(learningClient, /generation\s*!==\s*loadGenerationRef\.current/);
  assert.match(learningClient, /discardCandidate[\s\S]{0,300}URL\.revokeObjectURL/);
  assert.match(
    learningClient,
    /onQuarantined:[\s\S]{0,180}generation === loadGenerationRef\.current/,
  );
  assert.match(
    learningClient,
    /onChange:[\s\S]{0,120}generation === loadGenerationRef\.current/,
  );
  assert.match(
    learningClient,
    /return\s*\(\)\s*=>\s*\{\s*loadGenerationRef\.current\s*\+=\s*1/,
  );
  assert.match(
    learningClient,
    /completeClassroomLearningSession\(restored\.id\)[\s\S]{0,160}generation !== loadGenerationRef\.current/,
  );
});

test("checkpoint commits stay bound to the session and translator that started them", () => {
  const learningClient = readFileSync(
    "components/classroom/ClassroomLearningClient.tsx",
    "utf8",
  );

  assert.match(learningClient, /const activeSession = sessionRef\.current/);
  assert.match(learningClient, /const activeTranslator = translatorRef\.current/);
  assert.match(learningClient, /updateClassroomLearningCursor\(\s*activeSession\.id/);
  assert.match(
    learningClient,
    /sessionRef\.current\?\.id === activeSession\.id[\s\S]{0,100}setSession/,
  );
  assert.match(learningClient, /completeClassroomLearningSession\(\s*activeSession\.id/);
});

test("teacher report evidence labels are localized", () => {
  const reportPage = readFileSync("app/(utility)/teaching/reports/page.tsx", "utf8");

  assert.match(reportPage, /teaching\.reports\.evidenceCount/);
  assert.doesNotMatch(reportPage, />%\s*[\u3400-\u9fff]/);
});
