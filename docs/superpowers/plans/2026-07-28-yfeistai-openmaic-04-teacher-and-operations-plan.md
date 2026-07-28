# 教师备课发布与内容运营 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付从知识库/PDF 到大纲确认、完整生成、编辑、审核、发布、班级分配的教师闭环，以及支持部分成功、单项重试和双人审核的教研批量生产闭环。

**Architecture:** 教师和教研共用 `TeachingBriefBuilder`、课堂资产、草稿、不可变版本、任务、审核和发布模型。来源在生成前形成不可变快照；编辑只修改草稿；发布创建新版本；机构内容创建者不能审核自己的提交。三条入口均复用计划 02 的任务内核。

**Tech Stack:** FastAPI、Pydantic、SQLAlchemy async、PostgreSQL、现有 RAGService、OpenMAIC 契约、Next.js、React、Node test、pytest、Playwright

---

## Task 1: 建立课堂生命周期和不可变版本模型

**Files:**

- Create: `deeptutor/teaching/models/classrooms.py`
- Create: `deeptutor/teaching/repositories/classrooms.py`
- Create: `deeptutor/teaching/migrations/versions/20260728_0003_classroom_lifecycle.py`
- Create: `tests/teaching/test_classroom_state.py`
- Create: `tests/teaching/integration/test_classroom_version_immutability.py`

- [ ] Step 1: 写非法状态和版本覆盖失败测试

```python
def test_draft_cannot_publish_before_validation():
    with pytest.raises(InvalidClassroomTransition):
        transition("editing", "published")


async def test_published_version_cannot_be_updated(repository):
    version = await repository.insert_published_version(valid_version())
    with pytest.raises(ImmutableVersionError):
        await repository.replace_document(version.id, changed_document())
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_classroom_state.py tests/teaching/integration/test_classroom_version_immutability.py -q
```

Expected: FAIL。

- [ ] Step 3: 增加领域表

租户 Schema 至少新增：

```text
source_snapshots
tenant_source_bindings
source_uploads
teaching_briefs
classroom_assets
classroom_drafts
classroom_versions
classroom_exports
approvals
publications
assignments
batch_jobs
batch_items
```

`classroom_versions` 使用唯一约束 `(asset_id, version_number)`；发布后禁止 UPDATE 和 DELETE 的数据库触发器只允许平台保留策略使用单独的受审计维护过程。`assignments` 直接引用 `classroom_version_id`。

- [ ] Step 4: 实现显式状态机

```python
ALLOWED_TRANSITIONS = {
    "draft": {"generating_outline", "canceled"},
    "generating_outline": {"awaiting_outline", "failed", "canceled"},
    "awaiting_outline": {"generating_content", "canceled"},
    "generating_content": {"editing", "failed", "canceled"},
    "editing": {"submitted", "validated", "canceled"},
    "submitted": {"approved", "rejected"},
    "rejected": {"editing"},
    "validated": {"approved"},
    "approved": {"published"},
    "published": set(),
    "failed": {"draft"},
    "canceled": set(),
}
```

发布不是修改草稿状态的替代操作：它创建版本、Publication 和审计记录，再把资产当前发布指针切换到新版本。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_classroom_state.py tests/teaching/integration/test_classroom_version_immutability.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/models/classrooms.py deeptutor/teaching/repositories/classrooms.py deeptutor/teaching/migrations/versions/20260728_0003_classroom_lifecycle.py tests/teaching/test_classroom_state.py tests/teaching/integration/test_classroom_version_immutability.py
git commit -m "feat(teaching): add immutable classroom lifecycle"
```

## Task 2: 建立课程、班级和成员目录 API

**Files:**

- Create: `deeptutor/teaching/repositories/catalog.py`
- Create: `deeptutor/teaching/repositories/sources.py`
- Create: `deeptutor/teaching/services/catalog.py`
- Create: `deeptutor/teaching/services/sources.py`
- Create: `deeptutor/api/routers/teaching_catalog.py`
- Create: `tests/api/test_teaching_catalog.py`
- Create: `tests/api/test_teaching_source_uploads.py`
- Modify: `deeptutor/api/main.py`

- [ ] Step 1: 写课程范围和 Enrollment 失败测试

```python
def test_teacher_lists_only_granted_courses(client, teacher_headers):
    response = client.get("/api/v1/teaching/courses", headers=teacher_headers)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["course-a"]


def test_student_cannot_enroll_another_user(client, student_headers):
    response = client.post(
        "/api/v1/teaching/classes/class-a/enrollments",
        headers=student_headers,
        json={"user_id": "student-b"},
    )
    assert response.status_code == 403


def test_pdf_upload_rejects_spoofed_or_oversized_files(client, teacher_headers):
    response = client.post(
        "/api/v1/teaching/sources/pdf",
        headers=teacher_headers,
        files={"file": ("book.pdf", b"not-a-pdf", "application/pdf")},
    )
    assert response.status_code == 415
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/api/test_teaching_catalog.py tests/api/test_teaching_source_uploads.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现目录服务和 API

```text
GET  /api/v1/teaching/courses
POST /api/v1/teaching/courses
GET  /api/v1/teaching/courses/{course_id}/classes
POST /api/v1/teaching/courses/{course_id}/classes
GET  /api/v1/teaching/classes/{class_id}/enrollments
POST /api/v1/teaching/classes/{class_id}/enrollments
DELETE /api/v1/teaching/classes/{class_id}/enrollments/{user_id}
GET  /api/v1/teaching/sources
POST /api/v1/teaching/sources/pdf
POST /api/v1/teaching/sources/bind
DELETE /api/v1/teaching/sources/{binding_id}
```

课程、班级、Enrollment 和来源绑定全部位于当前租户 Schema。知识库来源绑定只引用 yFeiSTAI 已有知识库稳定资源 ID，不复制知识库；绑定前同时验证当前管理员能访问该知识库和当前机构授权。PDF 上传使用 100 MiB 流式上限、`%PDF-` 魔数/MIME 双校验、最多 2,000 页、拒绝加密文件、嵌入文件和可执行动作，并按 SHA-256 去重；通过后写入当前租户对象前缀并创建 `source_uploads` 与绑定记录，API 不返回物理路径或对象键。教师只能访问被授权课程、本人班级及当前租户绑定来源；组织管理员可以管理当前机构；学生只能读取自己的 Enrollment，不得自行把任意用户加入班级。

- [ ] Step 4: 运行测试

Run:

```powershell
python -m pytest tests/api/test_teaching_catalog.py tests/api/test_teaching_source_uploads.py tests/api/test_tenant_context.py -q
```

Expected: PASS。

- [ ] Step 5: 提交

```powershell
git add deeptutor/teaching/repositories/catalog.py deeptutor/teaching/repositories/sources.py deeptutor/teaching/services/catalog.py deeptutor/teaching/services/sources.py deeptutor/api/routers/teaching_catalog.py deeptutor/api/main.py tests/api/test_teaching_catalog.py tests/api/test_teaching_source_uploads.py
git commit -m "feat(teaching): add scoped course and class catalog"
```

## Task 3: 建立来源快照和 TeachingBriefBuilder

**Files:**

- Create: `deeptutor/teaching/source_snapshots.py`
- Create: `deeptutor/teaching/brief_builder.py`
- Modify: `deeptutor/teaching/repositories/sources.py`
- Create: `tests/teaching/test_source_snapshots.py`
- Create: `tests/teaching/test_brief_builder.py`
- Modify: `deeptutor/multi_user/knowledge_access.py`

- [ ] Step 1: 写权限、哈希和来源约束失败测试

```python
async def test_snapshot_rejects_kb_not_visible_to_current_user(builder, as_user):
    with as_user("teacher-a"):
        with pytest.raises(SourceAccessDenied):
            await builder.from_kb("admin:kb:private-b", request_spec())


async def test_grounded_brief_contains_only_authorized_fragments(builder):
    brief = await builder.from_kb("user:kb:course-a", grounded_spec())
    assert brief.content_mode == "source_grounded"
    assert brief.source_snapshot_sha256
    assert all(fragment.permission == "source.use" for fragment in brief.fragments)
    assert all(scene_ref.document_id for scene_ref in brief.source_refs)


async def test_source_bound_to_other_tenant_is_rejected(builder):
    with pytest.raises(SourceAccessDenied):
        await builder.from_kb("admin:kb:tenant-b-book", grounded_spec())
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_source_snapshots.py tests/teaching/test_brief_builder.py -q
```

Expected: FAIL。

- [ ] Step 3: 增加只读知识库来源接口

在 `knowledge_access.py` 提供已验证资源描述，不开放原始任意路径；随后用 `repositories/sources.py` 校验该资源已经绑定当前租户：

```python
@dataclass(frozen=True, slots=True)
class AuthorizedKnowledgeSource:
    resource_id: str
    name: str
    base_dir: Path
    read_only: bool


def resolve_authorized_source(kb_ref: str) -> AuthorizedKnowledgeSource:
    resource = resolve_for_rag(kb_ref)
    if resource is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return AuthorizedKnowledgeSource(
        resource_id=resource.id,
        name=resource.name,
        base_dir=resource.base_dir,
        read_only=resource.read_only,
    )
```

- [ ] Step 4: 实现来源快照

知识库使用 `RAGService.search()` 取得片段和 sources；PDF 使用现有受控附件读取与文档提取器。快照保存：

```text
source kind and stable id
content hash
authorized fragment text
document/page/section coordinates
permission summary
retrieval provider and index signature
created_at and created_by
```

共享数据面只收到快照中的授权片段，不收到知识库目录或原始全文。

- [ ] Step 5: 实现简报生成

`TeachingBriefBuilder` 输入课程、班级、教学目标、受众、时长、微/完整模式、来源模式、联网策略、模板和知识点。开放创作必须显式标记；教师和教研默认来源约束。

- [ ] Step 6: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_source_snapshots.py tests/teaching/test_brief_builder.py tests/multi_user/test_kb_manifest_access.py -q
```

Expected: PASS。

- [ ] Step 7: 提交

```powershell
git add deeptutor/teaching/source_snapshots.py deeptutor/teaching/brief_builder.py deeptutor/teaching/repositories/sources.py deeptutor/multi_user/knowledge_access.py tests/teaching/test_source_snapshots.py tests/teaching/test_brief_builder.py
git commit -m "feat(teaching): build grounded teaching briefs"
```

## Task 4: 实现教师备课 API

**Files:**

- Create: `deeptutor/teaching/services/classrooms.py`
- Create: `deeptutor/api/routers/classrooms.py`
- Create: `tests/api/test_teacher_classrooms.py`
- Modify: `deeptutor/api/main.py`

- [ ] Step 1: 写两阶段和并发编辑失败测试

```python
def test_teacher_full_classroom_stops_for_outline_confirmation(client, teacher_headers):
    response = client.post(
        "/api/v1/classrooms",
        headers=teacher_headers,
        json=full_classroom_request(),
    )
    assert response.status_code == 202
    job = wait_for_state(response.json()["job_id"], "awaiting_confirmation")
    assert job["outline"]
    assert job["classroom_version_id"] is None


def test_stale_draft_revision_is_rejected(client, teacher_headers):
    response = client.put(
        "/api/v1/classrooms/asset-1/draft",
        headers={**teacher_headers, "If-Match": '"revision-2"'},
        json=edited_document(),
    )
    assert response.status_code == 409


def test_draft_media_is_bound_to_asset_and_tenant(client, teacher_headers):
    media = upload_draft_media(client, teacher_headers, asset_id="asset-a")
    response = client.get(
        f"/api/v1/classrooms/asset-b/draft-media/{media['id']}",
        headers=teacher_headers,
    )
    assert response.status_code == 404
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/api/test_teacher_classrooms.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现 API

```text
POST /api/v1/classrooms
GET  /api/v1/classrooms
GET  /api/v1/classrooms/{asset_id}
GET  /api/v1/classrooms/{asset_id}/draft
PUT  /api/v1/classrooms/{asset_id}/outline
POST /api/v1/classrooms/{asset_id}/confirm-outline
PUT  /api/v1/classrooms/{asset_id}/draft
POST /api/v1/classrooms/{asset_id}/draft-media
GET  /api/v1/classrooms/{asset_id}/draft-media/{media_id}
POST /api/v1/classrooms/{asset_id}/validate
```

教师完整课堂必须先生成大纲并等待确认；大纲编辑后计算规范 JSON SHA-256 并传给内容阶段。草稿更新使用 `If-Match`，只允许有资源范围 `classroom.edit` 的成员。PPTX 导入和编辑器媒体上传先写入资产级临时区，服务端校验 MIME、大小、SHA-256 和当前租户/资产归属，再返回不可猜测的媒体 ID；保存草稿时只接受这些 ID，不接受客户端对象键或任意 URL。

- [ ] Step 4: 实现发布前校验报告

报告包含：

```text
dsl_integrity
media_integrity
knowledge_point_coverage
source_traceability
unsupported_claims
quiz_answerability
interactive_security
accessibility
export_readiness
```

严重项阻止提交或发布；警告项必须在审核页面显式展示。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/api/test_teacher_classrooms.py tests/teaching/test_classroom_state.py tests/teaching/test_brief_builder.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/services/classrooms.py deeptutor/api/routers/classrooms.py deeptutor/api/main.py tests/api/test_teacher_classrooms.py
git commit -m "feat(teaching): add two-stage teacher classroom API"
```

## Task 5: 实现审核、发布、分配和版本迁移

**Files:**

- Create: `deeptutor/teaching/services/reviews.py`
- Create: `deeptutor/teaching/services/publications.py`
- Create: `deeptutor/api/routers/classroom_reviews.py`
- Create: `tests/api/test_classroom_reviews.py`
- Create: `tests/api/test_classroom_publications.py`
- Modify: `deeptutor/api/main.py`

- [ ] Step 1: 写自审、政策和版本固定失败测试

```python
def test_content_author_cannot_approve_own_submission(client, author_headers):
    response = client.post(
        "/api/v1/classroom-reviews/review-1/approve",
        headers=author_headers,
        json={"comment": "approved"},
    )
    assert response.status_code == 403


def test_existing_assignment_stays_on_old_version_after_new_publish(repository):
    old = repository.publish("asset-1", draft="draft-1")
    assignment = repository.assign("class-a", old.id)
    repository.publish("asset-1", draft="draft-2")
    assert repository.get_assignment(assignment.id).version_id == old.id
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/api/test_classroom_reviews.py tests/api/test_classroom_publications.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现审核策略

```python
class ReviewPolicy:
    teacher_self_publish: bool
    org_content_requires_review: bool = True
    platform_template_requires_review: bool = True
    prohibit_self_review: bool = True
```

教师本人班级在租户允许时可自发布；机构内容必须由不同的 `content_reviewer` 审核；平台模板由平台审核。审核决策是追加记录，退回后产生新的草稿修订。

- [ ] Step 4: 实现 API

```text
POST /api/v1/classrooms/{asset_id}/submit
GET  /api/v1/classroom-reviews
POST /api/v1/classroom-reviews/{review_id}/approve
POST /api/v1/classroom-reviews/{review_id}/reject
POST /api/v1/classrooms/{asset_id}/publish
POST /api/v1/classroom-versions/{version_id}/assign
POST /api/v1/classroom-assignments/{assignment_id}/migrate
```

迁移请求必须明确旧版、新版、影响班级、执行人和原因；不自动迁移正在学习的班级。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/api/test_classroom_reviews.py tests/api/test_classroom_publications.py tests/teaching/integration/test_classroom_version_immutability.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/services/reviews.py deeptutor/teaching/services/publications.py deeptutor/api/routers/classroom_reviews.py deeptutor/api/main.py tests/api/test_classroom_reviews.py tests/api/test_classroom_publications.py
git commit -m "feat(teaching): review and publish immutable classrooms"
```

## Task 6: 实现版本固定的受控导出

**Files:**

- Create: `deeptutor/teaching/services/exports.py`
- Create: `deeptutor/api/routers/classroom_exports.py`
- Create: `tests/api/test_classroom_exports.py`
- Create: `tests/teaching/test_classroom_export_service.py`
- Modify: `deeptutor/api/main.py`

- [ ] Step 1: 写输入固定、权限和下载失败测试

```python
def test_draft_export_rejects_stale_revision(client, teacher_headers):
    response = client.post(
        "/api/v1/classrooms/asset-1/draft/exports",
        headers={**teacher_headers, "If-Match": '"revision-1"'},
        json={"format": "pptx"},
    )
    assert response.status_code == 409


async def test_published_export_uses_immutable_version_hash(export_service):
    export = await export_service.create_for_version("version-1", "classroom_zip")
    await export_service.edit_current_draft("asset-1", changed_document())
    assert export.input_sha256 == await export_service.version_sha256("version-1")


def test_other_tenant_cannot_read_export(client, tenant_b_headers):
    response = client.get(
        "/api/v1/classroom-exports/export-tenant-a",
        headers=tenant_b_headers,
    )
    assert response.status_code == 404
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/api/test_classroom_exports.py tests/teaching/test_classroom_export_service.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现导出服务和 API

```text
POST /api/v1/classrooms/{asset_id}/draft/exports
POST /api/v1/classroom-versions/{version_id}/exports
GET  /api/v1/classroom-exports/{export_id}
GET  /api/v1/classroom-exports/{export_id}/download
```

草稿导出要求 `If-Match` 并固定草稿修订号、课堂文档 SHA-256 和媒体清单 SHA-256；发布版导出固定不可变 `classroom_version_id`。格式只接受 `classroom_zip`、`pptx`、`offline_html` 和租户策略允许时的 `mp4`。服务创建计划 02 的持久化 `export` 任务与配额预留，保存 `classroom_exports` 关联记录；相同幂等键、输入哈希和格式返回同一任务。

- [ ] Step 4: 实现产物晋级和受控下载

Export Worker 从 yFeiSTAI 正式存储读取已固定输入并调用 OpenMAIC，不把对象存储凭证交给引擎。完成后逐文件校验清单、MIME、大小和 SHA-256，再晋级到当前租户前缀。下载路由只对资源所有者、被授权教师、内容运营和管理员开放，返回 yFeiSTAI 受控流或最多 60 秒的签名 URL，不返回 OpenMAIC 地址或物理对象键；计划 06 再为已授权学习会话增加绑定资源的短期读票据。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/api/test_classroom_exports.py tests/teaching/test_classroom_export_service.py tests/teaching/test_export_worker.py -q
```

Expected: PASS；修改当前草稿不会改变已排队导出的输入，跨租户读取返回 404，过期下载拒绝。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/services/exports.py deeptutor/api/routers/classroom_exports.py deeptutor/api/main.py tests/api/test_classroom_exports.py tests/teaching/test_classroom_export_service.py
git commit -m "feat(teaching): export pinned classroom versions"
```

## Task 7: 实现教研批量生产和部分成功

**Files:**

- Create: `deeptutor/teaching/services/batches.py`
- Create: `deeptutor/api/routers/classroom_batches.py`
- Create: `tests/api/test_classroom_batches.py`
- Create: `tests/teaching/test_batch_service.py`
- Modify: `deeptutor/api/main.py`

- [ ] Step 1: 写部分成功和单项重试失败测试

```python
async def test_batch_preserves_success_when_one_item_fails(service):
    batch = await service.create([valid_input("a"), invalid_input("b"), valid_input("c")])
    await service.run(batch.id)

    result = await service.get(batch.id)
    assert [item.status for item in result.items] == ["succeeded", "failed", "succeeded"]


async def test_retry_creates_job_only_for_failed_item(service):
    batch = await failed_middle_item_batch(service)
    retried = await service.retry_item(batch.id, item_id="b")
    assert retried.parent_item_id == "b"
    assert await service.job_count_for_item("a") == 1
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_batch_service.py tests/api/test_classroom_batches.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现批次服务

每个输入生成独立 `GenerationJob` 和 `BatchItem`，共享一个 `BatchJob`。批次汇总状态为：

```text
queued
running
awaiting_confirmation
partially_succeeded
succeeded
failed
canceled
```

每个完整课堂项先停在大纲确认状态，教研人员确认该项大纲及其哈希后才投递内容阶段；不得把批量生成变成一次性全流程。批次取消只影响未开始项；已成功项保持。批量任务使用最低优先级和所属租户可用槽位。

- [ ] Step 4: 实现 API

```text
POST /api/v1/classroom-batches
GET  /api/v1/classroom-batches
GET  /api/v1/classroom-batches/{batch_id}
POST /api/v1/classroom-batches/{batch_id}/items/{item_id}/confirm-outline
POST /api/v1/classroom-batches/{batch_id}/confirm-outlines
POST /api/v1/classroom-batches/{batch_id}/items/{item_id}/retry
POST /api/v1/classroom-batches/{batch_id}/cancel
```

批量确认请求逐项携带大纲 revision 和规范哈希，只确认教研人员已经查看的项目；未选项目继续等待。只有 `content_author` 或具备等价授权的成员可创建；送审后由不同的 `content_reviewer` 处理。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_batch_service.py tests/api/test_classroom_batches.py tests/teaching/test_scheduler.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/services/batches.py deeptutor/api/routers/classroom_batches.py deeptutor/api/main.py tests/teaching/test_batch_service.py tests/api/test_classroom_batches.py
git commit -m "feat(teaching): add partial-success content batches"
```

## Task 8: 实现教师与内容运营页面

**Files:**

- Create: `web/app/(utility)/teaching/layout.tsx`
- Create: `web/app/(utility)/teaching/classrooms/page.tsx`
- Create: `web/app/(utility)/teaching/classrooms/new/page.tsx`
- Create: `web/app/(utility)/teaching/classrooms/[assetId]/outline/page.tsx`
- Create: `web/app/(utility)/teaching/classrooms/[assetId]/edit/page.tsx`
- Create: `web/app/(utility)/teaching/reviews/page.tsx`
- Create: `web/app/(utility)/teaching/library/page.tsx`
- Create: `web/app/(utility)/teaching/batches/page.tsx`
- Create: `web/components/teaching/TeachingBriefForm.tsx`
- Create: `web/components/teaching/OutlineReview.tsx`
- Create: `web/components/teaching/ValidationReport.tsx`
- Create: `web/components/teaching/ReviewQueue.tsx`
- Create: `web/components/teaching/BatchWorkbench.tsx`
- Create: `web/lib/teaching-api.ts`
- Create: `web/tests/teaching-api.test.ts`
- Modify: `web/components/sidebar/UtilitySidebar.tsx`
- Modify: `web/locales/en/app.json`
- Modify: `web/locales/zh/app.json`

- [ ] Step 1: 写 API 状态映射失败测试

```typescript
test("awaiting confirmation maps to the outline review route", () => {
  assert.equal(
    classroomNextRoute({
      assetId: "asset-1",
      status: "awaiting_confirmation",
    }),
    "/teaching/classrooms/asset-1/outline",
  );
});

test("editing maps to the editor route", () => {
  assert.equal(
    classroomNextRoute({ assetId: "asset-1", status: "editing" }),
    "/teaching/classrooms/asset-1/edit",
  );
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
npm --prefix web run test:node
```

Expected: FAIL。

- [ ] Step 3: 实现备课向导

教师依次选择课程/班级、知识库或 PDF、来源模式、目标、时长、模板、联网与媒体策略。PDF 通过受控来源上传接口进入租户来源库；也可以在编辑页导入 PPTX。提交前展示估算场景、耗时和配额；完整课堂进入大纲页面后才允许确认。编辑页接入 `ClassroomExportMenu`，显示导出任务进度、失败原因和受控下载入口。

- [ ] Step 4: 实现审核和批量工作台

审核页并排展示来源片段、知识点覆盖、无来源支撑内容、校验报告和版本差异。批量页显示总数、等待大纲确认、排队、运行、成功、失败和可单项重试项，并提供逐项或勾选后的大纲确认；不提供创建者自审操作。

- [ ] Step 5: 运行前端门禁

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run i18n:check
npm --prefix web run build
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git --literal-pathspecs add "web/app/(utility)/teaching/layout.tsx" "web/app/(utility)/teaching/classrooms/page.tsx" "web/app/(utility)/teaching/classrooms/new/page.tsx" "web/app/(utility)/teaching/classrooms/[assetId]/outline/page.tsx" "web/app/(utility)/teaching/classrooms/[assetId]/edit/page.tsx" "web/app/(utility)/teaching/reviews/page.tsx" "web/app/(utility)/teaching/library/page.tsx" "web/app/(utility)/teaching/batches/page.tsx" web/components/teaching/TeachingBriefForm.tsx web/components/teaching/OutlineReview.tsx web/components/teaching/ValidationReport.tsx web/components/teaching/ReviewQueue.tsx web/components/teaching/BatchWorkbench.tsx web/lib/teaching-api.ts web/tests/teaching-api.test.ts web/components/sidebar/UtilitySidebar.tsx web/locales/en/app.json web/locales/zh/app.json
git commit -m "feat(web): add teacher and content operations workspace"
```

## Task 9: 验收教师与教研完整流程

**Files:**

- Create: `tests/e2e/test_teacher_classroom_flow.py`
- Create: `tests/e2e/test_content_operations_flow.py`
- Create: `web/tests/e2e/teacher-classroom-flow.spec.ts`
- Create: `web/tests/e2e/content-operations-flow.spec.ts`

- [ ] Step 1: 实现教师端到端测试

证明：

```text
KB/PDF -> source snapshot -> outline -> teacher edit -> confirm
-> full generation -> validation -> edit -> submit/self-publish policy
-> immutable version -> class assignment
-> classroom ZIP/PPTX/offline HTML export -> controlled download
```

- [ ] Step 2: 实现教研端到端测试

证明：

```text
batch create -> outline review/confirmation -> partial success -> failed item retry
-> author submit -> different reviewer approve
-> publish into organization library
```

- [ ] Step 3: 运行后端和浏览器验收

Run:

```powershell
python -m pytest tests/e2e/test_teacher_classroom_flow.py tests/e2e/test_content_operations_flow.py -q
npm --prefix web exec playwright -- test tests/e2e/teacher-classroom-flow.spec.ts tests/e2e/content-operations-flow.spec.ts
```

Expected: PASS。

- [ ] Step 4: 提交

```powershell
git add tests/e2e/test_teacher_classroom_flow.py tests/e2e/test_content_operations_flow.py web/tests/e2e/teacher-classroom-flow.spec.ts web/tests/e2e/content-operations-flow.spec.ts
git commit -m "test(teaching): verify teacher and operations flows"
```
