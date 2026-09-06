import assert from "node:assert/strict";
import test from "node:test";

import {
  IntentKeys,
  managementAccess,
  operationPhase,
  parseObjectJson,
  shouldPollOperations,
} from "../components/knowledge/ragflow/management-state";

test("accepted and unknown writes are never treated as completed", () => {
  for (const state of ["INTENT_RECORDED", "SEND_STARTED", "ACKNOWLEDGED", "RECONCILING"]) {
    assert.equal(operationPhase(state), "running");
  }
  assert.equal(operationPhase("RECONCILED"), "success");
  assert.equal(operationPhase("FAILED"), "failed");
  assert.equal(operationPhase("RESULT_UNKNOWN"), "attention");
  assert.equal(operationPhase("NEEDS_ATTENTION"), "attention");
  assert.equal(operationPhase("FUTURE_STATE"), "attention");
});

test("management privileges require the current server binding", () => {
  const local = { id: "admin:kb:one", read_only: false, assigned: false };
  const binding = { id: local.id, can_manage: true, can_write: false };
  assert.deepEqual(managementAccess(local, binding), { manage: true, write: false });
  assert.deepEqual(managementAccess(local, { ...binding, can_write: true }), {
    manage: true,
    write: true,
  });
  assert.deepEqual(managementAccess(local, { ...binding, id: "admin:kb:old" }), {
    manage: false,
    write: false,
  });
  assert.deepEqual(managementAccess({ ...local, assigned: true }, binding), {
    manage: false,
    write: false,
  });
  assert.deepEqual(managementAccess({ ...local, read_only: true }, binding), {
    manage: false,
    write: false,
  });
  assert.deepEqual(managementAccess(local, null), { manage: false, write: false });
});

test("an unknown mutation retains its idempotency key across repeat clicks", () => {
  let generated = 0;
  const keys = new IntentKeys(() => `key-${++generated}`);
  const first = keys.get("parse:doc-1", { rebuild: false });
  assert.equal(keys.get("parse:doc-1", { rebuild: false }), first);
  keys.accept("parse:doc-1", { rebuild: false }, ["op-1"]);
  keys.observe([{ operation_id: "op-1", state: "NEEDS_ATTENTION" }]);
  assert.equal(keys.get("parse:doc-1", { rebuild: false }), first);
  keys.observe([{ operation_id: "op-1", state: "RECONCILED" }]);
  assert.notEqual(keys.get("parse:doc-1", { rebuild: false }), first);
});

test("an unreceipted write blocks different intents but allows the same-key recovery", () => {
  const keys = new IntentKeys(() => "stable-key");
  keys.get("upload", { file: "A" });
  keys.markUncertain("upload", { file: "A" });
  assert.equal(keys.canSubmit("upload", { file: "A" }), true);
  assert.equal(keys.canSubmit("upload", { file: "B" }), false);
  assert.equal(keys.canSubmit("create-document", { name: "B" }), false);
  keys.observe([]);
  assert.equal(keys.canSubmit("upload", { file: "B" }), false);
  keys.accept("upload", { file: "A" }, ["op-recovered"]);
  assert.equal(keys.canSubmit("upload", { file: "B" }), true);
});

test("different documents and payloads never share an intent", () => {
  let generated = 0;
  const keys = new IntentKeys(() => `key-${++generated}`);
  const first = keys.get("edit:doc-1", { name: "A", metadata: { b: 2, a: 1 } });
  assert.equal(keys.get("edit:doc-1", { metadata: { a: 1, b: 2 }, name: "A" }), first);
  assert.notEqual(keys.get("edit:doc-2", { name: "A" }), first);
  assert.notEqual(keys.get("edit:doc-1", { name: "B" }), first);
});

test("JSON editors accept objects only and report malformed drafts", () => {
  assert.deepEqual(parseObjectJson('{"subject":"geometry"}'), { subject: "geometry" });
  assert.deepEqual(parseObjectJson(""), {});
  for (const input of ["null", "[]", '"text"', "{"]) {
    assert.throws(() => parseObjectJson(input));
  }
});

test("polling stops at confirmed terminal and manual-attention states", () => {
  assert.equal(shouldPollOperations([]), false);
  for (const state of ["RECONCILED", "FAILED", "NEEDS_ATTENTION", "FUTURE_STATE"]) {
    assert.equal(shouldPollOperations([{ state }]), false);
  }
  for (const state of [
    "INTENT_RECORDED",
    "SEND_STARTED",
    "ACKNOWLEDGED",
    "RECONCILING",
    "RESULT_UNKNOWN",
  ]) {
    assert.equal(shouldPollOperations([{ state }]), true);
  }
});
