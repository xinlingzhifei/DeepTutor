# OpenMAIC 引擎契约与持久化任务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以最小、可回馈上游的覆盖层补齐 OpenMAIC 真正的两阶段生成、服务鉴权和取消能力，并在 yFeiSTAI 建立可恢复、按租户公平且具备配额账本的任务内核。

**Architecture:** OpenMAIC 固定在提交 `0cf2a330411681190e89f48e20f305345ff99f87`，构建时把 `integrations/openmaic/overlay` 覆盖到上游源码，不复制完整应用。yFeiSTAI 在一个 PostgreSQL 事务中创建租户任务、配额预留和 Outbox；Dispatcher 投递到数据库队列，Worker 通过租约与槽位表领取任务、选择共享或独立数据面、调用 OpenMAIC、校验产物并晋级到 yFeiSTAI 对象存储。

**Tech Stack:** OpenMAIC 0.3.1、Next.js Route Handlers、TypeScript、Node crypto、Vitest、FastAPI、httpx、SQLAlchemy async、PostgreSQL `FOR UPDATE SKIP LOCKED`、pytest

---

## Task 1: 固化跨语言课堂契约

**Files:**

- Create: `contracts/classroom/teaching-brief.schema.json`
- Create: `contracts/classroom/generation-request.schema.json`
- Create: `contracts/classroom/outline-bundle.schema.json`
- Create: `contracts/classroom/classroom-document.schema.json`
- Create: `contracts/classroom/generation-job.schema.json`
- Create: `contracts/classroom/export-request.schema.json`
- Create: `contracts/classroom/export-job.schema.json`
- Create: `deeptutor/teaching/contracts.py`
- Create: `tests/teaching/test_contracts.py`
- Create: `scripts/verify_classroom_contracts.py`

- [ ] Step 1: 写契约失败测试

```python
def test_generation_request_never_contains_provider_secret():
    from deeptutor.teaching.contracts import GenerationRequest

    fields = set(GenerationRequest.model_fields)
    assert "provider_api_key" not in fields
    assert "provider_base_url" not in fields
    assert {"tenant_id", "job_id", "phase", "teaching_brief"} <= fields


def test_confirmed_outline_hash_is_required_for_content_phase():
    with pytest.raises(ValidationError):
        GenerationRequest(
            tenant_id="t1",
            job_id="j1",
            phase="content",
            teaching_brief=valid_brief(),
        )


def test_export_contract_uses_only_supported_formats():
    from deeptutor.teaching.contracts import ExportFormat

    assert {item.value for item in ExportFormat} == {
        "classroom_zip",
        "pptx",
        "offline_html",
        "mp4",
    }
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_contracts.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现 Pydantic 契约

```python
class GenerationRequest(BaseModel):
    schema_version: Literal["1.0"]
    tenant_id: str
    job_id: str
    idempotency_key: str
    phase: Literal["outline", "content", "micro"]
    teaching_brief: TeachingBrief
    confirmed_outline: OutlineBundle | None = None
    confirmed_outline_sha256: str | None = None
    data_plane_route_id: str
    priority: Literal["student_micro", "interaction", "teacher", "full", "batch"]

    @model_validator(mode="after")
    def require_confirmed_outline(self) -> "GenerationRequest":
        if self.phase == "content":
            if self.confirmed_outline is None or self.confirmed_outline_sha256 is None:
                raise ValueError("content phase requires a confirmed outline and hash")
        return self
```

`TeachingBrief` 必须包含来源模式、来源片段、引用、权限摘要、知识点、目标、时长和内容哈希。`ClassroomDocument` 必须包含 OpenMAIC DSL 版本、Stage、Scenes、`sourceRefs`、知识点映射、媒体清单、生成元数据和校验结果。`ExportRequest` 必须固定课堂文档 SHA-256、媒体清单 SHA-256、格式、语言和导出策略；支持格式精确为 `classroom_zip`、`pptx`、`offline_html`、`mp4`，不得携带对象存储或 Provider 凭证。

- [ ] Step 4: 生成并校验 JSON Schema

`scripts/verify_classroom_contracts.py` 用 Pydantic 输出结果与已提交 JSON Schema 做规范化比较；发现漂移返回非零退出码。

Run:

```powershell
python scripts/verify_classroom_contracts.py
python -m pytest tests/teaching/test_contracts.py -q
```

Expected: PASS。

- [ ] Step 5: 提交

```powershell
git add contracts/classroom/teaching-brief.schema.json contracts/classroom/generation-request.schema.json contracts/classroom/outline-bundle.schema.json contracts/classroom/classroom-document.schema.json contracts/classroom/generation-job.schema.json contracts/classroom/export-request.schema.json contracts/classroom/export-job.schema.json deeptutor/teaching/contracts.py tests/teaching/test_contracts.py scripts/verify_classroom_contracts.py
git commit -m "feat(classroom): define versioned engine contracts"
```

## Task 2: 建立锁版 OpenMAIC 覆盖层和服务签名

**Files:**

- Create: `integrations/openmaic/UPSTREAM.json`
- Create: `integrations/openmaic/Dockerfile`
- Create: `integrations/openmaic/overlay/lib/yfeistai/contracts.ts`
- Create: `integrations/openmaic/overlay/lib/yfeistai/service-auth.ts`
- Create: `integrations/openmaic/overlay/app/api/yfeistai/v1/health/route.ts`
- Create: `integrations/openmaic/overlay/tests/yfeistai/service-auth.test.ts`
- Create: `scripts/verify_openmaic_overlay.py`
- Create: `tests/scripts/test_openmaic_overlay.py`

- [ ] Step 1: 写锁版和签名失败测试

```typescript
test("rejects stale and body-mismatched service signatures", async () => {
  const signed = signServiceRequest({
    secret: "test-secret",
    method: "POST",
    path: "/api/yfeistai/v1/outlines",
    tenantId: "tenant-a",
    jobId: "job-a",
    timestamp: 1_800_000_000,
    body: '{"schema_version":"1.0"}',
  });

  expect(
    verifyServiceRequest(signed, {
      secret: "test-secret",
      nowSeconds: 1_800_000_061,
      body: '{"schema_version":"1.0"}',
    }),
  ).toEqual({ ok: false, reason: "expired" });
  expect(
    verifyServiceRequest(signed, {
      secret: "test-secret",
      nowSeconds: 1_800_000_000,
      body: '{"schema_version":"2.0"}',
    }),
  ).toEqual({ ok: false, reason: "signature" });
});
```

- [ ] Step 2: 运行覆盖层测试并确认失败

Build test command:

```powershell
python scripts/verify_openmaic_overlay.py --test service-auth
```

Expected: FAIL，覆盖层尚未组装。

- [ ] Step 3: 固定上游来源

`UPSTREAM.json` 内容必须为：

```json
{
  "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
  "commit": "0cf2a330411681190e89f48e20f305345ff99f87",
  "appVersion": "0.3.1"
}
```

Docker 构建阶段克隆该提交、校验 `git rev-parse HEAD`，再复制覆盖层。最终镜像标签写入提交短 SHA；生产 Compose 使用镜像摘要，不在目标服务器运行 Git 克隆。

- [ ] Step 4: 实现签名协议

签名原文固定为：

```text
METHOD
PATH
TENANT_ID
JOB_ID
UNIX_TIMESTAMP
IDEMPOTENCY_KEY
SHA256(BODY)
```

使用 `HMAC-SHA256` 和 `timingSafeEqual`。时间窗口为 60 秒；写请求必须有幂等键；租户、任务、动作和正文全部参与签名。密钥只从 `/run/secrets/openmaic_service_secret` 读取。

- [ ] Step 5: 实现能力健康接口

`GET /api/yfeistai/v1/health` 返回：

```json
{
  "service": "openmaic",
  "upstreamCommit": "0cf2a330411681190e89f48e20f305345ff99f87",
  "appVersion": "0.3.1",
  "contractVersions": ["1.0"],
  "capabilities": ["outline", "content", "micro", "export", "cancel", "artifact-manifest"],
  "exportFormats": ["classroom_zip", "pptx", "offline_html", "mp4"]
}
```

健康接口只在容器私有网络可达；业务接口全部要求服务签名。

- [ ] Step 6: 运行校验

Run:

```powershell
python scripts/verify_openmaic_overlay.py --test service-auth
python -m pytest tests/scripts/test_openmaic_overlay.py -q
```

Expected: PASS；验证器同时确认覆盖层没有新增登录页面、账号表或客户端 Provider 参数。

- [ ] Step 7: 提交

```powershell
git add integrations/openmaic/UPSTREAM.json integrations/openmaic/Dockerfile integrations/openmaic/overlay/lib/yfeistai/contracts.ts integrations/openmaic/overlay/lib/yfeistai/service-auth.ts integrations/openmaic/overlay/app/api/yfeistai/v1/health/route.ts integrations/openmaic/overlay/tests/yfeistai/service-auth.test.ts scripts/verify_openmaic_overlay.py tests/scripts/test_openmaic_overlay.py
git commit -m "feat(openmaic): add pinned authenticated service overlay"
```

## Task 3: 增加独立大纲生成接口

**Files:**

- Create: `integrations/openmaic/overlay/lib/yfeistai/outline-generation.ts`
- Create: `integrations/openmaic/overlay/lib/yfeistai/job-store.ts`
- Create: `integrations/openmaic/overlay/app/api/yfeistai/v1/outlines/route.ts`
- Create: `integrations/openmaic/overlay/app/api/yfeistai/v1/outlines/[jobId]/route.ts`
- Create: `integrations/openmaic/overlay/tests/yfeistai/outline-generation.test.ts`

- [ ] Step 1: 写双阶段边界失败测试

```typescript
test("outline job stops before scene generation", async () => {
  const generateScenes = vi.fn();
  const result = await generateOutlineJob(validRequest(), {
    generateOutlines: async () => validOutlineBundle(),
    generateScenes,
  });

  expect(result.status).toBe("succeeded");
  expect(result.result?.outline.scenes).toHaveLength(4);
  expect(generateScenes).not.toHaveBeenCalled();
});

test("source-grounded request keeps source refs", async () => {
  const result = await generateOutlineJob(groundedRequest(), fakeDependencies());
  expect(result.result?.outline.scenes.every((scene) => scene.sourceRefs.length > 0)).toBe(true);
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python scripts/verify_openmaic_overlay.py --test outline-generation
```

Expected: FAIL。

- [ ] Step 3: 实现大纲任务

复用上游：

```text
lib/generation/outline-generator.ts
lib/server/resolve-model.ts
lib/web-search/*
```

不得调用上游 `generateClassroom()`，因为该函数会继续生成场景。输出规范化为 `OutlineBundle`，包括课程标题、语言指令、场景大纲、知识点覆盖、`sourceRefs`、估算场景数和契约哈希。

- [ ] Step 4: 实现提交与轮询

```text
POST /api/yfeistai/v1/outlines
GET  /api/yfeistai/v1/outlines/{jobId}
```

POST 使用 yFeiSTAI 提供的 `job_id` 和幂等键；重复请求返回同一任务。请求体不得包含 Provider ID、API Key 或任意 Provider Base URL。Provider 只由当前 OpenMAIC 数据面的服务端配置解析。

- [ ] Step 5: 运行测试

Run:

```powershell
python scripts/verify_openmaic_overlay.py --test outline-generation
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git --literal-pathspecs add integrations/openmaic/overlay/lib/yfeistai/outline-generation.ts integrations/openmaic/overlay/lib/yfeistai/job-store.ts integrations/openmaic/overlay/app/api/yfeistai/v1/outlines/route.ts integrations/openmaic/overlay/app/api/yfeistai/v1/outlines/[jobId]/route.ts integrations/openmaic/overlay/tests/yfeistai/outline-generation.test.ts
git commit -m "feat(openmaic): expose outline-only generation"
```

## Task 4: 增加确认大纲后的内容生成、受控导出、产物清单和取消

**Files:**

- Create: `integrations/openmaic/overlay/lib/yfeistai/content-generation.ts`
- Create: `integrations/openmaic/overlay/lib/yfeistai/export-generation.ts`
- Create: `integrations/openmaic/overlay/lib/yfeistai/artifact-manifest.ts`
- Create: `integrations/openmaic/overlay/app/api/yfeistai/v1/classrooms/route.ts`
- Create: `integrations/openmaic/overlay/app/api/yfeistai/v1/classrooms/[jobId]/route.ts`
- Create: `integrations/openmaic/overlay/app/api/yfeistai/v1/jobs/[jobId]/cancel/route.ts`
- Create: `integrations/openmaic/overlay/app/api/yfeistai/v1/artifacts/[jobId]/[...path]/route.ts`
- Create: `integrations/openmaic/overlay/app/api/yfeistai/v1/exports/route.ts`
- Create: `integrations/openmaic/overlay/app/api/yfeistai/v1/exports/[jobId]/route.ts`
- Create: `integrations/openmaic/overlay/tests/yfeistai/content-generation.test.ts`
- Create: `integrations/openmaic/overlay/tests/yfeistai/export-generation.test.ts`
- Create: `integrations/openmaic/overlay/tests/yfeistai/cancel.test.ts`
- Create: `integrations/openmaic/overlay/tests/yfeistai/artifact-manifest.test.ts`

- [ ] Step 1: 写确认哈希和取消失败测试

```typescript
test("rejects a changed outline after confirmation", async () => {
  const request = contentRequest({
    confirmedOutlineSha256: sha256(canonicalJson(validOutlineBundle())),
    confirmedOutline: changedOutlineBundle(),
  });

  await expect(generateContentJob(request, fakeDependencies())).rejects.toThrow(
    "confirmed outline hash mismatch",
  );
});

test("canceled jobs never publish a succeeded result", async () => {
  const job = await startBlockingContentJob();
  await cancelJob(job.id);
  releaseBlockingGenerator();
  expect((await readJob(job.id)).status).toBe("canceled");
});

test("export is bound to the submitted document and media hashes", async () => {
  const request = exportRequest({
    documentSha256: sha256(canonicalJson(validClassroomDocument())),
    mediaManifestSha256: "wrong-hash",
    format: "classroom_zip",
  });
  await expect(generateExportJob(request, fakeDependencies())).rejects.toThrow(
    "media manifest hash mismatch",
  );
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python scripts/verify_openmaic_overlay.py --test content-generation
python scripts/verify_openmaic_overlay.py --test cancel
python scripts/verify_openmaic_overlay.py --test export-generation
```

Expected: FAIL。

- [ ] Step 3: 实现从已确认大纲生成

复用上游场景和媒体能力：

```text
lib/generation/scene-generator.ts
lib/server/classroom-media-generation.ts
lib/media/video-manifest.ts
lib/server/resolve-model.ts
```

禁止重新生成大纲。每个场景完成后检查取消信号；取消后的临时产物保留为诊断输入但不得标记成功。

- [ ] Step 4: 规范化可移植课堂文档

输出的 `ClassroomDocument` 对场景内容采用明确的可移植联合类型：

```typescript
type PortableSceneContent =
  | SlideContent
  | QuizContent
  | {
      type: "interactive";
      html: string;
      bridgeVersion: "1.0";
      sandbox: { allowScripts: true; allowSameOrigin: false };
    }
  | {
      type: "pbl";
      scenario: string;
      roles: Array<{ id: string; name: string; brief: string }>;
      milestones: Array<{ id: string; title: string; rubric: string }>;
    };
```

生成任务的产物清单至少包含 DSL JSON 和媒体文件；导出任务的产物清单包含导出文件。两类清单都记录每个文件的 SHA-256、字节数、MIME、临时下载路径和过期时间。

- [ ] Step 5: 实现可复用导出流水线

从锁版上游的 `lib/export`、`lib/video-export` 和 `render-service` 提取无 UI 的导出入口，并由覆盖层统一校验输入哈希。格式行为固定为：

```text
classroom_zip -> 完整 Stage、Scene、Action 和媒体的 .maic.zip
pptx          -> 可编辑幻灯片；互动/PBL 场景附带静态预览和原课堂链接说明
offline_html  -> 资源内联、无公网依赖的可离线播放包
mp4           -> 通过私网 openmaic-render 容器异步渲染；未配置时明确失败，不降级为假 MP4
```

导出只接受 yFeiSTAI 提交的规范 `ClassroomDocument` 和清单内媒体；禁止读取 OpenMAIC 浏览器存储、任意本地路径或任意 URL。SSRF、压缩包路径穿越、压缩炸弹、外链内联失败和 MP4 渲染超时均返回稳定错误码。所有输出进入该引擎任务的临时目录并写入同一份产物清单。

- [ ] Step 6: 实现接口

```text
POST /api/yfeistai/v1/classrooms
GET  /api/yfeistai/v1/classrooms/{jobId}
POST /api/yfeistai/v1/exports
GET  /api/yfeistai/v1/exports/{jobId}
POST /api/yfeistai/v1/jobs/{jobId}/cancel
GET  /api/yfeistai/v1/artifacts/{jobId}/{path}
```

微课堂使用同一个 `/classrooms` 接口但 `phase=micro`，内部仍生成完整、可校验的 `ClassroomDocument`。
导出接口与产物读取接口同样要求服务签名；产物读取只允许该任务清单中规范化后的相对路径，并拒绝绝对路径、`..` 和符号链接逃逸。

- [ ] Step 7: 运行覆盖层测试和构建

Run:

```powershell
python scripts/verify_openmaic_overlay.py --test content-generation
python scripts/verify_openmaic_overlay.py --test cancel
python scripts/verify_openmaic_overlay.py --test artifact-manifest
python scripts/verify_openmaic_overlay.py --test export-generation
python scripts/verify_openmaic_overlay.py --build
```

Expected: PASS。

- [ ] Step 8: 提交

```powershell
git --literal-pathspecs add integrations/openmaic/overlay/lib/yfeistai/content-generation.ts integrations/openmaic/overlay/lib/yfeistai/export-generation.ts integrations/openmaic/overlay/lib/yfeistai/artifact-manifest.ts integrations/openmaic/overlay/app/api/yfeistai/v1/classrooms/route.ts integrations/openmaic/overlay/app/api/yfeistai/v1/classrooms/[jobId]/route.ts integrations/openmaic/overlay/app/api/yfeistai/v1/exports/route.ts integrations/openmaic/overlay/app/api/yfeistai/v1/exports/[jobId]/route.ts integrations/openmaic/overlay/app/api/yfeistai/v1/jobs/[jobId]/cancel/route.ts integrations/openmaic/overlay/app/api/yfeistai/v1/artifacts/[jobId]/[...path]/route.ts integrations/openmaic/overlay/tests/yfeistai/content-generation.test.ts integrations/openmaic/overlay/tests/yfeistai/export-generation.test.ts integrations/openmaic/overlay/tests/yfeistai/cancel.test.ts integrations/openmaic/overlay/tests/yfeistai/artifact-manifest.test.ts
git commit -m "feat(openmaic): generate and export classroom artifacts"
```

## Task 5: 实现 yFeiSTAI OpenMAIC 客户端与数据面路由

**Files:**

- Create: `deeptutor/teaching/openmaic/__init__.py`
- Create: `deeptutor/teaching/openmaic/auth.py`
- Create: `deeptutor/teaching/openmaic/client.py`
- Create: `deeptutor/teaching/openmaic/data_planes.py`
- Create: `tests/teaching/openmaic/test_auth.py`
- Create: `tests/teaching/openmaic/test_client.py`
- Create: `tests/teaching/openmaic/test_data_planes.py`

- [ ] Step 1: 写签名、能力和路由失败测试

```python
async def test_dedicated_tenant_never_falls_back_to_shared_plane(router):
    router.add(
        tenant_id="tenant-private",
        mode="dedicated",
        base_url="http://openmaic-private:3000",
        secret_ref="openmaic/private",
    )
    router.mark_unhealthy("openmaic/private")

    with pytest.raises(DataPlaneUnavailable):
        await router.resolve("tenant-private")


async def test_client_rejects_incompatible_contract(mock_transport):
    mock_transport.health(contract_versions=["2.0"])
    client = OpenMAICClient(mock_transport)
    with pytest.raises(IncompatibleOpenMAIC):
        await client.assert_compatible()
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/openmaic -q
```

Expected: FAIL。

- [ ] Step 3: 实现服务请求签名和 HTTP 客户端

```python
class OpenMAICClient:
    async def health(self) -> OpenMAICHealth: ...
    async def submit_outline(self, request: GenerationRequest) -> EngineJob: ...
    async def submit_content(self, request: GenerationRequest) -> EngineJob: ...
    async def submit_export(self, request: ExportRequest) -> EngineJob: ...
    async def poll(self, engine_job_id: str) -> EngineJob: ...
    async def cancel(self, engine_job_id: str) -> None: ...
    async def stream_artifact(self, path: str) -> AsyncIterator[bytes]: ...
```

每个请求使用 60 秒服务签名；连接、读取和总超时分别配置；轮询使用有上限的指数退避和抖动。日志只记录 `tenant_id + job_id + route_id`，不记录来源正文或密钥。

- [ ] Step 4: 实现数据面解析

`DataPlaneRoute` 只保存 `base_url`、模式、能力状态和 `secret_ref`。Secret Resolver 从挂载文件读取密钥。标准租户解析到共享池；独立租户不可在失败时回退共享池。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/teaching/openmaic -q
python -m ruff check deeptutor/teaching/openmaic tests/teaching/openmaic
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/openmaic/__init__.py deeptutor/teaching/openmaic/auth.py deeptutor/teaching/openmaic/client.py deeptutor/teaching/openmaic/data_planes.py tests/teaching/openmaic/test_auth.py tests/teaching/openmaic/test_client.py tests/teaching/openmaic/test_data_planes.py
git commit -m "feat(teaching): add OpenMAIC data plane client"
```

## Task 6: 建立 Outbox、配额账本、持久化队列和公平槽位

**Files:**

- Create: `deeptutor/teaching/models/jobs.py`
- Create: `deeptutor/teaching/repositories/jobs.py`
- Create: `deeptutor/teaching/quota.py`
- Create: `deeptutor/teaching/scheduler.py`
- Create: `deeptutor/teaching/dispatcher.py`
- Create: `deeptutor/teaching/migrations/versions/20260728_0002_generation_jobs.py`
- Create: `tests/teaching/test_quota.py`
- Create: `tests/teaching/test_scheduler.py`
- Create: `tests/teaching/integration/test_outbox_dispatch.py`
- Create: `tests/teaching/integration/test_fair_scheduler.py`

- [ ] Step 1: 写原子性、公平和配额失败测试

```python
async def test_job_and_quota_reservation_roll_back_together(repository):
    repository.fail_outbox_insert = True
    with pytest.raises(RuntimeError):
        await repository.create_job_and_reserve(valid_request())

    assert await repository.count_jobs() == 0
    assert await repository.quota_balance("tenant-a") == initial_balance


async def test_busy_tenant_cannot_take_another_tenants_slots(scheduler):
    await scheduler.enqueue_many("tenant-a", count=20, priority="batch")
    await scheduler.enqueue_many("tenant-b", count=1, priority="teacher")

    claimed = [await scheduler.claim("shared") for _ in range(3)]
    assert any(job.tenant_id == "tenant-b" for job in claimed)
    assert sum(job.tenant_id == "tenant-a" for job in claimed) <= 2
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_quota.py tests/teaching/test_scheduler.py -q
```

Expected: FAIL。

- [ ] Step 3: 增加表和状态机

平台 Schema：

```text
outbox_messages
generation_queue
generation_slots
tenant_scheduler_state
```

租户 Schema：

```text
generation_jobs
quota_ledger
```

任务状态严格遵循：

```text
generation:
created -> quota_reserved -> queued -> generating_outline
-> awaiting_confirmation -> generating_content -> validating
-> materializing -> succeeded | failed | canceled

export:
created -> quota_reserved -> queued -> exporting
-> validating -> materializing -> succeeded | failed | canceled
```

队列记录具有 `job_kind=generation|export`；状态更新使用期望旧状态的条件 UPDATE；跳跃和从终态返回运行态均失败。MP4 导出使用独立导出槽位，不能占满 20 个课堂生成槽位；其租户公平和配额仍由同一账本约束。

- [ ] Step 4: 实现原子创建和 Outbox 投递

在同一个 PostgreSQL 事务中写入 `tenant.generation_jobs`、`tenant.quota_ledger` 预留项和 `platform.outbox_messages`。Dispatcher 通过 `FOR UPDATE SKIP LOCKED` 领取 Outbox，幂等插入 `generation_queue` 后标记已投递。

- [ ] Step 5: 实现槽位与公平领取

`generation_slots` 为共享池建立 20 个全局槽位，并为每个标准租户建立 2 个默认槽位。领取事务同时锁定：

1. 一个可用数据面全局槽位；
2. 一个可用租户槽位；
3. 该租户优先级最高、最早入队的任务。

租户选择按 `tenant_scheduler_state.last_dispatched_at` 排序，使用 `FOR UPDATE SKIP LOCKED`；批量优先级低于学生微课堂、课堂内交互和教师任务。

- [ ] Step 6: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_quota.py tests/teaching/test_scheduler.py tests/teaching/integration/test_outbox_dispatch.py tests/teaching/integration/test_fair_scheduler.py -q
```

Expected: PASS；20 个全局槽位和每租户 2 个槽位在多个并发领取者下仍不超额。

- [ ] Step 7: 提交

```powershell
git add deeptutor/teaching/models/jobs.py deeptutor/teaching/repositories/jobs.py deeptutor/teaching/quota.py deeptutor/teaching/scheduler.py deeptutor/teaching/dispatcher.py deeptutor/teaching/migrations/versions/20260728_0002_generation_jobs.py tests/teaching/test_quota.py tests/teaching/test_scheduler.py tests/teaching/integration/test_outbox_dispatch.py tests/teaching/integration/test_fair_scheduler.py
git commit -m "feat(teaching): add durable fair generation queue"
```

## Task 7: 实现 Worker、租约、取消、重试和原子产物晋级

**Files:**

- Create: `deeptutor/teaching/worker.py`
- Create: `deeptutor/teaching/export_worker.py`
- Create: `deeptutor/teaching/artifact_validation.py`
- Create: `deeptutor/teaching/job_errors.py`
- Create: `tests/teaching/test_worker_retry.py`
- Create: `tests/teaching/test_export_worker.py`
- Create: `tests/teaching/test_artifact_validation.py`
- Create: `tests/integration/test_generation_recovery.py`

- [ ] Step 1: 写恢复和半成品失败测试

```python
async def test_expired_lease_is_reclaimed_once(worker_harness):
    job = await worker_harness.claim_and_crash()
    await worker_harness.advance_past_lease()

    reclaimed = await worker_harness.claim()
    assert reclaimed.id == job.id
    assert reclaimed.attempt == 2


async def test_bad_manifest_never_creates_classroom_version(worker_harness):
    job = await worker_harness.run_with_manifest(
        manifest_with_wrong_sha256()
    )
    assert job.status == "failed"
    assert await worker_harness.classroom_version_count(job.id) == 0


async def test_export_worker_pins_input_hash_and_materializes_once(export_harness):
    job = await export_harness.submit(format="pptx", input_sha256="doc-sha")
    await export_harness.run(job.id)
    assert await export_harness.artifact_count(job.id) == 1
    assert await export_harness.materialized_input_sha256(job.id) == "doc-sha"
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_worker_retry.py tests/teaching/test_export_worker.py tests/teaching/test_artifact_validation.py tests/integration/test_generation_recovery.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现租约和心跳

Worker 每 15 秒续租，租期 60 秒；状态、任务租约、全局槽位和租户槽位在同一事务更新。失联后只有租期到期的任务可被重新领取。
`export_worker.py` 复用相同租约和晋级协议，但调用 `submit_export()`；它按提交时固定的文档与媒体哈希读取输入，不能导出后来被编辑的草稿。

- [ ] Step 4: 实现错误分类和重试

自动重试：

```text
connect_timeout
read_timeout
provider_429
provider_5xx
engine_unavailable
worker_lost
```

不自动重试：

```text
permission_denied
policy_denied
source_snapshot_invalid
contract_invalid
confirmed_outline_hash_mismatch
```

指数退避上限 5 分钟；DSL 自动修复最多 2 次；显式“重新生成”始终创建新任务。

- [ ] Step 5: 实现校验和晋级

依次验证契约版本、DSL、场景类型、来源引用、媒体清单、MIME、大小、SHA-256、租户前缀和安全策略。全部通过后才把临时对象复制到正式前缀，并在同一数据库事务中创建不可变版本记录和结算配额。

- [ ] Step 6: 实现取消

排队任务立即取消并释放预留；运行任务向 OpenMAIC 发取消请求并停止晋级。取消与成功竞态使用状态条件更新，只有一个终态胜出。

- [ ] Step 7: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_worker_retry.py tests/teaching/test_export_worker.py tests/teaching/test_artifact_validation.py tests/integration/test_generation_recovery.py -q
```

Expected: PASS。

- [ ] Step 8: 提交

```powershell
git add deeptutor/teaching/worker.py deeptutor/teaching/export_worker.py deeptutor/teaching/artifact_validation.py deeptutor/teaching/job_errors.py tests/teaching/test_worker_retry.py tests/teaching/test_export_worker.py tests/teaching/test_artifact_validation.py tests/integration/test_generation_recovery.py
git commit -m "feat(teaching): recover and materialize generation jobs"
```

## Task 8: 暴露任务 API 并注册生命周期进程

**Files:**

- Create: `deeptutor/api/routers/classroom_jobs.py`
- Create: `deeptutor/teaching/processes.py`
- Create: `tests/api/test_classroom_jobs.py`
- Modify: `deeptutor/api/main.py`
- Modify: `deeptutor/runtime/launcher.py`

- [ ] Step 1: 写权限和幂等 API 失败测试

```python
def test_duplicate_idempotency_key_returns_same_job(client, teacher_headers):
    first = client.post("/api/v1/classroom-jobs", headers=teacher_headers, json=request_body())
    second = client.post("/api/v1/classroom-jobs", headers=teacher_headers, json=request_body())

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]


def test_student_cannot_read_another_users_private_job(client, student_headers):
    response = client.get("/api/v1/classroom-jobs/job-other", headers=student_headers)
    assert response.status_code == 404
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/api/test_classroom_jobs.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现 API

```text
POST /api/v1/classroom-jobs
GET  /api/v1/classroom-jobs/{job_id}
POST /api/v1/classroom-jobs/{job_id}/confirm-outline
POST /api/v1/classroom-jobs/{job_id}/cancel
POST /api/v1/classroom-jobs/{job_id}/retry
```

返回 yFeiSTAI 任务 ID、阶段、进度、等待原因、可取消状态和大纲；不得返回 OpenMAIC 内部路径、Provider 信息或密钥。

- [ ] Step 4: 注册独立进程入口

`deeptutor.teaching.processes` 提供：

```text
python -m deeptutor.teaching.processes dispatcher
python -m deeptutor.teaching.processes worker
python -m deeptutor.teaching.processes export-worker
python -m deeptutor.teaching.processes reaper
```

Launcher 只在教学平台开启时启动本地开发进程；生产由 Compose 独立管理。

- [ ] Step 5: 运行门禁

Run:

```powershell
python -m pytest tests/api/test_classroom_jobs.py tests/teaching tests/integration/test_generation_recovery.py -q
python -m ruff check deeptutor/teaching deeptutor/api/routers/classroom_jobs.py tests/teaching tests/api/test_classroom_jobs.py
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/api/routers/classroom_jobs.py deeptutor/teaching/processes.py deeptutor/api/main.py deeptutor/runtime/launcher.py tests/api/test_classroom_jobs.py
git commit -m "feat(teaching): expose durable classroom jobs"
```
