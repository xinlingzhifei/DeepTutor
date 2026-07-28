# yFeiSTAI × OpenMAIC 首期交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在一个首期版本内同时交付教师备课发布、学生按需生成、教研内容运营三条业务线，并证明多租户隔离、唯一身份源、学习数据回传和私有化部署满足已批准设计。

**Architecture:** yFeiSTAI 是唯一身份源、业务控制面和学习数据事实源；OpenMAIC 是锁版、独立容器运行的生成与导出引擎。两者通过持久化任务、服务签名和版本化契约协作，浏览器只访问 yFeiSTAI；前端通过 `openmaic-adapter` 直接使用 OpenMAIC DSL、Renderer、Importer 和编辑入口。

**Tech Stack:** Python 3.11、FastAPI、Pydantic 2、SQLAlchemy 2 async、PostgreSQL 16、Alembic、S3/MinIO、Next.js 16、React 19、TypeScript 5、Tailwind CSS 4、OpenMAIC 0.3.1、pytest、Node test、Vitest、Playwright、Docker Compose

---

## 固定基线

- 已批准设计：`docs/superpowers/specs/2026-07-28-yfeistai-openmaic-integration-design.md`
- yFeiSTAI 设计基线提交：`194d7676c764ffadf6f0e116a08d80bcaccb2ef2`
- OpenMAIC 上游提交：`0cf2a330411681190e89f48e20f305345ff99f87`
- OpenMAIC 应用版本：`0.3.1`
- `@openmaic/dsl`：`0.4.0`
- `@openmaic/renderer`：`0.0.3`
- `@openmaic/importer`：`0.1.0`
- 首期容量：50 个机构、10 万注册用户、1 万日活、200 个同时在线课堂、平台 20 个生成槽位、标准租户默认 2 个生成槽位。

上述版本必须精确锁定到 lockfile 或提交 SHA；不得使用 `latest`、`main` 或浮动版本范围作为生产输入。

## 计划套件与执行顺序

| 顺序 | 计划 | 交付边界 | 完成后门禁 |
|---|---|---|---|
| 1 | [平台与租户底座](./2026-07-28-yfeistai-openmaic-01-platform-foundation-plan.md) | PostgreSQL、Schema、租户上下文、权限、对象存储 | 跨租户负向测试、迁移测试、旧模式回归 |
| 2 | [OpenMAIC 引擎与任务内核](./2026-07-28-yfeistai-openmaic-02-engine-and-jobs-plan.md) | 双阶段生成、四类导出、服务签名、Outbox、队列、公平调度、产物晋级 | 契约测试、重启恢复、取消与幂等测试 |
| 3 | [Tailwind 4 与前端适配](./2026-07-28-yfeistai-openmaic-03-web-adapter-plan.md) | 全站 Tailwind 4、SDK 适配、编辑器、播放器 | 构建、类型、Node 测试、视觉矩阵 |
| 4 | [教师与内容运营](./2026-07-28-yfeistai-openmaic-04-teacher-and-operations-plan.md) | PDF/知识库来源、备课、审核、发布、版本、受控导出、批量生产 | 教师和教研端到端流程 |
| 5 | [学生按需生成](./2026-07-28-yfeistai-openmaic-05-student-generation-plan.md) | Capability、微课堂/完整课堂、策略、配额、超额审批 | Capability、策略和学生端到端流程 |
| 6 | [学习事件与掌握度](./2026-07-28-yfeistai-openmaic-06-learning-events-plan.md) | 学习会话、事件幂等、测验、记忆、掌握度、报表 | 重复事件、伪造事件、聚合延迟测试 |
| 7 | [部署与首期验收](./2026-07-28-yfeistai-openmaic-07-deployment-and-acceptance-plan.md) | 私有化拓扑、密钥、观测、安全、容量、备份恢复 | 三条业务线同时启用后的发布门禁 |

第 1 项是其余后端计划的前置条件；第 2 项和第 3 项在第 1 项完成后可以并行；第 4、5、6 项共享同一领域与任务内核，按表中顺序集成；第 7 项只在前三条业务线均通过聚焦测试后执行。

## 跨计划不变量

1. 现有 CLI、单用户、本地文件工作区与非教学 Capability 在 `teaching.enabled=false` 时保持原行为。
2. 浏览器不直接调用 OpenMAIC，不接收 Provider 密钥，也不决定数据面路由。
3. 活跃租户由服务端从已认证身份和成员关系解析；客户端选择只能在其成员列表内生效。
4. 所有租户业务表、队列记录、对象键、审计和日志关联信息都具有可验证的租户归属。
5. 发布版本不可覆盖；已分配班级固定到版本；迁移必须产生显式操作和审计记录。
6. OpenMAIC 临时目录不是正式存储；只有 yFeiSTAI 校验并晋级的 DSL、媒体和导出文件可被发布；课堂 ZIP、PPTX、离线 HTML 和 MP4 都固定输入哈希。
7. 标准租户只使用平台 Provider；BYOK、私有 API 和数据不出域租户只路由到其独立数据面。
8. 学习事件是追加事实；只有绑定知识点且具备有效评分证据的事件可以改变掌握度。
9. 共享生成池总槽位为 20，标准租户默认 2；批量任务不得饿死学生微课堂和教师任务。
10. 三条业务线必须在同一个首期发布候选中同时打开，不以任何一个局部切片替代首期完成。

## Task 1: 建立执行基线

**Files:**

- Verify: `docs/superpowers/specs/2026-07-28-yfeistai-openmaic-integration-design.md`
- Verify: `pyproject.toml`
- Verify: `web/package-lock.json`
- Verify: `docker-compose.yml`

- [ ] Step 1: 确认工作区只包含已知用户改动

Run:

```powershell
git status --short
git rev-parse HEAD
```

Expected: HEAD 包含设计提交；任何既有未跟踪或未提交文件都记录在执行日志中且不被后续提交带入。

- [ ] Step 2: 运行改动前基线

Run:

```powershell
python -m pytest tests/api/test_auth_contextvar.py tests/multi_user tests/scripts/test_docker_compose.py -q
npm --prefix web run test:node
npm --prefix web run build
```

Expected: 记录每条命令的通过结果；如存在与本项目无关的基线失败，保留完整错误并单独归类，不修改无关代码来掩盖失败。

- [ ] Step 3: 建立首期验收记录

在当前执行任务的工作日志中记录：

```text
yFeiSTAI_HEAD=<实际提交>
OpenMAIC_PIN=0cf2a330411681190e89f48e20f305345ff99f87
baseline_backend=<pass 或已归类失败>
baseline_web_tests=<pass 或已归类失败>
baseline_web_build=<pass 或已归类失败>
```

Expected: 后续每个子计划都能引用同一份基线。

## Task 2: 按依赖顺序执行子计划

- [ ] Step 1: 完成计划 01，并确认数据库、租户和对象存储门禁通过。
- [ ] Step 2: 完成计划 02，并确认真实的两阶段生成契约已经可用。
- [ ] Step 3: 完成计划 03，并确认 Tailwind 4 全站回归和课堂适配器通过。
- [ ] Step 4: 完成计划 04，并确认教师及教研流程均能产生不可变发布版本。
- [ ] Step 5: 完成计划 05，并确认学生可选择微课堂或完整课堂且策略生效。
- [ ] Step 6: 完成计划 06，并确认事件、记忆和掌握度只有一个服务端事实源。
- [ ] Step 7: 完成计划 07，并在同一发布候选上执行完整验收。

每完成一个子计划，运行：

```powershell
git status --short
git log -1 --oneline
```

Expected: 提交只包含该子计划列出的文件；用户原有未跟踪文件仍保持未跟踪。

## Task 3: 首期总验收

**Files:**

- Test: `tests/e2e/test_teacher_classroom_flow.py`
- Test: `tests/e2e/test_student_classroom_flow.py`
- Test: `tests/e2e/test_content_operations_flow.py`
- Test: `tests/integration/test_tenant_isolation.py`
- Test: `tests/integration/test_generation_recovery.py`
- Test: `web/tests/e2e/classroom-first-release.spec.ts`
- Verify: `docker-compose.platform.yml`

- [ ] Step 1: 执行后端契约、隔离与恢复测试

Run:

```powershell
python -m pytest tests/teaching tests/integration/test_tenant_isolation.py tests/integration/test_generation_recovery.py -q
```

Expected: 全部通过。

- [ ] Step 2: 执行前端和三条业务线浏览器验收

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run i18n:check
npm --prefix web run build
npm --prefix web exec playwright -- test tests/e2e/classroom-first-release.spec.ts
```

Expected: 全部通过，桌面与移动端、明暗主题、中英文均有证据。

- [ ] Step 3: 执行部署、安全、容量与恢复门禁

Run:

```powershell
python scripts/verify_classroom_release.py
python scripts/load_classroom.py --profile first-release
```

Expected: 验证器确认服务健康、迁移版本、对象存储、20 个平台槽位、每租户 2 个默认槽位、200 个并发播放会话、跨租户拒绝、票据过期拒绝、任务重启恢复和备份恢复。

- [ ] Step 4: 核对首期范围

必须同时给出以下证据：

```text
teacher_flow=pass
student_micro_flow=pass
student_full_flow=pass
content_operations_flow=pass
classroom_exports=pass
tenant_isolation=pass
learning_event_idempotency=pass
openmaic_shared_plane=pass
openmaic_dedicated_plane=pass
tailwind4_visual_matrix=pass
backup_restore=pass
gateway_only_public=pass
```

Expected: 任一项缺失时不得宣称首期完成。
