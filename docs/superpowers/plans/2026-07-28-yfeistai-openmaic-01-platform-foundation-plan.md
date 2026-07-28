# yFeiSTAI 教学平台与租户底座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏现有单用户/CLI 模式的前提下，建立可供 50 个机构使用的 PostgreSQL 多租户底座、范围权限、对象存储边界和服务端租户上下文。

**Architecture:** 教学平台由显式开关启用。控制数据位于 PostgreSQL `platform` Schema；每个租户使用服务端派生的 `tenant_<sha256前16位>` Schema，并通过 SQLAlchemy `schema_translate_map` 访问。身份仍来自 yFeiSTAI 现有登录，成员关系和资源权限由教学平台数据库补充；浏览器通过服务端校验后的 HttpOnly 活跃租户 Cookie 切换机构。

**Tech Stack:** FastAPI、Pydantic 2、SQLAlchemy 2 async、asyncpg、Alembic、PostgreSQL 16、boto3、MinIO、pytest、Docker Compose

---

## Task 1: 增加教学平台依赖和 fail-closed 配置

**Files:**

- Create: `deeptutor/services/config/platform_settings.py`
- Create: `tests/services/config/test_platform_settings.py`
- Modify: `deeptutor/services/config/__init__.py`
- Modify: `pyproject.toml`
- Modify: `requirements/server.txt`
- Modify: `requirements/dev.txt`

- [ ] Step 1: 写配置失败测试

```python
def test_enabled_platform_requires_database_url(tmp_path, monkeypatch):
    from deeptutor.services.config.platform_settings import load_platform_settings

    settings = tmp_path / "platform.json"
    settings.write_text('{"enabled": true}', encoding="utf-8")
    monkeypatch.delenv("DEEPTUTOR_PLATFORM_DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="database_url"):
        load_platform_settings(settings)


def test_disabled_platform_keeps_local_mode(tmp_path):
    from deeptutor.services.config.platform_settings import load_platform_settings

    settings = tmp_path / "platform.json"
    settings.write_text('{"enabled": false}', encoding="utf-8")

    loaded = load_platform_settings(settings)
    assert loaded.enabled is False
    assert loaded.database_url is None
```

- [ ] Step 2: 运行测试并确认失败

Run:

```powershell
python -m pytest tests/services/config/test_platform_settings.py -q
```

Expected: FAIL，模块尚不存在。

- [ ] Step 3: 实现最小配置模型

```python
class PlatformSettings(BaseModel):
    enabled: bool = False
    database_url: SecretStr | None = None
    database_host: str = "postgres"
    database_port: int = 5432
    database_name: str = "yfeistai"
    database_user: str = "yfeistai"
    database_password_file: Path | None = None
    object_store_mode: Literal["local", "s3"] = "local"
    object_store_endpoint: str | None = None
    object_store_bucket: str = "yfeistai-classrooms"
    object_store_region: str = "us-east-1"
    object_store_tenant_credentials_dir: Path | None = None
    classroom_ticket_secret_file: Path | None = None
    openmaic_service_secret_file: Path | None = None
    shared_generation_limit: int = 20
    default_tenant_generation_limit: int = 2

    @model_validator(mode="after")
    def validate_enabled_runtime(self) -> "PlatformSettings":
        if (
            self.enabled
            and self.database_url is None
            and self.database_password_file is None
        ):
            raise ValueError(
                "platform database_url or database_password_file is required when enabled"
            )
        if self.enabled and self.object_store_mode == "s3":
            required = (
                self.object_store_endpoint,
                self.object_store_tenant_credentials_dir,
            )
            if not all(required):
                raise ValueError(
                    "S3 endpoint and tenant credentials directory are required"
                )
        return self
```

从 `data/user/settings/platform.json` 读取非敏感配置；本地测试可以用进程环境覆盖数据库 URL，生产通过主机、端口、库名、用户和 Docker Secret 密码文件构造 URL。S3 只配置租户凭据根目录，实际 Access Key/Secret Key 由 `secret_ref` 逐租户解析；yFeiSTAI API/Worker 不挂载 MinIO 管理员凭据。其他密钥也只从显式文件路径读取。不得读取项目根目录 `.env`。

- [ ] Step 4: 同步依赖

在根项目依赖、`server` extra 和 `requirements/server.txt` 中加入同一组约束：

```text
SQLAlchemy>=2.0.36,<2.1.0
asyncpg>=0.30.0,<1.0.0
alembic>=1.14.0,<2.0.0
boto3>=1.35.0,<2.0.0
```

在 `dev` extra 和 `requirements/dev.txt` 中加入：

```text
testcontainers[postgres]>=4.9.0,<5.0.0
```

MinIO 集成测试使用 `testcontainers.core.container.DockerContainer` 启动锁版镜像；
不依赖不存在或版本不稳定的 `minio` extra。

- [ ] Step 5: 运行聚焦测试

Run:

```powershell
python -m pytest tests/services/config/test_platform_settings.py tests/services/config/test_runtime_settings.py -q
```

Expected: PASS。

- [ ] Step 6: 提交

```powershell
git add pyproject.toml requirements/server.txt requirements/dev.txt deeptutor/services/config/platform_settings.py deeptutor/services/config/__init__.py tests/services/config/test_platform_settings.py
git commit -m "feat(teaching): add platform runtime settings"
```

## Task 2: 建立数据库会话和双范围迁移

**Files:**

- Create: `alembic.ini`
- Create: `deeptutor/teaching/__init__.py`
- Create: `deeptutor/teaching/database.py`
- Create: `deeptutor/teaching/schema_names.py`
- Create: `deeptutor/teaching/models/__init__.py`
- Create: `deeptutor/teaching/models/platform.py`
- Create: `deeptutor/teaching/models/tenant.py`
- Create: `deeptutor/teaching/migrations/env.py`
- Create: `deeptutor/teaching/migrations/script.py.mako`
- Create: `deeptutor/teaching/migrations/versions/20260728_0001_foundation.py`
- Create: `tests/teaching/test_schema_names.py`
- Create: `tests/teaching/test_database_scope.py`
- Create: `tests/teaching/integration/test_foundation_migration.py`

- [ ] Step 1: 写 Schema 名称和会话隔离失败测试

```python
def test_tenant_schema_is_deterministic_and_not_raw_input():
    from deeptutor.teaching.schema_names import tenant_schema_name

    schema = tenant_schema_name("org/acme")
    assert schema.startswith("tenant_")
    assert "/" not in schema
    assert schema == tenant_schema_name("org/acme")
    assert schema != tenant_schema_name("org/other")


async def test_tenant_session_uses_schema_translate_map(fake_engine):
    from deeptutor.teaching.database import tenant_connection

    async with tenant_connection(fake_engine, "t_acme"):
        pass

    assert fake_engine.execution_options_seen == {
        "schema_translate_map": {"tenant": "tenant_bf4fcb0bb5997635"}
    }
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_schema_names.py tests/teaching/test_database_scope.py -q
```

Expected: FAIL，教学数据库模块尚不存在。

- [ ] Step 3: 实现派生 Schema 和会话工厂

```python
def tenant_schema_name(tenant_id: str) -> str:
    normalized = tenant_id.strip()
    if not normalized:
        raise ValueError("tenant_id is required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"tenant_{digest}"


@asynccontextmanager
async def tenant_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    schema = tenant_schema_name(tenant_id)
    engine = get_platform_engine().execution_options(
        schema_translate_map={"tenant": schema}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
```

控制模型使用固定 `platform` Schema；租户模型声明逻辑 Schema `tenant`。首个迁移至少建立：

```text
platform.tenants
platform.tenant_memberships
platform.role_grants
platform.data_plane_routes
platform.tenant_storage_credentials
platform.tenant_provisioning_jobs
platform.audit_log
tenant.courses
tenant.classes
tenant.enrollments
```

`platform.tenant_storage_credentials` 只保存 `tenant_id`、`secret_ref`、Access Key
指纹、状态和轮换时间，不保存 Access Key 或 Secret Key 明文。

- [ ] Step 4: 配置 Alembic 双范围执行

`env.py` 接受以下两种明确命令：

```powershell
python -m alembic -x scope=platform upgrade head
python -m alembic -x scope=tenant -x tenant_schema=tenant_bf4fcb0bb5997635 upgrade head
```

平台范围的版本表位于 `platform.alembic_version`；每个租户的版本表位于自己的 Schema。`scope=tenant` 时必须校验 Schema 名只匹配 `^tenant_[0-9a-f]{16}$`。

- [ ] Step 5: 运行迁移集成测试

Run:

```powershell
python -m pytest tests/teaching/integration/test_foundation_migration.py -q
```

Expected: PASS；测试 PostgreSQL 中存在平台 Schema、两个不同租户 Schema 和各自独立的版本表。

- [ ] Step 6: 运行格式与聚焦测试

Run:

```powershell
python -m ruff check deeptutor/teaching tests/teaching
python -m pytest tests/teaching/test_schema_names.py tests/teaching/test_database_scope.py -q
```

Expected: PASS。

- [ ] Step 7: 提交

```powershell
git add alembic.ini deeptutor/teaching/__init__.py deeptutor/teaching/database.py deeptutor/teaching/schema_names.py deeptutor/teaching/models/__init__.py deeptutor/teaching/models/platform.py deeptutor/teaching/models/tenant.py deeptutor/teaching/migrations/env.py deeptutor/teaching/migrations/script.py.mako deeptutor/teaching/migrations/versions/20260728_0001_foundation.py tests/teaching/test_schema_names.py tests/teaching/test_database_scope.py tests/teaching/integration/test_foundation_migration.py
git commit -m "feat(teaching): add tenant schema database foundation"
```

## Task 3: 建立租户成员关系、角色授权与活跃租户上下文

**Files:**

- Create: `deeptutor/teaching/tenant_context.py`
- Create: `deeptutor/teaching/repositories/tenants.py`
- Create: `deeptutor/teaching/services/tenant_provisioning.py`
- Create: `deeptutor/teaching/permissions.py`
- Create: `deeptutor/api/routers/tenants.py`
- Create: `tests/teaching/test_permissions.py`
- Create: `tests/api/test_tenant_context.py`
- Modify: `deeptutor/api/routers/auth.py`
- Modify: `deeptutor/api/main.py`
- Modify: `deeptutor/multi_user/context.py`
- Modify: `web/lib/auth.ts`
- Modify: `web/hooks/useAuthStatus.ts`

- [ ] Step 1: 写跨租户和硬编码角色失败测试

```python
def test_permission_requires_matching_scope():
    grant = RoleGrant(
        permission="classroom.edit",
        scope_type="course",
        scope_id="course-a",
    )
    assert grant.allows("classroom.edit", "course", "course-a")
    assert not grant.allows("classroom.edit", "course", "course-b")


def test_active_tenant_cookie_cannot_select_non_member(client, auth_cookie):
    response = client.put(
        "/api/v1/tenants/active",
        cookies=auth_cookie,
        json={"tenant_id": "tenant-other"},
    )
    assert response.status_code == 403
    assert "dt_tenant" not in response.headers.get("set-cookie", "")


def test_new_tenant_is_not_selectable_until_provisioning_completes(
    client, platform_admin_headers
):
    created = client.post(
        "/api/v1/tenants",
        headers=platform_admin_headers,
        json={"name": "Institution A"},
    )
    assert created.status_code == 202
    assert created.json()["status"] == "provisioning"
    assert cannot_activate(created.json()["tenant_id"])
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_permissions.py tests/api/test_tenant_context.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现请求级租户上下文

```python
@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    schema_name: str
    user_id: str
    permissions: frozenset[ScopedPermission]


async def require_tenant(
    payload: TokenPayload | None = Depends(require_auth),
    dt_tenant: str | None = Cookie(default=None),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
) -> TenantContext:
    user = get_current_user()
    requested = x_tenant_id or dt_tenant
    context = await tenant_repository.resolve_for_user(user.id, requested)
    set_current_tenant(context)
    return context
```

当教学平台关闭时，`require_tenant` 返回本地合成租户；当教学平台开启时，未指定租户且用户只有一个成员关系时自动选中，多个成员关系时要求先切换。平台管理员也必须显式选择目标租户，不能跳过租户上下文。

- [ ] Step 4: 实现默认角色到授权模板

默认角色只用于发放授权，业务服务只检查权限：

```python
DEFAULT_ROLE_PERMISSIONS = {
    "platform_admin": frozenset(
        {
            "tenant.manage",
            "template.manage",
            "policy.manage",
            "classroom.approve",
            "classroom.publish",
        }
    ),
    "org_admin": frozenset({"classroom.*", "source.use", "learning_event.read"}),
    "content_author": frozenset(
        {"classroom.create", "classroom.edit", "classroom.submit", "source.use"}
    ),
    "content_reviewer": frozenset({"classroom.approve", "learning_event.read"}),
    "teacher": frozenset(
        {
            "classroom.create",
            "classroom.edit",
            "classroom.submit",
            "classroom.publish",
            "classroom.assign",
            "source.use",
            "learning_event.read",
        }
    ),
    "student": frozenset(
        {"classroom.generate.micro", "classroom.generate.full"}
    ),
}
```

`classroom.*` 只能在授权展开阶段展开为已知权限，领域代码不得使用字符串前缀放行未知权限。

- [ ] Step 5: 扩展认证状态和租户 API

新增：

```text
GET /api/v1/tenants/mine
PUT /api/v1/tenants/active
POST /api/v1/tenants
GET /api/v1/tenants/{tenant_id}/provisioning
POST /api/v1/tenants/{tenant_id}/members
PUT /api/v1/tenants/{tenant_id}/members/{user_id}/grants
```

`POST /tenants` 只写入 `status=provisioning` 的平台记录和幂等
`tenant_provisioning_jobs`，返回 202；只有租户 Schema 迁移、独立对象存储凭据及策略验证全部完成后才能原子切换为 `active`。失败保留可重试状态和脱敏原因，不让半初始化租户进入成员列表。`PUT /active` 校验 active 状态和成员关系后写入 `dt_tenant` HttpOnly Cookie，属性与 `dt_token` 的 Secure/SameSite 策略一致。认证状态返回 `active_tenant_id` 和用户可访问租户摘要，不把完整授权矩阵写进 JWT。

- [ ] Step 6: 运行测试

Run:

```powershell
python -m pytest tests/api/test_auth_contextvar.py tests/api/test_tenant_context.py tests/teaching/test_permissions.py tests/multi_user -q
```

Expected: PASS；现有 `admin` / `user` 登录兼容测试保持通过。

- [ ] Step 7: 提交

```powershell
git add deeptutor/teaching/tenant_context.py deeptutor/teaching/repositories/tenants.py deeptutor/teaching/services/tenant_provisioning.py deeptutor/teaching/permissions.py deeptutor/api/routers/tenants.py deeptutor/api/routers/auth.py deeptutor/api/main.py deeptutor/multi_user/context.py web/lib/auth.ts web/hooks/useAuthStatus.ts tests/api/test_tenant_context.py tests/teaching/test_permissions.py
git commit -m "feat(teaching): enforce tenant-scoped permissions"
```

## Task 4: 增加租户切换前端

**Files:**

- Create: `web/lib/tenant-api.ts`
- Create: `web/context/TenantContext.tsx`
- Create: `web/components/tenant/TenantSwitcher.tsx`
- Create: `web/tests/tenant-context.test.ts`
- Modify: `web/app/layout.tsx`
- Modify: `web/components/sidebar/SidebarShell.tsx`
- Modify: `web/locales/en/app.json`
- Modify: `web/locales/zh/app.json`

- [ ] Step 1: 写 API 和状态失败测试

```typescript
test("switchTenant calls the validated server endpoint", async () => {
  const seen: RequestInit[] = [];
  globalThis.fetch = async (_input, init) => {
    seen.push(init ?? {});
    return new Response(JSON.stringify({ active_tenant_id: "t-school" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await switchTenant("t-school");
  assert.equal(seen[0].method, "PUT");
  assert.equal(seen[0].body, JSON.stringify({ tenant_id: "t-school" }));
});
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
npm --prefix web run test:node
```

Expected: FAIL，模块尚不存在。

- [ ] Step 3: 实现租户上下文与切换器

`TenantContext` 从认证状态加载租户列表；切换成功后调用 `router.refresh()`，不在 localStorage 保存权威租户 ID。切换器在只有一个租户时显示机构名称而非下拉菜单。

- [ ] Step 4: 运行前端门禁

Run:

```powershell
npm --prefix web run test:node
npm --prefix web run i18n:check
npm --prefix web run build
```

Expected: PASS。

- [ ] Step 5: 提交

```powershell
git add web/lib/tenant-api.ts web/context/TenantContext.tsx web/components/tenant/TenantSwitcher.tsx web/tests/tenant-context.test.ts web/app/layout.tsx web/components/sidebar/SidebarShell.tsx web/locales/en/app.json web/locales/zh/app.json
git commit -m "feat(web): add validated tenant switching"
```

## Task 5: 建立对象存储协议和租户前缀

**Files:**

- Create: `deeptutor/teaching/artifacts.py`
- Create: `deeptutor/teaching/object_store.py`
- Create: `deeptutor/teaching/storage_credentials.py`
- Create: `tests/teaching/test_object_store.py`
- Create: `tests/teaching/integration/test_s3_object_store.py`

- [ ] Step 1: 写租户键和路径穿越失败测试

```python
def test_artifact_key_is_server_derived():
    key = classroom_artifact_key(
        tenant_id="tenant-a",
        asset_id="asset-1",
        version=3,
        relative_name="classroom.json",
    )
    assert key == "tenants/tenant-a/classrooms/asset-1/versions/3/classroom.json"


@pytest.mark.parametrize("name", ["../secret", "/root", "a/../../b"])
def test_artifact_key_rejects_escape(name):
    with pytest.raises(ValueError):
        classroom_artifact_key("tenant-a", "asset-1", 3, name)


async def test_tenant_credentials_cannot_list_other_prefix(s3_harness):
    tenant_a = await s3_harness.client_for("tenant-a")
    with pytest.raises(ObjectStoreAccessDenied):
        await tenant_a.list_prefix("tenants/tenant-b/")
```

- [ ] Step 2: 运行并确认失败

Run:

```powershell
python -m pytest tests/teaching/test_object_store.py -q
```

Expected: FAIL。

- [ ] Step 3: 实现协议、Local 和 S3 适配器

```python
class ClassroomArtifactStore(Protocol):
    async def put_verified(
        self, key: str, body: AsyncIterator[bytes], sha256: str, size: int
    ) -> StoredArtifact: ...

    async def open(self, key: str) -> AsyncIterator[bytes]: ...

    async def presign_download(self, key: str, expires_seconds: int) -> str: ...
```

正式键只能由 `classroom_artifact_key()` 生成。`ClassroomArtifactStoreFactory` 根据平台表中的 `secret_ref` 为当前租户加载独立 S3 Access Key/Secret Key；对象存储策略只允许该凭证访问 `tenants/{tenant_id}/`，不得用一个共享应用凭证绕过前缀策略。上传先进入 `tenants/{tenant_id}/temporary/{job_id}/`，校验 MIME、大小、SHA-256、DSL 清单和租户归属后再复制到同一租户的正式前缀。Local 实现仅用于本地开发和单元测试；平台部署必须使用 S3 模式。

- [ ] Step 4: 运行 MinIO 集成测试

Run:

```powershell
python -m pytest tests/teaching/test_object_store.py tests/teaching/integration/test_s3_object_store.py -q
```

Expected: PASS；租户 A 的实际 S3 凭证无法读取或列举租户 B 的前缀，失败上传不会出现正式对象。

- [ ] Step 5: 提交

```powershell
git add deeptutor/teaching/artifacts.py deeptutor/teaching/object_store.py deeptutor/teaching/storage_credentials.py tests/teaching/test_object_store.py tests/teaching/integration/test_s3_object_store.py
git commit -m "feat(teaching): isolate classroom artifact storage"
```

## Task 6: 建立平台底座回归门禁

**Files:**

- Create: `tests/integration/test_tenant_isolation.py`
- Modify: `tests/scripts/test_docker_compose.py`

- [ ] Step 1: 写跨边界验收矩阵

覆盖：

```text
database_schema
active_tenant_cookie
permission_scope
object_store_prefix
data_plane_route
audit_log
```

每一项都创建租户 A 和租户 B，以 A 的身份尝试读取、修改或列举 B 的资源，预期为 403、404 或空结果，且不得泄漏 B 的标识以外的数据。

- [ ] Step 2: 运行完整底座测试

Run:

```powershell
python -m pytest tests/services/config/test_platform_settings.py tests/api/test_auth_contextvar.py tests/api/test_tenant_context.py tests/teaching tests/integration/test_tenant_isolation.py tests/multi_user tests/scripts/test_docker_compose.py -q
```

Expected: PASS。

- [ ] Step 3: 核对旧模式

Run:

```powershell
$env:DEEPTUTOR_PLATFORM_ENABLED = "0"
python -m pytest tests/api tests/runtime tests/cli -q
```

Expected: 现有 API、运行时和 CLI 聚焦套件通过；完成后关闭当前 PowerShell 会话中的该临时覆盖。

- [ ] Step 4: 提交

```powershell
git add tests/integration/test_tenant_isolation.py tests/scripts/test_docker_compose.py
git commit -m "test(teaching): gate tenant isolation foundation"
```
