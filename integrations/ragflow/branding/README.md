# yFei 品牌定制

这份定制基于 RAGFlow `v0.27.1`，上游提交由 [../UPSTREAM.json](../UPSTREAM.json) 锁定。可见产品品牌使用 `yFei`；Logo 直接复用 yFeiSTAI 的 [web/public/logo.png](../../../web/public/logo.png)，没有重新绘制或修改原项目素材。

## 修改范围

- 页签图标、默认头像、导航 Logo、深浅色横版字标；登录背景内的旧 Logo 和路径化文字一并替换，原背景图和装饰路径保留。
- 前端页面标题、登录/管理入口、分享与搜索、默认产品名称，以及 16 种语言的产品文案。
- 上游 README、使用文档及非 Web 的产品显示文案，包括终端横幅、邮件/消息标题、CLI 帮助和服务说明。
- README 使用本地横版 Logo；上游宣传图片改为来源链接，避免继续内嵌旧 Logo 或将上游历史成绩归属于本项目。

保留内容：Apache 许可证、版权与作者信息、上游历史资料；HTTP API 锁版参考文件；API/SDK 标识、包名和命令入口、环境变量、路由、存储键、数据库与镜像配置、上游链接及第三方归因字段。模型供应商和第三方工具图标不是本产品 Logo，不替换。

因此，源码搜索仍会找到 `ragflow`/`RAGFlow` 技术标识与上游署名；这不是遗漏的产品换肤。

## 保存与重放

`upstream/` 是被父仓库忽略的独立源码检出，不能只依靠该目录保存改动：

- `yfei.patch`：全部品牌差异，可随父项目纳入 Git。
- `manifest.json`：固定基线、补丁 SHA-256、项目 Logo 摘要、各修改文件的前后摘要。文本摘要统一按 UTF-8/LF 计算。
- `apply-branding.mjs`：应用前核验版本与文件内容；遇到目标文件有本地修改或只应用了一部分时停止，不覆盖；重复运行只核验，不重复修改。

在 yFeiSTAI 项目根目录执行（需要 Node.js 和 Git）：

```powershell
# 已克隆源码：先做只读检查，再应用。
node integrations/ragflow/branding/apply-branding.mjs --check
node integrations/ragflow/branding/apply-branding.mjs

# 可指定同一固定版本的其他检出目录。
node integrations/ragflow/branding/apply-branding.mjs E:/work/ragflow --check

# 定向回归；依赖主项目 web/node_modules 中的 TypeScript。
node --test integrations/ragflow/branding/branding.test.mjs integrations/ragflow/branding/frontend-contract.test.mjs

# 非 Web 源码合同；只用 Python 3.11+ 标准库，不启动上游服务。
python integrations/ragflow/branding/backend-contract.py
```

不要对已有改动执行强制重置。升级上游时先独立检查版本差异，再重新核对品牌修改，不能跳过脚本的版本检查。

新检出请遵循集成 README 中的 `core.autocrlf=false` 克隆命令；锁版 HTTP 文档校验使用原始字节。品牌补丁自己的 `.gitattributes` 强制 LF，避免 Windows 检出转换导致补丁摘要失效。

## 验证与生效边界

自动检查覆盖项目 Logo 字节一致、原背景保留、第三方图标不变、源码固定版本/许可证/API 契约/部署配置不变、前端语法与结构、翻译键和技术字符串，以及在独立临时 Git 索引中重放补丁。另用仅检出所需文件的临时克隆验证首次应用、重复运行及拒绝覆盖本地修改。测试产物仅写入主项目被忽略的 `web/output/playwright/`，不会修改上游真实索引或已有暂存区。

浏览器已直接加载修改后的真实 SVG，检查深浅色字标、16/32/48/80 px 图标及登录背景；桌面和 390 px 窄屏均加载成功且无水平溢出。资源预览不等于完整业务页面验收。

本轮没有安装上游前端依赖，也没有完成完整应用构建、镜像发布或真实 RAGFlow 部署。`Dockerfile` 构建前端后将 `web/dist` 放入镜像；标准 Compose 使用预构建镜像，不挂载本地 Web 源码。因此仅重启原镜像不会生效，部署前必须构建并使用包含本定制的新镜像；Go 终端/服务文案还需重新构建对应 Go 二进制。

本次补丁共 162 个上游文件：35 个前端源文件/资源、92 个文档、32 个非 Web 源文件、3 个 Shell 脚本。Go 检查是固定基线的允许字符串差异核验，不是 Go 编译或运行验证。

2026-09-06 验证记录：Node 品牌/前端/补丁重放测试 **89 项通过**；非 Web 契约 **32 文件通过**；原适配层 `test_ragflow_source_contract.py`、`test_ragflow_client.py`、`test_ragflow_pipeline.py` 合计 **91 项通过**；3 个 Shell 脚本的 Bash 语法检查通过。上述命令最终退出码均为 0。浏览器品牌资源预览为 7 张图片加载成功、桌面与 390 px 窄屏无水平溢出、控制台 0 error / 0 warning。
