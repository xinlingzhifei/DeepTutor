# RAGFlow API adaptation implementation plan

Goal: implement the approved server-side RAGFlow integration with isolated credentials, generation-pinned retrieval, document/chunk APIs and durable operations.

Architecture: the RAGFlow HTTP client owns the upstream wire contract. Knowledge-domain services own binding, visibility, operations and recovery. Existing RAGService and authenticated knowledge routes remain the application entry points.

Tech stack: Python, httpx, FastAPI, SQLite, pytest; fixed RAGFlow v0.27.1 source contract.

This checklist executes the approved DESIGN.md. The user has authorized implementation without repeated design confirmations. Existing unrelated workspace edits are preserved; no automatic Git commit, push or deployment is part of this work.

- [x] HTTP contract: implement `pipelines/ragflow/config.py`, `client.py`, `probe.py`; verify real request construction, business error envelopes, bounded reads, no write retry, and safe identifiers using MockTransport in `tests/services/rag/test_ragflow_client.py`.
- [x] Durable state: implement `knowledge/ragflow/operations.py`; verify actual SQLite persistence, scoped visibility, idempotency conflicts, lease takeover, CAS and opaque references in `tests/knowledge/test_ragflow_operations.py`.
- [x] Platform binding: implement `knowledge/ragflow/profiles.py`, `binding.py`, manager registration and KB capabilities. Verify secret separation, shared-owner resolution, rebind/credential-rotation races and no local raw-folder ownership.
- [x] Retrieval: implement `pipelines/ragflow/pipeline.py` and factory routing. Verify explicit active document filters, disabled enhancements, response provenance validation, opaque citations and before/after authorization checks in `tests/services/rag/test_ragflow_pipeline.py`.
- [x] Lifecycle API: implement `knowledge/ragflow/lifecycle.py`, `recovery.py`, `api/routers/ragflow_knowledge.py`. Verify attach/create, upload staging, parse/cancel, Chunk operations, visibility, download and durable status using route and multi-user authorization tests.
- [x] Integrate existing create/upload and application lifespan with focused compatibility tests. Preserve other provider contracts and ensure opaque resource references remain scoped to the original binding.
- [x] Run focused new tests, existing RAG/knowledge/access/router regression tests, scoped Ruff check/format and compile checks. Final combined run: 539 passed, 2 skipped, exit 0. See [VERIFICATION.md](VERIFICATION.md) for commands and evidence boundaries.
- [x] Update README with implemented APIs, examples and explicit operational limits. Do not report mock-based success as real deployment or document parsing success.
- [x] Connect Knowledge Center, engine settings and create modal to RAGFlow profiles and stable KB references. Preserve existing providers and unrelated UI changes.
- [x] Implement document/Chunk lifecycle, publication, retrieval tests, durable-operation presentation and capability-based management controls. Include stale-response, idempotency, read-only and pending-state regression checks.
- [x] Verify frontend types, node regression tests, scoped lint, translations and browser flows with isolated API fixtures. Real RAGFlow deployment remains outside this verification.

Verification commands are executed from the project root:

```powershell
python -m pytest tests/services/rag/test_ragflow_client.py tests/services/rag/test_ragflow_pipeline.py tests/knowledge/test_ragflow_operations.py tests/api/test_ragflow_knowledge.py -q
python -m pytest tests/services/rag/test_lightrag_server_pipeline.py tests/services/rag/test_ima_pipeline.py tests/services/rag/test_retrieval_view.py tests/knowledge tests/multi_user/test_knowledge_generation_identity.py tests/multi_user/test_rag_tool_no_kb_fallback.py tests/api/test_knowledge_router.py tests/tools/test_rag_tool.py -q
```
