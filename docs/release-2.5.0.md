# AegisCopilot 2.5.0 发布核验记录

## 版本与提交

- 版本：`2.5.0`
- 实现提交：`6d49c0be4876e0cce9a74269fa803ab6d84df027`（`feat: add direct command planning and clarifications`）
- 发布报告提交：见本文件所在提交；标签：`v2.5.0`

## 本次交付

- 新增确定性命令规划器：信息完整且命中可信配方时输出单个、无占位符、可复制命令块；缺参时一次只询问一个 slot；跳过、拒绝或一次无效输入安全回退到单个模板。
- 新增 `conversation_command_plans` 持久化表（schema v5），支持重启恢复、24 小时过期、取消/重新开始、乐观锁及知识库 revision 失效。
- 命令渲染按 PowerShell、CMD、POSIX shell 分开处理路径转义；敏感凭证不落库、不发送模型；模型不能覆盖规划器命令或引用。
- 新增带 provenance 的 recipe 目录，覆盖 Python venv、Git revert、Docker run、Windows 文件/端口、MySQL、Redis；同步接入 manifest、sources.lock 与索引 revision。
- 保留并回归父子块检索、精确引用和用户个性化上下文能力；旧 Answer/SQLite 数据按模板安全兼容。
- 前端新增 clarification/direct/template 三态卡片、快捷选项、历史卡片锁定、ARIA live/busy 与键盘/触控可访问性。

## 自动化测试与构建

- 后端全量：`130 passed, 2 warnings`。
- 命令规划器专项：`18 passed, 2 warnings`；recipe 专项：`4 passed`。
- 前端：`34 passed`；Vite 生产构建成功（1596 modules）。
- 桌面端：`16 passed`。
- `compileall` 成功；`git diff --check` 无错误。
- Windows x64 NSIS 构建成功；包内后端烟测通过：health `2.5.0`、300 个知识主题、`hybrid` 检索、profile/memory CRUD、离线 SSE、四步逐项澄清到单条 PowerShell 命令（无 `<...>` 且含引用）。烟测不会执行生成的命令。
- 构建日志中的 Anaconda 环境、缺少 `onnx`/`tzdata` 的 PyInstaller 警告不影响产物生成；后端测试的两条警告为既有 Starlette/httpx 弃用提示。

## 产物校验

- 安装包：`desktop/dist/AegisCopilot Setup 2.5.0.exe`
  - 大小：`205,521,913` bytes
  - SHA-256：`C29B58A4B42A522C87FF3C3D451AA34AFDEB31A3567472351E0095873DF7E77D`
- 解包应用：`desktop/dist/win-unpacked/AegisCopilot.exe`
  - 大小：`210,954,752` bytes
  - SHA-256：`A555384B9C3C095DF732F77FA5205BE5830FE205D5AAC4F1395136941E01F086`
- 包内后端：`resources/backend/aegis-backend.exe`
  - 大小：`16,305,572` bytes
  - SHA-256：`600EFFB1E0AED76349A387C1B2A0D73D4B1FC1FCC5D971D569A015D637225804`
- recipe revision：`3af841c515e2df6c009868a0ce298485d45439583fd4e8aada4ab69fe417c19d`（8 recipes）。

## Computer Use 状态

本次会话未提供原生桌面 CUA surface：`cua.getState()` 仅返回浏览器且 `apps=[]`，`getApp` 不可用；备用 `sky` RPC 也报告 trusted RPC 未配置。因此无法在不冒充的前提下完成 AegisCopilot 原生窗口点击验收。已完成包内真实 HTTP/SSE 烟测和全部自动化门禁；没有自动化终端来代替 CUA，也没有执行任何生成命令或上传敏感数据。应在提供 native app surface 的 host 上补跑截图中的 UI 场景。
