import assert from "node:assert/strict";
import test from "node:test";
import {
  ragflowApi,
  RagflowApiError,
  ragflowErrorMessage,
  newOperationKey,
  isOperationPending,
} from "../lib/ragflow-api";

const base = "/api/v1/knowledge";
const operation = {
  operation_id: "op-1", task_id: "op-1", action: "parse_document",
  state: "ACKNOWLEDGED", revision: 2, accepted: true, error_type: null, result: {},
};
const profile = {
  profile_id: "primary", base_url: "https://ragflow.example", allow_http: false,
  connect_timeout: 5, read_timeout: 60, upload_timeout: 300,
  credential_revision: 1, api_contract: "0.27.1", has_api_key: true,
};
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
async function withFetch(implementation: typeof fetch, run: () => Promise<void>) {
  const original = globalThis.fetch;
  globalThis.fetch = implementation;
  try { await run(); } finally { globalThis.fetch = original; }
}

test("read methods use exact routes, encoded stable refs, query fields and cancellation", async () => {
  const signal = new AbortController().signal;
  const cases: Array<[() => Promise<unknown>, string, unknown]> = [
    [() => ragflowApi.listProfiles({ signal }), "/rag-pipelines/ragflow/profiles", { profiles: [profile] }],
    [() => ragflowApi.getProfile("primary", { signal }), "/rag-pipelines/ragflow/profiles/primary", profile],
    [() => ragflowApi.listDatasets({ profile_id: "primary", page: 2, page_size: 10 }, { signal }), "/ragflow/datasets?profile_id=primary&page=2&page_size=10", { datasets: [{ id: "remote-1", name: "remote" }] }],
    [() => ragflowApi.getBinding("kb 空", { signal }), "/kb%20%E7%A9%BA/ragflow/binding", { id: "kb 空", name: "Name", ownership: "attached", access_mode: "read_only", read_only: true, can_manage: false, can_write: false }],
    [() => ragflowApi.listDocuments("kb", { page: 2, page_size: 10, keywords: "a & b" }, { signal }), "/kb/documents?page=2&page_size=10&keywords=a+%26+b", { documents: [], total: 0 }],
    [() => ragflowApi.getDocument("kb", "doc", { signal }), "/kb/documents/doc", { document_ref: "doc", name: "Document", state: "ready", visibility: "active", revision: 1, chunk_count: 2, progress: 1 }],
    [() => ragflowApi.listChunks("kb", "doc", { page: 1, page_size: 20 }, { signal }), "/kb/documents/doc/chunks?page=1&page_size=20", { chunks: [], total: 0 }],
    [() => ragflowApi.getChunk("kb", "doc", "chunk", { signal }), "/kb/documents/doc/chunks/chunk", { chunk_ref: "chunk", document_ref: "doc", content: "Text" }],
    [() => ragflowApi.getRetrievalSettings("kb", { signal }), "/kb/retrieval-settings", { page_size: 8, similarity_threshold: 0.2, vector_similarity_weight: 0.3, rerank_id: null }],
    [() => ragflowApi.listOperations("kb", { signal }), "/kb/operations", { operations: [operation] }],
    [() => ragflowApi.getOperation("kb", "op-1", { signal }), "/kb/operations/op-1", operation],
    [() => ragflowApi.getEvents("kb", "op-1", { after_id: 3 }, { signal }), "/kb/operations/op-1/events?after_id=3", { events: [{ id: 4, event_type: "reconciled", state: "RECONCILED", revision: 4, created_at: 1788660000 }] }],
  ];
  for (const [call, path, body] of cases) {
    await withFetch(async (input, init) => {
      assert.equal(input, base + path);
      assert.equal(init?.method, "GET");
      assert.equal(init?.signal, signal);
      assert.equal(init?.cache, "no-store");
      assert.equal(init?.credentials, "include");
      assert.equal(new Headers(init?.headers).get("Idempotency-Key"), null);
      return json(body);
    }, async () => { assert.deepEqual(await call(), body); });
  }
});

test("mutations preserve snake_case payloads and explicit idempotency keys without retry", async () => {
  const options = { idempotencyKey: "intent-123" };
  const cases: Array<[() => Promise<unknown>, string, string, unknown, unknown?]> = [
    [() => ragflowApi.saveProfile("primary", { base_url: profile.base_url, api_key: "test-key-only" }, options), "/rag-pipelines/ragflow/profiles/primary", "PUT", { base_url: profile.base_url, api_key: "test-key-only" }, profile],
    [() => ragflowApi.connect({ name: "KB", profile_id: "primary", dataset_id: "remote-1", selected_document_ids: [], access_mode: "read_only" }, options), "/connect-ragflow", "POST", { name: "KB", profile_id: "primary", dataset_id: "remote-1", selected_document_ids: [], access_mode: "read_only" }, { id: "kb", name: "KB", type: "ragflow", ownership: "attached", access_mode: "read_only", active_documents: 0 }],
    [() => ragflowApi.createDocument("kb", { name: "Empty", type: "empty" }, options), "/kb/documents", "POST", { name: "Empty", type: "empty" }],
    [() => ragflowApi.updateDocument("kb", "doc", { name: "New", meta_fields: { section: 1 } }, options), "/kb/documents/doc", "PATCH", { name: "New", meta_fields: { section: 1 } }],
    [() => ragflowApi.deleteDocument("kb", "doc", options), "/kb/documents/doc", "DELETE", undefined],
    [() => ragflowApi.parse("kb", "doc", { use_ingestion_pipeline: true, rebuild: true, confirm_rebuild: true }, options), "/kb/documents/doc/parse", "POST", { use_ingestion_pipeline: true, rebuild: true, confirm_rebuild: true }],
    [() => ragflowApi.cancel("kb", "doc", {}, options), "/kb/documents/doc/cancel", "POST", {}],
    [() => ragflowApi.addChunk("kb", "doc", { content: "Text", important_keywords: ["word"] }, options), "/kb/documents/doc/chunks", "POST", { content: "Text", important_keywords: ["word"] }],
    [() => ragflowApi.updateChunk("kb", "doc", "chunk", { content: "New", available: false }, options), "/kb/documents/doc/chunks/chunk", "PATCH", { content: "New", available: false }],
    [() => ragflowApi.deleteChunk("kb", "doc", "chunk", options), "/kb/documents/doc/chunks/chunk", "DELETE", undefined],
    [() => ragflowApi.setChunkAvailability("kb", "doc", "chunk", { available: false }, options), "/kb/documents/doc/chunks/chunk/availability", "PUT", { available: false }],
    [() => ragflowApi.setVisibility("kb", "doc", { visibility: "active", expected_revision: 3 }, options), "/kb/documents/doc/visibility", "PUT", { visibility: "active", expected_revision: 3 }, { document_ref: "doc", visibility: "active", revision: 4 }],
    [() => ragflowApi.saveRetrievalSettings("kb", { page_size: 4, similarity_threshold: 0.5, vector_similarity_weight: 0.6, rerank_id: null }, options), "/kb/retrieval-settings", "PUT", { page_size: 4, similarity_threshold: 0.5, vector_similarity_weight: 0.6, rerank_id: null }, { page_size: 4, similarity_threshold: 0.5, vector_similarity_weight: 0.6, rerank_id: null }],
    [() => ragflowApi.reconcile("kb", "op-1", { expected_revision: 2 }, options), "/kb/operations/op-1/reconcile", "POST", { expected_revision: 2 }],
    [() => ragflowApi.deleteDataset("kb", { confirm_name: "KB" }, options), "/kb/ragflow/dataset", "DELETE", { confirm_name: "KB" }],
  ];
  for (const [call, path, method, payload, response = operation] of cases) {
    let requests = 0;
    await withFetch(async (input, init) => {
      requests += 1;
      assert.equal(input, base + path);
      assert.equal(init?.method, method);
      assert.equal(new Headers(init?.headers).get("Idempotency-Key"), "intent-123");
      assert.deepEqual(init?.body === undefined ? undefined : JSON.parse(String(init.body)), payload);
      return json(response, response === operation ? 202 : 200);
    }, async () => { assert.deepEqual(await call(), response); });
    assert.equal(requests, 1);
  }
});

test("managed create preserves HTTP acceptance separately from operation completion", async () => {
  await withFetch(async (input, init) => {
    assert.equal(input, base + "/ragflow/create");
    assert.deepEqual(JSON.parse(String(init?.body)), { name: "KB", profile_id: "primary" });
    return json({ ...operation, id: "kb", name: "KB" }, 202);
  }, async () => {
    const result = await ragflowApi.create({ name: "KB", profile_id: "primary" });
    assert.equal(result.http_status, 202);
    assert.equal("state" in result && isOperationPending(result), true);
  });
  await withFetch(async () => json({ id: "kb", name: "KB", status: "ready" }), async () => {
    assert.deepEqual(await ragflowApi.create({ name: "KB", profile_id: "primary" }), { id: "kb", name: "KB", status: "ready", http_status: 200 });
  });
});

test("probe and retrieval POST are read-only, abortable and never carry a write key", async () => {
  const signal = new AbortController().signal;
  const probe = { ok: false, health: "healthy", auth: "invalid", contract: "unknown", version_unknown: true, contract_version: "0.27.1", checks: {} };
  await withFetch(async (input, init) => {
    assert.equal(input, base + "/ragflow/probe");
    assert.equal(init?.signal, signal);
    assert.equal(new Headers(init?.headers).get("Idempotency-Key"), null);
    assert.deepEqual(JSON.parse(String(init?.body)), { profile_id: "primary" });
    return json(probe);
  }, async () => { assert.deepEqual(await ragflowApi.probe({ profile_id: "primary" }, { signal, idempotencyKey: "ignored" }), probe); });
  const retrieval = { query: "Question", content: "Text", answer: "Text", sources: [], provider: "ragflow", content_kind: "retrieval_context", mode: "hybrid" };
  await withFetch(async (input, init) => {
    assert.equal(input, base + "/kb/retrieval-test");
    assert.equal(init?.signal, signal);
    assert.deepEqual(JSON.parse(String(init?.body)), { query: "Question", preview_document_refs: ["doc"] });
    return json(retrieval);
  }, async () => { assert.deepEqual(await ragflowApi.retrieve("kb", { query: "Question", preview_document_refs: ["doc"] }, { signal }), retrieval); });
});

test("uploads use repeated multipart files with no manual content type and retain every operation", async () => {
  const files = [new File(["A"], "a.txt"), new File(["B"], "b.txt")];
  const result = { accepted: true, operations: [operation, { ...operation, operation_id: "op-2", state: "RESULT_UNKNOWN" }] };
  await withFetch(async (input, init) => {
    assert.equal(input, base + "/kb/documents/upload");
    assert.equal(init?.method, "POST");
    assert.equal(new Headers(init?.headers).get("Content-Type"), null);
    assert.equal(new Headers(init?.headers).get("Idempotency-Key"), "upload-intent");
    assert.ok(init?.body instanceof FormData);
    assert.deepEqual(init.body.getAll("files").map((entry) => typeof entry === "string" ? entry : entry.name), ["a.txt", "b.txt"]);
    return json(result, 202);
  }, async () => { assert.deepEqual(await ragflowApi.upload("kb", files, { idempotencyKey: "upload-intent" }), result); });
});

test("download returns bytes through authenticated fetch without a key in its URL", async () => {
  const signal = new AbortController().signal;
  await withFetch(async (input, init) => {
    assert.equal(input, base + "/kb/documents/doc/download");
    assert.equal(init?.credentials, "include");
    assert.equal(init?.signal, signal);
    assert.equal(init?.cache, "no-store");
    return new Response("document bytes", { headers: { "Content-Type": "text/plain" } });
  }, async () => { assert.equal(await (await ragflowApi.downloadDocument("kb", "doc", { signal })).text(), "document bytes"); });
});

test("HTTP and retrieval business errors use fixed safe messages, never raw body or secret", async () => {
  for (const [status, body, code] of [
    [403, { detail: "secret-key raw private URL" }, "forbidden"],
    [409, { detail: "ragflow_operation_conflict" }, "ragflow_operation_conflict"],
    [422, { detail: [{ msg: "bad", input: "secret-key" }] }, "validation_error"],
    [502, { detail: "upstream_error", message: "secret-key" }, "upstream_error"],
    [200, { error_type: "unavailable_chunk", answer: "secret-key" }, "unavailable_chunk"],
  ] as const) {
    let count = 0;
    await withFetch(async () => { count += 1; return json(body, status); }, async () => {
      await assert.rejects(ragflowApi.retrieve("kb", { query: "Question" }), (error: unknown) => {
        assert.ok(error instanceof RagflowApiError);
        assert.equal(error.status, status);
        assert.equal(error.code, code);
        assert.equal(error.message.includes("secret-key"), false);
        assert.equal(JSON.stringify(error).includes("secret-key"), false);
        assert.equal(ragflowErrorMessage(error), error.message);
        return true;
      });
    });
    assert.equal(count, 1);
  }
  assert.equal(ragflowErrorMessage(new Error("secret-key")), ragflowErrorMessage(null));
});

test("failed writes never resend automatically and network exceptions are redacted", async () => {
  let calls = 0;
  await withFetch(async () => { calls += 1; throw new Error("https://internal secret-key"); }, async () => {
    await assert.rejects(ragflowApi.deleteDocument("kb", "doc"), (error: unknown) => {
      assert.ok(error instanceof RagflowApiError);
      assert.equal(error.code, "transport_error");
      assert.equal(error.status, 0);
      assert.equal(error.message.includes("internal"), false);
      assert.equal("cause" in error, false);
      return true;
    });
  });
  assert.equal(calls, 1);
});

test("abort stays distinguishable without leaking arbitrary exception text", async () => {
  await withFetch(async () => { throw new DOMException("secret-key", "AbortError"); }, async () => {
    await assert.rejects(ragflowApi.listProfiles(), (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.name, "AbortError");
      assert.equal(error.message.includes("secret-key"), false);
      return true;
    });
  });
});

test("malformed JSON and unknown operation states fail closed", async () => {
  await withFetch(async () => new Response("<html>private error</html>"), async () => {
    await assert.rejects(ragflowApi.listProfiles(), { code: "invalid_response" });
  });
  await withFetch(async () => json({ ...operation, state: "FUTURE_SUCCESS" }), async () => {
    await assert.rejects(ragflowApi.getOperation("kb", "op-1"), { code: "invalid_response" });
  });
  await withFetch(async () => json({ accepted: true }), async () => {
    await assert.rejects(ragflowApi.parse("kb", "doc"), { code: "invalid_response" });
  });
});

test("download errors remain errors rather than JSON files", async () => {
  await withFetch(async () => json({ detail: "not_found" }, 404), async () => {
    await assert.rejects(ragflowApi.downloadDocument("kb", "doc"), { status: 404, code: "not_found" });
  });
  await withFetch(async () => json({ error_type: "upstream_error" }), async () => {
    await assert.rejects(ragflowApi.downloadDocument("kb", "doc"), { code: "upstream_error" });
  });
  await withFetch(async () => new Response('{"source":"legitimate JSON document"}', { headers: { "Content-Type": "application/json", "Content-Disposition": 'attachment; filename="source.json"' } }), async () => {
    assert.equal(await (await ragflowApi.downloadDocument("kb", "doc")).text(), '{"source":"legitimate JSON document"}');
  });
});

test("profiles discard accidental secret fields instead of retaining them in UI state", async () => {
  await withFetch(async () => json({ ...profile, api_key: "secret-key", secret_ref: "private-ref" }), async () => {
    assert.deepEqual(await ragflowApi.getProfile("primary"), profile);
  });
});

test("stable refs reject traversal and upload rejects empty selection without any request", async () => {
  let calls = 0;
  await withFetch(async () => { calls += 1; return json({}); }, async () => {
    for (const kb of ["", ".", "..", "../other", "kb/other", "kb?x=1", "kb#fragment", "kb\\other"]) {
      await assert.rejects(async () => ragflowApi.getBinding(kb), { code: "validation_error" });
    }
    await assert.rejects(ragflowApi.upload("kb", []), { code: "validation_error" });
  });
  assert.equal(calls, 0);
});

test("operation helpers keep uncertainty pending and generate fresh intent keys", () => {
  for (const state of ["INTENT_RECORDED", "SEND_STARTED", "ACKNOWLEDGED", "RESULT_UNKNOWN", "RECONCILING", "unexpected"]) assert.equal(isOperationPending(state), true);
  for (const state of ["RECONCILED", "FAILED", "NEEDS_ATTENTION"]) assert.equal(isOperationPending(state), false);
  assert.equal(isOperationPending({ state: "ACKNOWLEDGED" }), true);
  const first = newOperationKey();
  assert.match(first, /^[a-zA-Z0-9_-]{16,128}$/);
  assert.notEqual(first, newOperationKey());
});

test("operation keys work on HTTP LAN origins without crypto.randomUUID", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: { getRandomValues: (bytes: Uint8Array) => { bytes.fill(123); return bytes; } },
  });
  try { assert.match(newOperationKey(), /^[a-zA-Z0-9_-]{16,128}$/); }
  finally { if (original) Object.defineProperty(globalThis, "crypto", original); }
});

test("download body stream failures never expose raw exception messages", async () => {
  await withFetch(async () => new Response(new ReadableStream({
    start(controller) { controller.error(new Error("private URL secret-key")); },
  })), async () => {
    await assert.rejects(ragflowApi.downloadDocument("kb", "doc"), (error: unknown) => {
      assert.ok(error instanceof RagflowApiError);
      assert.equal(error.code, "transport_error");
      assert.equal(error.message.includes("secret-key"), false);
      return true;
    });
  });
});

test("error translation receives only an allowlisted key and fixed safe fallback", () => {
  const calls: Array<{ key: string; options: { defaultValue: string } }> = [];
  const translate = (key: string, options: { defaultValue: string }) => {
    calls.push({ key, options });
    return `translated:${key}`;
  };
  assert.equal(ragflowErrorMessage(new RagflowApiError(403, "forbidden"), translate), "translated:ragflow.error.forbidden");
  for (const error of [new Error("private-url secret-key"), "private-url secret-key", { code: "auth_error", message: "secret-key" }]) {
    assert.equal(ragflowErrorMessage(error, translate), "translated:ragflow.error.request_failed");
  }
  assert.equal(ragflowErrorMessage("parse_failed", translate), "translated:ragflow.error.parse_failed");
  assert.equal(JSON.stringify(calls).includes("secret-key"), false);
  assert.equal(JSON.stringify(calls).includes("private-url"), false);
  assert.equal(calls[0].options.defaultValue, ragflowErrorMessage(new RagflowApiError(403, "forbidden")));
  assert.deepEqual(Object.keys(calls[0].options), ["defaultValue"]);
});
