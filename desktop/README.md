# AegisCopilot v2.1 Electron 桌面端

Electron 桌面版提供两条运行路径：开发模式自动启动仓库中的 FastAPI/Vite，发布模式运行内置 React 资源和 PyInstaller 后端。

## 环境准备

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
- `dist/AegisCopilot Setup 2.1.0.exe`：Windows x64 NSIS 安装包

## 数据与模型

开发版和安装版都使用独立的用户目录：

- `%APPDATA%\AegisCopilot\storage`
- `%APPDATA%\AegisCopilot\logs`

首次启动无需登录，可直接使用本地 RAG。DeepSeek Key 通过右下角“模型设置”抽屉输入，由 Electron `safeStorage` / Windows DPAPI 加密保存；Renderer 只能读取“是否已配置”，不能读取密钥本身。

## 当前边界

- 仅构建 Windows x64
- 未配置代码签名，SmartScreen 可能提示未知发布者
- 不包含自动更新、托盘常驻和 macOS/Linux 安装包
- 构建时会把“命令提示符 + 分层知识页”图标和 AegisCopilot 产品信息写入 Windows EXE
- 从旧版升级后，已固定到任务栏的旧快捷方式可能仍使用 Windows 图标缓存；解除固定后从新安装的开始菜单重新固定
