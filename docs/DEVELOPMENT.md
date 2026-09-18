# 开发、测试与打包

## 环境

- Windows 10/11 x64
- Python 3.11 或 3.12
- Node.js 20+
- 可选：任一 OpenAI-compatible API Key（也可连接无需 Key 的本机 Ollama）
- 桌面开发：Node.js 22+（Electron 39 与官方 OpenAI Node SDK 运行时要求）
- 多模型协议：当前内置提供商使用官方 OpenAI SDK 的 OpenAI-compatible Chat Completions 接口；原生 Anthropic Messages 等非兼容协议需使用其兼容端点或自定义兼容网关。

## 本地开发

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"

cd ..\frontend
npm install

cd ..\desktop
npm install
npm run dev
```

Electron 会分配空闲端口启动 FastAPI 和 Vite。会话数据库与加密配置位于 `%APPDATA%\AegisCopilot`。不要在源码环境文件中配置真实 Key；使用应用设置抽屉。

## SQLite v2.4 升级

v2.4 的数据库 schema 版本为 4。已有 `user_version=3` 的 v2.3 数据库第一次由 v2.4 打开时，仓储层会先使用 SQLite Backup API 创建 `%APPDATA%\AegisCopilot\storage\aegis-v2.sqlite3.pre-v2.4.backup`，通过 `PRAGMA quick_check` 后再保留为迁移前快照；已有且有效的同名备份不会被覆盖。迁移随后创建本机用户画像、长期记忆和后台提取任务表，并把 `user_version` 更新为 4。

该备份用于人工回退，应用不会自动恢复它。升级验收完成前不要删除；需要回退时应先完全退出 AegisCopilot，并同时保留当前数据库以便排查。

启动恢复不会覆盖已经可用的文档 revision：被中断的新上传索引和既有文档重建都会重新标记为待处理并由后台工作线程继续；中断的长期记忆提取也会从 `extracting` 回到 `pending`，再按当前画像开关和 epoch 校验后执行或取消。

v2.10 将数据库 schema 更新为 6：把 v2.5 长期记忆来源的 `local/deepseek/manual` 限制扩展为任意已配置 provider id。首次打开旧数据库时会在迁移前创建 `.pre-v2.10.backup`，已有对话、文档和记忆内容会原样保留。

## 重新生成知识

`scripts/sync_knowledge.py` 固定使用 tldr 提交 `d7f4fcb00a22fa5e5a595323705df61e1ecff707`，生成主题、manifest、评测集和声明：

```powershell
.\backend\.venv\Scripts\python.exe scripts\sync_knowledge.py
```

运行时不依赖该仓库，也不打包 `all-in-rag` 教程内容。

## 自动化测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm test
npm run build

cd ..\desktop
npm test
```

测试覆盖单轮/多轮检索评测、父子块检索、四格式精确定位、上下文预算与隔离、摘要恢复和并发、长期记忆信号门与 60 条评测、知识库作用域、冲突替代、敏感数据拒绝、后台提取竞态与状态脱敏、任务/索引重启恢复、来源追溯、模型引用校验、无有效引用段的本地回退、上传命令证据、原子重建回滚、模型失败回退、危险命令、SSE、会话 CRUD、密钥密文、preload 边界、进程路径和自定义协议。

2026-09-16 的本地回归结果为：Backend 108 passed、Frontend 29 passed、Desktop 16 passed，Vite 生产构建通过；隔离服务下的四格式上传、预览、重复拒绝和重建验收通过。测试数会随用例变化，发布时应以同一提交上重新运行的结果为准。

需要复现 BM25 与 BGE + RRF 两组完整指标时运行：

```powershell
.\backend\.venv\Scripts\python.exe scripts\evaluate_rag.py
```

输出同时包含 300 条单轮集合与 60 组多轮集合的 BM25、混合检索 Top-1/Top-3 指标。

上传文档的 80 条父子检索专项评测可独立复现：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_parent_child_retrieval_evaluation.py -q -s
```

该项离线关闭向量召回，直接验证正式结构化提取、子块 BM25 检索和父块聚合路径，不依赖 API Key、网络或模型缓存。

长期记忆的 60 条跨对话个性化离线信号与安全评测可独立复现：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_personalization_evaluation.py -q
```

数据集位于 `knowledge/personalization_eval_dataset.json`，当前结果为 60/60 通过（30 条应提取、15 条应忽略、15 条应拒绝敏感内容）。该测试验证本地信号门与作用域契约，不调用真实模型，也不包含真实个人信息或密钥。

## Windows 构建

确认 `models/cache/` 已有离线 BGE 模型后运行：

```powershell
cd desktop
npm run dist:dir
.\scripts\smoke-backend.ps1
npm run dist:win
```

流水线依次执行：ImageGen PNG 转多尺寸 ICO、Vite 生产构建、PyInstaller onedir、electron-builder NSIS x64。最终安装包在 `desktop/dist/`。

当前 `desktop/package.json` 设置 `signAndEditExecutable: false`，项目没有配置 Windows 代码签名。生成的 EXE/NSIS 安装包可能被 SmartScreen 标记并显示未知发布者，不能把自动化打包通过表述为已完成签名或发布认证。正式分发前还需在干净 Windows 环境验证安装、启动、v2.3→v2.4 覆盖升级、SQLite 数据保留、快捷方式和卸载。
