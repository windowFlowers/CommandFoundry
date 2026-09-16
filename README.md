# AegisCopilot v2.4

AegisCopilot 是一个面向开发者的 Windows 命令 RAG 助手，也是一个可在面试中完整讲清楚的本地优先 AI 应用样例。输入自然语言问题后，它会检索内置知识、给出可复制命令，并明确标注平台、前置条件、风险和来源。

> 应用只复制命令，不会执行命令、打开终端或远程操作系统。

## 核心能力

- 300 个内置 IT 知识主题，覆盖 Linux、Git、SQL、Docker、HTTP、Python、Node、PowerShell、Kubernetes、网络、Redis、Java、CI/CD 与测试调试。
- 多知识库：对话固定绑定一个知识库；可创建、重命名和删除自定义库，并上传 TXT、Markdown、PDF、DOCX（20 MB）。
- 会话内分层记忆：滚动结构化摘要与最近两轮原始对话组合，支持平台替换、代词和“第二种方法”等追问；记忆跨重启保存，但不跨会话或知识库共享。
- 单用户长期记忆：只从用户明确表达的语言、详略、熟练度、平台、工具链和项目约束中提取可编辑记忆；支持全局/知识库作用域、固定、撤销、冲突替代、关闭和彻底清空。含值的中英文密钥、令牌或密码、私钥、连接串、完整本地路径、代码和命令会在进入长期记忆或后台模型队列前拒绝。
- 每轮最多使用 4 条脱敏个性化记忆。当前问题和当前会话始终优先；个性化只调整检索和表达，不充当技术证据，也不能降低命令风险。本地确定性回退也只把选中的语言、详略和熟练度用于表达，命令、引用和风险结论仍来自本地证据。
- 上传文档在本机完成结构化提取与父子分块：约 420 字符的子块负责检索，受限的章节父块负责生成；代码围栏与命令证据保持完整。
- `rank-bm25` 与 FastEmbed `BAAI/bge-small-zh-v1.5`（512 维 ONNX）双路召回，RRF 先融合 20 个子块候选，再按父块聚合为 Top 4（每份文档最多 2 个父块）。
- 回答正文和命令卡使用行内引用编号；上传文档引用可定位到页码、标题/行号或 DOCX 段落/表格行，并高亮精确文本区间。
- DeepSeek `deepseek-chat` 只负责整理已检索内容，Pydantic 校验结构；回答显示真实 token、耗时或准确回退原因。模型正文没有有效的精确引用段时会弃用该正文，改用证据派生的本地回退回答。
- 对删除、强制回滚、强推、数据库破坏性操作、Docker 清理、`curl | sh` 与 `sudo` 做确定性风险复核。
- Electron `safeStorage` / Windows DPAPI 加密 API Key；密钥不返回渲染进程、不进 SQLite、不进日志。
- 本机 SQLite 保存会话、知识库、文本块和向量；运行时不访问 GitHub，断网仍能检索与回答。
- SQLite 以 `PRAGMA user_version` 管理升级；现有 v2.3 数据库首次升级到 v2.4 前，会通过 SQLite Backup API 创建并校验 `aegis-v2.sqlite3.pre-v2.4.backup`。文档重建通过单事务原子切换，失败时旧索引仍可使用；进程中断的索引或重建会在下次启动重新入队。
- 普通问答仍只调用一次 DeepSeek；仅当历史达到压缩阈值，或用户明确表达了本地规则无法结构化的长期偏好时，才排队执行对应的后台模型任务。异步记忆提取通过不含来源文本和错误详情的状态接口反馈 `ready`、`NOOP`、失败或取消；只有真正新建且仍生效的记忆可从提示中撤销。

## 技术栈

| 层 | 选择 |
|---|---|
| Desktop | Electron 39、custom protocol、hidden title bar、electron-builder / NSIS |
| Frontend | React 18、Vite 5、Lucide React |
| Backend | Python 3.11+、FastAPI、Pydantic、SQLite、SSE |
| Retrieval | rank-bm25、FastEmbed、ONNX Runtime、NumPy、RRF |
| Generation | DeepSeek OpenAI-compatible API |
| Packaging | PyInstaller onedir、electron-builder x64 |

## 最快启动

```powershell
cd C:\path\to\AegisCopilot-v2\backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"

cd ..\frontend
npm install

cd ..\desktop
npm install
npm run dev
```

源码开发环境在缓存缺失时会将约 90 MB 的 BGE ONNX 模型下载到 `models/cache/`；下载期间 BM25 已可工作，完成后自动切换到混合检索。Windows 安装包已内置固定版本模型并强制本地加载，不需要联网下载。模型 Key 可在应用右下角“模型设置”中输入；连接测试会发送最小 Chat Completions 请求。

## 验证与构建

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm test
npm run build

cd ..\desktop
npm test
npm run verify:deepseek
npm run verify:rag
npm run verify:uploads
npm run dist:win
```

2026-09-16 的本地离线回归结果为 Backend 108 项、Frontend 29 项、Desktop 16 项全部通过，Vite 生产构建通过；长期个性化信号门数据集 60/60 项通过，且隔离服务下的 TXT、Markdown、PDF、DOCX 上传、预览、重复拒绝与重建验收通过。`verify:deepseek` 和使用真实 Key 的完整 RAG 验证属于可选联网验收。

构建命令将安装包输出到 `desktop/dist/`。当前项目未配置 Windows 代码签名，生成的 NSIS 安装包会显示未知发布者，SmartScreen 也可能提示风险；正式对外分发前应配置可信代码签名并在干净 Windows 环境验证安装、升级和卸载。大模型缓存、虚拟环境、依赖目录、运行数据库和日志不会提交到 Git。

## 文档

- [架构与关键取舍](docs/ARCHITECTURE.md)
- [开发、测试与打包](docs/DEVELOPMENT.md)
- [RAG 评测报告](docs/RAG_EVALUATION.md)
- [安全边界](docs/SECURITY.md)
- [5 分钟面试演示脚本](docs/INTERVIEW_DEMO.md)
- [第三方来源与许可证](knowledge/THIRD_PARTY_NOTICES.md)

## 许可证

项目代码采用 MIT License。内置知识中的 tldr 改编内容按 CC BY 4.0 提供，具体来源、固定提交、URL、许可证和文件哈希见 `knowledge/manifest.json` 与 `knowledge/sources.lock.json`。
