# AegisCopilot 2.10.0 发布核验记录

## 范围

本版本将终端改为不遮挡对话的 Codex 风格右侧可调宽度面板，并把多模型调用统一迁移到官方 OpenAI Python/Node SDK。模型设置继续使用本地加密配置，可选择 DeepSeek、OpenAI、通义千问、Moonshot、SiliconFlow、Ollama 或自定义 OpenAI 兼容 Chat Completions 端点。

## 多模型实现边界

- 后端使用 `openai>=1.63,<2` 的官方 Python SDK；桌面端使用 `openai@7.18.0` 官方 Node SDK。
- SDK 负责 `base_url/baseURL`、认证、超时、HTTP 状态错误和 Chat Completions 响应解析；项目只保留提供商预设、密钥加密、结构化 JSON/Pydantic 校验和证据安全审查。
- 这是 OpenAI-compatible 协议支持，不把原生 Anthropic Messages 等非兼容协议伪装成 Chat Completions；这类服务可通过其兼容端点或“自定义 OpenAI 兼容 API”接入。
- Ollama 使用本机 OpenAI 兼容端点，可不填写 API Key；传给 SDK 的 `ollama` 仅是 SDK 要求的非秘密占位值。
- SQLite schema v6 会把旧版长期记忆来源约束迁移为开放的 provider id；迁移前保留 `.pre-v2.10.backup`，既有内容不丢失。

## 安全边界

- 回答生成时绝不自动执行命令；只有 `answer_kind=direct`、无未解析占位符且用户确认后才写入 PTY。
- 模板、澄清卡片和旧版回答没有执行按钮；高风险命令还要求输入“确认执行”。
- 终端只允许 `powershell.exe` 和 `cmd.exe`，最多 4 个临时会话，不保存输出、输入历史或工作目录。
- API Key 通过 Electron `safeStorage` / Windows DPAPI 加密，不进入 SQLite、日志、Renderer 或模型提示词。

## 自动化门禁

- Backend：143 passed，2 条既有 Starlette/httpx 弃用警告。
- Frontend：40 passed；Vite production build passed。
- Desktop：24 passed；包含官方 Node SDK 连接测试、兼容参数降级、Ollama 无 Key 路径、provider 密钥隔离、模型配置加密、PTY allowlist、右侧终端面板、会话上限、清理和 preload IPC 边界。
- 后端 PyInstaller onedir 构建通过；包内后端 smoke 覆盖 health、knowledge、profile、memory 和离线 SSE。
- `pip check` 无 broken requirements；桌面生产依赖 `npm audit --omit=dev --audit-level=high` 报告 0 vulnerabilities。

## 构建产物

- 安装包：`desktop/dist/AegisCopilot Setup 2.10.0.exe`
- 架构：Windows x64 NSIS
- Git commit：待最终提交后填写
- SHA-256：待最终打包后填写
- 文件大小：待最终打包后填写

构建和校验命令：

```powershell
cd backend; .\.venv\Scripts\python.exe -m pytest -q
cd ..\frontend; npm.cmd test; npm.cmd run build
cd ..\desktop; npm.cmd test; npm.cmd run build:frontend; npm.cmd run build:backend; .\scripts\smoke-backend.ps1; npm.cmd run dist:win
Get-FileHash .\dist\AegisCopilot Setup 2.10.0.exe -Algorithm SHA256
```

## Computer Use 验收

只在提供原生 AegisCopilot UI surface 时验收模型设置、右侧终端面板和对话不被遮挡；不通过 UI 自动化终端、不执行助手生成命令、不上传敏感数据。若当前主机没有原生窗口 surface，应明确记录为未完成 CUA，而不能用浏览器或脚本冒充。

本次 Computer Use 初始化返回 `Trusted RPC service is not configured: sky`，没有可绑定的原生 AegisCopilot 窗口；因此未以脚本或浏览器冒充 UI 验收。源码测试、安装包构建和后端 smoke 独立完成，原生窗口服务可用后再按上述边界补做目视验收。
