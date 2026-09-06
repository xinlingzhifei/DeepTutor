import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  buildKnowledgeConnection,
  buildRagflowProfilePayload,
  canUseRagflowProfile,
  connectionResult,
  createConnectionRequests,
  emptyRagflowProfileForm,
} from "../lib/ragflow-connection";

const kbRef = "user:kb:20c5f394-1948-4a1f-8e82-dd4f491f500f";

test("profile form never hydrates a saved API key or credential reference", () => {
  const response = {
    profile_id: "default", base_url: "https://ragflow.example", allow_http: false,
    has_api_key: true, api_key: "must-not-hydrate", secret_ref: "private-reference",
  };
  const form = emptyRagflowProfileForm(response);
  assert.deepEqual(form, {
    profileId: "default", baseUrl: "https://ragflow.example", apiKey: "", allowHttp: false,
  });
  assert.equal(JSON.stringify(form).includes("must-not-hydrate"), false);
  assert.equal(JSON.stringify(form).includes("private-reference"), false);
});

test("blank key preserves existing secret and is rejected for a new connection", () => {
  const form = { profileId: "default", baseUrl: "https://ragflow.example", apiKey: "", allowHttp: false };
  assert.deepEqual(buildRagflowProfilePayload(form, true), {
    base_url: "https://ragflow.example", allow_http: false,
  });
  assert.throws(() => buildRagflowProfilePayload(form, false));
  assert.deepEqual(buildRagflowProfilePayload({ ...form, apiKey: "new-key" }, true), {
    base_url: "https://ragflow.example", allow_http: false, api_key: "new-key",
  });
});

test("connection settings reject embedded credentials and unapproved HTTP", () => {
  const form = { profileId: "default", baseUrl: "https://ragflow.example", apiKey: "key", allowHttp: false };
  for (const baseUrl of ["https://user:secret@ragflow.example", "http://ragflow.example", "https://ragflow.example?key=value"])
    assert.throws(() => buildRagflowProfilePayload({ ...form, baseUrl }, false));
  assert.throws(() => buildRagflowProfilePayload({ ...form, profileId: "../other" }, false));
});

test("connection validation exposes stable translation codes with the existing safe fallback", () => {
  const form = { profileId: "default", baseUrl: "https://ragflow.example", apiKey: "key", allowHttp: false };
  const cases: [() => unknown, string, string][] = [
    [() => buildRagflowProfilePayload({ ...form, profileId: "../other" }, false), "profile_id_invalid", "连接标识只能包含字母、数字、下划线、点和短横线。"],
    [() => buildRagflowProfilePayload(form, false, ["default"]), "profile_exists", "连接标识已存在。请先从列表选择该连接，再轮换密钥。"],
    [() => buildRagflowProfilePayload({ ...form, baseUrl: "invalid" }, false), "service_url_required", "请填写完整的 RAGFlow 服务地址。"],
    [() => buildRagflowProfilePayload({ ...form, baseUrl: "https://user:secret@ragflow.example" }, false), "service_url_unsafe", "服务地址需使用 HTTPS，且不能包含用户名、密码或查询参数。"],
    [() => buildRagflowProfilePayload({ ...form, apiKey: "" }, false), "api_key_invalid", "请填写有效的 API Key。"],
    [() => buildKnowledgeConnection({ mode: "managed", name: "..", profileId: "default" }), "knowledge_name_invalid", "请填写有效的知识库名称。"],
    [() => buildKnowledgeConnection({ mode: "managed", name: "Course", profileId: "" }), "profile_required", "请先选择并检测连接。"],
    [() => buildKnowledgeConnection({ mode: "attach", name: "Course", profileId: "default", datasetId: "" }), "dataset_required", "请选择要接入的数据集。"],
    [() => connectionResult({ id: "Course", status: "ready" }, "managed"), "knowledge_identity_invalid", "服务未返回有效的知识库标识，请刷新知识库列表核对。"],
  ];
  for (const [run, code, message] of cases) assert.throws(run, { code, message });
});

test("new profile creation cannot silently rotate an existing profile identifier", () => {
  const form = { profileId: "default", baseUrl: "https://ragflow.example", apiKey: "replacement", allowHttp: false };
  assert.throws(() => buildRagflowProfilePayload(form, false, ["default"]));
  assert.equal(buildRagflowProfilePayload(form, true, ["default"]).api_key, "replacement");
});

test("credential rotation preserves existing non-default connection timeouts", () => {
  const form = { profileId: "default", baseUrl: "https://ragflow.example", apiKey: "replacement", allowHttp: false };
  const timeouts = { connect_timeout: 12, read_timeout: 90, upload_timeout: 600 };
  assert.deepEqual(buildRagflowProfilePayload(form, true, ["default"], timeouts), {
    base_url: "https://ragflow.example", api_key: "replacement", allow_http: false, ...timeouts,
  });
});

test("attached datasets start unpublished and read-only unless explicitly selected", () => {
  const input = { mode: "attach" as const, name: " Course ", profileId: "default", datasetId: "dataset-1" };
  assert.deepEqual(buildKnowledgeConnection(input), {
    mode: "attach", payload: { name: "Course", profile_id: "default", dataset_id: "dataset-1", access_mode: "read_only", selected_document_ids: [] },
  });
  assert.equal(buildKnowledgeConnection({ ...input, accessMode: "manage_documents" }).payload.access_mode, "manage_documents");
  assert.throws(() => buildKnowledgeConnection({ ...input, datasetId: "" }));
});

test("managed creation only sends logical selection and never profile secrets", () => {
  const result = buildKnowledgeConnection({ mode: "managed", name: "Course", profileId: "default" });
  assert.deepEqual(result, { mode: "managed", payload: { name: "Course", profile_id: "default" } });
  assert.equal("api_key" in result.payload, false);
  assert.equal("base_url" in result.payload, false);
});

test("probe authorization requires explicit health, auth and contract verdicts", () => {
  const probe = { ok: true, health: "healthy", auth: "valid", contract: "compatible", version_unknown: true };
  assert.equal(canUseRagflowProfile(probe, false), true);
  assert.equal(canUseRagflowProfile(probe, true), false);
  assert.equal(canUseRagflowProfile({ ...probe, auth: "unknown" }, false), false);
  assert.equal(canUseRagflowProfile(null, false), false);
});

test("profile changes and newer page requests invalidate old async responses", () => {
  const requests = createConnectionRequests();
  const oldProbe = requests.begin("probe");
  const oldDatasets = requests.begin("datasets");
  requests.invalidate();
  const currentProbe = requests.begin("probe");
  assert.equal(requests.isCurrent(oldProbe), false);
  assert.equal(requests.isCurrent(oldDatasets), false);
  assert.equal(requests.isCurrent(currentProbe), true);
  const oldPage = requests.begin("datasets");
  const newPage = requests.begin("datasets");
  assert.equal(requests.isCurrent(oldPage), false);
  assert.equal(requests.isCurrent(newPage), true);
  assert.equal(requests.isCurrent(currentProbe), true);
});

test("HTTP 202 never becomes a completed creation even with a reconciled remote receipt", () => {
  assert.deepEqual(connectionResult({ id: kbRef, state: "RECONCILED", operation_id: "op-1", http_status: 202 }, "managed"), {
    kbRef, operationId: "op-1", status: "pending",
  });
  assert.equal(connectionResult({ id: kbRef, status: "ready", http_status: 200 }, "managed").status, "ready");
  assert.equal(connectionResult({ id: kbRef, state: "NEEDS_ATTENTION", operation_id: "op-1", http_status: 202 }, "managed").status, "attention");
  assert.throws(() => connectionResult({ id: "Course", status: "ready" }, "managed"));
});

test("connection panel keeps credentials ephemeral and clears the input after saving", () => {
  const source = readFileSync(path.resolve("components/knowledge/ragflow/RagflowConnectionPanel.tsx"), "utf8");
  assert.doesNotMatch(source, /(?:localStorage|sessionStorage|document\.cookie)\s*[.(]/);
  assert.match(source, /type="password"/);
  assert.match(source, /emptyRagflowProfileForm\(saved\)/);
  assert.match(source, /requests\.isCurrent\(ticket\)/);
  assert.match(source, /controller\.abort\(\)/);
});

test("connection panel translates input and API errors with the active translator", () => {
  const source = readFileSync(path.resolve("components/knowledge/ragflow/RagflowConnectionPanel.tsx"), "utf8");
  assert.match(source, /t\(`ragflow\.input\.\$\{failure\.code\}`,\s*\{\s*defaultValue:\s*failure\.message\s*\}\)/);
  assert.doesNotMatch(source, /ragflowErrorMessage\(failure\)/);
  assert.match(source, /ragflowErrorMessage\(failure,\s*t\)/);
});
