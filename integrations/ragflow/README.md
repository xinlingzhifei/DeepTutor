# RAGFlow integration

本目录保存 yFeiSTAI 对接 RAGFlow 的版本锁定信息、设计和上游源码。

| 内容 | 位置 |
| --- | --- |
| 完整 API 深度对接方案、验收矩阵和实施顺序 | [DESIGN.md](DESIGN.md) |
| 已实现的 API、调用顺序与 PowerShell 示例 | [API.md](API.md) |
| 前端入口、知识生命周期操作与权限边界 | [FRONTEND.md](FRONTEND.md) |
| 本轮实现清单与验证边界 | [IMPLEMENTATION.md](IMPLEMENTATION.md) |
| 验证结果与可复现回归命令 | [VERIFICATION.md](VERIFICATION.md) |
| 上游仓库、标签、提交与 API 文档摘要 | [UPSTREAM.json](UPSTREAM.json) |
| 已下载的 RAGFlow 源码 | [upstream/README.md](upstream/README.md) |
| 固定版本 HTTP API 文档 | [upstream/docs/references/http_api_reference.md](upstream/docs/references/http_api_reference.md) |
| 上游许可证 | [upstream/LICENSE](upstream/LICENSE) |
| yFei 品牌定制、可重放补丁与验证 | [branding/README.md](branding/README.md) |

源码位置：`E:\yFeiSTAI\integrations\ragflow\upstream`。

固定版本：`v0.27.1`，提交：`b9df87c4c75a5b0d35c90d15329fc0f6f91cb73e`。

`upstream` 是保留自身 `.git` 的独立浅克隆：包含该版本完整的 5,847 个受版本管理文件，不包含完整历史。它被本目录 `.gitignore` 排除，避免父仓库意外提交嵌套仓库。父应用的 Docker 构建上下文也排除了这份源码。版本清单和集成文档可以随 yFeiSTAI 正常纳入版本管理。

上游工作树现已应用 yFei 品牌定制：产品 Logo 使用 yFeiSTAI 现有素材，可见产品名称改为 `yFei`。技术标识、上游归属及锁版 HTTP 契约不改。`branding/` 保存对应补丁和摘要；重新克隆后按其说明应用，不能仅依赖被忽略的上游工作树保存定制。现成上游镜像不包含这些品牌改动。

截至 2026-09-06，服务端 API 适配已落地：RAGFlow Provider、连接密钥管理、附加/创建知识库、文档与 Chunk 管理、上传/解析/取消、发布/停用/归档、受权限和可见性约束的检索，以及持久化操作和重启恢复。现有创建/上传接口已分流，应用启动时自动运行恢复对账。

前端知识中心现已接入连接配置、专用文档/Chunk 管理、检索测试、操作记录和设置页面，支持中英文及窄屏布局。知识库导航使用不可变 ID，管理按钮由服务端能力控制，运行中操作只读轮询。

本轮没有启动或部署 RAGFlow，也没有真实实例地址/API Key 的联调证据。新增测试使用固定版本 HTTP 契约模拟与真实本地 SQLite；浏览器验证使用隔离 API 响应。它们不能证明目标服务器的模型配置、真实 PDF 解析或线上可用性。不可变文档版本/审批系统和物理删除恢复不属于已实现内容。

实现位置：

- `deeptutor/services/rag/pipelines/ragflow/`：HTTP 客户端、探测及安全检索。
- `deeptutor/knowledge/ragflow/`：凭据、绑定、可见性、操作状态、对账与恢复。
- `deeptutor/api/routers/ragflow_knowledge.py`：认证后的管理 API。
- `web/lib/ragflow-api.ts`、`web/components/knowledge/ragflow/`：前端适配和知识库管理页面。

新接入遵循“配置 profile → 探测 → 附加 Dataset 或创建 managed Dataset → 上传 → 等待操作 → 解析 → 校验并发布”。解析成功不会自动发布。HTTP `202` 仅表示已受理，必须通过操作查询确认终态。详细示例见 [API.md](API.md)。

当前保守边界：网页抓取明确禁用；父子 Chunk 扩展返回 `unavailable_chunk`；普通检索最多显式包含 1,000 个 active 文档；同一工作区禁止重复绑定同一个 Dataset ID，分享应使用现有知识库授权。RAGFlow 外部管理台或其他写入方不受本平台锁约束，严格一致性部署需使用单一写入口。

## 在其他检出目录复现

以下 PowerShell 命令在 yFeiSTAI 仓库根目录执行；若目标已经存在，先检查其状态，不覆盖它。

```powershell
$ragflowPin = Get-Content -LiteralPath integrations/ragflow/UPSTREAM.json -Raw | ConvertFrom-Json
$ragflowTarget = Join-Path (Get-Location).Path 'integrations/ragflow/upstream'
if (Test-Path -LiteralPath $ragflowTarget) {
    throw 'RAGFlow source directory already exists; inspect it before proceeding.'
}
git -c core.longpaths=true -c core.autocrlf=false clone --depth 1 --single-branch --branch $ragflowPin.tag $ragflowPin.repository $ragflowTarget
if ($LASTEXITCODE -ne 0) { throw 'RAGFlow clone failed.' }
$ragflowCommit = git -C $ragflowTarget rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $ragflowCommit -ne $ragflowPin.commit) {
    throw 'RAGFlow commit differs from UPSTREAM.json.'
}
git -C $ragflowTarget fsck --full
if ($LASTEXITCODE -ne 0) { throw 'RAGFlow Git object verification failed.' }
$ragflowDocPath = Join-Path $ragflowTarget $ragflowPin.httpApiReference
$ragflowDocHash = (Get-FileHash -LiteralPath $ragflowDocPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ragflowDocHash -ne $ragflowPin.httpApiReferenceSha256) {
    throw 'RAGFlow API reference differs from UPSTREAM.json.'
}
```

开发与部署时使用固定提交；升级先在独立目录验证新版本接口契约，再更新版本清单。上游服务独立运行，通过服务端 HTTP API 接入，无需将 RAGFlow 运行依赖安装进 yFeiSTAI 的 Python 环境。
