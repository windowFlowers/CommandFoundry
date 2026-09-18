# AegisCopilot v2.10 Electron 桌面端

Electron 桌面版提供两条运行路径：开发模式自动启动仓库中的 FastAPI/Vite，发布模式运行内置 React 资源和 PyInstaller 后端。

## 环境准备

桌面开发需要 Node.js 22+；发布安装包的用户不需要单独安装 Node.js。

```powershell
cd backend
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"
cd ..\frontend
npm.cmd install
cd ..\desktop
npm.cmd install
```

## 开发模式

```powershell
cd desktop
npm.cmd run dev
```

Electron 会直接启动 `.venv` 后端和 Vite CLI。后端使用动态空闲端口，Vite 仍支持热更新；关闭窗口后两个子进程都会停止。

## 构建命令

```powershell
npm.cmd test
npm.cmd run build:frontend
npm.cmd run build:backend
.\scripts\smoke-backend.ps1
npm.cmd run dist:dir
npm.cmd run dist:win
```

输出：

- `dist/win-unpacked/AegisCopilot.exe`：未安装目录
- `dist/AegisCopilot Setup 2.10.0.exe`：Windows x64 NSIS 安装包

## 数据与模型

开发版和安装版都使用独立的用户目录：

- `%APPDATA%\AegisCopilot\storage`
- `%APPDATA%\AegisCopilot\logs`

首次启动无需登录，可直接使用本地 RAG。模型设置支持官方 OpenAI SDK 兼容的 `base_url`，用户可选择 DeepSeek、OpenAI、通义千问、Moonshot、SiliconFlow、Ollama 或自定义端点。API Key 通过右下角“模型设置”抽屉输入，由 Electron `safeStorage` / Windows DPAPI 加密保存；Renderer 只能读取“是否已配置”，不能读取密钥本身。

## 当前边界

- 仅构建 Windows x64
- 未配置代码签名，SmartScreen 可能提示未知发布者
- 不包含自动更新、托盘常驻和 macOS/Linux 安装包
- 构建时会把“命令提示符 + 分层知识页”图标和 AegisCopilot 产品信息写入 Windows EXE
- 从旧版升级后，已固定到任务栏的旧快捷方式可能仍使用 Windows 图标缓存；解除固定后从新安装的开始菜单重新固定

## 内置终端与执行确认

- 终端由 Electron 主进程通过 `node-pty` 管理，渲染进程只能通过 preload IPC 访问；前端以右侧可调宽度面板展示，不遮挡聊天内容。
- 模型设置复用官方 OpenAI Node SDK 的 `baseURL` 配置和真实 Chat Completions 测试，不手写一套第二协议；支持 DeepSeek、OpenAI、通义千问、Moonshot、SiliconFlow、Ollama 和自定义 OpenAI 兼容接口。API Key 仍由 Electron `safeStorage` / Windows DPAPI 加密。
- 只允许 `powershell.exe` 和 `cmd.exe`，默认 PowerShell、默认工作目录为用户目录，最多 4 个临时会话。
- 只有无占位符的 `direct` 命令显示“在终端执行”；模板、澄清卡片和旧版回答没有该按钮。
- 用户确认后才向 PTY 写入命令并回车；高风险命令需要额外输入“确认执行”。应用不会自动或远程执行命令。
- 终端输出、输入历史和工作目录不会保存到 SQLite，也不会在重启后恢复。
