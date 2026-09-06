# RAGFlow API 适配验证记录

日期：2026-09-06。位置：`E:\yFeiSTAI`。本轮为本地服务端实现，未提交 Git、推送远端或部署服务。

## 最终检查

| 检查 | 结果 |
| --- | --- |
| 新增适配 + 既有知识库/RAG/多用户/API 回归的合并运行 | **539 passed, 2 skipped**，111.45 秒，最终进程退出码 **0** |
| 修改范围内 Ruff 检查 | 通过，退出码 0 |
| 新增适配及测试的 Ruff 格式检查 | 22 个文件已格式化，退出码 0；既有文件的无关格式保持原状 |
| 适配模块、接线模块 `compileall` | 通过，退出码 0 |
| 范围内 `git diff --check` | 通过，退出码 0 |
| 应用 OpenAPI 生成 | 311 个路径，31 个 RAGFlow 专用路由操作；退出码 0 |
| 上游源码身份 | `v0.27.1` / `b9df87c4c75a5b0d35c90d15329fc0f6f91cb73e`；工作树无改动；5,847 个受版本管理文件 |
| 固定 API 文档 SHA-256 | `3aa926033d0d84553272b52b1a3a1298fb4ad5f00741428037c1e71f4287dd4f`，与清单一致 |

两个跳过项属于既有 LlamaIndex 工厂测试中的可选依赖分支，未计作通过。OpenAPI 生成保留了既有 `outputs.py` 路由的重复 operation ID 警告；本轮没有修改该模块。

最终合并回归命令如下。在项目根目录执行；它不是全仓测试，也不包含浏览器和真实上游服务验收：

```powershell
python -m pytest tests/services/rag/test_ragflow_client.py tests/services/rag/test_ragflow_pipeline.py tests/services/rag/test_ragflow_source_contract.py tests/services/rag/test_lightrag_server_pipeline.py tests/services/rag/test_ima_pipeline.py tests/services/rag/test_retrieval_view.py tests/knowledge tests/multi_user/test_ragflow_isolation.py tests/multi_user/test_knowledge_generation_identity.py tests/multi_user/test_rag_tool_no_kb_fallback.py tests/multi_user/test_kb_manifest_access.py tests/api/test_ragflow_knowledge.py tests/api/test_ragflow_route_wiring.py tests/api/test_ragflow_end_to_end.py tests/api/test_knowledge_router.py tests/api/test_task_id_manager.py tests/tools/test_rag_tool.py -q
if ($LASTEXITCODE -ne 0) { throw 'RAGFlow regression checks failed.' }
```

## 组合行为与安全边界

`test_ragflow_end_to_end.py` 使用真实的本地权限、ProfileStore、知识库配置、绑定和 SQLite 操作存储；只有远端 HTTP 响应用 MockTransport 模拟。验证了：

- 连接已有 Dataset，上传文件并保存原文件哈希与元数据；同一幂等键重试只发送一次上传。
- 操作查询、解析真实测试终态、显式 active 发布和带不透明文档/Chunk 引用的检索。
- 停用后不发出上游检索请求；其他测试确认上游离线时仍可本地下架、停用、归档。
- 在途身份撤权、凭据轮换、Dataset 换绑、可见性变更会拒绝旧操作或丢弃检索结果。
- 写意图与文档下架原子提交；未知写入不自动重发；人工重新授权入口仅启动只读对账。
- 数据库重启恢复、租约接管、CAS、Dataset 激活前后崩溃窗口、删除互斥及发布锁。

测试过程中曾由旧创建流程误生成本地 `course` 测试库；核验其唯一测试任务 ID 后，目录已移入系统回收站并撤销测试登记，可恢复。新增组合测试已隔离所有运行时路径到临时目录。用户已有的品牌/UI/部署改动未纳入本轮修改。

## 前端管理页验收（2026-09-06）

本次新增知识中心/新建弹窗/引擎设置接入、连接管理、文档与 Chunk 管理、检索测试、操作摘要与按需事件明细。保持既有主题、导航和其他引擎流程，补齐中英文和窄屏展示。后台新增安全能力查询及文档编辑字段。

验证结果（均获得最终退出码 0）：

- 全量前端 node 回归 **744 passed**；最终 RAGFlow 专项 **41 passed**（属于全量回归覆盖范围，不叠加计数）。
- 前端 TypeScript `--noEmit --incremental false`；改动范围 ESLint `--max-warnings 0`；i18n parity；`git diff --check`。
- 后台路由、真实隔离权限/存储及模拟上游端到端回归 **54 passed**；改动 Python 文件 Ruff check 和 format check。
- 浏览器在本地开发服务、隔离 API 响应下验证：稳定 ID 进入管理页、发布状态更新、Chunk 列表/编辑弹窗、检索来源及查询范围快照、操作摘要/按需事件和 Unix 秒级时间戳、只读管理控件隐藏、文档请求失败不阻断审计、创建待核验可只读对账、终态停止轮询。
- 连接浏览器检查覆盖密钥不回填、轮换后清空、探测保留 `version_unknown`、HTTP 202 保持待完成。新建弹窗切换 RAGFlow 后不显示旧引擎上传/提交控件；桌面与 390px 窄屏截图已检查。

可复现自动回归命令：

```powershell
# E:\yFeiSTAI\web
node node_modules/typescript/bin/tsc --noEmit --incremental false
node scripts/run-node-tests.mjs
node scripts/i18n_parity.mjs
# E:\yFeiSTAI
python -m pytest tests/api/test_ragflow_knowledge.py tests/api/test_ragflow_route_wiring.py tests/api/test_ragflow_end_to_end.py tests/multi_user/test_ragflow_isolation.py -q
```

浏览器辅助 fixtures 和截图保存在本地被 Git 忽略的 `web/output/playwright/ragflow-*`，不是生产数据或线上验收。首次开发编译及热更新期间曾出现超时/依赖数组热更新告警；冷刷新后的最终交互已重新验证。模拟文档 404 场景的网络错误属于故意注入。浏览器和本次测试用 3016 开发服务已关闭，Next 自动改动的 `next-env.d.ts` 已恢复。

## 尚未验证或未交付

没有实际 RAGFlow 实例地址、API Key、模型配置及真实文档解析验收证据；没有服务部署、生产构建或线上运行结论。不可变文档版本/审批系统、原生网页抓取、父子 Chunk 检索及物理删除恢复未作为已交付能力。未创建 Git 提交、推送或发布。

使用前按 [API.md](API.md) 配置连接并对目标实例联调。临时上传文件目前保留在受限服务端 spool 供对账，达到容量上限会拒绝继续接收；未自动清理用户原文件或清空回收站。
