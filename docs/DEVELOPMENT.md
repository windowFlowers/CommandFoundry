# 开发、测试与打包

## 环境

- Windows 10/11 x64
- Python 3.11 或 3.12
- Node.js 20+
- 可选：DeepSeek API Key

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

测试覆盖单轮/多轮检索评测、上下文预算与隔离、摘要恢复和并发、来源追溯、模型失败回退、危险命令、SSE、会话 CRUD、密钥密文、preload 边界、进程路径和自定义协议。

需要复现 BM25 与 BGE + RRF 两组完整指标时运行：

```powershell
.\backend\.venv\Scripts\python.exe scripts\evaluate_rag.py
```

输出同时包含 300 条单轮集合与 60 组多轮集合的 BM25、混合检索 Top-1/Top-3 指标。

## Windows 构建

确认 `models/cache/` 已有离线 BGE 模型后运行：

```powershell
cd desktop
npm run dist:dir
npm run dist:win
```

流水线依次执行：ImageGen PNG 转多尺寸 ICO、Vite 生产构建、PyInstaller onedir、electron-builder NSIS x64。最终安装包在 `desktop/dist/`。
