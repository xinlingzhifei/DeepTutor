import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

import {
  canConfirmStudentClassroomConfig,
  confirmStudentClassroomOutline,
  createStudentClassroomConfig,
  estimateStudentClassroom,
  extractStudentClassroomTaskFromEvents,
  getStudentClassroom,
  getStudentClassroomJob,
  getStudentClassroomVersionId,
  listStudentClassroomOptions,
  restoreStudentClassroomConfig,
  resolveStudentClassroomCardState,
  shouldPollStudentClassroom,
  StudentClassroomRequestError,
  studentClassroomApprovalState,
  studentClassroomEstimateIsReady,
  studentClassroomEstimateRequestKey,
  studentClassroomPollIntervalMs,
  studentClassroomPollRetryDelay,
  studentClassroomStatusKind,
  studentClassroomEffectiveKnowledgeBases,
  studentClassroomPlayRoute,
  studentClassroomRequiresOutline,
  toCapabilityConfig,
  updateStudentClassroomOutline,
  validateStudentClassroomConfig,
} from "../lib/student-classroom-config";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

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

function studentClassroomPayload(overrides: Record<string, unknown> = {}) {
  return {
    assetId: "asset-1",
    requestId: "request-1",
    approvalId: null,
    generationJobId: "job-1",
    status: "generating_outline",
    courseId: "course-a",
    classId: "class-a",
    mode: "full",
    ownerId: "student-a",
    revision: 2,
    outline: null,
    classroomVersionId: null,
    ...overrides,
  };
}

function classroomJobPayload(overrides: Record<string, unknown> = {}) {
  return {
    job_id: "job-1",
    job_kind: "generation",
    phase: "outline",
    status: "generating_outline",
    progress_percent: 35,
    waiting_reason: null,
    cancellable: true,
    retryable: false,
    outline: null,
    error_category: null,
    error_code: null,
    retry_of_job_id: null,
    export_format: null,
    download_ready: false,
    ...overrides,
  };
}

function interactiveResultMetadata(
  classroomOverrides: Record<string, unknown> = {},
  estimateOverrides: Record<string, unknown> = {},
) {
  return {
    response: "Queued",
    estimate: {
      scene_range: [1, 5],
      duration_minutes_range: [3, 25],
      quota_units: 5,
      requires_outline_confirmation: false,
      requires_approval: false,
      ...estimateOverrides,
    },
    approval_id: null,
    job_id: "job-1",
    outline: null,
    classroom: {
      asset_id: "asset-1",
      request_id: "request-1",
      approval_id: null,
      generation_job_id: "job-1",
      status: "queued",
      course_id: "course-a",
      class_id: "class-a",
      mode: "micro",
      owner_id: "student-a",
      revision: 1,
      outline: null,
      classroom_version_id: null,
      ...classroomOverrides,
    },
    metadata: { cost_summary: {} },
  };
}

test("student must choose micro or full before submit", () => {
  const result = validateStudentClassroomConfig({
    courseId: "course-a",
    mode: null,
  });

  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.error, "classroom_mode_required");
});

test("student classroom config starts without choosing a mode", () => {
  assert.deepEqual(createStudentClassroomConfig(), {
    courseId: "",
    mode: null,
    contentMode: "source_grounded",
  });
});

test("stored classroom config fails closed to source-grounded mode", () => {
  assert.deepEqual(
    restoreStudentClassroomConfig({
      courseId: " course-a ",
      mode: "full",
      contentMode: "open_creation",
    }),
    {
      courseId: "course-a",
      mode: "full",
      contentMode: "source_grounded",
    },
  );
  assert.deepEqual(
    restoreStudentClassroomConfig({ courseId: 7, mode: "micro" }),
    createStudentClassroomConfig(),
  );
});

test("source-grounded confirmation requires a course, explicit mode, and KB", () => {
  const option = {
    courseId: "course-a",
    title: "Course A",
    allowedModes: ["micro"] as const,
    allowedContentModes: ["source_grounded", "open_creation"] as const,
  };
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: null, contentMode: "source_grounded" },
      { authorizedSourceCount: 1, option, estimateReady: true },
    ),
    false,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "micro", contentMode: "source_grounded" },
      { authorizedSourceCount: 0, option, estimateReady: true },
    ),
    false,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "micro", contentMode: "source_grounded" },
      { authorizedSourceCount: 1, option, estimateReady: true },
    ),
    true,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "micro", contentMode: "open_creation" },
      {
        authorizedSourceCount: 0,
        option: { ...option, allowedContentModes: ["source_grounded"] },
        estimateReady: true,
      },
    ),
    false,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "micro", contentMode: "open_creation" },
      { authorizedSourceCount: 0, option, estimateReady: true },
    ),
    true,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "full", contentMode: "open_creation" },
      { authorizedSourceCount: 0, option, estimateReady: true },
    ),
    false,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "micro", contentMode: "source_grounded" },
      { authorizedSourceCount: 1, option, estimateReady: false },
    ),
    false,
  );
});

test("student options use the owner-scoped endpoint and exact minimal shape", async () => {
  const options = await withFetch(
    async input => {
      assert.equal(input, "/api/v1/student-classrooms/options");
      return jsonResponse({
        items: [
          {
            courseId: "course-a",
            title: "Course A",
            allowedModes: ["micro", "full"],
            allowedContentModes: ["source_grounded", "open_creation"],
          },
        ],
      });
    },
    () => listStudentClassroomOptions(),
  );

  assert.deepEqual(options, [
    {
      courseId: "course-a",
      title: "Course A",
      allowedModes: ["micro", "full"],
      allowedContentModes: ["source_grounded", "open_creation"],
    },
  ]);

  await assert.rejects(
    withFetch(
      async () =>
        jsonResponse({
          items: [
            {
              courseId: "course-a",
              title: "Course A",
              classId: "must-stay-private",
              allowedModes: ["micro"],
              allowedContentModes: ["source_grounded"],
            },
          ],
        }),
      () => listStudentClassroomOptions(),
    ),
    /unknown fields/,
  );
});

test("config estimate uses the student-scoped contract without a class id", async () => {
  const estimate = await withFetch(
    async (input, init) => {
      assert.equal(input, "/api/v1/student-classrooms/estimate");
      assert.equal(init?.method, "POST");
      assert.deepEqual(JSON.parse(String(init?.body)), {
        courseId: "course-a",
        mode: "full",
        contentMode: "source_grounded",
        sourceType: "knowledge_base",
        sourceRef: "kb-course-a",
      });
      assert.equal("classId" in JSON.parse(String(init?.body)), false);
      return jsonResponse({
        sceneRange: [7, 19],
        durationMinutesRange: [21, 95],
        quotaUnits: 19,
        requiresOutlineConfirmation: true,
        requiresApproval: true,
      });
    },
    () =>
      estimateStudentClassroom({
        courseId: "course-a",
        mode: "full",
        contentMode: "source_grounded",
        sourceRef: "kb-course-a",
      }),
  );

  assert.deepEqual(estimate, {
    sceneRange: [7, 19],
    durationMinutesRange: [21, 95],
    quotaUnits: 19,
    requiresOutlineConfirmation: true,
    requiresApproval: true,
  });
});

test("only the current successful estimate request unlocks confirmation", () => {
  const currentRequestKey = studentClassroomEstimateRequestKey({
    courseId: "course-a",
    mode: "full",
    contentMode: "source_grounded",
    sourceRef: "kb-course-a",
  });
  const staleRequestKey = studentClassroomEstimateRequestKey({
    courseId: "course-a",
    mode: "micro",
    contentMode: "source_grounded",
    sourceRef: "kb-course-a",
  });

  assert.equal(studentClassroomEstimateIsReady(currentRequestKey, null), false);
  assert.equal(
    studentClassroomEstimateIsReady(currentRequestKey, {
      requestKey: currentRequestKey,
      status: "loading",
    }),
    false,
  );
  assert.equal(
    studentClassroomEstimateIsReady(currentRequestKey, {
      requestKey: currentRequestKey,
      status: "failed",
    }),
    false,
  );
  assert.equal(
    studentClassroomEstimateIsReady(currentRequestKey, {
      requestKey: staleRequestKey,
      status: "ready",
    }),
    false,
  );
  assert.equal(
    studentClassroomEstimateIsReady(currentRequestKey, {
      requestKey: currentRequestKey,
      status: "ready",
    }),
    true,
  );
  assert.equal(
    studentClassroomEstimateRequestKey({
      courseId: "course-a",
      mode: "micro",
      contentMode: "open_creation",
    }),
    studentClassroomEstimateRequestKey({
      courseId: "course-a",
      mode: "micro",
      contentMode: "open_creation",
      sourceRef: "ignored-session-kb",
    }),
  );
});

test("language changes preserve a successful student classroom estimate", () => {
  const config = readFileSync(
    "components/classroom/StudentClassroomConfig.tsx",
    "utf8",
  );
  const optionsEffectStart = config.indexOf(
    "useEffect(() => {",
    config.indexOf("const [options"),
  );
  const optionsEffectEnd = config.indexOf(
    "const selectedOption",
    optionsEffectStart,
  );
  assert.ok(optionsEffectStart >= 0 && optionsEffectEnd > optionsEffectStart);
  const optionsEffect = config.slice(optionsEffectStart, optionsEffectEnd);

  assert.match(optionsEffect, /\}, \[onOptionsChange\]\);/);
  assert.doesNotMatch(optionsEffect, /\bt\(/);
  assert.match(config, /setErrorKey\("studentClassroom\.config\.courseLoadFailed"\)/);
  assert.match(config, /errorKey\s*\?\s*\([\s\S]*?\{t\(errorKey\)\}/);
});

test("malformed classroom statuses, estimates, and job invariants fail closed", async () => {
  assert.equal(
    extractStudentClassroomTaskFromEvents([
      {
        type: "result",
        source: "interactive_classroom",
        metadata: interactiveResultMetadata({ status: "future_state" }),
      },
    ]),
    null,
  );
  assert.equal(
    extractStudentClassroomTaskFromEvents([
      {
        type: "result",
        source: "interactive_classroom",
        metadata: interactiveResultMetadata({}, { scene_range: [5, 1] }),
      },
    ]),
    null,
  );

  await assert.rejects(
    withFetch(
      async () => jsonResponse(studentClassroomPayload({ status: "future_state" })),
      () => getStudentClassroom("asset-1"),
    ),
    /status/,
  );
  await assert.rejects(
    withFetch(
      async () =>
        jsonResponse(
          classroomJobPayload({
            status: "succeeded",
            progress_percent: 80,
          }),
        ),
      () => getStudentClassroomJob("job-1"),
    ),
    /progress_percent/,
  );
  await assert.rejects(
    withFetch(
      async () =>
        jsonResponse({
          sceneRange: [0, 5],
          durationMinutesRange: [3, 25],
          quotaUnits: 0,
          requiresOutlineConfirmation: false,
          requiresApproval: false,
        }),
      () =>
        estimateStudentClassroom({
          courseId: "course-a",
          mode: "micro",
          contentMode: "open_creation",
        }),
    ),
    /sceneRange|quotaUnits/,
  );

  const approvedDraft = await withFetch(
    async () =>
      jsonResponse(
        studentClassroomPayload({
          status: "draft",
          approvalId: "approval-1",
          generationJobId: null,
        }),
      ),
    () => getStudentClassroom("asset-1"),
  );
  assert.equal(approvedDraft.status, "draft");
});

test("outline editing recognizes both job and student-detail status names", () => {
  assert.equal(studentClassroomRequiresOutline("awaiting_confirmation"), true);
  assert.equal(
    studentClassroomRequiresOutline("awaiting_outline_confirmation"),
    true,
  );
  assert.equal(studentClassroomRequiresOutline("generating_outline"), false);
});

test("student detail is authoritative for workflow status and outline", () => {
  const task = {
    assetId: "asset-1",
    jobId: "job-1",
    approvalId: null,
    status: "generating_outline",
    mode: "full" as const,
    revision: 1,
    outline: { title: "stale job outline" },
    estimate: {
      sceneRange: [6, 24] as [number, number],
      durationMinutesRange: [18, 120] as [number, number],
      quotaUnits: 24,
      requiresOutlineConfirmation: true,
      requiresApproval: false,
    },
  };
  const classroom = {
    assetId: "asset-1",
    requestId: "request-1",
    approvalId: null,
    generationJobId: "job-1",
    status: "awaiting_outline_confirmation",
    courseId: "course-a",
    classId: "class-a",
    mode: "full" as const,
    ownerId: "student-a",
    revision: 3,
    outline: { title: "authoritative classroom outline" },
    classroomVersionId: null,
  };

  assert.deepEqual(resolveStudentClassroomCardState(task, classroom), {
    jobId: "job-1",
    status: "awaiting_outline_confirmation",
    outline: { title: "authoritative classroom outline" },
    approvalId: null,
    classroomVersionId: null,
  });
});

test("terminal and outline-wait states stop while approval wait keeps polling", () => {
  assert.equal(
    shouldPollStudentClassroom({
      status: "succeeded",
      classroomVersionId: null,
    }),
    false,
  );
  assert.equal(
    shouldPollStudentClassroom({
      status: "awaiting_outline_confirmation",
      classroomVersionId: null,
    }),
    false,
  );
  assert.equal(
    shouldPollStudentClassroom({
      status: "awaiting_approval",
      classroomVersionId: null,
    }),
    true,
  );
  assert.equal(
    shouldPollStudentClassroom({
      status: "generating_content",
      classroomVersionId: null,
    }),
    true,
  );
});

test("approval wait polls at a low frequency until the teacher decides", () => {
  assert.equal(studentClassroomPollIntervalMs("awaiting_approval"), 15_000);
  assert.equal(studentClassroomPollIntervalMs("generating_content"), 2_500);
});

test("classroom polling stops on stable access errors and bounds transient retries", async () => {
  assert.equal(
    studentClassroomPollRetryDelay(
      new StudentClassroomRequestError("missing", 404),
      1,
    ),
    null,
  );
  assert.equal(
    studentClassroomPollRetryDelay(
      new StudentClassroomRequestError("forbidden", 403),
      1,
    ),
    null,
  );
  assert.equal(studentClassroomPollRetryDelay(new Error("temporary"), 1), 2500);
  assert.equal(studentClassroomPollRetryDelay(new Error("temporary"), 2), 5000);
  assert.equal(studentClassroomPollRetryDelay(new Error("temporary"), 3), 10000);
  assert.equal(studentClassroomPollRetryDelay(new Error("temporary"), 4), 30000);
  assert.equal(studentClassroomPollRetryDelay(new Error("temporary"), 10), 30000);

  const malformedNotFound = await withFetch(
    async () => jsonResponse("missing", 404),
    () => getStudentClassroom("asset-1").catch(error => error as unknown),
  );
  assert.equal(malformedNotFound instanceof StudentClassroomRequestError, true);
  assert.equal(studentClassroomPollRetryDelay(malformedNotFound, 1), null);
});

test("terminal failures never use the running status presentation", () => {
  assert.equal(studentClassroomStatusKind("failed"), "failure");
  assert.equal(studentClassroomStatusKind("canceled"), "failure");
  assert.equal(studentClassroomStatusKind("rejected"), "failure");
  assert.equal(studentClassroomStatusKind("expired"), "failure");
  assert.equal(studentClassroomStatusKind("awaiting_approval"), "waiting");
  assert.equal(studentClassroomStatusKind("awaiting_outline_confirmation"), "waiting");
  assert.equal(studentClassroomStatusKind("generating_content"), "running");
});

test("a successful classroom lookup does not reset repeated job failures", () => {
  const card = readFileSync(
    "components/classroom/ClassroomJobCard.tsx",
    "utf8",
  );
  const jobLookup = card.indexOf("await getStudentClassroomJob");
  const successReset = card.indexOf("\n        failureCount = 0;");
  assert.notEqual(jobLookup, -1);
  assert.ok(
    successReset > jobLookup,
    "the transient failure counter must reset only after the job lookup succeeds",
  );
});

test("approval summary follows actual approval identity and workflow status", () => {
  assert.equal(
    studentClassroomApprovalState({
      status: "awaiting_approval",
      approvalId: "approval-1",
      jobId: null,
    }),
    "waiting",
  );
  assert.equal(
    studentClassroomApprovalState({
      status: "generating_content",
      approvalId: "approval-1",
      jobId: "job-1",
    }),
    "approved",
  );
  assert.equal(
    studentClassroomApprovalState({
      status: "queued",
      approvalId: null,
      jobId: "job-direct",
    }),
    "notRequired",
  );
  assert.equal(
    studentClassroomApprovalState({
      status: "rejected",
      approvalId: "approval-1",
      jobId: null,
    }),
    "notApproved",
  );
});

test("interactive classroom turns require exactly one authorized source", () => {
  assert.deepEqual(
    studentClassroomEffectiveKnowledgeBases(
      "source_grounded",
      ["kb-course-a"],
    ),
    ["kb-course-a"],
  );
  assert.deepEqual(
    studentClassroomEffectiveKnowledgeBases(
      "open_creation",
      ["kb-course-a"],
    ),
    [],
  );
  assert.equal(
    studentClassroomEffectiveKnowledgeBases(
      "source_grounded",
      ["kb-course-a", "kb-course-b"],
    ),
    null,
  );
});

test("interactive classroom sends the catalog-authorized source despite stale preferences", () => {
  const hydratedPreferences = ["kb-stale", "kb-course-a"];
  const catalogNames = new Set(["kb-course-a"]);
  const authorizedSourceNames = hydratedPreferences.filter(name =>
    catalogNames.has(name),
  );

  assert.deepEqual(
    studentClassroomEffectiveKnowledgeBases(
      "source_grounded",
      authorizedSourceNames,
    ),
    ["kb-course-a"],
  );
  assert.deepEqual(
    studentClassroomEffectiveKnowledgeBases("open_creation", authorizedSourceNames),
    [],
  );
});

test("full choice is serialized into capability config", () => {
  assert.deepEqual(
    toCapabilityConfig({
      courseId: "course-a",
      mode: "full",
      contentMode: "source_grounded",
    }),
    {
      course_id: "course-a",
      mode: "full",
      content_mode: "source_grounded",
    },
  );
});

test("persisted interactive-classroom result restores its server task identity", () => {
  const task = extractStudentClassroomTaskFromEvents([
    {
      type: "result",
      source: "interactive_classroom",
      metadata: {
        response: "The classroom outline has been queued.",
        estimate: {
          scene_range: [6, 24],
          duration_minutes_range: [18, 120],
          quota_units: 24,
          requires_outline_confirmation: true,
          requires_approval: false,
        },
        approval_id: null,
        job_id: "job-1",
        outline: null,
        classroom: {
          asset_id: "asset-1",
          request_id: "request-1",
          approval_id: null,
          generation_job_id: "job-1",
          status: "generating_outline",
          course_id: "course-a",
          class_id: "class-a",
          mode: "full",
          owner_id: "student-a",
          revision: 2,
          outline: null,
          classroom_version_id: null,
        },
        metadata: { cost_summary: {} },
      },
    },
  ]);

  assert.deepEqual(task, {
    assetId: "asset-1",
    jobId: "job-1",
    approvalId: null,
    status: "generating_outline",
    mode: "full",
    revision: 2,
    outline: null,
    estimate: {
      sceneRange: [6, 24],
      durationMinutesRange: [18, 120],
      quotaUnits: 24,
      requiresOutlineConfirmation: true,
      requiresApproval: false,
    },
  });
});

test("student task APIs use existing private routes and preserve outline revision", async () => {
  const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
  const responses = [
    studentClassroomPayload({
      assetId: "asset / 1",
      generationJobId: "job / 1",
    }),
    classroomJobPayload({ job_id: "job / 1" }),
    studentClassroomPayload({
      assetId: "asset / 1",
      generationJobId: "job / 1",
      status: "awaiting_outline_confirmation",
      revision: 3,
      outline: { title: "Revised" },
    }),
    studentClassroomPayload({
      assetId: "asset / 1",
      generationJobId: "job / 1",
      status: "queued",
      revision: 4,
    }),
  ];

  await withFetch(
    async (input, init) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    },
    async () => {
      const classroom = await getStudentClassroom("asset / 1");
      const job = await getStudentClassroomJob("job / 1");
      const updated = await updateStudentClassroomOutline(
        "asset / 1",
        { title: "Revised" },
        2,
      );
      const confirmed = await confirmStudentClassroomOutline("asset / 1");

      assert.equal(classroom.assetId, "asset / 1");
      assert.equal(job.status, "generating_outline");
      assert.equal(updated.revision, 3);
      assert.equal(confirmed.status, "queued");
    },
  );

  assert.deepEqual(
    requests.map(({ input }) => input),
    [
      "/api/v1/student-classrooms/asset%20%2F%201",
      "/api/v1/classroom-jobs/job%20%2F%201",
      "/api/v1/student-classrooms/asset%20%2F%201/outline",
      "/api/v1/student-classrooms/asset%20%2F%201/confirm-outline",
    ],
  );
  assert.equal(requests[2].init?.method, "PUT");
  assert.deepEqual(requests[2].init?.headers, {
    "Content-Type": "application/json",
    "If-Match": '"revision-2"',
  });
  assert.equal(requests[2].init?.body, JSON.stringify({ outline: { title: "Revised" } }));
  assert.equal(requests[3].init?.method, "POST");
});

test("succeeded task opens only its owner-scoped student classroom version", async () => {
  const versionId = await withFetch(
    async input => {
      assert.equal(input, "/api/v1/student-classrooms/asset%20%2F%201");
      return jsonResponse(
        studentClassroomPayload({
          assetId: "asset / 1",
          status: "succeeded",
          classroomVersionId: "version / 1",
        }),
      );
    },
    () => getStudentClassroomVersionId("asset / 1"),
  );

  assert.equal(versionId, "version / 1");
  assert.equal(
    studentClassroomPlayRoute(versionId),
    "/learn/classrooms/version%20%2F%201",
  );
  assert.equal(studentClassroomPlayRoute(null), null);
});

test("home chat wires the explicit config and durable classroom task card", () => {
  const configPath = "components/classroom/StudentClassroomConfig.tsx";
  const jobCardPath = "components/classroom/ClassroomJobCard.tsx";
  assert.equal(existsSync(configPath), true, "student config component is missing");
  assert.equal(existsSync(jobCardPath), true, "classroom job card is missing");

  const home = readFileSync("app/(workspace)/home/[[...sessionId]]/page.tsx", "utf8");
  const messages = readFileSync("components/chat/home/ChatMessages.tsx", "utf8");
  const context = readFileSync("context/UnifiedChatContext.tsx", "utf8");
  const composer = readFileSync("components/chat/home/ChatComposer.tsx", "utf8");
  const composerInput = readFileSync("components/chat/home/ComposerInput.tsx", "utf8");
  const config = readFileSync(configPath, "utf8");
  const jobCard = readFileSync(jobCardPath, "utf8");

  assert.match(home, /value:\s*"interactive_classroom"/);
  assert.match(home, /<StudentClassroomConfig/);
  assert.match(home, /toCapabilityConfig\(studentClassroomConfig\)/);
  assert.match(
    home,
    /studentClassroom\.validation\.contentModeUnavailable/,
  );
  assert.match(home, /studentClassroom\.validation\.sourceMustBeSingle/);
  assert.match(messages, /<ClassroomJobCard\s+task=/);
  assert.match(
    context,
    /events:\s*Array\.isArray\(message\.events\)\s*\?\s*message\.events\s*:\s*\[\]/,
  );
  assert.match(
    context,
    /replaySnapshot\?\.knowledgeBases\s*\?\?\s*options\?\.knowledgeBases\s*\?\?\s*session\.knowledgeBases/,
  );
  assert.match(home, /knowledgeBases:\s*studentTurnKnowledgeBases/);
  assert.match(
    home,
    /item\.name === selected && item\.metadata\?\.type !== "subagent"/,
  );
  const capabilitySwitchStart = home.indexOf(
    "const handleSelectCapability = useCallback",
  );
  const capabilitySwitchEnd = home.indexOf(
    "const fileToAttachment",
    capabilitySwitchStart,
  );
  assert.ok(capabilitySwitchStart >= 0 && capabilitySwitchEnd > capabilitySwitchStart);
  const capabilitySwitch = home.slice(capabilitySwitchStart, capabilitySwitchEnd);
  assert.match(
    capabilitySwitch,
    /cap\.value !== "interactive_classroom" && config\.knowledgeBase/,
  );
  assert.doesNotMatch(
    capabilitySwitch,
    /realKnowledgeBaseNames|state\.knowledgeBases\.filter\(/,
    "classroom capability switches must not rewrite session KB preferences",
  );
  assert.match(
    home,
    /if \(!isStudentClassroomMode && selectedAgent && subagentBudget\)/,
  );
  assert.match(
    messages,
    /capability === "interactive_classroom"\) return "Student Classroom"/,
  );
  assert.match(config, /type="radio"/);
  assert.match(config, /listStudentClassroomOptions/);
  assert.match(config, /estimateStudentClassroom/);
  assert.match(config, /onEstimateReadinessChange/);
  assert.match(config, /status:\s*"loading"/);
  assert.match(config, /status:\s*"failed"/);
  assert.match(config, /status:\s*"ready"/);
  assert.match(config, /estimateRetryNonce/);
  assert.match(config, /setEstimateRetryNonce\(current => current \+ 1\)/);
  assert.match(config, /studentClassroom\.config\.retryEstimate/);
  assert.match(config, /onClick=\{retryEstimate\}/);
  const estimateEffectStart = config.indexOf(
    "useEffect(() => {",
    config.indexOf("const estimateLoading"),
  );
  const estimateEffectEnd = config.indexOf("const selectMode", estimateEffectStart);
  assert.ok(estimateEffectStart >= 0 && estimateEffectEnd > estimateEffectStart);
  const estimateEffect = config.slice(estimateEffectStart, estimateEffectEnd);
  assert.match(
    estimateEffect,
    /onEstimateReadinessChange\(\{[\s\S]*?requestKey:\s*estimateRequestKey,[\s\S]*?status:\s*"loading"/,
  );
  assert.match(
    estimateEffect,
    /\[[\s\S]*?estimateRequestKey,[\s\S]*?estimateRetryNonce,[\s\S]*?\]/,
  );
  assert.match(estimateEffect, /if \(!active\) return;/);
  assert.match(estimateEffect, /active && !controller\.signal\.aborted/);
  assert.match(estimateEffect, /active = false;[\s\S]*?controller\.abort\(\)/);
  assert.match(
    home,
    /studentClassroomEffectiveKnowledgeBases\(\s*studentClassroomConfig\.contentMode,\s*studentAuthorizedSourceNames,?\s*\)/,
  );
  assert.doesNotMatch(
    home,
    /studentClassroomTurnKnowledgeBases\([\s\S]{0,240}state\.knowledgeBases/,
    "classroom send must use the same authorized source set as estimate readiness",
  );
  assert.doesNotMatch(config, /MODE_SUMMARY/);
  assert.doesNotMatch(config, /listTeachingCourses/);
  assert.match(config, /allowedContentModes\.includes\("open_creation"\)/);
  assert.match(jobCard, /getStudentClassroomJob/);
  assert.match(jobCard, /updateStudentClassroomOutline/);
  assert.match(jobCard, /confirmStudentClassroomOutline/);
  assert.match(jobCard, /studentClassroomApprovalState/);
  assert.doesNotMatch(jobCard, /task\.estimate\.requiresApproval/);
  assert.doesNotMatch(jobCard, /task\.approvalId\s*\|\|/);
  assert.match(
    composer,
    /const doSend = useCallback\([\s\S]*?if \(!canSend\)[\s\S]*?return false;/,
  );
  assert.match(composerInput, /if \(onSend\(content\) === false\) return;/);
  assert.match(
    home,
    /capabilityConfigConfirmed=\{[\s\S]*?studentClassroomCanConfirm[\s\S]*?\}/,
  );
  assert.match(home, /studentClassroomEstimateIsReady/);
  assert.match(home, /estimateReady:\s*studentClassroomEstimateReady/);
  assert.match(
    home,
    /onEstimateReadinessChange=\{setStudentClassroomEstimateReadiness\}/,
  );
});
