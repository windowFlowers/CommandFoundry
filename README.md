# AegisCopilot 2.5

面向开发者的本地优先命令 RAG 助手，支持 Windows 桌面端。

AegisCopilot 将自然语言操作转换为有来源、可复制的命令。它检索本地知识，保留父子文档上下文，引用精确证据范围；当命令缺少路径、Shell 或其他参数时，逐项向用户澄清，不猜测路径、端口、凭证或隐式记忆。

> AegisCopilot 只展示供复制的命令，不会自动执行命令、打开终端或远程操作系统。

## 核心能力

- **直接命令规划**：信息完整且命中可信配方时输出唯一命令块，不含占位符；缺少信息时一次只询问一个参数。
- **安全模板回退**：用户跳过、拒绝提供或一次输入无效后，回退为一个明确标注的模板，不无限追问。
- **Shell 专用渲染**：PowerShell、CMD、POSIX 使用独立转义规则，覆盖带空格/中文路径、UNC 路径和尾部反斜杠。
- **可信配方目录**：Python 虚拟环境、Git revert、Docker run、Windows 文件/端口操作、MySQL、Redis 配方均带来源 provenance。
- **父子块检索**：子块负责精确召回，受限父块保留上下文；引用可定位到子块、父块和文档精确位置。
- **个性化上下文**：用户明确表达的语言、详略、熟练度、平台和工具偏好可按全局或知识库作用域保存；敏感信息不会进入长期记忆。
- **本地优先**：BM25 与 FastEmbed 本地检索无需云端 Key；DeepSeek 只负责整理已验证证据，不能覆盖规划器命令或引用。
- **桌面安全**：API Key 通过 Electron `safeStorage` / Windows DPAPI 加密，不暴露给渲染进程，也不写入 SQLite。

## 手动测试：逐项补参生成可执行命令

1. 新建对话，选择“开发者 IT 知识库”。
2. 输入：`Python 怎么创建并激活虚拟环境？`
3. 被询问目标目录时输入：`C:\Users\CZX\Documents\AegisCopilot 手动测试`。
4. 选择 **PowerShell**，再选择 **.venv**。
5. 最终回答应只有一个可复制的 PowerShell 命令块，无 `<...>` 占位符，引用可展开，内容应类似：

```powershell
python -m venv 'C:\Users\CZX\Documents\AegisCopilot 手动测试\.venv'
& 'C:\Users\CZX\Documents\AegisCopilot 手动测试\.venv\Scripts\Activate.ps1'
```

手动测试时不要执行生成的命令。

## 技术架构

| 层 | 技术 |
|---|---|
| Desktop | Electron 39、custom protocol、electron-builder / NSIS |
| Frontend | React 18、Vite 5、Lucide React |
| Backend | Python 3.11+、FastAPI、Pydantic、SQLite、SSE |
| Retrieval | rank-bm25、FastEmbed `BAAI/bge-small-zh-v1.5`、ONNX Runtime、RRF |
| Generation | 可选 DeepSeek OpenAI-compatible API |
| Packaging | PyInstaller onedir、electron-builder Windows x64 |

命令规划器使用 SQLite schema v5 保存可恢复计划，默认 24 小时过期，并记录来源 revision 与 citation IDs。知识库重建后，依赖旧证据的 pending 计划会自动失效；凭证值不会进入计划或模型输入。

## 开发环境

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"

cd ..\frontend
npm.cmd install

cd ..\desktop
npm.cmd install
npm.cmd run dev
```

首次源码运行可能会把 BGE ONNX 模型下载到 `models/cache/`。Windows 安装包包含固定版本的本地模型，首次启动不需要联网下载。

## 测试与打包

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm.cmd test
npm.cmd run build

cd ..\desktop
npm.cmd test
npm.cmd run dist:win
```

v2.5.0 发布门禁结果：

- Backend：130 项通过；
- 命令规划器：18 项通过；recipe 目录：4 项通过；
- Frontend：34 项通过，Vite 生产构建通过；
- Desktop：16 项通过；
- 包内后端烟测通过，覆盖 profile/memory CRUD、混合检索、离线 SSE 和四步澄清流程。

Windows x64 安装包输出到 `desktop/dist/AegisCopilot Setup 2.5.0.exe`，应作为 GitHub Release asset 分发，不提交到源码历史。

## 知识库与重新索引

上传文档会在本机完成结构化提取、父子分块、向量化和索引。启用父子块规则之前建立的旧文档，应在“知识库管理”中执行重建/重新索引。重建会更新 index revision，并安全使依赖旧证据的命令计划失效。

## 安全边界

- 不自动执行命令；
- 桌面端不自动化终端；
- 凭证、API Key、Token、私钥和连接串不进入 SQLite、日志、长期记忆或模型提示词；
- 风险审查会拒绝不安全命令和未解析占位符；
- API Key 使用 Electron `safeStorage` / Windows DPAPI 静态加密。

## 项目规则与文档

- [English README](README.en.md)
- [项目规则（含后续任务必须使用子 agent 的规则）](AGENTS.md)
- [v2.5.0 发布核验记录](docs/release-2.5.0.md)
- [架构与关键取舍](docs/ARCHITECTURE.md)
- [开发、测试与打包](docs/DEVELOPMENT.md)
- [RAG 评测报告](docs/RAG_EVALUATION.md)
- [安全边界](docs/SECURITY.md)
- [第三方来源与许可证](knowledge/THIRD_PARTY_NOTICES.md)

## 许可证

项目代码采用 MIT License。内置知识中的 tldr 改编内容按 CC BY 4.0 提供；来源 URL、固定 revision、许可证和文件哈希见 `knowledge/manifest.json` 与 `knowledge/sources.lock.json`。
