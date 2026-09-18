# AegisCopilot 2.9.0 发布核验记录

## 范围

本版本合并 v2.6–v2.9 四阶段改进：主页与统一下拉菜单、Windows 开发者知识库、逐项命令参数规划，以及用户确认后的本机 PowerShell/CMD 终端执行。

## 安全边界

- 回答生成时绝不自动执行命令；只有 `answer_kind=direct`、无未解析占位符且用户确认后才写入 PTY。
- 模板、澄清卡片和旧版回答没有执行按钮；高风险命令还要求输入“确认执行”。
- 终端只允许 `powershell.exe` 和 `cmd.exe`，最多 4 个临时会话，不保存输出、输入历史或工作目录。
- 不远程执行，不把终端输出写入应用数据库。

## 自动化门禁

- Backend：138 passed。
- Frontend：38 passed；Vite production build passed。
- Desktop：19 passed；包含 shell allowlist、PTY 数据转发、resize、退出清理、四会话上限、malformed input 拒绝和 preload IPC 边界。
- Windows 知识专项：33 个主题、来源 revision/license/hash、PowerShell/CMD 标记和 99 条离线评测数据已生成。
- v2.8 Windows 配方专项覆盖双路径复制、PID 停止、主机/端口连通性、winget 安装、路径与占位符校验、风险复核和 `example.com` 等域名边界。

## 构建产物

- 安装包：`desktop/dist/AegisCopilot Setup 2.9.0.exe`
- 架构：Windows x64 NSIS
- Git commit：`4d8c405a46273db6f4c64d92138c0cc5d2589524`
- SHA-256：`14FB96B98EBD5EBABF07C189DE11BBECF2AE1ABEB8EAEA2247D2FA80480EB119`
- 文件大小：`208,687,568` 字节
- 终端运行时：`node-pty` 作为生产依赖打包，`npmRebuild=false` 并通过 `asarUnpack` 保留 `win32-x64` 预构建原生模块；已核验安装包解包目录包含 `pty.node`、`conpty.node` 和 `conpty_console_list.node`。

依赖审计备注：npm 当前对完整桌面依赖树报告 14 个漏洞（13 high、1 critical），本次没有自动执行 `npm audit fix`，以避免未经审查的 Electron/构建链升级；运行时终端依赖已单独纳入安装包验证，剩余依赖升级应在后续安全维护变更中处理。

构建和校验命令：

```powershell
cd frontend; npm.cmd test; npm.cmd run build
cd ..\backend; .\.venv\Scripts\python.exe -m pytest -q
cd ..\desktop; npm.cmd test; npm.cmd run build:frontend; npm.cmd run build:backend; npm.cmd run dist:dir; .\scripts\smoke-backend.ps1; npm.cmd run dist:win
Get-FileHash .\dist\AegisCopilot Setup 2.9.0.exe -Algorithm SHA256
```

## Computer Use 验收

只在提供原生 AegisCopilot UI surface 时验收主页三示例、SelectMenu、终端面板和确认对话框；不通过 UI 自动化终端、不执行助手生成命令、不上传敏感数据。若当前主机没有原生窗口 surface，应明确记录为未完成 CUA，而不能用浏览器或脚本冒充。
