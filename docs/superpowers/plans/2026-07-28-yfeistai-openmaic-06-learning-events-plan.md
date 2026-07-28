# 学习事件、记忆与掌握度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让课堂播放、测验和 PBL 行为可靠回传 yFeiSTAI，形成幂等的学习会话、进度、短期记忆和掌握度更新，并保证只有有效评分证据可以改变掌握度。

**Architecture:** 浏览器先用 yFeiSTAI 身份创建学习会话；每批写事件使用不超过五分钟、单次消费的签名票据。API 只从票据和服务端会话派生租户、用户和课堂版本。原始事件追加写入，Projector 异步更新进度、测验尝试、记忆和掌握度；重复或非法事件不产生二次副作用。

**Tech Stack:** FastAPI、python-jose、Pydantic、SQLAlchemy async、PostgreSQL、现有 `compute_mastery`、MemoryStore、Next.js、React、pytest、Node test、Playwright

---

## Task 1: 建立学习会话、事件和投影表

**Files:**

- Create: `deeptutor/teaching/models/learning.py`
- Create: `deeptutor/teaching/migrations/versions/20260728_0005_learning_events.py`
- Create: `deeptutor/teaching/repositories/learning_events.py`
- Create: `tests/teaching/integration/test_learning_event_migration.py`
- Create: `tests/teaching/test_learning_event_repository.py`

- [ ] Step 1: 写事件幂等和顺序失败测试

```python
async def test_duplicate_event_id_is_stored_once(repository):
    event = valid_event(event_id="event-1")
    first = await repository.append(event)
    second = await repository.append(event)

    assert first.outcome == "accepted"
    assert second.outcome == "duplicate"
    assert await repository.count_events("session-1") == 1


async def test_server_assigns_monotonic_session_sequence(repository):
    a = await repository.append(valid_event(event_id="a"))
    b = await repository.append(valid_event(event_id="b"))
    assert (a.seq, b.seq) == (1, 2)
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_learning_event_repository.py tests/teaching/integration/test_learning_event_migration.py -q
```

Expected: FAIL。

- [ ] Step 3: 增加表

租户 Schema：

```text
learning_sessions
learning_events
learning_projection_queue
quiz_attempts
mastery_evidence
mastery_levels
learning_progress
learning_event_quarantine
```

约束：

```text
learning_events.event_id unique
learning_events(session_id, seq) unique
quiz_attempts.event_id unique
mastery_evidence.event_id unique
mastery_levels(user_id, knowledge_point_id) unique
```

事件正文使用 JSONB，但索引列 `event_type`、`occurred_at`、`session_id`、`classroom_version_id` 和 `knowledge_point_id` 必须独立存在。

- [ ] Step 4: 实现原子追加

追加事务锁定会话行、分配下一个 seq、插入事件和投影队列记录。相同 `event_id` 返回 duplicate，不抛 500。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_learning_event_repository.py tests/teaching/integration/test_learning_event_migration.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/models/learning.py deeptutor/teaching/migrations/versions/20260728_0005_learning_events.py deeptutor/teaching/repositories/learning_events.py tests/teaching/test_learning_event_repository.py tests/teaching/integration/test_learning_event_migration.py
git commit -m "feat(teaching): add append-only learning events"
```

## Task 2: 实现学习会话和短期单次票据

**Files:**

- Create: `deeptutor/teaching/tickets.py`
- Create: `deeptutor/teaching/services/learning_sessions.py`
- Create: `tests/teaching/test_classroom_tickets.py`
- Create: `tests/teaching/test_learning_sessions.py`

- [ ] Step 1: 写过期、越权和重放失败测试

```python
def test_event_ticket_is_bound_to_version_and_action(ticket_service):
    token = ticket_service.issue(
        tenant_id="tenant-a",
        user_id="student-a",
        session_id="session-a",
        classroom_version_id="version-a",
        allowed_action="learning_event.append",
        ttl_seconds=300,
    )
    claims = ticket_service.verify(
        token,
        expected_action="learning_event.append",
        expected_version_id="version-a",
    )
    assert claims.user_id == "student-a"
    with pytest.raises(TicketScopeError):
        ticket_service.verify(
            token,
            expected_action="artifact.write",
            expected_version_id="version-a",
        )


async def test_event_ticket_cannot_be_consumed_twice(session_service):
    token = await session_service.issue_event_ticket("session-a")
    await session_service.consume_event_ticket(token)
    with pytest.raises(TicketReplay):
        await session_service.consume_event_ticket(token)


def test_media_ticket_cannot_read_another_resource(ticket_service):
    token = ticket_service.issue(
        tenant_id="tenant-a",
        user_id="student-a",
        session_id="session-a",
        classroom_version_id="version-a",
        allowed_action="classroom.media.read",
        resource_id="media-a",
        ttl_seconds=60,
    )
    with pytest.raises(TicketScopeError):
        ticket_service.verify(token, expected_resource_id="media-b")
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_classroom_tickets.py tests/teaching/test_learning_sessions.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现票据 Claims

```python
class ClassroomTicketClaims(BaseModel):
    iss: Literal["yfeistai"]
    aud: Literal["yfeistai-classroom"]
    tenant_id: str
    user_id: str
    session_id: str
    classroom_version_id: str
    resource_id: str | None = None
    allowed_action: Literal[
        "classroom.enter",
        "learning_event.append",
        "classroom.media.read",
        "classroom.export.read",
    ]
    exp: int
    iat: int
    jti: str
```

使用独立密钥文件签名；写票据有效期最多 300 秒，`jti` 在消费事务中唯一插入，重复即拒绝。读票据有效期最多 60 秒，可以在有效期内重复使用，但仍绑定具体版本、资源和动作。

- [ ] Step 4: 实现会话服务

创建会话时验证当前学生有 Assignment 或个人课堂所有权，固定 `classroom_version_id`，记录开始时间和最后游标。客户端提供的租户、用户和版本归属都以服务端查询结果覆盖。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_classroom_tickets.py tests/teaching/test_learning_sessions.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/tickets.py deeptutor/teaching/services/learning_sessions.py tests/teaching/test_classroom_tickets.py tests/teaching/test_learning_sessions.py
git commit -m "feat(teaching): secure classroom learning sessions"
```

## Task 3: 实现事件 Schema 和接收 API

**Files:**

- Create: `contracts/classroom/learning-event.schema.json`
- Create: `deeptutor/teaching/learning_events.py`
- Create: `deeptutor/api/routers/classroom_learning.py`
- Create: `deeptutor/api/routers/classroom_content.py`
- Create: `tests/teaching/test_learning_event_contract.py`
- Create: `tests/api/test_classroom_learning_events.py`
- Create: `tests/api/test_classroom_content_tickets.py`
- Modify: `deeptutor/api/main.py`
- Modify: `deeptutor/api/routers/classroom_exports.py`
- Modify: `scripts/verify_classroom_contracts.py`

- [ ] Step 1: 写伪造字段和无效评分失败测试

```python
def test_api_ignores_client_identity_and_uses_ticket(client, event_ticket):
    payload = event_batch(
        tenant_id="tenant-forged",
        user_id="user-forged",
        events=[scene_completed()],
    )
    response = client.post(
        "/api/v1/classroom-sessions/session-a/events",
        headers={"X-Classroom-Ticket": event_ticket},
        json=payload,
    )
    assert response.status_code == 202
    stored = load_event(response.json()["accepted"][0])
    assert stored.tenant_id == "tenant-a"
    assert stored.user_id == "student-a"


def test_quiz_event_without_assessment_binding_is_quarantined(client, event_ticket):
    response = post_events(
        client,
        event_ticket,
        [quiz_graded(assessment_id="missing")],
    )
    assert response.json()["quarantined"][0]["reason"] == "assessment_not_in_version"


def test_media_route_rejects_ticket_for_another_media(
    client, student_headers, media_b_ticket
):
    response = client.get(
        "/api/v1/classroom-versions/version-a/media/media-a",
        headers={**student_headers, "X-Classroom-Ticket": media_b_ticket},
    )
    assert response.status_code == 403
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_learning_event_contract.py tests/api/test_classroom_learning_events.py tests/api/test_classroom_content_tickets.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现版本化事件联合类型

支持：

```text
classroom.started
scene.completed
quiz.graded
hint.used
pbl.milestone_completed
classroom.completed
```

公共字段：

```python
class LearningEventBase(BaseModel):
    schema_version: Literal["1.0"]
    event_id: str
    event_type: str
    occurred_at: datetime
    scene_id: str | None = None
    knowledge_point_id: str | None = None
```

客户端不提交权威租户、用户、版本和 session；这些值来自票据与数据库。

- [ ] Step 4: 实现 API

```text
POST /api/v1/classroom-sessions
POST /api/v1/classroom-sessions/{session_id}/event-ticket
POST /api/v1/classroom-sessions/{session_id}/read-ticket
POST /api/v1/classroom-sessions/{session_id}/events
PUT  /api/v1/classroom-sessions/{session_id}/cursor
POST /api/v1/classroom-sessions/{session_id}/complete
GET  /api/v1/classroom-sessions/{session_id}
GET  /api/v1/classroom-versions/{version_id}/document
GET  /api/v1/classroom-versions/{version_id}/media/{media_id}
GET  /api/v1/classroom-exports/{export_id}/download
```

所有接口都使用 `Depends(require_auth)`；事件和学生受控读取接口同时校验当前登录用户、票据 `user_id` 和服务端会话所有者完全一致。`read-ticket` 只为该会话固定版本清单中的文档、媒体或允许学生下载的导出签发资源级票据；教师和管理员仍可凭计划 04 的资源权限读取，不需要伪造学习会话。文档、媒体和导出下载从 yFeiSTAI 对象存储读取，不代理到 OpenMAIC；票据不能在登录过期后单独充当账号凭证。

事件批次最多 100 项、正文最多 256 KiB。接收结果逐项返回 accepted、duplicate 或 quarantined。

- [ ] Step 5: 运行契约和 API 测试

Run:

```powershell
python scripts/verify_classroom_contracts.py
python -m pytest tests/teaching/test_learning_event_contract.py tests/api/test_classroom_learning_events.py tests/api/test_classroom_content_tickets.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add contracts/classroom/learning-event.schema.json deeptutor/teaching/learning_events.py deeptutor/api/routers/classroom_learning.py deeptutor/api/routers/classroom_content.py deeptutor/api/routers/classroom_exports.py deeptutor/api/main.py tests/teaching/test_learning_event_contract.py tests/api/test_classroom_learning_events.py tests/api/test_classroom_content_tickets.py scripts/verify_classroom_contracts.py
git commit -m "feat(teaching): ingest signed classroom events"
```

## Task 4: 实现进度、测验和掌握度 Projector

**Files:**

- Create: `deeptutor/teaching/projectors/__init__.py`
- Create: `deeptutor/teaching/projectors/progress.py`
- Create: `deeptutor/teaching/projectors/mastery.py`
- Create: `deeptutor/teaching/projector_worker.py`
- Create: `tests/teaching/test_progress_projector.py`
- Create: `tests/teaching/test_mastery_projector.py`
- Modify: `deeptutor/teaching/processes.py`

- [ ] Step 1: 写“只有评分证据改变掌握度”失败测试

```python
@pytest.mark.parametrize(
    "event",
    [
        classroom_started(),
        scene_completed(),
        hint_used(),
        classroom_completed(),
    ],
)
async def test_engagement_events_do_not_change_mastery(projector, event):
    before = await projector.mastery("student-a", "kp-1")
    await projector.apply(event)
    after = await projector.mastery("student-a", "kp-1")
    assert after == before


async def test_duplicate_quiz_event_changes_mastery_once(projector):
    event = valid_quiz_graded(event_id="quiz-event-1", correct=True)
    await projector.apply(event)
    await projector.apply(event)
    assert await projector.evidence_count("student-a", "kp-1") == 1
    assert await projector.mastery("student-a", "kp-1") == compute_mastery([True])
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_progress_projector.py tests/teaching/test_mastery_projector.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现评分验证

`quiz.graded` 必须匹配已发布版本中的 assessment、题目、知识点和评分规则。选择题由服务端根据版本答案重新计算；主观题必须携带受信任评分器或教师评阅记录。`pbl.milestone_completed` 只有在里程碑存在、rubric 完整且评分来源受信任时进入掌握度证据。

- [ ] Step 4: 复用现有掌握度算法

```python
correctness = await repository.list_correctness(
    user_id=event.user_id,
    knowledge_point_id=event.knowledge_point_id,
)
level = compute_mastery(correctness)
await repository.upsert_mastery(
    user_id=event.user_id,
    knowledge_point_id=event.knowledge_point_id,
    level=level,
    last_evidence_event_id=event.event_id,
)
```

不得在教学模块复制另一套评分公式。现有 `LearningService.calculate_mastery()` 和课堂 Projector 都继续调用 `deeptutor.learning.mastery.compute_mastery`。

- [ ] Step 5: 实现投影 Worker

使用数据库租约领取 `learning_projection_queue`；一次事件的进度、attempt、evidence 和 mastery 更新在同一事务内完成。失败按原因重试或进入 quarantine；正常情况下 60 秒内可见。

- [ ] Step 6: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_progress_projector.py tests/teaching/test_mastery_projector.py deeptutor/learning/tests/test_mastery_choices.py deeptutor/learning/tests/test_guided_mastery_updates.py -q
```

Expected: PASS。

- [ ] Step 7: 提交

```powershell
git add deeptutor/teaching/projectors/__init__.py deeptutor/teaching/projectors/progress.py deeptutor/teaching/projectors/mastery.py deeptutor/teaching/projector_worker.py deeptutor/teaching/processes.py tests/teaching/test_progress_projector.py tests/teaching/test_mastery_projector.py
git commit -m "feat(teaching): project valid classroom mastery evidence"
```

## Task 5: 回写记忆并增加教学报表

**Files:**

- Create: `deeptutor/teaching/projectors/memory.py`
- Create: `deeptutor/teaching/services/reports.py`
- Create: `deeptutor/api/routers/teaching_reports.py`
- Create: `tests/teaching/test_classroom_memory_projector.py`
- Create: `tests/api/test_teaching_reports.py`
- Modify: `deeptutor/services/memory/paths.py`
- Modify: `deeptutor/api/main.py`

- [ ] Step 1: 写记忆表面和报表权限失败测试

```python
def test_classroom_is_a_registered_memory_surface():
    from deeptutor.services.memory.paths import SURFACES
    assert "classroom" in SURFACES


def test_teacher_report_is_limited_to_assigned_class(client, teacher_headers):
    response = client.get(
        "/api/v1/teaching-reports/classes/class-b",
        headers=teacher_headers,
    )
    assert response.status_code == 403
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_classroom_memory_projector.py tests/api/test_teaching_reports.py -q
```

Expected: FAIL。

- [ ] Step 3: 增加 `classroom` Memory Surface

原始事件摘要进入 L1，聚合后的困难知识点、完成情况和有效测验结果进入 L2；不得把完整答题正文、Provider 信息或其他学生数据写入个人记忆。后台处理时显式安装目标用户的 PathService 上下文。

- [ ] Step 4: 实现报表 API

```text
GET /api/v1/teaching-reports/classes/{class_id}
GET /api/v1/teaching-reports/classes/{class_id}/students/{user_id}
GET /api/v1/teaching-reports/classrooms/{version_id}
GET /api/v1/teaching-reports/quarantine
```

展示完成率、场景进度、有效测验、提示使用、PBL 里程碑、知识点掌握度和投影延迟。权限使用资源范围 `learning_event.read`。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_classroom_memory_projector.py tests/api/test_teaching_reports.py tests/services/memory -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/projectors/memory.py deeptutor/teaching/services/reports.py deeptutor/api/routers/teaching_reports.py deeptutor/services/memory/paths.py deeptutor/api/main.py tests/teaching/test_classroom_memory_projector.py tests/api/test_teaching_reports.py
git commit -m "feat(teaching): project classroom memory and reports"
```

## Task 6: 连接播放器事件和学习进度界面

**Files:**

- Create: `web/lib/classroom-events.ts`
- Create: `web/components/classroom/LearningProgressPanel.tsx`
- Create: `web/app/(workspace)/learn/classrooms/[versionId]/page.tsx`
- Create: `web/app/(utility)/teaching/reports/page.tsx`
- Create: `web/tests/classroom-events.test.ts`
- Modify: `web/components/classroom/ClassroomPlayer.tsx`
- Modify: `web/lib/learning-api.ts`
- Modify: `web/locales/en/app.json`
- Modify: `web/locales/zh/app.json`

- [ ] Step 1: 写批次、离线恢复和身份边界失败测试

```typescript
test("event payload excludes authoritative identity fields", () => {
  const event = toLearningEvent(sceneCompletedRuntimeEvent());
  assert.equal("tenant_id" in event, false);
  assert.equal("user_id" in event, false);
  assert.equal("classroom_version_id" in event, false);
});

test("accepted events leave the retry queue and duplicates also settle", async () => {
  const queue = createEventQueue([eventA(), eventB()]);
  await queue.flush(fakeResponse({ accepted: ["a"], duplicate: ["b"] }));
  assert.equal(queue.size, 0);
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
npm --prefix web run test:node
```

Expected: FAIL。

- [ ] Step 3: 实现事件队列

播放器把六类标准事件放入本地短期重试队列，最多 100 项或 15 秒发送一次。发送前申请单次事件票据；accepted 和 duplicate 都从队列移除，quarantined 显示诊断但不无限重试。

- [ ] Step 4: 实现学习与报表页面

学习页从服务端恢复游标，展示当前场景、总进度和完成状态。教师报表页按班级和知识点展示聚合，不允许前端下载超出授权范围的原始事件。

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
git --literal-pathspecs add web/lib/classroom-events.ts web/components/classroom/LearningProgressPanel.tsx "web/app/(workspace)/learn/classrooms/[versionId]/page.tsx" "web/app/(utility)/teaching/reports/page.tsx" web/tests/classroom-events.test.ts web/components/classroom/ClassroomPlayer.tsx web/lib/learning-api.ts web/locales/en/app.json web/locales/zh/app.json
git commit -m "feat(web): send classroom events and show progress"
```

## Task 7: 验收学习闭环

**Files:**

- Create: `tests/e2e/test_classroom_learning_loop.py`
- Create: `web/tests/e2e/classroom-learning-loop.spec.ts`

- [ ] Step 1: 覆盖正常学习

证明：

```text
enter -> start -> scene complete -> quiz grade -> hint -> PBL milestone
-> classroom complete -> progress/report/memory visible
```

- [ ] Step 2: 覆盖负向和恢复

证明：

```text
duplicate event -> no duplicate score
expired ticket -> rejected
replayed write ticket -> rejected
wrong version/scene/knowledge point -> quarantined
network interruption -> local retry -> accepted once
engagement-only events -> mastery unchanged
```

- [ ] Step 3: 运行验收

Run:

```powershell
python -m pytest tests/e2e/test_classroom_learning_loop.py tests/teaching/test_mastery_projector.py -q
npm --prefix web exec playwright -- test tests/e2e/classroom-learning-loop.spec.ts
```

Expected: PASS；正常投影在 60 秒目标内可见。

- [ ] Step 4: 提交

```powershell
git add tests/e2e/test_classroom_learning_loop.py web/tests/e2e/classroom-learning-loop.spec.ts
git commit -m "test(teaching): verify classroom learning loop"
```
