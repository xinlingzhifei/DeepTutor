import assert from "node:assert/strict";
import test from "node:test";

import { deleteUser, UserIdentityConflictError } from "../lib/admin-api";
import { createTeachingCourse, createTeachingClass, getTeachingCatalogPermissions } from "../lib/teaching-api";
import { chatModelIssue, type LLMOption } from "../lib/llm-options";
import { fetchCourseModelStatus } from "../lib/course-model-status";

async function withFetch(implementation: typeof fetch, run: () => Promise<void>) {
  const original = globalThis.fetch;
  globalThis.fetch = implementation;
  try { await run(); } finally { globalThis.fetch = original; }
}

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

test("deletion sends the exact displayed identity and distinguishes an identity conflict", async () => {
  await withFetch(async (url, init) => {
    assert.equal(url, "/api/v1/auth/users/alice%20smith");
    assert.equal(init?.method, "DELETE");
    assert.equal(new Headers(init?.headers).get("content-type"), "application/json");
    assert.deepEqual(JSON.parse(String(init?.body)), { expected_user_id: "u-original" });
    return new Response(null, { status: 204 });
  }, () => deleteUser("alice smith", "u-original"));
  await withFetch(async () => json({ detail: "identity changed" }, 409), async () => {
    await assert.rejects(deleteUser("alice", "u-old"), UserIdentityConflictError);
  });
});

test("course and class creation preserve retry IDs and trim names", async () => {
  await withFetch(async (url, init) => {
    const body = JSON.parse(String(init?.body));
    assert.equal(init?.method, "POST");
    if (url === "/api/v1/teaching/courses") {
      assert.deepEqual(body, { id: "course-new", title: "Physics" });
      return json({ ...body, status: "active", createdAt: null }, 201);
    }
    assert.equal(url, "/api/v1/teaching/courses/course-new/classes");
    assert.deepEqual(body, { id: "class-new", name: "Class A" });
    return json({ ...body, courseId: "course-new", status: "active", createdAt: null }, 201);
  }, async () => {
    assert.equal((await createTeachingCourse("course-new", " Physics ")).id, "course-new");
    assert.equal((await createTeachingClass("course-new", "class-new", " Class A ")).courseId, "course-new");
  });
});

test("creation permission checks fail closed on malformed responses", async () => {
  await withFetch(async url => {
    assert.equal(url, "/api/v1/teaching/catalog-permissions?course_id=course-a");
    return json({ canCreateCourse: false, canCreateClass: true });
  }, async () => {
    assert.deepEqual(await getTeachingCatalogPermissions("course-a"), { canCreateCourse: false, canCreateClass: true });
  });
  await withFetch(async () => json({ canCreateCourse: "true" }), async () => {
    await assert.rejects(getTeachingCatalogPermissions());
  });
});

test("chat blocks missing, stale, incomplete and loading selections", () => {
  const option: LLMOption = { profile_id: "p1", model_id: "m1", profile_name: "Provider", model_name: "Model", model: "model", provider: "openai", is_active_default: true, configured: true };
  assert.ok(chatModelIssue([], null, null, false, false));
  assert.ok(chatModelIssue([option], null, null, false, false));
  assert.ok(chatModelIssue([option], { profile_id: "gone", model_id: "gone" }, option, false, false));
  assert.ok(chatModelIssue([{ ...option, configured: false }], null, option, false, false));
  assert.ok(chatModelIssue([option], option, option, true, false));
  assert.ok(chatModelIssue([option], option, option, false, true));
  assert.equal(chatModelIssue([option], null, option, false, false), null);
  assert.equal(chatModelIssue([option], option, null, false, false), null);
});

test("course readiness distinguishes shared status, tenant routing and saved resync", async () => {
  const calls: Array<[string, string]> = [];
  await withFetch(async (url, init) => {
    calls.push([String(url), init?.method || "GET"]);
    return json({ state: "synced", ready: true, can_configure: true });
  }, async () => {
    assert.equal((await fetchCourseModelStatus()).ready, true);
    await fetchCourseModelStatus(true);
    await fetchCourseModelStatus(false, true);
  });
  assert.deepEqual(calls, [
    ["/api/v1/settings/openmaic-status", "GET"],
    ["/api/v1/teaching/model-readiness", "GET"],
    ["/api/v1/settings/openmaic-sync", "POST"],
  ]);
  await withFetch(async () => json({ state: "not_configured", ready: true, can_configure: true }), async () => {
    await assert.rejects(fetchCourseModelStatus());
  });
  await withFetch(async () => json({}, 503), async () => {
    await assert.rejects(fetchCourseModelStatus());
  });
});
