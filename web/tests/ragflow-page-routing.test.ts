import assert from "node:assert/strict";
import test from "node:test";
import * as helpers from "../lib/knowledge-helpers";

test("RAGFlow selection uses stable identity when display names collide", () => {
  assert.equal(typeof helpers.resolveKnowledgeSelection, "function");
  const own = { id: "user:kb:own-generation", name: "Course" };
  const assigned = { id: "admin:kb:shared-generation", name: "Course", assigned: true, read_only: true };
  assert.equal(helpers.resolveKnowledgeSelection([own, assigned], assigned.id), assigned);
  assert.equal(helpers.resolveKnowledgeSelection([own, assigned], "Course"), null);
  assert.equal(helpers.resolveKnowledgeSelection([own, assigned], "admin:kb:removed-generation"), null);
  assert.equal(helpers.resolveKnowledgeSelection([own], "Course"), own);
  assert.equal(helpers.resolveKnowledgeSelection([own], null), own);
});

test("RAGFlow type identifies its dedicated manager even without statistics", () => {
  assert.equal(helpers.kbProvider({ name: "Course", metadata: { type: "ragflow" } }), "ragflow");
});

test("RAGFlow provisioning must not subscribe to legacy local-index progress", () => {
  assert.equal(helpers.kbHasLiveProgress({ name: "Course", status: "initializing", progress: { stage: "initializing" }, metadata: { type: "ragflow" } }), false);
});

test("RAGFlow engine availability is not reported as verified remote readiness", () => {
  assert.equal(helpers.providerConnectionStatus({ id: "ragflow", configured: true, requires_connection: true }), "connection");
});
