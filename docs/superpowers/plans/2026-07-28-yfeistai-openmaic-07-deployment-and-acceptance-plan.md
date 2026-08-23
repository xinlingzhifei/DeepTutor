# 私有化部署与首期验收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 yFeiSTAI、PostgreSQL、MinIO、持久化任务进程和锁版 OpenMAIC 部署在同一私有环境，以统一域名对外服务，并用安全、容量、故障和恢复证据完成首期发布。

**Architecture:** 生产 Compose 只暴露统一反向代理；PostgreSQL、MinIO、共享 OpenMAIC、Dispatcher、Worker、Reaper 和 Projector 仅在私有网络。独立数据面使用同一 OpenMAIC 镜像和契约、独立 Secret/Provider/存储网络，由平台数据面路由登记。所有镜像在 CI 构建并记录摘要，目标服务器不下载源码。

**Tech Stack:** Docker Compose、Nginx、PostgreSQL 16、MinIO/S3、OpenMAIC 0.3.1、Prometheus client、pytest、Playwright、Python async load harness

---

## Task 1: 建立平台 Compose 拓扑与锁版镜像

**Files:**

- Modify: `Dockerfile`
- Create: `.github/workflows/private-platform-images.yml`
- Create: `docker-compose.platform.yml`
- Create: `docker-compose.data-plane.yml`
- Create: `deploy/image-lock.json`
- Create: `deploy/platform.example.json`
- Create: `scripts/render_platform_compose.py`
- Create: `tests/scripts/test_platform_compose.py`
- Modify: `scripts/docker_compose.py`
- Modify: `tests/scripts/test_docker_compose.py`
- Modify: `docs/superpowers/plans/2026-07-28-yfeistai-openmaic-07-deployment-and-acceptance-plan.md`

- [ ] Step 1: 写网络暴露和服务依赖失败测试

```python
def test_internal_services_have_no_host_ports():
    compose = load_rendered_compose(
        "docker-compose.yml",
        "docker-compose.platform.yml",
    )
    for name, service in compose["services"].items():
        if name != "gateway":
            assert not service.get("ports"), name
    published = {
        str(port["published"])
        for port in compose["services"]["gateway"]["ports"]
    }
    assert published == {"80", "443"}


def test_api_waits_for_migration_and_storage():
    compose = load_rendered_compose(
        "docker-compose.yml",
        "docker-compose.platform.yml",
    )
    dependencies = compose["services"]["deeptutor"]["depends_on"]
    assert dependencies["teaching-migrate"]["condition"] == "service_completed_successfully"
    assert dependencies["minio-bootstrap"]["condition"] == "service_completed_successfully"
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/scripts/test_platform_compose.py tests/scripts/test_docker_compose.py -q
```

Expected: FAIL。

- [ ] Step 3: 增加平台服务

`docker-compose.platform.yml` 包含：

```text
gateway
postgres
minio
minio-bootstrap
teaching-migrate
tenant-provisioner
openmaic
openmaic-render
teaching-dispatcher
teaching-worker
teaching-export-worker
teaching-reaper
learning-projector
```

`deeptutor` 与以上服务合并到同一内部网络。`gateway` 是唯一具有主机端口的服务；平台覆盖文件使用 Docker Compose v2.24.4+ 的 `!reset []` 清除基础 Compose 中 `deeptutor` 和 `pocketbase` 的端口，测试必须检查两份文件合并后的配置而不是只解析覆盖文件。`tenant-provisioner` 是唯一同时拥有数据库迁移权限、租户 Secret 写目录和 MinIO bootstrap 凭据的内部进程；API 与普通 Worker 没有该权限。OpenMAIC 镜像由 `integrations/openmaic/Dockerfile` 构建为 `ghcr.io/xinlingzhifei/openmaic:0.3.1-0cf2a330`；`openmaic-render` 从同一锁版源码的 `render-service` 构建并发布为 `ghcr.io/xinlingzhifei/openmaic-render:0.3.1-0cf2a330`，只在私网接收 MP4 导出；平台应用镜像发布为 `ghcr.io/xinlingzhifei/deeptutor:first-release`。共享生成 Worker 初始并发 20；MP4 导出使用独立 Worker/槽位；数据库槽位仍是最终并发约束。

- [ ] Step 4: 增加独立数据面模板

`docker-compose.data-plane.yml` 只包含某一机构的 OpenMAIC、按租户策略启用的 Render Service 和必要私有依赖；使用 Compose project name、独立 Secret 和独立内部网络。它不加入共享 Provider 网络，也不暴露公共端口。

- [ ] Step 5: 扩展 Compose 包装器

支持：

```powershell
python scripts/docker_compose.py --platform up -d
python scripts/docker_compose.py --data-plane tenant-acme up -d
```

包装器从 `data/user/settings/platform.json` 渲染非敏感参数，从 Docker Secret 文件读取敏感值。不得把密钥写入 `docker.env`。

选择 `--platform` 或 `--data-plane` 后，包装器必须 fail-closed：拒绝调用方追加
`-f/--file`、`--env-file`、`--project-directory` 或 `-p/--project-name`
来改变受审计拓扑；平台模式拒绝所有 profile，独立数据面只允许显式
`mp4-export`。传给 Docker Compose 的环境必须移除宿主 `COMPOSE_FILE` 和
`COMPOSE_PROFILES`，防止启用基础 Compose 的本地源码构建、浮动镜像或覆盖
专属数据面 project。

- [ ] Step 6: 锁定镜像摘要

专用 CI workflow 以固定的 yFeiSTAI 提交和 OpenMAIC 提交构建并推送三张自定义镜像；三张镜像全部推送成功后运行：

```powershell
python scripts/render_platform_compose.py --write-image-lock
```

`deploy/image-lock.json` 记录 yFeiSTAI、OpenMAIC、OpenMAIC Render、Nginx、PostgreSQL 和 MinIO 的仓库、标签及实际内容摘要。生产渲染只接受带摘要的镜像。
远端摘要解析使用 `docker buildx imagetools inspect --raw` 返回的原始 manifest
字节计算 SHA-256；不得假设 manifest JSON 自带其自身摘要，也不得依赖本地镜像缓存。

- [ ] Step 7: 运行测试

Run:

```powershell
python -m pytest tests/scripts/test_platform_compose.py tests/scripts/test_docker_compose.py -q
docker compose -f docker-compose.yml -f docker-compose.platform.yml config --quiet
```

Expected: PASS。

- [ ] Step 8: 提交

```powershell
git add Dockerfile .github/workflows/private-platform-images.yml docker-compose.platform.yml docker-compose.data-plane.yml deploy/image-lock.json deploy/platform.example.json scripts/render_platform_compose.py scripts/docker_compose.py tests/scripts/test_platform_compose.py tests/scripts/test_docker_compose.py docs/superpowers/plans/2026-07-28-yfeistai-openmaic-07-deployment-and-acceptance-plan.md
git commit -m "feat(deploy): add private teaching platform topology"
```

## Task 2: 建立 Secret、迁移和启动前检查

**Files:**

- Create: `scripts/init_platform_secrets.py`
- Create: `scripts/provision_tenant_storage.py`
- Create: `scripts/migrate_teaching.py`
- Create: `scripts/platform_preflight.py`
- Create: `tests/scripts/test_platform_secrets.py`
- Create: `tests/scripts/test_tenant_storage_provisioning.py`
- Create: `tests/scripts/test_platform_preflight.py`
- Modify: `deeptutor/teaching/processes.py`
- Modify: `.gitignore`

- [ ] Step 1: 写密钥拒绝和迁移失败测试

```python
def test_secret_initializer_never_overwrites_existing_secret(tmp_path):
    target = tmp_path / "openmaic_service_secret"
    target.write_text("keep-me", encoding="utf-8")
    initialize_secret(target, bytes_count=32)
    assert target.read_text(encoding="utf-8") == "keep-me"


def test_preflight_rejects_world_readable_secret(tmp_path):
    target = tmp_path / "classroom_ticket_secret"
    target.write_text("secret", encoding="utf-8")
    make_overly_permissive(target)
    result = run_preflight(secret_dir=tmp_path)
    assert "classroom_ticket_secret permissions" in result.errors


async def test_provisioned_tenant_credentials_are_prefix_scoped(minio):
    tenant_a = await provision_tenant_storage(minio, "tenant-a")
    tenant_b = await provision_tenant_storage(minio, "tenant-b")
    await tenant_a.put("tenants/tenant-a/probe", b"a")
    await tenant_b.put("tenants/tenant-b/probe", b"b")
    with pytest.raises(ObjectStoreAccessDenied):
        await tenant_a.get("tenants/tenant-b/probe")
    assert tenant_a.secret_ref != tenant_b.secret_ref
```

同一 RED 还必须覆盖 `python -m deeptutor.teaching.processes tenant-provisioner`
的运行时装配：当平台对象存储为 S3/MinIO 时，进程必须注入可用的租户存储
管理适配器，并明确拒绝落入 `UnavailableS3TenantStorageAdmin`；仅验证 Compose
进程名不算完成。

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/scripts/test_platform_secrets.py tests/scripts/test_tenant_storage_provisioning.py tests/scripts/test_platform_preflight.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现 Secret 初始化

在 `data/system/secrets/` 创建缺失项：

```text
platform_database_password
platform_database_app_password
platform_database_migration_password
minio_bootstrap_access_key
minio_bootstrap_secret_key
classroom_ticket_secret
openmaic_service_secret
```

采用安全随机值、原子落盘和仅服务账户可读权限；已存在文件不覆盖，内容不打印。`platform_database_password` 仅用于首次 PostgreSQL 管理与角色初始化；角色初始化创建独立的应用角色和迁移角色，并分别读取 `platform_database_app_password` 与 `platform_database_migration_password`，普通 API 不获得管理或迁移凭据。MinIO bootstrap 凭据只挂载到 `minio`、`minio-bootstrap` 和一次性租户存储 provisioner，不挂载到 `deeptutor`、Worker 或 OpenMAIC。

TLS 的 `gateway_fullchain_pem` 与 `gateway_private_key_pem` 由部署方或证书自动化系统提供，初始化脚本不得生成自签名证书冒充生产证书。Compose 以只读 Secret 挂载，Preflight 校验证书域名、密钥匹配和剩余有效期至少 14 天。

- [ ] Step 4: 实现租户对象存储凭据初始化

`scripts/provision_tenant_storage.py --tenant-id <id>` 使用 bootstrap 凭据调用 MinIO 管理 API，为 `tenants/{tenant_id}/` 创建只允许该前缀读写、列举和删除的独立策略及服务账号。密钥写入：

```text
data/system/secrets/tenants/tenant_<sha256前16位>/object-store-access-key
data/system/secrets/tenants/tenant_<sha256前16位>/object-store-secret-key
```

数据库只登记目录 `secret_ref`、Access Key 指纹和状态。脚本必须幂等，默认不轮换已有凭据；显式 `--rotate` 先创建并验证新凭据，再原子切换 `secret_ref`，最后吊销旧凭据。测试用租户 A 的真实凭据读取、列举、写入租户 B 前缀必须被 MinIO 拒绝。

`python -m deeptutor.teaching.processes tenant-provisioner` 以租约领取
`tenant_provisioning_jobs`，依次创建并迁移租户 Schema、调用上述存储 provisioner、执行跨前缀负向探针，最后在一个平台事务中登记 `secret_ref` 并把租户切换为 `active`。任一步失败都保持 `provisioning_failed`，释放租约后可由同一幂等任务重试。

- [ ] Step 5: 实现迁移入口

`scripts/migrate_teaching.py` 先升级 `platform`，再锁定租户目录并逐个升级租户 Schema。任何租户失败都使启动检查失败，并输出租户 ID、Schema 和 revision，不输出连接密码。

- [ ] Step 6: 实现 Preflight

检查：

```text
platform settings
required secret files and permissions
gateway TLS certificate, private key, domain match and expiry
database connectivity and migrations
bucket existence and write/read probe
every active tenant has a readable secret_ref
tenant credential own-prefix probe and cross-prefix denial probe
OpenMAIC health and contract 1.0
image lock match
shared slot count 20
standard tenant slot count 2
Docker Compose >= 2.24.4 and only gateway has public ports
```

- [ ] Step 7: 运行测试

Run:

```powershell
python -m pytest tests/scripts/test_platform_secrets.py tests/scripts/test_tenant_storage_provisioning.py tests/scripts/test_platform_preflight.py -q
python scripts/platform_preflight.py --config deploy/platform.example.json --offline-contract-check
```

Expected: PASS。

- [ ] Step 8: 提交

```powershell
git add scripts/init_platform_secrets.py scripts/provision_tenant_storage.py scripts/migrate_teaching.py scripts/platform_preflight.py deeptutor/teaching/processes.py tests/scripts/test_platform_secrets.py tests/scripts/test_tenant_storage_provisioning.py tests/scripts/test_platform_preflight.py .gitignore
git commit -m "feat(deploy): validate teaching secrets and migrations"
```

## Task 3: 配置统一域名反向代理

**Files:**

- Create: `deploy/nginx/yfeistai-classroom.conf`
- Create: `deploy/nginx/Dockerfile.check`
- Create: `tests/deploy/test_nginx_classroom_routes.py`

- [ ] Step 1: 写公开路由失败测试

```python
def test_openmaic_is_not_publicly_proxied():
    config = Path("deploy/nginx/yfeistai-classroom.conf").read_text(encoding="utf-8")
    assert "proxy_pass http://openmaic" not in config
    assert "location /openmaic" not in config


def test_only_yfeistai_origin_is_public():
    config = Path("deploy/nginx/yfeistai-classroom.conf").read_text(encoding="utf-8")
    assert "proxy_pass http://deeptutor" in config
    assert "client_max_body_size 100m" in config
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/deploy/test_nginx_classroom_routes.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现代理

80 端口只重定向到 HTTPS；443 使用只读挂载的正式证书。统一域名只转发到 yFeiSTAI Web/API。课堂 JSON、媒体、事件和导出继续走 `/api/v1/...`；WebSocket 保留升级头；上传和下载设置合理超时、大小限制、HSTS、`X-Content-Type-Options` 和私有缓存策略。

OpenMAIC 没有公开 location、登录入口或直接静态资源路径。

- [ ] Step 4: 增加可重复的 Nginx 配置检查镜像

`deploy/nginx/Dockerfile.check`：

```dockerfile
ARG NGINX_IMAGE
FROM ${NGINX_IMAGE}
COPY yfeistai-classroom.conf /etc/nginx/conf.d/default.conf
RUN nginx -t
```

`NGINX_IMAGE` 必须由 `deploy/image-lock.json` 中的带摘要引用提供；Dockerfile 没有浮动默认值。

- [ ] Step 5: 验证配置

Run:

```powershell
python -m pytest tests/deploy/test_nginx_classroom_routes.py -q
$nginxImage = python scripts/render_platform_compose.py --print-image nginx
docker build --pull=false --build-arg "NGINX_IMAGE=$nginxImage" -f deploy/nginx/Dockerfile.check deploy/nginx
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deploy/nginx/yfeistai-classroom.conf deploy/nginx/Dockerfile.check tests/deploy/test_nginx_classroom_routes.py
git commit -m "feat(deploy): proxy classrooms through yFeiSTAI only"
```

## Task 4: 增加健康、指标和脱敏日志

**Files:**

- Create: `deeptutor/teaching/health.py`
- Create: `deeptutor/teaching/metrics.py`
- Create: `deeptutor/api/routers/teaching_health.py`
- Create: `tests/teaching/test_teaching_health.py`
- Create: `tests/teaching/test_teaching_metrics.py`
- Create: `tests/teaching/test_log_redaction.py`
- Modify: `deeptutor/api/main.py`
- Modify: `pyproject.toml`
- Modify: `requirements/server.txt`

- [ ] Step 1: 写健康和脱敏失败测试

```python
def test_health_reports_degraded_when_dispatcher_heartbeat_is_stale(service):
    service.set_heartbeat("dispatcher", age_seconds=91)
    report = service.report()
    assert report.status == "degraded"
    assert report.components["dispatcher"].status == "stale"


def test_teaching_logs_redact_sensitive_values(caplog):
    log_generation_failure(
        tenant_id="tenant-a",
        job_id="job-a",
        source_text="private textbook content",
        provider_key="sk-secret",
    )
    assert "private textbook content" not in caplog.text
    assert "sk-secret" not in caplog.text
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_teaching_health.py tests/teaching/test_teaching_metrics.py tests/teaching/test_log_redaction.py -q
```

Expected: FAIL。

- [ ] Step 3: 增加指标依赖和接口

在根 `[project].dependencies`、`[project.optional-dependencies].server` 和
`requirements/server.txt` 同时加入：

```text
prometheus-client>=0.21.1,<1.0.0
```

内部指标至少包括：

```text
yfeistai_generation_queue_seconds
yfeistai_generation_stage_seconds
yfeistai_generation_jobs_total
yfeistai_generation_retries_total
yfeistai_generation_slots_in_use
yfeistai_quota_units_total
yfeistai_learning_events_total
yfeistai_learning_projection_lag_seconds
yfeistai_artifact_validation_failures_total
yfeistai_openmaic_health
```

租户标签使用不可逆短哈希，禁止 user_id、来源文本和密钥进入标签。

- [ ] Step 4: 实现健康接口

```text
GET /api/v1/system/teaching-health
GET /internal/metrics
```

前者供管理员查看，后者只在私有网络开放。健康报告包含 DB、迁移、对象存储、Tenant Provisioner、Dispatcher、Generation Worker、Export Worker、Projector、共享 OpenMAIC/Render 数据面和已登记独立数据面。

- [ ] Step 5: 运行测试

Run:

```powershell
python -m pytest tests/teaching/test_teaching_health.py tests/teaching/test_teaching_metrics.py tests/teaching/test_log_redaction.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add deeptutor/teaching/health.py deeptutor/teaching/metrics.py deeptutor/api/routers/teaching_health.py deeptutor/api/main.py pyproject.toml requirements/server.txt tests/teaching/test_teaching_health.py tests/teaching/test_teaching_metrics.py tests/teaching/test_log_redaction.py
git commit -m "feat(teaching): observe classroom runtime health"
```

## Task 5: 建立备份、恢复和独立数据面演练

**Files:**

- Modify: `Dockerfile`
- Modify: `docker-compose.platform.yml`
- Modify: `deeptutor/services/config/platform_settings.py`
- Modify: `deploy/platform.example.json`
- Create: `scripts/backup_teaching.py`
- Create: `scripts/restore_teaching_validation.py`
- Create: `scripts/register_data_plane.py`
- Create: `tests/scripts/test_teaching_backup_manifest.py`
- Create: `tests/integration/test_teaching_restore.py`
- Create: `tests/integration/test_dedicated_data_plane.py`
- Modify: `tests/scripts/test_docker_compose.py`
- Modify: `tests/scripts/test_platform_compose.py`
- Modify: `tests/services/config/test_platform_settings.py`
- Modify: existing S3 `PlatformSettings` fixtures to supply the stable test namespace ID

- [ ] Step 1: 写备份清单和无共享回退失败测试

```python
def test_backup_manifest_binds_database_and_objects(manifest):
    assert manifest.database.sha256
    assert manifest.object_inventory_sha256
    assert manifest.classroom_versions_count >= 0
    assert manifest.learning_events_count >= 0


async def test_private_tenant_never_uses_shared_provider(harness):
    harness.stop_dedicated_plane("tenant-private")
    result = await harness.submit_generation("tenant-private")
    assert result.status == "failed"
    assert result.error_code == "dedicated_data_plane_unavailable"
    assert harness.shared_plane.requests_for("tenant-private") == 0
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/scripts/test_teaching_backup_manifest.py tests/integration/test_teaching_restore.py tests/integration/test_dedicated_data_plane.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现一致性备份

备份脚本创建 PostgreSQL 一致性 dump、对象清单、Schema revisions、课堂版本计数、事件计数和 SHA-256 清单。输出目录由调用者显式提供；脚本不清理旧备份。

- [ ] Step 4: 实现非破坏性恢复验证

备份清单以受摘要保护的稳定对象存储 namespace ID + bucket 绑定源命名空间；该 ID 由运维显式配置且不得随 endpoint/CNAME/代理入口变化。恢复脚本只恢复到新数据库和与源身份不同的专属新对象桶；目标桶必须为空且已启用版本化，并按运行时可直接读取的 canonical `tenants/<tenant-id>/...` key 恢复。脚本验证目标 ETag/VersionId、来源快照、媒体、事件、配额、审计关联和 `yfeistai_app` 权限后输出报告，不覆盖当前环境。仅使用同桶 `restore-validation/<run-id>/` 前缀不满足本合同，因为数据库中的 canonical object key 会与物理对象 key 脱节。

- [ ] Step 5: 实现独立数据面登记

`register_data_plane.py` 校验健康、契约、Secret 引用和目标租户后写入平台控制 Schema。密钥仍留在 Secret 文件；共享池无法读取独立数据面 Provider。

- [ ] Step 6: 运行演练

Run:

```powershell
python -m pytest tests/scripts/test_teaching_backup_manifest.py tests/integration/test_teaching_restore.py tests/integration/test_dedicated_data_plane.py -q
```

Expected: PASS。

- [ ] Step 7: 提交

```powershell
git add scripts/backup_teaching.py scripts/restore_teaching_validation.py scripts/register_data_plane.py tests/scripts/test_teaching_backup_manifest.py tests/integration/test_teaching_restore.py tests/integration/test_dedicated_data_plane.py
git commit -m "feat(deploy): verify backup and dedicated data planes"
```

## Task 6: 建立安全与容量测试

**Files:**

- Create: `scripts/load_classroom.py`
- Create: `tests/security/test_classroom_security.py`
- Create: `tests/load/test_scheduler_capacity.py`
- Create: `tests/load/test_learning_event_capacity.py`

- [ ] Step 1: 写容量断言

```python
def test_first_release_profile():
    profile = load_profile("first-release")
    assert profile.tenants == 50
    assert profile.registered_users == 100_000
    assert profile.daily_active_users == 10_000
    assert profile.concurrent_classrooms == 200
    assert profile.shared_generation_slots == 20
    assert profile.default_tenant_slots == 2
```

- [ ] Step 2: 实现模拟 Provider 压测

`scripts/load_classroom.py --profile first-release` 使用可控延迟和错误率的模拟 OpenMAIC Provider，验证：

```text
50 tenants
20 concurrent shared generation jobs
2 default slots per standard tenant
fair scheduling under one noisy tenant
200 concurrent classroom sessions
event ingest p95 below 1 second
non-generation core API p95 below 500 ms
job submission visible within 2 seconds
mastery projection visible within 60 seconds
```

- [ ] Step 3: 实现安全测试

覆盖：

```text
cross-tenant database reads
cross-prefix object reads
ticket expiry and replay
service signature tampering
forged tenant/user/version fields
interactive iframe message spoofing
unsupported MIME and oversized artifacts
provider secret exposure in APIs and logs
public OpenMAIC route probing
```

- [ ] Step 4: 运行门禁

Run:

```powershell
python -m pytest tests/security/test_classroom_security.py tests/load/test_scheduler_capacity.py tests/load/test_learning_event_capacity.py -q
python scripts/load_classroom.py --profile first-release
```

Expected: PASS；压测报告保存原始样本、p50/p95/p99、错误率和资源占用。

- [ ] Step 5: 提交

```powershell
git add scripts/load_classroom.py tests/security/test_classroom_security.py tests/load/test_scheduler_capacity.py tests/load/test_learning_event_capacity.py
git commit -m "test(teaching): gate security and first-release capacity"
```

## Task 7: 建立一键发布验证器和最终浏览器验收

**Files:**

- Create: `scripts/verify_classroom_release.py`
- Create: `tests/scripts/test_verify_classroom_release.py`
- Create: `web/tests/e2e/classroom-first-release.spec.ts`

- [ ] Step 1: 写验证器失败条件

```python
def test_verifier_fails_when_any_business_flow_is_missing(fake_runtime):
    fake_runtime.set_result("teacher_flow", "pass")
    fake_runtime.set_result("student_micro_flow", "pass")
    result = verify(fake_runtime)
    assert result.ok is False
    assert "content_operations_flow" in result.missing
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/scripts/test_verify_classroom_release.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现发布验证器

验证器报告以下层次，不能混为一个“部署成功”：

```text
source_head
image_digests
database_revisions
running_containers
service_health
public_routes
teacher_flow
student_micro_flow
student_full_flow
content_operations_flow
learning_loop
export_formats
tenant_isolation
dedicated_data_plane
backup_restore
capacity_profile
```

- [ ] Step 4: 实现统一浏览器验收

同一套环境依次执行教师、学生微课堂、学生完整课堂、教研批量、审核、发布、播放、课堂 ZIP/PPTX/离线 HTML/MP4 导出、事件回传和报表。切换两个租户证明导航、列表、文件、导出和事件不串租户。

- [ ] Step 5: 构建并启动候选版本

Run:

```powershell
python scripts/docker_compose.py --platform build
python scripts/docker_compose.py --platform up -d
python scripts/platform_preflight.py
```

Expected: 所有服务达到健康状态，迁移和 MinIO bootstrap 完成。

- [ ] Step 6: 执行完整门禁

Run:

```powershell
python -m pytest tests/teaching tests/integration tests/security tests/load -q
npm --prefix web run test:node
npm --prefix web run i18n:check
npm --prefix web run lint
npm --prefix web run build
npm --prefix web exec playwright -- test tests/e2e/classroom-first-release.spec.ts
python scripts/verify_classroom_release.py
python scripts/load_classroom.py --profile first-release
```

Expected: 全部通过。

- [ ] Step 7: 受控真实 Provider 冒烟

用一个平台 Provider 和一个独立数据面 Provider 各生成一门代表性课堂，人工检查大纲、DSL、媒体、引用、编辑、发布、播放和事件回传；至少用平台课堂真实导出课堂 ZIP、PPTX、离线 HTML 和 MP4 并打开检查。密钥不写入测试结果。

- [ ] Step 8: 提交

```powershell
git add scripts/verify_classroom_release.py tests/scripts/test_verify_classroom_release.py web/tests/e2e/classroom-first-release.spec.ts
git commit -m "test(release): verify all classroom first-release flows"
```

## Task 8: 首期发布判定

- [ ] Step 1: 记录实际版本

```powershell
git rev-parse HEAD
docker compose -f docker-compose.yml -f docker-compose.platform.yml ps
python scripts/verify_classroom_release.py --json
```

- [ ] Step 2: 逐项核对已批准范围

发布报告必须明确：

```text
教师备课与发布：通过
学生微课堂：通过
学生完整课堂：通过
内容运营生产：通过
课堂 ZIP、PPTX、离线 HTML 和 MP4 导出：通过
唯一身份源：通过
多租户混合隔离：通过
共享和独立数据面：通过
来源约束和开放创作：通过
学习事件、记忆和掌握度：通过
Tailwind 4 全站回归：通过
唯一公网网关与内部服务零主机端口：通过
私有化部署、备份与恢复：通过
```

- [ ] Step 3: 只有所有项通过才标记首期完成

任何业务链路、隔离证明、恢复演练或容量门禁缺失时，报告状态为 `not_ready`，列出具体阻塞项，不以构建成功、容器运行或局部测试替代。
