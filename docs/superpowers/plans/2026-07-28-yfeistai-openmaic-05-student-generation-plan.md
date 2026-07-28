# 学生按需微课堂与完整课堂 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 学生可在聊天入口明确选择微课堂或完整课堂；系统依据课程策略、来源权限、内容安全和分级配额生成个人课堂，超额或受限请求进入教师审批。

**Architecture:** 新建 `interactive_classroom` Pipeline Capability，但生成规则不写在 Capability 内；它只把对话上下文转换为 `StudentGenerationRequest` 并调用共享 `StudentGenerationService`。微课堂可一阶段生成，完整课堂必须先确认大纲。学生产物默认本人可见且不可直接公开发布。

**Tech Stack:** yFeiSTAI Capability/StreamBus、FastAPI、Pydantic、PostgreSQL、计划 02 任务内核、Next.js、React、pytest、Node test、Playwright

---

## Task 1: 建立课程生成策略、估算和超额审批

**Files:**

- Create: `deeptutor/teaching/models/student_generation.py`
- Create: `deeptutor/teaching/policies/student_generation.py`
- Create: `deeptutor/teaching/services/student_generation.py`
- Create: `deeptutor/teaching/migrations/versions/20260728_0004_student_generation.py`
- Create: `tests/teaching/test_student_generation_policy.py`
- Create: `tests/teaching/test_student_generation_service.py`

- [ ] Step 1: 写策略和配额失败测试

```python
def test_full_classroom_requires_explicit_course_permission():
    policy = CourseGenerationPolicy(
        allow_student_micro=True,
        allow_student_full=False,
    )
    decision = evaluate_student_request(
        policy=policy,
        request=student_request(mode="full"),
        quota=available_quota(),
    )
    assert decision.outcome == "denied"
    assert decision.reason == "full_classroom_disabled"


def test_over_quota_request_requires_approval_instead_of_queueing():
    decision = evaluate_student_request(
        policy=permissive_policy(),
        request=student_request(mode="micro"),
        quota=exhausted_quota(),
    )
    assert decision.outcome == "approval_required"
    assert decision.estimated_units > 0
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_student_generation_policy.py tests/teaching/test_student_generation_service.py -q
```

Expected: FAIL。

- [ ] Step 3: 增加课程策略和个人请求表

租户 Schema 新增：

```text
course_generation_policies
student_generation_requests
student_generation_approvals
```

策略字段至少包括：

```python
class CourseGenerationPolicy(BaseModel):
    allow_student_micro: bool = True
    allow_student_full: bool = False
    allowed_content_modes: frozenset[Literal["source_grounded", "open_creation"]]
    allow_web_search: bool = False
    require_approval_for_restricted_topics: bool = True
    minor_safety_mode: bool = True
    micro_scene_limit: int = 5
    full_scene_limit: int = 24
    daily_student_units: int
    monthly_student_units: int
```

- [ ] Step 4: 实现统一策略判断

判断顺序固定为：

```text
enrollment -> permission -> course mode -> tenant policy
-> source permission -> safety -> quota -> approval -> accepted
```

每次判断返回结构化 `PolicyDecision`，拒绝和转审批均写审计；不得在 API 或 Capability 中复制判断。

- [ ] Step 5: 实现估算

估算返回场景范围、预计时长、配额单位、是否需要大纲确认和是否需要审批。微课堂上限 5 场景；完整课堂上限由课程策略决定且不超过 24。

- [ ] Step 6: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_student_generation_policy.py tests/teaching/test_student_generation_service.py -q
```

Expected: PASS。

- [ ] Step 7: 提交

```powershell
git add deeptutor/teaching/models/student_generation.py deeptutor/teaching/policies/student_generation.py deeptutor/teaching/services/student_generation.py deeptutor/teaching/migrations/versions/20260728_0004_student_generation.py tests/teaching/test_student_generation_policy.py tests/teaching/test_student_generation_service.py
git commit -m "feat(teaching): enforce student classroom policy"
```

## Task 2: 实现学生生成与教师审批 API

**Files:**

- Create: `deeptutor/api/routers/student_classrooms.py`
- Create: `tests/api/test_student_classrooms.py`
- Create: `tests/api/test_student_generation_approvals.py`
- Modify: `deeptutor/api/main.py`

- [ ] Step 1: 写私有可见性和审批失败测试

```python
def test_student_classroom_is_private_to_owner(client, alice_headers, bob_headers):
    created = client.post(
        "/api/v1/student-classrooms",
        headers=alice_headers,
        json=micro_request(),
    ).json()
    response = client.get(
        f"/api/v1/student-classrooms/{created['asset_id']}",
        headers=bob_headers,
    )
    assert response.status_code == 404


def test_over_quota_request_does_not_create_generation_job(client, student_headers):
    response = client.post(
        "/api/v1/student-classrooms",
        headers=student_headers,
        json=over_quota_request(),
    )
    assert response.status_code == 202
    assert response.json()["status"] == "awaiting_approval"
    assert response.json()["generation_job_id"] is None
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/api/test_student_classrooms.py tests/api/test_student_generation_approvals.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现学生 API

```text
POST /api/v1/student-classrooms/estimate
POST /api/v1/student-classrooms
GET  /api/v1/student-classrooms
GET  /api/v1/student-classrooms/{asset_id}
PUT  /api/v1/student-classrooms/{asset_id}/outline
POST /api/v1/student-classrooms/{asset_id}/confirm-outline
POST /api/v1/student-classrooms/{asset_id}/cancel
```

请求不接受任意用户 ID、租户 ID、对象键或 Provider 参数。服务从当前身份、租户和课程 Enrollment 解析上下文。

- [ ] Step 4: 实现教师审批和复制

```text
GET  /api/v1/student-generation-approvals
POST /api/v1/student-generation-approvals/{approval_id}/approve
POST /api/v1/student-generation-approvals/{approval_id}/reject
POST /api/v1/student-classrooms/{asset_id}/copy-to-teacher-draft
```

审批通过后重新运行策略检查并预留配额；不能复用过期的来源授权。复制产生新的教师资产和草稿，保留来源学生资产的审计关系，不改变学生版本。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/api/test_student_classrooms.py tests/api/test_student_generation_approvals.py tests/teaching/test_student_generation_service.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/api/routers/student_classrooms.py deeptutor/api/main.py tests/api/test_student_classrooms.py tests/api/test_student_generation_approvals.py
git commit -m "feat(teaching): add private student classroom API"
```

## Task 3: 注册 `interactive_classroom` Capability

**Files:**

- Create: `deeptutor/agents/interactive_classroom/__init__.py`
- Create: `deeptutor/agents/interactive_classroom/capability.py`
- Create: `deeptutor/agents/interactive_classroom/request_config.py`
- Create: `deeptutor/agents/interactive_classroom/prompts/en/interactive_classroom.yaml`
- Create: `deeptutor/agents/interactive_classroom/prompts/zh/interactive_classroom.yaml`
- Create: `tests/capabilities/test_interactive_classroom_capability.py`
- Create: `tests/runtime/test_request_contracts_classroom.py`
- Modify: `deeptutor/runtime/bootstrap/builtin_capabilities.py`
- Modify: `deeptutor/runtime/request_contracts.py`

- [ ] Step 1: 写 Manifest、配置和统一结果失败测试

```python
def test_classroom_request_requires_student_choice():
    with pytest.raises(ValueError, match="mode"):
        validate_interactive_classroom_request_config(
            {"course_id": "course-a", "question": "解释傅里叶变换"}
        )


async def test_micro_capability_emits_unified_result(stream):
    capability = InteractiveClassroomCapability(service=fake_accepted_service())
    await capability.run(micro_context(), stream)
    result = stream.last_result()
    assert result["job_id"] == "job-1"
    assert "cost_summary" in result
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/capabilities/test_interactive_classroom_capability.py tests/runtime/test_request_contracts_classroom.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现请求契约

```python
class InteractiveClassroomRequestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["micro", "full"]
    course_id: str = Field(min_length=1)
    question: str = Field(default="", max_length=4000)
    content_mode: Literal["source_grounded", "open_creation"] = "source_grounded"
```

学生必须显式提供 `mode`；前端配置卡和 CLI `--config mode=...` 都走同一 Schema。

- [ ] Step 4: 实现 Capability

Manifest：

```python
manifest = CapabilityManifest(
    name="interactive_classroom",
    description="Generate a private interactive classroom for the current course.",
    stages=["policy_check", "briefing", "outline", "queued"],
    tools_used=["rag"],
    cli_aliases=["classroom"],
    request_schema=get_capability_request_schema("interactive_classroom"),
)
```

Capability 只执行：

1. 解析配置和 `UnifiedContext`；
2. 调用 `StudentGenerationService`；
3. 用 `StreamBus.stage()` 发出状态；
4. 用 `emit_capability_result()` 返回估算、审批 ID、任务 ID、大纲或课堂入口。

它不直接调用 OpenMAIC、不实现配额逻辑、不等待长任务完成。

- [ ] Step 5: 增加中英文状态

两种语言具有相同键：

```text
policy_checking
building_grounded_brief
outline_queued
micro_queued
awaiting_approval
awaiting_outline_confirmation
generation_denied
```

- [ ] Step 6: 运行 Capability 门禁

Run:

```powershell
python -m pytest tests/capabilities/test_interactive_classroom_capability.py tests/runtime/test_request_contracts_classroom.py tests/capabilities/test_status_i18n_consistency.py tests/runtime/test_orchestrator.py -q
```

Expected: PASS。

- [ ] Step 7: 提交

```powershell
git add deeptutor/agents/interactive_classroom/__init__.py deeptutor/agents/interactive_classroom/capability.py deeptutor/agents/interactive_classroom/request_config.py deeptutor/agents/interactive_classroom/prompts/en/interactive_classroom.yaml deeptutor/agents/interactive_classroom/prompts/zh/interactive_classroom.yaml deeptutor/runtime/bootstrap/builtin_capabilities.py deeptutor/runtime/request_contracts.py tests/capabilities/test_interactive_classroom_capability.py tests/runtime/test_request_contracts_classroom.py
git commit -m "feat(capability): add interactive classroom generation"
```

## Task 4: 在聊天编排器中呈现学生选择和异步任务

**Files:**

- Create: `web/components/classroom/StudentClassroomConfig.tsx`
- Create: `web/components/classroom/ClassroomJobCard.tsx`
- Create: `web/lib/student-classroom-config.ts`
- Create: `web/tests/student-classroom-config.test.ts`
- Modify: `web/components/chat/home/CapabilityConfigCard.tsx`
- Modify: `web/components/chat/home/ChatComposer.tsx`
- Modify: `web/context/UnifiedChatContext.tsx`
- Modify: `web/lib/capability-routes.ts`
- Modify: `web/locales/en/app.json`
- Modify: `web/locales/zh/app.json`

- [ ] Step 1: 写模式选择和请求序列化失败测试

```typescript
test("student must choose micro or full before submit", () => {
  const result = validateStudentClassroomConfig({
    courseId: "course-a",
    mode: null,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error, "classroom_mode_required");
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
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
npm --prefix web run test:node
```

Expected: FAIL。

- [ ] Step 3: 实现配置卡

配置卡展示：

```text
微课堂：1 至 5 个场景，可直接生成
完整课堂：先确认大纲，课程必须允许
来源约束：默认
开放创作：仅课程策略允许时显示
预计场景、时间、配额和审批状态
```

没有选择模式时提交按钮不可用；不能以默认模式替学生做决定。

- [ ] Step 4: 实现任务卡

Capability Result 映射为可持续轮询的任务卡。状态为 `awaiting_confirmation` 时显示大纲编辑/确认；状态为 `succeeded` 时打开 `/learn/classrooms/{versionId}`。刷新页面后从服务端任务 ID 恢复。

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
git add web/components/classroom/StudentClassroomConfig.tsx web/components/classroom/ClassroomJobCard.tsx web/lib/student-classroom-config.ts web/tests/student-classroom-config.test.ts web/components/chat/home/CapabilityConfigCard.tsx web/components/chat/home/ChatComposer.tsx web/context/UnifiedChatContext.tsx web/lib/capability-routes.ts web/locales/en/app.json web/locales/zh/app.json
git commit -m "feat(web): let students choose classroom length"
```

## Task 5: 验收微课堂、完整课堂和审批路径

**Files:**

- Create: `tests/e2e/test_student_classroom_flow.py`
- Create: `web/tests/e2e/student-classroom-flow.spec.ts`

- [ ] Step 1: 覆盖微课堂

证明：

```text
student chooses micro -> policy accepted -> direct generation
-> private classroom -> playable result
```

- [ ] Step 2: 覆盖完整课堂

证明：

```text
student chooses full -> outline generation -> student edits/confirms
-> content generation -> private classroom -> playable result
```

- [ ] Step 3: 覆盖拒绝和审批

证明：

```text
course disables full -> denied
over quota -> awaiting approval without generation job
teacher approves -> policy rechecked -> quota reserved -> queued
other student -> cannot read result
```

- [ ] Step 4: 运行验收

Run:

```powershell
python -m pytest tests/e2e/test_student_classroom_flow.py tests/capabilities/test_interactive_classroom_capability.py -q
npm --prefix web exec playwright -- test tests/e2e/student-classroom-flow.spec.ts
```

Expected: PASS。

- [ ] Step 5: 提交

```powershell
git add tests/e2e/test_student_classroom_flow.py web/tests/e2e/student-classroom-flow.spec.ts
git commit -m "test(teaching): verify student classroom choices"
```
