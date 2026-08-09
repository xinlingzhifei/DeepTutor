import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

import {
  canConfirmStudentClassroomConfig,
  confirmStudentClassroomOutline,
  createStudentClassroomConfig,
  extractStudentClassroomTaskFromEvents,
  getStudentClassroom,
  getStudentClassroomJob,
  getStudentClassroomVersionId,
  listStudentClassroomOptions,
  restoreStudentClassroomConfig,
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
      { hasAuthorizedSource: true, option },
    ),
    false,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "micro", contentMode: "source_grounded" },
      { hasAuthorizedSource: false, option },
    ),
    false,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "micro", contentMode: "source_grounded" },
      { hasAuthorizedSource: true, option },
    ),
    true,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "micro", contentMode: "open_creation" },
      {
        hasAuthorizedSource: false,
        option: { ...option, allowedContentModes: ["source_grounded"] },
      },
    ),
    false,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "micro", contentMode: "open_creation" },
      { hasAuthorizedSource: false, option },
    ),
    true,
  );
  assert.equal(
    canConfirmStudentClassroomConfig(
      { courseId: "course-a", mode: "full", contentMode: "open_creation" },
      { hasAuthorizedSource: false, option },
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

test("outline editing recognizes both job and student-detail status names", () => {
  assert.equal(studentClassroomRequiresOutline("awaiting_confirmation"), true);
  assert.equal(
    studentClassroomRequiresOutline("awaiting_outline_confirmation"),
    true,
  );
  assert.equal(studentClassroomRequiresOutline("generating_outline"), false);
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
        },
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
  const config = readFileSync(configPath, "utf8");
  const jobCard = readFileSync(jobCardPath, "utf8");

  assert.match(home, /value:\s*"interactive_classroom"/);
  assert.match(home, /<StudentClassroomConfig/);
  assert.match(home, /toCapabilityConfig\(studentClassroomConfig\)/);
  assert.match(
    home,
    /studentClassroom\.validation\.contentModeUnavailable/,
  );
  assert.match(messages, /<ClassroomJobCard\s+task=/);
  assert.match(
    context,
    /events:\s*Array\.isArray\(message\.events\)\s*\?\s*message\.events\s*:\s*\[\]/,
  );
  assert.match(
    messages,
    /capability === "interactive_classroom"\) return "Student Classroom"/,
  );
  assert.match(config, /type="radio"/);
  assert.match(config, /listStudentClassroomOptions/);
  assert.doesNotMatch(config, /listTeachingCourses/);
  assert.match(config, /allowedContentModes\.includes\("open_creation"\)/);
  assert.match(jobCard, /getStudentClassroomJob/);
  assert.match(jobCard, /updateStudentClassroomOutline/);
  assert.match(jobCard, /confirmStudentClassroomOutline/);
  assert.match(jobCard, /task\.estimate\.requiresApproval/);
  assert.doesNotMatch(jobCard, /task\.approvalId\s*\|\|/);
});
