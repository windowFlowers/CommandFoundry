# AegisCopilot 2.9

面向开发者的本地优先命令 RAG 助手，支持 Windows 桌面端。

AegisCopilot 将自然语言操作转换为有来源、可复制的命令。它检索本地知识，保留父子文档上下文，引用精确证据范围；当命令缺少路径、Shell 或其他参数时，逐项向用户澄清，不猜测路径、端口、凭证或隐式记忆。

> AegisCopilot 不会自动执行或远程执行命令。用户明确确认后，direct 命令才可以写入本机的 PowerShell/CMD 终端；模板和澄清回答始终只能复制。

English version: [README.en.md](README.en.md)

## 核心能力

- **直接命令规划**：信息完整且命中可信配方时输出唯一命令块，不含占位符；缺少信息时一次只询问一个参数。
- **安全模板回退**：用户跳过、拒绝提供或一次输入无效后，回退为一个明确标注的模板，不无限追问。
- **Shell 专用渲染**：PowerShell、CMD、POSIX 使用独立转义规则，覆盖带空格/中文路径、UNC 路径和尾部反斜杠。
- **可信配方目录**：Python 虚拟环境、Git revert、Docker run、Windows 文件/端口操作、MySQL、Redis 配方均带来源 provenance。
- **Windows 开发者知识库**：内置 33 个 PowerShell/CMD 主题，覆盖文件、进程、服务、网络、压缩、哈希、HTTP、环境变量、winget/Chocolatey/Scoop 和 robocopy 风险提示。
- **父子块检索**：子块负责精确召回，受限父块保留上下文；引用可定位到子块、父块和文档精确位置。
- **个性化上下文**：当前问题优先于会话、知识库和全局偏好；每轮最多使用 4 条脱敏记忆，只影响检索补充和表达，不充当命令、事实或风险证据。
- **多知识库与上传**：支持创建、绑定和管理多个知识库，上传 TXT、Markdown、PDF、DOCX，单文件上限 20 MB。
- **本地优先**：BM25 与 FastEmbed 本地检索无需云端 Key；DeepSeek 只负责整理已验证证据，不能覆盖规划器命令或引用。
- **桌面安全**：API Key 通过 Electron `safeStorage` / Windows DPAPI 加密，不暴露给渲染进程，也不写入 SQLite。
- **内置终端**：仅允许 PowerShell 与 CMD，最多 4 个临时 PTY；执行前显示 Shell、工作目录、完整命令、风险和警告，高风险命令还要求输入“确认执行”。终端输出、输入历史和工作目录不会写入应用数据库。

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

手动测试澄清流程时不要执行生成的命令。终端功能如需验收，请只在用户确认框中手动输入无害命令，不要把助手生成的高风险命令用于测试。

## 技术架构

| 层 | 技术 |
|---|---|
| Desktop | Electron 39、custom protocol、electron-builder / NSIS |
| Frontend | React 18、Vite 5、Lucide React |
| Backend | Python 3.11+、FastAPI、Pydantic、SQLite、SSE |
| Retrieval | rank-bm25、FastEmbed `BAAI/bge-small-zh-v1.5`、ONNX Runtime、RRF |
| Generation | 可选 DeepSeek OpenAI-compatible API |
| Packaging | PyInstaller onedir、electron-builder Windows x64 |

命令规划器使用 SQLite schema v5 保存可恢复计划，支持重启恢复、取消/重新开始、乐观锁和默认 24 小时过期，并记录来源 revision 与 citation IDs。知识库重建后，依赖旧证据的 pending 计划会自动失效；凭证值不会进入计划或模型输入。当前 recipe 目录包含 21 条带 provenance 的高价值配方。

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
npm.cmd run build:frontend
npm.cmd run build:backend
npm.cmd run dist:dir
.\scripts\smoke-backend.ps1
npm.cmd run dist:win
```

v2.9.0 发布门禁结果：

- Backend：138 项通过；
- Frontend：38 项通过，Vite 生产构建通过；
- Desktop：19 项通过，包含 PTY allowlist、会话上限、清理和 preload IPC 边界；
- 包内后端烟测覆盖 profile/memory CRUD、混合检索、离线 SSE 和逐项澄清流程。

Windows x64 安装包输出到 `desktop/dist/AegisCopilot Setup 2.9.0.exe`，应作为 GitHub Release asset 分发，不提交到源码历史。安装包 SHA-256、构建提交和完整门禁记录见 [docs/release-2.9.0.md](docs/release-2.9.0.md)。

## 安装与发布边界

- 目标平台：Windows 10/11 x64；安装包内置本地模型，首次启动无需下载模型。
- 安装后数据目录：`%APPDATA%\AegisCopilot\storage`，日志目录：`%APPDATA%\AegisCopilot\logs`。
- 安装包未配置代码签名，Windows SmartScreen 可能显示未知发布者；当前没有自动更新，也不提供 macOS/Linux 安装包。
- GitHub Release 中的安装包适合测试和个人使用；正式分发前应配置可信代码签名，并在干净 Windows 环境验证安装、升级和卸载。

## 知识库与重新索引

上传文档会在本机完成结构化提取、父子分块、向量化和索引。启用父子块规则之前建立的旧文档，应在“知识库管理”中执行重建/重新索引。重建会更新 index revision，并安全使依赖旧证据的命令计划失效。

## 安全边界

- 不自动执行或远程执行命令；
- 仅在用户确认后向本机 PowerShell/CMD PTY 写入 direct 命令；模板、澄清卡片和旧版回答没有执行按钮；
- 终端最多 4 个临时会话，关闭软件时清理，输出、输入历史和工作目录不持久化；
- 凭证、API Key、Token、私钥和连接串不进入 SQLite、日志、长期记忆或模型提示词；
- 确定性规则会标注/警告高风险命令；direct 规划结果不含未解析占位符，template 回退可能保留占位符，应用本身不是沙箱且不会替用户执行；
- API Key 使用 Electron `safeStorage` / Windows DPAPI 静态加密。

## 项目规则与文档

- [English README](README.en.md)
- [项目规则（含后续任务必须使用子 agent 的规则）](AGENTS.md)
- [v2.9.0 发布核验记录](docs/release-2.9.0.md)
- [架构与关键取舍](docs/ARCHITECTURE.md)
- [开发、测试与打包](docs/DEVELOPMENT.md)
- [RAG 评测报告](docs/RAG_EVALUATION.md)
- [安全边界](docs/SECURITY.md)
- [第三方来源与许可证](knowledge/THIRD_PARTY_NOTICES.md)

## 许可证

项目代码采用 MIT License。内置知识中的 tldr 改编内容按 CC BY 4.0 提供；来源 URL、固定 revision、许可证和文件哈希见 `knowledge/manifest.json` 与 `knowledge/sources.lock.json`。
