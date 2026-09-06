# yFeiSTAI RAGFlow API 使用说明

本文对应当前 yFeiSTAI 服务端适配实现，RAGFlow HTTP 契约固定为 `v0.27.1`。RAGFlow 独立运行；下载 `upstream/` 源码不等于已启动服务。本轮验证使用本地测试替身，**尚未进行真实 RAGFlow 服务联调**。

所有下述路径均相对于 yFeiSTAI 的 `/api/v1/knowledge`。不要把它们直接发送给 RAGFlow，也不要用 RAGFlow API Key 代替 yFeiSTAI 登录凭据。

## 1. 接入顺序与权限

推荐顺序为：保存连接 → 探测 → 查看数据集 → 新建或附加知识库 → 上传 → 等待上传完成 → 解析 → 等待解析完成 → 发布为 `active` → 检索验证。

- yFeiSTAI 请求使用已有登录的 `Authorization: Bearer ...` 或 `dt_token` Cookie。以下示例采用 Bearer 方式。
- 当前实现仅管理员可以通过 API 保存/轮换连接 profile。profile 的查询和探测以当前账户的凭据所有者为范围。
- 管理接口要求当前用户拥有知识库管理权限。分配给普通用户的管理员知识库，通过现有 `rag` Tool 检索；这些用户不能调用其文档管理接口。
- `ownership=managed` 表示 yFeiSTAI 创建的远端 Dataset；`ownership=attached` 表示附加已有 Dataset。
- `access_mode=read_only` 禁止远端文档/Chunk 写操作；`manage_documents` 允许这些写操作。只有 `managed` 才允许显式删除远端 Dataset。
- `kb_ref` 使用创建/附加接口返回的 `id`。文档和 Chunk 管理使用 yFeiSTAI 返回的 `document_ref`、`chunk_ref`，不能把远端 ID 填进这些位置。远端 Dataset ID 仅用于选择和附加。
- `GET /{kb_ref}/ragflow/binding` 返回当前 `can_manage`、`can_write`、`can_reconcile`、`read_only` 和 `lifecycle_state`，不返回连接密钥或远端标识。创建待核验时可能仅允许 `can_reconcile`；权限能力不取代各写接口的实时授权检查。
- 文档详情 `GET /{kb_ref}/documents/{document_ref}` 返回可编辑的 `meta_fields`、`chunk_method`、`parser_config`，但过滤本平台内部元数据；列表仅返回摘要。
- 同一 workspace 不允许重复绑定同一个非空 Dataset ID，即使使用不同的 profile。多人使用同一份资料时，应共享同一个 yFeiSTAI 知识库的权限。平台操作锁只覆盖本平台记录的操作，RAGFlow 原生界面或其他外部 API 写入者不受这些锁控制。

## 2. PowerShell 7 公共准备

先通过本机的安全凭据管理方式提供 `YFEISTAI_API_TOKEN`、`RAGFLOW_API_KEY` 环境变量。示例不包含真实密钥，不会回显它们。这里的环境变量仅用于调用脚本；连接配置通过 API 保存，不能依赖项目根 `.env` 自动生效。

```powershell
$ragflowApi = 'http://127.0.0.1:8001/api/v1/knowledge'
$ragflowProfileId = 'default'
$ragflowHeaders = @{}
if ($env:YFEISTAI_API_TOKEN) {
    $ragflowHeaders['Authorization'] = 'Bearer ' + $env:YFEISTAI_API_TOKEN
}
if (-not $env:RAGFLOW_API_KEY) {
    throw '请先安全配置 RAGFLOW_API_KEY 环境变量。'
}

function Invoke-YfsKnowledgeJson {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [object]$Payload = $null,
        [string]$IdempotencyKey = ''
    )
    $requestHeaders = @{} + $ragflowHeaders
    if ($IdempotencyKey) {
        $requestHeaders['Idempotency-Key'] = $IdempotencyKey
    }
    $requestArguments = @{
        Method = $Method
        Uri = $ragflowApi + $Path
        Headers = $requestHeaders
        ErrorAction = 'Stop'
    }
    if ($null -ne $Payload) {
        $requestArguments['ContentType'] = 'application/json; charset=utf-8'
        $requestArguments['Body'] = [Text.Encoding]::UTF8.GetBytes(
            ($Payload | ConvertTo-Json -Depth 30 -Compress)
        )
    }
    Invoke-RestMethod @requestArguments
}

function Wait-YfsKnowledgeOperation {
    param(
        [Parameter(Mandatory)][string]$KbRef,
        [Parameter(Mandatory)][string]$OperationId,
        [int]$TimeoutSeconds = 600
    )
    $encodedKb = [Uri]::EscapeDataString($KbRef)
    $encodedOperation = [Uri]::EscapeDataString($OperationId)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $operation = Invoke-YfsKnowledgeJson -Method GET -Path (
            '/' + $encodedKb + '/operations/' + $encodedOperation
        )
        if ($operation.state -eq 'RECONCILED') {
            return $operation
        }
        if ($operation.state -in @('FAILED', 'NEEDS_ATTENTION')) {
            throw ('知识操作未完成：' + $operation.state + ' / ' + $operation.error_type)
        }
        Start-Sleep -Seconds 2
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw ('等待超时，操作仍可能继续；保留 operation_id：' + $OperationId)
}
```

在认证开启的环境中，缺少有效 yFeiSTAI Token 会得到 `401`。认证关闭的本地开发环境允许省略该 Token；这不影响 RAGFlow 自身仍需 API Key。

## 3. 保存连接、探测和列出 Dataset

```powershell
$profile = Invoke-YfsKnowledgeJson -Method PUT `
    -Path ('/rag-pipelines/ragflow/profiles/' + $ragflowProfileId) `
    -Payload @{
        base_url = 'https://ragflow.example.internal'
        api_key = $env:RAGFLOW_API_KEY
        allow_http = $false
        connect_timeout = 5
        read_timeout = 60
        upload_timeout = 300
    }

$probe = Invoke-YfsKnowledgeJson -Method POST -Path '/ragflow/probe' `
    -Payload @{ profile_id = $ragflowProfileId }
if (-not $probe.ok) {
    throw ('连接未就绪：health=' + $probe.health + ', auth=' + $probe.auth + ', contract=' + $probe.contract)
}

$datasets = Invoke-YfsKnowledgeJson -Method GET -Path (
    '/ragflow/datasets?profile_id=' + [Uri]::EscapeDataString($ragflowProfileId) + '&page=1&page_size=50'
)
$datasets.datasets | Select-Object id, name
```

`base_url` 是 RAGFlow 服务根地址，例如 `https://ragflow.example.internal`，不要附加 `/api/v1`。HTTP 仅在显式设置 `allow_http=true` 时允许，例如隔离的本地测试地址 `http://127.0.0.1:9380`。地址不得包含用户名、密码、查询参数或片段；客户端禁止重定向并保持 TLS 验证。

Profile 响应包含 `has_api_key`、`credential_revision` 和非敏感连接参数，不返回 API Key 或 `secret_ref`。轮换时使用相同 PUT 路径并提交新 Key；当前请求模型仍要求 `base_url`。省略 `api_key` 可保留已有 Key；其他省略字段会按该请求模型默认值处理，应在更新时显式提交需要保留的超时和 `allow_http`。

Probe 将 `health`、`auth`、`contract` 分别报告。`contract=compatible` 表示已探测的健康/列表接口形态兼容，**不证明所有写接口均已验证**；`version_unknown=true` 表示尚未探明服务端精确版本，`contract_version=0.27.1` 是本客户端适配基线。

| 方法和路径 | 说明 |
| --- | --- |
| `GET /rag-pipelines/ragflow/profiles` | 当前凭据所有者的 profile 列表 |
| `GET /rag-pipelines/ragflow/profiles/{profile_id}` | 查询 profile 安全视图 |
| `PUT /rag-pipelines/ragflow/profiles/{profile_id}` | 管理员保存/更新 profile |
| `POST /ragflow/probe` | JSON：`profile_id` |
| `GET /ragflow/datasets?profile_id=...&page=1&page_size=50` | 当前 profile 可见的 Dataset，`page_size` 最大 100 |

## 4. 新建 managed 知识库

```powershell
$createKey = [Guid]::NewGuid().ToString('N')
$created = Invoke-YfsKnowledgeJson -Method POST -Path '/ragflow/create' `
    -IdempotencyKey $createKey `
    -Payload @{
        name = 'ragflow-course'
        profile_id = $ragflowProfileId
        description = '课程资料'
        chunk_method = 'naive'
    }
$ragflowKbRef = $created.id
$ragflowKbPath = '/' + [Uri]::EscapeDataString($ragflowKbRef)
if ($created.operation_id) {
    $createReceipt = Wait-YfsKnowledgeOperation -KbRef $ragflowKbRef -OperationId $created.operation_id
}
```

`chunk_method`、`embedding_model` 可选，未填写时交由 RAGFlow 的默认配置处理。远端 Dataset 创建使用操作标识命名，以便超时后对账；yFeiSTAI 名称仍是请求中的 `name`。

成功创建并核实绑定返回 `201`；未完成时返回 `202` 和 `operation_id`。已存在且已激活的同名 managed 绑定会先通过远端 GET 核验 Dataset 仍然存在，再直接返回 `status=ready`，没有新的 `operation_id`。远端 Dataset 已不存在时返回 `410`、`ragflow_dataset_deleted`，不会自动重建。原请求超时重试时，应复用同一 `Idempotency-Key` 和完全相同的请求。

## 5. 附加已有 Dataset（替代第 4 步）

```powershell
$attached = Invoke-YfsKnowledgeJson -Method POST -Path '/connect-ragflow' `
    -Payload @{
        name = 'ragflow-existing-course'
        profile_id = $ragflowProfileId
        dataset_id = '从数据集列表选择的远端ID'
        selected_document_ids = @()
        access_mode = 'manage_documents'
        description = '接入已有资料'
    }
$ragflowKbRef = $attached.id
$ragflowKbPath = '/' + [Uri]::EscapeDataString($ragflowKbRef)
$documents = Invoke-YfsKnowledgeJson -Method GET -Path ($ragflowKbPath + '/documents?page=1&page_size=30')
$documents.documents | Select-Object document_ref, name, state, visibility, revision
```

`selected_document_ids` 必须显式给出，可为空，最多 1000 个远端文档 ID。非空时，每个选定文档必须已经解析成功且有 Chunk；它们才会成为初始 `active` 文档。没有选定的远端文档不会自动进入聊天检索。

以上空选择示例先附加，再通过文档列表获得 `document_ref`，逐个确认并发布。只需检索时将 `access_mode` 改为 `read_only`；此模式下仍可由本地知识库管理者配置文档可见性，但不能上传、解析或改写远端内容。

## 6. 上传和等待回执

以下例子适用于 managed 库，或 `access_mode=manage_documents` 的 attached 库；使用 PowerShell 7 的 `-Form` 上传。

```powershell
$ragflowInput = 'E:\course-materials\lesson.pdf'
$uploadKey = [Guid]::NewGuid().ToString('N')
$uploadHeaders = @{} + $ragflowHeaders
$uploadHeaders['Idempotency-Key'] = $uploadKey
$uploaded = Invoke-RestMethod -Method POST `
    -Uri ($ragflowApi + $ragflowKbPath + '/documents/upload') `
    -Headers $uploadHeaders `
    -Form @{ files = Get-Item -LiteralPath $ragflowInput } `
    -ErrorAction Stop
$uploadReceipt = Wait-YfsKnowledgeOperation -KbRef $ragflowKbRef -OperationId $uploaded.operation_id
$ragflowDocumentRef = $uploadReceipt.result.document_ref
$ragflowDocumentPath = $ragflowKbPath + '/documents/' + [Uri]::EscapeDataString($ragflowDocumentRef)
```

上传接口为 `POST /{kb_ref}/documents/upload`，multipart 字段名是 **`files`**。一批最多 20 个文件，HTTP 上传批次总大小最多 100 MiB；临时暂存区有 1 GiB 容量限制。空文件会被拒绝。暂存保留用于故障恢复；当前接口未提供自动清理或清空接口。

单文件返回一个操作对象；多文件返回 `{"accepted":true,"operations":[...]}`，应逐项等待和处理失败。重试同批上传需复用同一 Key、文件内容及顺序；变更内容不能复用旧 Key。请求被接收后不会在网络不确定时自动重复远端写操作。

创建空文档可用 `POST /{kb_ref}/documents`，JSON 为 `{"name":"notes.txt","type":"empty"}`；同样返回 `202` 和操作 ID。当前公开 API 明确拒绝 `type=web` 或包含 `url` 的创建请求，错误为 `remote_web_ingestion_disabled`，不能通过低层客户端支持网页上传来推断平台已经开放此功能。

## 7. 解析、取消和发布

上传成功后文档仍为 `draft`。先解析，再发布；单纯上传或解析成功不会自动允许普通聊天检索。

```powershell
$parseKey = [Guid]::NewGuid().ToString('N')
$parsing = Invoke-YfsKnowledgeJson -Method POST -Path ($ragflowDocumentPath + '/parse') `
    -IdempotencyKey $parseKey `
    -Payload @{
        use_ingestion_pipeline = $false
        rebuild = $false
        confirm_rebuild = $false
    }
$parseReceipt = Wait-YfsKnowledgeOperation -KbRef $ragflowKbRef -OperationId $parsing.operation_id
if ($parseReceipt.result.document_status -ne 'ready') {
    throw '文档未达到 ready 状态，不执行发布。'
}

$document = Invoke-YfsKnowledgeJson -Method GET -Path $ragflowDocumentPath
if ($document.state -ne 'ready' -or $document.chunk_count -lt 1) {
    throw '远端文档尚无可发布的解析结果。'
}
$published = Invoke-YfsKnowledgeJson -Method PUT -Path ($ragflowDocumentPath + '/visibility') `
    -Payload @{ visibility = 'active'; expected_revision = $document.revision }
```

`use_ingestion_pipeline=false` 对应 RAGFlow 内置分块解析；数据集使用独立 ingestion pipeline 时必须设置为 `true`。`rebuild` 仅在 ingestion 请求中映射到上游 `delete` 参数；内置解析没有这个独立参数。存在人工 Chunk 编辑时重新解析需要 `confirm_rebuild=true`，表示接受重建可能覆盖这些编辑。重新解析会使旧 Chunk 引用失效。

取消解析使用 `POST /{kb_ref}/documents/{document_ref}/cancel`，请求模型与 `/parse` 相同；正常取消传 `rebuild=false`，并保持原来的 `use_ingestion_pipeline` 选择。取消被受理不代表后台工作立即终止，仍需等待操作状态与文档状态。

文档的远端状态包括 `unstart`、`parsing`、`ready`、`failed`、`cancelled`、`unknown`。本地可见性是独立字段：`draft`、`active`、`disabled`、`archived`，删除对账后成为 `deleted`。

`visibility` 更新要求最新 `expected_revision`，发生并发变更返回 `409`。发布 `active` 还会校验远端已就绪、Chunk 数量和可用内容；切到 `disabled` 或 `archived` 后不再参与普通聊天检索。正在执行的检索会复核可见性版本，版本变化时丢弃旧结果。

## 8. 检索验证

```powershell
$retrieved = Invoke-YfsKnowledgeJson -Method POST -Path ($ragflowKbPath + '/retrieval-test') `
    -Payload @{ query = '这份资料的核心概念是什么？' }
if ($retrieved.error_type) {
    throw ('检索失败：' + $retrieved.error_type)
}
$retrieved.sources | Select-Object document_id, chunk_id, title, page, score
$retrieved.content
```

这是管理者的检索测试接口，返回检索上下文及来源，不调用 RAGFlow Chat/Agent 完成答案生成。正常 `rag` Tool 通过同一个 Provider 接入，最终回答仍由 yFeiSTAI 生成。

无 `preview_document_refs` 时，只检索当前绑定下的 `active` 文档。管理预览可以显式提交 `{"query":"...","preview_document_refs":["doc_..."]}`，以检查草稿内容；它需要本地知识库写权限，不会把预览文档自动发布为 `active`，也不会改写远端内容。普通聊天不能通过参数覆盖 Dataset、Document 或启用扩展检索模式。

普通检索最多作用于 1000 个 active 文档，展示来源上限为 20，默认检索 `page_size=8`。来源使用不透明文档和 Chunk 引用，经过 Dataset、文档归属及 Chunk 可用性复核。

管理者可通过 `GET /{kb_ref}/retrieval-settings` 读取当前参数，再通过 `PUT /{kb_ref}/retrieval-settings` 更新以下字段：`page_size`（1–20，默认 8）、`similarity_threshold`（0–1，默认 0.2）、`vector_similarity_weight`（0–1，默认 0.3）、`rerank_id`（可选，最长 256 字符）。PUT 按请求模型重建配置，省略字段取默认值；它不是局部 PATCH。设置变化会使正在使用旧绑定快照的操作失效。

当前父子 Chunk 模式未支持：RAGFlow 将父块标记为不可直接检索，而其检索接口可能自动展开父块。适配层遇到不可用 Chunk 时明确返回 `error_type=unavailable_chunk`，不会把它伪装成空检索，也不会放行这些内容；应使用普通可用 Chunk。知识图谱、目录增强和知识编译扩展在此 Provider 查询中也被关闭。

## 9. 文档与 Chunk 管理接口

下表中的路径仍相对于 `/api/v1/knowledge`，所有远端写操作应携带 `Idempotency-Key`。返回 `202` 的操作均通过操作接口读取最终结果。

| 方法和路径 | 请求/结果 |
| --- | --- |
| `GET /{kb_ref}/documents?page=1&page_size=30&keywords=...` | `documents`、`total`；页大小最大 100 |
| `GET /{kb_ref}/documents/{document_ref}` | 文档名称、状态、可见性、revision、Chunk 数及进度 |
| `GET /{kb_ref}/documents/{document_ref}/download` | 原文件流；下载大小上限 100 MiB |
| `PATCH /{kb_ref}/documents/{document_ref}` | `name`、`meta_fields`、`chunk_method`、`parser_config`，至少一项 |
| `DELETE /{kb_ref}/documents/{document_ref}` | 删除单个文档，`202` |
| `GET /{kb_ref}/documents/{document_ref}/chunks` | 分页 Chunk 列表，返回 `chunk_ref` |
| `POST /{kb_ref}/documents/{document_ref}/chunks` | `content` 必填；可选 `important_keywords`、`questions`、`tag_kwd` |
| `GET /{kb_ref}/documents/{document_ref}/chunks/{chunk_ref}` | 指定 Chunk 的安全视图 |
| `PATCH /{kb_ref}/documents/{document_ref}/chunks/{chunk_ref}` | 内容、关键词、问题、标签、`available` 的非空补丁 |
| `DELETE /{kb_ref}/documents/{document_ref}/chunks/{chunk_ref}` | 删除单个 Chunk，`202` |
| `PUT /{kb_ref}/documents/{document_ref}/chunks/{chunk_ref}/availability` | `{"available":false}` 或 `true`，`202` |
| `PUT /{kb_ref}/documents/{document_ref}/visibility` | 本地生命周期状态及 `expected_revision` |
| `GET /{kb_ref}/operations` | 当前知识库代次的操作历史 |
| `POST /{kb_ref}/retrieval-test` | `query`，可选管理预览文档引用 |
| `GET /{kb_ref}/retrieval-settings` | 当前检索参数 |
| `PUT /{kb_ref}/retrieval-settings` | 更新分页大小、相似度阈值、向量权重和 reranker |
| `GET /{kb_ref}/operations/{operation_id}` | 单个操作状态和脱敏结果 |
| `GET /{kb_ref}/operations/{operation_id}/events?after_id=0` | 事件列表；以最后一个事件 `id` 增量读取 |
| `POST /{kb_ref}/operations/{operation_id}/reconcile` | 必填 `expected_revision`；人工重新授权只读对账，`202` |
| `DELETE /{kb_ref}/ragflow/dataset` | JSON：`{"confirm_name":"知识库名称"}`；仅 managed，`202` |

平台保留的 `meta_fields` 键不能被用户覆盖：`yfeistai_generation`、`yfeistai_operation`、`yfeistai_source_sha256`、`yfeistai_source_kind`。这里的事件接口返回 JSON 列表，不是 SSE 或 WebSocket。

删除 Dataset 是独立的远端删除动作，必须输入准确的知识库名称。`attached` 库禁止调用该远端删除操作；普通知识库解除绑定不会隐式删除 Dataset。远端整库删除核验完成后，本地仍保留绑定和审计指针，便于查看删除记录；如需从知识库列表断开，应在确认远端删除完成后调用现有 `DELETE /{kb_name}`。重新调用创建接口不会悄悄重建被删除的远端库。

## 10. 状态与错误判定

**HTTP `202`、`accepted=true`、`ACKNOWLEDGED` 都不是操作完成。** 业务调用者应等待 `RECONCILED`，并根据动作检查结果，例如解析还应要求 `document_status=ready`。

| 操作状态 | 调用方处理 |
| --- | --- |
| `INTENT_RECORDED` | 意图已持久化，等待后台处理 |
| `SEND_STARTED` | 已进入发送阶段，不得直接另发同类远端写请求 |
| `ACKNOWLEDGED` | 收到回执，仍在核验结果 |
| `RESULT_UNKNOWN` | 网络、权限变更或部分成功造成结果不确定，等待只读对账 |
| `RECONCILING` | 正在核验远端资源或等待解析结束 |
| `RECONCILED` | 本操作完成核验；随后按动作执行下一步 |
| `FAILED` | 已确认失败，释放平台资源操作锁；查看安全 `error_type` |
| `NEEDS_ATTENTION` | 自动流程停止，保留平台资源操作锁；需要处理权限变化、未知或部分结果 |

服务进程启动及运行中的恢复任务会读取持久化流水继续处理；操作查询 GET 自身不是“强制重试”命令。等待超时不等于远端失败，不应立即生成新 Key 重复上传/创建。

解析明确返回 `FAIL`，或已经完成但没有可用 Chunk 时，操作按 `FAILED` 处理，释放平台锁；修正问题后可以创建新的解析操作。结果未知或部分成功不能据此认定“远端没有写入”：这些操作保持 `RESULT_UNKNOWN`/`RECONCILING`，或进入持锁的 `NEEDS_ATTENTION`，直到完成核验。

对于 `NEEDS_ATTENTION` 或没有存活 worker 租约的 `RESULT_UNKNOWN`，有管理权限的操作者可以在核查原因后，使用当前 revision 显式重新授权只读对账：

```powershell
$reviewOperationId = '从操作列表选择的operation_id'
$reviewOperationPath = $ragflowKbPath + '/operations/' + [Uri]::EscapeDataString($reviewOperationId)
$reviewOperation = Invoke-YfsKnowledgeJson -Method GET -Path $reviewOperationPath
$reconciliation = Invoke-YfsKnowledgeJson -Method POST -Path ($reviewOperationPath + '/reconcile') `
    -Payload @{ expected_revision = $reviewOperation.revision }
$reviewReceipt = Wait-YfsKnowledgeOperation -KbRef $ragflowKbRef -OperationId $reconciliation.operation_id
```

`expected_revision` 必填，缺失会触发请求校验失败。该接口要求当前账户重新通过本地知识库及远端内容管理权限校验，无权返回 `403`；revision 已变化、状态不允许或已有存活 worker 租约时返回 `409`。重新授权仅限同一 owner、workspace、知识库代次和 Dataset 绑定作用域；不能把旧操作转移到另一个 Dataset/绑定，作用域不匹配的操作不可通过此入口访问。凭据轮换但资源绑定作用域相同的情况，可以在重新授权后仅查询远端结果。

该动作不是重新执行原请求：它更新授权快照、记录审计事件并启动只读对账，**绝不重发原来的创建、上传、解析、编辑或删除写请求**。即使返回 `202`，仍需等待对账结果；无法证明完整成功的操作仍会保留待处理状态和锁。

Profile 轮换会使旧执行快照失效，但同一 Dataset 的本地文档引用与可见性保持；更换 Dataset/绑定版本会改变资源作用域。历史操作可供查询，不赋予在新绑定中继续执行旧写操作的权限。

公开管理路由把上游认证错误封装为 `502` + 安全 `detail=auth_error`，以免把 RAGFlow 凭据失效误判为 yFeiSTAI 用户应退出登录。平台自己的 `401/403` 仍表示平台登录/授权问题。参数错误一般为 `400/422`，代次或 revision 冲突为 `409`，上传容量错误为 `413`。检索测试可能以 HTTP `200` 返回 `error_type`，必须检查该字段，不能只检查 HTTP 状态。

## 11. 旧接口兼容

- 原 `POST /{kb_name}/upload` 对 RAGFlow 绑定分流到专用上传流程；新调用推荐使用 `POST /{kb_ref}/documents/upload`，便于明确使用操作流水和幂等头。
- 原 `POST /create` 是 multipart 接口。`rag_provider=ragflow` 时需要 `profile_id`，且不能同时上传 `files`；带文件会返回 `400`，必须先创建知识库，再调用上传接口。
- 旧接口分流不代表已经完成前端创建弹窗、知识中心管理页面或所有 CLI 交互更新。本文描述的是服务端 API。

可用以下只读命令核对当前检出的专用路由；运行前应在 yFeiSTAI 项目根目录，安装项目服务端依赖：

```powershell
python -c "from deeptutor.api.routers.ragflow_knowledge import router; print('\n'.join(','.join(sorted(r.methods)) + ' /api/v1/knowledge' + r.path for r in router.routes))"
if ($LASTEXITCODE -ne 0) { throw '路由导入验证失败。' }
```

以上调用示例尚未在真实 RAGFlow 实例执行。上线前仍需以目标实例验证连接凭据、模型配置、真实文档解析、来源质量、恢复行为和部署网络访问策略。
