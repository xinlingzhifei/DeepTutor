import type { Page, Request, Route } from "@playwright/test";

import { apiPayload } from "./support/baseline-api-fixtures";
import { expect, test } from "./support/teaching-flow-test";

const ASSET_ID = "asset-student-full";
const JOB_ID = "job-student-outline";
const VERSION_ID = "version-student-private";
const SESSION_ID = "session-student-classroom";
const MICRO_ASSET_ID = "asset-student-micro";
const MICRO_JOB_ID = "job-student-micro";
const MICRO_VERSION_ID = "version-student-micro";
const MICRO_SESSION_ID = "session-student-micro";

type ApiCall = {
  body: unknown;
  headers: Record<string, string>;
};

function requestCall(request: Request): ApiCall {
  const raw = request.postData();
  return {
    body: raw ? (JSON.parse(raw) as unknown) : null,
    headers: request.headers(),
  };
}

async function json(route: Route, payload: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

function interactiveClassroomResult() {
  return {
    response: "The classroom outline is ready for confirmation.",
    estimate: {
      scene_range: [6, 24],
      duration_minutes_range: [18, 120],
      quota_units: 24,
      requires_outline_confirmation: true,
      requires_approval: false,
    },
    approval_id: null,
    job_id: JOB_ID,
    outline: {
      title: "Energy transfer",
      scenes: [{ title: "Trace the pathway" }],
    },
    classroom: {
      asset_id: ASSET_ID,
      request_id: "request-student-full",
      approval_id: null,
      generation_job_id: JOB_ID,
      status: "awaiting_confirmation",
      course_id: "course-energy",
      class_id: "class-7a",
      mode: "full",
      owner_id: "student-e2e",
      revision: 2,
      outline: {
        title: "Energy transfer",
        scenes: [{ title: "Trace the pathway" }],
      },
      classroom_version_id: null,
    },
    metadata: { cost_summary: {} },
  };
}

function microClassroomResult() {
  return {
    response: "Your micro-classroom is ready.",
    estimate: {
      scene_range: [1, 4],
      duration_minutes_range: [5, 15],
      quota_units: 4,
      requires_outline_confirmation: false,
      requires_approval: false,
    },
    approval_id: null,
    job_id: MICRO_JOB_ID,
    outline: null,
    classroom: {
      asset_id: MICRO_ASSET_ID,
      request_id: "request-student-micro",
      approval_id: null,
      generation_job_id: MICRO_JOB_ID,
      status: "succeeded",
      course_id: "course-energy",
      class_id: "class-7a",
      mode: "micro",
      owner_id: "student-e2e",
      revision: 1,
      outline: null,
      classroom_version_id: MICRO_VERSION_ID,
    },
    metadata: { cost_summary: {} },
  };
}

async function installStudentMicroBackend(page: Page) {
  const state = { unexpected: [] as string[] };

  await page.route("**/api/v1/**", async route => {
    const request = route.request();
    const method = request.method();
    const pathname = new URL(request.url()).pathname;

    if (method === "GET" && pathname === `/api/v1/sessions/${MICRO_SESSION_ID}`) {
      await json(route, {
        id: MICRO_SESSION_ID,
        session_id: MICRO_SESSION_ID,
        title: "Student micro-classroom",
        created_at: 1,
        updated_at: 2,
        status: "completed",
        preferences: {
          capability: "interactive_classroom",
          tools: [],
          knowledge_bases: [],
          language: "en",
        },
        messages: [
          {
            id: 1,
            session_id: MICRO_SESSION_ID,
            role: "user",
            content: "Build a micro-classroom about energy transfer",
            capability: "interactive_classroom",
            events: [],
            attachments: [],
            created_at: 1,
            parent_message_id: null,
          },
          {
            id: 2,
            session_id: MICRO_SESSION_ID,
            role: "assistant",
            content: "Your micro-classroom is ready.",
            capability: "interactive_classroom",
            events: [
              {
                type: "result",
                source: "interactive_classroom",
                turn_id: "turn-student-micro",
                timestamp: 2,
                metadata: microClassroomResult(),
              },
            ],
            attachments: [],
            created_at: 2,
            parent_message_id: 1,
          },
        ],
        active_turns: [],
      });
      return;
    }
    if (method === "GET" && pathname === "/api/v1/subagents/connections") {
      await json(route, { connections: [] });
      return;
    }
    if (method === "GET" && pathname === "/api/v1/student-classrooms/options") {
      await json(route, {
        items: [
          {
            courseId: "course-energy",
            title: "Energy Science",
            allowedModes: ["micro", "full"],
            allowedContentModes: ["source_grounded", "open_creation"],
          },
        ],
      });
      return;
    }
    if (
      method === "GET" &&
      pathname === `/api/v1/student-classrooms/${MICRO_ASSET_ID}`
    ) {
      await json(route, {
        assetId: MICRO_ASSET_ID,
        requestId: "request-student-micro",
        approvalId: null,
        generationJobId: MICRO_JOB_ID,
        status: "succeeded",
        courseId: "course-energy",
        classId: "class-7a",
        mode: "micro",
        ownerId: "student-e2e",
        revision: 1,
        outline: null,
        classroomVersionId: MICRO_VERSION_ID,
      });
      return;
    }
    if (method === "GET") {
      const payload = apiPayload(pathname, "snow");
      if (payload !== undefined) {
        await json(route, payload);
        return;
      }
    }

    state.unexpected.push(`${method} ${pathname}`);
    await json(route, { detail: "Unexpected E2E API request" }, 404);
  });

  return state;
}

async function installStudentBackend(page: Page) {
  const state = {
    phase: "outline" as "outline" | "generating" | "succeeded",
    revision: 2,
    outline: interactiveClassroomResult().outline as Record<string, unknown>,
    outlineCalls: [] as ApiCall[],
    confirmCalls: 0,
    unexpected: [] as string[],
  };

  const classroom = () => ({
    assetId: ASSET_ID,
    requestId: "request-student-full",
    approvalId: null,
    generationJobId: state.phase === "outline" ? JOB_ID : "job-student-content",
    status:
      state.phase === "outline"
        ? "awaiting_confirmation"
        : state.phase === "generating"
          ? "queued"
          : "succeeded",
    courseId: "course-energy",
    classId: "class-7a",
    mode: "full",
    ownerId: "student-e2e",
    revision: state.revision,
    outline: state.outline,
    classroomVersionId: state.phase === "succeeded" ? VERSION_ID : null,
  });

  await page.route("**/api/v1/**", async route => {
    const request = route.request();
    const method = request.method();
    const pathname = new URL(request.url()).pathname;

    if (method === "GET" && pathname === `/api/v1/sessions/${SESSION_ID}`) {
      await json(route, {
        id: SESSION_ID,
        session_id: SESSION_ID,
        title: "Student classroom",
        created_at: 1,
        updated_at: 2,
        status: "completed",
        preferences: {
          capability: "interactive_classroom",
          tools: [],
          knowledge_bases: [],
          language: "en",
        },
        messages: [
          {
            id: 1,
            session_id: SESSION_ID,
            role: "user",
            content: "Build a full classroom about energy transfer",
            capability: "interactive_classroom",
            events: [],
            attachments: [],
            created_at: 1,
            parent_message_id: null,
          },
          {
            id: 2,
            session_id: SESSION_ID,
            role: "assistant",
            content: "The outline is ready.",
            capability: "interactive_classroom",
            events: [
              {
                type: "result",
                source: "interactive_classroom",
                turn_id: "turn-student-full",
                timestamp: 2,
                metadata: interactiveClassroomResult(),
              },
            ],
            attachments: [],
            created_at: 2,
            parent_message_id: 1,
          },
        ],
        active_turns: [],
      });
      return;
    }
    if (method === "GET" && pathname === "/api/v1/subagents/connections") {
      await json(route, { connections: [] });
      return;
    }
    if (method === "GET" && pathname === "/api/v1/student-classrooms/options") {
      await json(route, {
        items: [
          {
            courseId: "course-energy",
            title: "Energy Science",
            allowedModes: ["micro", "full"],
            allowedContentModes: ["source_grounded", "open_creation"],
          },
        ],
      });
      return;
    }
    if (method === "GET" && pathname === `/api/v1/student-classrooms/${ASSET_ID}`) {
      if (state.phase === "generating") state.phase = "succeeded";
      await json(route, classroom());
      return;
    }
    if (
      method === "PUT" &&
      pathname === `/api/v1/student-classrooms/${ASSET_ID}/outline`
    ) {
      const call = requestCall(request);
      state.outlineCalls.push(call);
      state.outline = (call.body as { outline: Record<string, unknown> }).outline;
      state.revision = 3;
      await json(route, classroom());
      return;
    }
    if (
      method === "POST" &&
      pathname === `/api/v1/student-classrooms/${ASSET_ID}/confirm-outline`
    ) {
      state.confirmCalls += 1;
      state.phase = "generating";
      state.revision = 4;
      await json(route, classroom());
      return;
    }

    if (method === "GET") {
      const payload = apiPayload(pathname, "snow");
      if (payload !== undefined) {
        await json(route, payload);
        return;
      }
    }

    state.unexpected.push(`${method} ${pathname}`);
    await json(route, { detail: "Unexpected E2E API request" }, 404);
  });

  return state;
}

export async function runStudentClassroomFlow({
  page,
}: {
  page: Page;
}) {
  test.setTimeout(180_000);
  await page.addInitScript(() => {
    localStorage.setItem("deeptutor-language", "en");
  });
  const state = await installStudentBackend(page);

  await page.goto(`/home/${SESSION_ID}`);
  const card = page.getByTestId("student-classroom-job-card");
  await expect(card).toBeVisible({ timeout: 60_000 });

  const editedOutline = {
    title: "Energy transfer",
    scenes: [{ title: "Trace, compare, and explain" }],
  };
  await card.getByRole("textbox").fill(JSON.stringify(editedOutline));
  await card.getByRole("button", { name: "Confirm outline" }).click();

  const handoff = card.locator(`a[href="/learn/classrooms/${VERSION_ID}"]`);
  await expect(handoff).toBeVisible({ timeout: 60_000 });
  expect(state.outlineCalls).toHaveLength(1);
  expect(state.outlineCalls[0].body).toEqual({ outline: editedOutline });
  expect(state.outlineCalls[0].headers["if-match"]).toBe('"revision-2"');
  expect(state.confirmCalls).toBe(1);
  expect(state.unexpected).toEqual([]);
}

export async function runStudentMicroClassroomFlow({
  page,
}: {
  page: Page;
}) {
  test.setTimeout(180_000);
  await page.addInitScript(() => {
    localStorage.setItem("deeptutor-language", "en");
  });
  const state = await installStudentMicroBackend(page);

  await page.goto(`/home/${MICRO_SESSION_ID}`);
  const card = page.getByTestId("student-classroom-job-card");
  await expect(card).toBeVisible({ timeout: 60_000 });
  await expect(
    card.locator(`a[href="/learn/classrooms/${MICRO_VERSION_ID}"]`),
  ).toBeVisible({ timeout: 60_000 });
  await expect(
    card.getByRole("button", { name: "Confirm outline" }),
  ).toHaveCount(0);
  expect(state.unexpected).toEqual([]);
}
