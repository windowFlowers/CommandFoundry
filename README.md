# AegisCopilot v2.1

AegisCopilot 是一个面向开发者的 Windows 命令 RAG 助手，也是一个可在面试中完整讲清楚的本地优先 AI 应用样例。输入自然语言问题后，它会检索内置知识、给出可复制命令，并明确标注平台、前置条件、风险和来源。

> 应用只复制命令，不会执行命令、打开终端或远程操作系统。

## 核心能力

- 300 个内置 IT 知识主题，覆盖 Linux、Git、SQL、Docker、HTTP、Python、Node、PowerShell、Kubernetes、网络、Redis、Java、CI/CD 与测试调试。
- 多知识库：对话固定绑定一个知识库；可创建、重命名和删除自定义库，并上传 TXT、Markdown、PDF、DOCX（20 MB）。
- 上传文档在本机后台提取、分块和索引，不发送给 DeepSeek；代码围栏不会被切断。
- `rank-bm25` 与 FastEmbed `BAAI/bge-small-zh-v1.5`（512 维 ONNX）双路召回，RRF 融合后返回 Top 4。
- DeepSeek `deepseek-chat` 只负责整理已检索内容，Pydantic 校验结构；回答显示真实 token、耗时或准确回退原因。
- 对删除、强制回滚、强推、数据库破坏性操作、Docker 清理、`curl | sh` 与 `sudo` 做确定性风险复核。
- Electron `safeStorage` / Windows DPAPI 加密 API Key；密钥不返回渲染进程、不进 SQLite、不进日志。
- 本机 SQLite 保存会话、知识库、文本块和向量；运行时不访问 GitHub，断网仍能检索与回答。

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

安装包输出在 `desktop/dist/`。大模型缓存、虚拟环境、依赖目录、运行数据库和日志不会提交到 Git。

## 文档

- [架构与关键取舍](docs/ARCHITECTURE.md)
- [开发、测试与打包](docs/DEVELOPMENT.md)
- [RAG 评测报告](docs/RAG_EVALUATION.md)
- [安全边界](docs/SECURITY.md)
- [5 分钟面试演示脚本](docs/INTERVIEW_DEMO.md)
- [第三方来源与许可证](knowledge/THIRD_PARTY_NOTICES.md)

## 许可证

项目代码采用 MIT License。内置知识中的 tldr 改编内容按 CC BY 4.0 提供，具体来源、固定提交、URL、许可证和文件哈希见 `knowledge/manifest.json` 与 `knowledge/sources.lock.json`。
