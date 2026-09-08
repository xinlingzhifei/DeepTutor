# 独立 RAGFlow Python 引擎发布契约

scripts/ragflow_engine_release.py 仅准备构建输入、生成和校验独立引擎收据。
它不修改现有平台 image-lock、四个定制镜像、九成员发布归档或 Compose，
也不构建、推送、部署镜像。当前没有真实引擎发布成功的证明。

## 固定输入

- 平台源仓库身份仍是 xinlingzhifei/DeepTutor，不是显示名称 yFeiSTAI。
- 上游：UPSTREAM.json 中的 v0.27.1 /
  b9df87c4c75a5b0d35c90d15329fc0f6f91cb73e。
- 原始配方：该提交的 Dockerfile_ci，原始 4,771 字节的 SHA-256 为
  ae86c755169894288eb235e0fba82643bb3550dc93a2b8073e7cbd532102c33e。
- 当前 CI 基座记录在 ENGINE-BUILD.json：infiniflow/ragflow-base:v2.1@
  sha256:21b5afcfb32f57f2cb38d197254fcac8be5a81d7dd80adeac8523d277cb83e80。
  2026-09-07 独立读取 registry 原始 OCI index 并计算 SHA-256 一致；linux/amd64
  子摘要为 sha256:5f67514110dfce2e9a57e86f68d3d5fbaa7dd757e4fd84a99e1517d6d3faf6a5。
  该检查只读取 manifest，不下载 image blobs，不证明品牌引擎已经构建。
  准备 API 仍要求显式非零摘要，仓库严格限定为 infiniflow/ragflow-base；不接受仅标签。
- 品牌输入绑定 branding/manifest.json、branding/yfei.patch、
  branding/apply-branding.mjs 的实际字节；补丁摘要必须等于清单声明。

准备阶段只替换配方中两个 FROM infiniflow/ragflow-base:v2.1 的镜像引用，
其他配方字节不变。生成文件不覆盖原始配方；输出目录必须不存在。
基座和配方锁定不意味着所有构建过程完全离线或可逐字节重现。

CI 必须先检查平台候选身份和上游检出，再运行品牌脚本的应用步骤及随后检查。
单独运行 --check 可能只表示未应用补丁的干净检出可应用，因此不能把它当作
“品牌已经应用”的证明。准备工具本身不检查品牌目标文件的已应用状态。

## CLI 接口

以下是已接入独立 CI 的 Bash 接口示例，不是已运行的发布记录。
所有环境变量都必须由经过验证的候选、Actions 上下文和实际构建结果提供；
ENGINE_PREPARED 应在上游检出之外，且尚不存在。

~~~bash
python scripts/ragflow_engine_release.py prepare \
  --repository-root "$GITHUB_WORKSPACE" \
  --upstream-recipe "$RAGFLOW_CHECKOUT/Dockerfile_ci" \
  --base-image "$RAGFLOW_BASE_IMAGE" \
  --output-dir "$ENGINE_PREPARED"
~~~

输出只有 Dockerfile 和规范 build-inputs.json。构建必须使用前者作为
Dockerfile、已应用品牌的固定检出作为 context，并显式选择 linux/amd64。
实际 build/push 步骤位于 .github/workflows/private-ragflow-engine-image.yml。
它响应同一不可变发布标签，但不把引擎加入原平台四镜像或九成员 artifact。
两个工作流使用同一并发组并明确 queue: max，避免等待中的候选被另一个运行替换。
CI 先验证身份、固定检出、应用并验证品牌、检查原始 base manifest，然后构建
linux/amd64 production 并仅推送 runId/runAttempt 唯一 staging，允许首次推送初始化
包命名空间；staging 的原始摘要和平台验证通过后，才检查正式标签并发布。
不允许重跑已有候选或覆盖正式标签；最后复核正式标签原始 manifest 并输出独立收据。

scripts/ghcr_tag_guard.py 是唯一的正式标签准入检查，凭据只从该步骤显式设置的
GITHUB_TOKEN/GITHUB_ACTOR 环境读取。它仅向固定 ghcr.io 的 token endpoint 和
精确候选 manifest endpoint 发起认证 GET；只有 manifest 的 HTTP 404 且所有
结构化错误码均为 MANIFEST_UNKNOWN 才放行。已存在标签（即使摘要相同）、
NAME_UNKNOWN、401/403、网络错误、重定向、非 JSON、畸形或重复 JSON 均拒绝；
token endpoint 的任何错误都不是标签缺失证据。日志仅包含阶段、状态码和允许列表
错误码，不输出凭据、原始响应或完整异常。成功 artifact 仍严格为原有六个成员。

该 GET 检查不是 GET→PUT 原子操作。共享发布队列及没有队列外其他写入者是安全
前提；正式发布后仍必须检查原始摘要，部署权威仍是验证过的 digest 而不是标签。

标准 runner 必须具备至少4核、16 GB RAM及50 GB可用磁盘。不足只终止独立引擎
构建，不删除 runner 工具，不自动购买更大 runner，也不阻塞无关本地开发。
目前这些工作流检查仅有离线合同证据；真实 Actions 和远端发布结果另行记录。

在构建、发布同一候选标签并通过可信渠道取得最终远端 manifest 原始字节后：

~~~bash
python scripts/ragflow_engine_release.py write-receipt \
  --repository-root "$GITHUB_WORKSPACE" \
  --upstream-recipe "$RAGFLOW_CHECKOUT/Dockerfile_ci" \
  --base-image "$RAGFLOW_BASE_IMAGE" \
  --prepared-dir "$ENGINE_PREPARED" \
  --source-repository "$GITHUB_REPOSITORY" \
  --source-head "$GITHUB_SHA" \
  --release-tag "$GITHUB_REF_NAME" \
  --run-id "$GITHUB_RUN_ID" \
  --run-attempt "$GITHUB_RUN_ATTEMPT" \
  --image "$RAGFLOW_IMAGE_REFERENCE" \
  --build-digest "$RAGFLOW_BUILD_DIGEST" \
  --remote-manifest "$FINAL_REMOTE_MANIFEST_FILE" \
  --output "$ENGINE_RECEIPT_FILE"
~~~

校验使用相同参数，把子命令改为 validate-receipt，
并把 --output 改为 --receipt。remote-manifest 必须保留原始字节，
不能重新排版或添加换行。其 SHA-256 必须等于实际 build 输出 digest。
镜像引用严格为
ghcr.io/xinlingzhifei/yfeistai-ragflow:<同一 releaseTag>@<同一 build digest>。
输出文件已存在时拒绝覆盖；失败留下的输出应先检查，不自动清理。

## 收据与信任边界

收据为独立的 schemaVersion: 1 / kind: ragflow-engine-publication，
不是平台 image-lock 的新版本。规范 JSON 使用 UTF-8、排序键、紧凑分隔符、
且末尾仅一个 LF；未知字段、重复键、BOM、不同类型和非规范字节均被拒绝。

它绑定平台 sourceRepository/sourceHead/releaseTag、上游固定版本、
原始与生成配方摘要、所有品牌输入摘要、实际基座引用、最终镜像引用、
原始 manifest 摘要，以及：

- platform: linux/amd64、backendMode: python；
- workflow 路径 .github/workflows/private-ragflow-engine-image.yml；
- 正整数 Actions runId 和严格的整数 runAttempt: 1。

校验器从显式期望参数和实际输入字节重新构造完整文档，不把收据中自报字段
当作权威来源。它只证明**本地契约一致**。可信 Actions 成功记录、workflow
身份、源提交对应关系、品牌应用状态、平台构建参数、最终 registry 抓取及
镜像内容仍需独立验证；伪造本地文档不能替代这些证据。

backendMode 是发布/部署契约，不是当前配方新增的环境变量。
后续 companion 部署必须明确设置 API_PROXY_SCHEME=python 并独立验证，
不得仅凭本收据宣称 Python 服务已运行。本切片不实施该部署绑定。

## 平台候选的独立 companion 绑定

scripts/ragflow_companion_binding.py 提供 create_binding、binding_bytes 和
validate_binding 三个本地 API。调用者须显式提供可信的共同源码仓库/提交/标签、
平台与引擎的首次 Actions run 身份、完整引擎预期输入及部署批次 ID；不得从待验
收据自报的字段反推这些预期值。它复用平台 promotion 与引擎收据验证器，记录两份
收据、image-lock 的实际摘要和引擎镜像引用，拒绝跨候选或跨批次混接。

规范字节在五个候选输入及两份 promotion 文档的原始文件句柄仍打开时生成，
最终不变性复核通过后才返回。本模块没有文件写入接口，持久化沿用已有发布事务；
不增加平台归档成员。binding 的 validationScope 明确为 local-contract-only，
不证明 registry 发布权威、真实 API 配置、部署就绪或生命周期验收通过。

## 离线回归

~~~bash
python -m pytest --confcutdir=tests/scripts tests/scripts/test_ragflow_engine_release.py -q
python -m ruff check scripts/ragflow_engine_release.py tests/scripts/test_ragflow_engine_release.py
~~~

测试不调用 Git、Docker、网络、SSH 或真实子进程；不依赖被忽略的上游检出。
它们使用固定配方字节和临时目录验证本地契约，不构成真实发布证据。
