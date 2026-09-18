# AegisCopilot 2.9

Local-first command RAG for developers on Windows.

AegisCopilot turns a natural-language operation into grounded, copyable commands. It retrieves local knowledge, preserves parent/child document context, cites the exact evidence range, and asks for missing command parameters one at a time instead of inventing paths, ports, shells, or credentials.

> AegisCopilot never executes commands automatically or remotely. After an explicit confirmation, a direct command may be written to a local PowerShell/CMD terminal; templates and clarification answers remain copy-only.

中文版本：[README.md](README.md)

## Highlights

- **Direct command planning**: complete facts produce one command block without placeholders; missing facts become one clarification question at a time.
- **Safe fallback**: skipped, refused, or invalid input falls back to one clearly labelled template instead of asking indefinitely.
- **Shell-aware rendering**: PowerShell, CMD, and POSIX variants use dedicated quoting rules. Windows paths with spaces, Unicode, UNC paths, and trailing separators are covered by tests.
- **Curated recipes**: provenance-backed recipes cover Python virtual environments, Git revert, Docker run, Windows file/port operations, MySQL, and Redis.
- **Windows developer knowledge**: 33 PowerShell/CMD topics cover files, processes, services, networking, archives, hashes, HTTP, environment variables, winget/Chocolatey/Scoop, and robocopy warnings.
- **Parent/child retrieval**: small child chunks drive retrieval while bounded parent chunks preserve context for generation; citations point to the exact child/parent locator.
- **Personalized context**: the current question takes precedence over conversation, knowledge-base and global preferences. At most four redacted memories are used per turn, only to enrich retrieval and presentation—not as command, fact, or risk evidence.
- **Multiple knowledge bases and uploads**: create and bind multiple knowledge bases; upload TXT, Markdown, PDF, and DOCX files up to 20 MB each.
- **Offline-first operation**: BM25 and local FastEmbed retrieval work without a cloud key. DeepSeek is optional and may only explain already grounded evidence.
- **Desktop safety**: API keys are protected by Electron `safeStorage` / Windows DPAPI and are never exposed to the renderer or SQLite.
- **Embedded terminal**: only PowerShell and CMD are allowed, with at most four temporary PTY sessions. Before execution the UI shows the shell, working directory, full command, risk and warnings; high-risk commands also require typing `确认执行`. Terminal output, input history and working directories are never stored in the app database.

## Quick manual test

1. Create a new conversation and select the developer IT knowledge base.
2. Ask `Python 怎么创建并激活虚拟环境？`.
3. Provide `C:\Users\CZX\Documents\AegisCopilot 手动测试` when asked for the target directory.
4. Select **PowerShell**, then select **.venv**.
5. The final answer should contain one copyable PowerShell block, no `<...>` placeholders, and expandable citations. It should resemble:

```powershell
python -m venv 'C:\Users\CZX\Documents\AegisCopilot 手动测试\.venv'
& 'C:\Users\CZX\Documents\AegisCopilot 手动测试\.venv\Scripts\Activate.ps1'
```

Do not execute a generated command during the clarification UI test. To verify the terminal, manually use a harmless command only after the confirmation dialog; never use an assistant-generated high-risk command for testing.

## Architecture

| Layer | Technology |
|---|---|
| Desktop | Electron 39, custom protocol, electron-builder / NSIS |
| Frontend | React 18, Vite 5, Lucide React |
| Backend | Python 3.11+, FastAPI, Pydantic, SQLite, SSE |
| Retrieval | rank-bm25, FastEmbed `BAAI/bge-small-zh-v1.5`, ONNX Runtime, RRF |
| Generation | Optional DeepSeek OpenAI-compatible API |
| Packaging | PyInstaller onedir, electron-builder Windows x64 |

The command planner persists resumable plans in SQLite schema v5. Plans support restart recovery, cancellation/restart, optimistic locking, source revisions and citation IDs, and expire after 24 hours by default. A knowledge-base rebuild invalidates plans that depend on the old evidence. Credentials are deliberately excluded from plan values and model input. The catalog currently contains 21 provenance-backed high-value recipes.

## Development setup

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

The first source checkout may download the BGE ONNX model to `models/cache/`. The Windows package contains the pinned local model and does not need to download it on first launch.

## Tests and packaging

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

The v2.9.0 release gate passed:

- Backend: 138 tests passed.
- Frontend: 38 tests passed; Vite production build passed.
- Desktop: 19 tests passed, including PTY allowlisting, session limits, cleanup and preload IPC boundaries.
- Packaged backend smoke test covers profile/memory CRUD, hybrid retrieval, offline SSE and the one-slot clarification flow.

The Windows x64 installer is generated at `desktop/dist/AegisCopilot Setup 2.9.0.exe` and is intended to be distributed as a GitHub Release asset rather than committed to source history. Its SHA-256, build commit and complete gate record are documented in [docs/release-2.9.0.md](docs/release-2.9.0.md).

## Installation and release boundaries

- Target platform: Windows 10/11 x64. The installer includes the local model and does not download it on first launch.
- Installed data: `%APPDATA%\AegisCopilot\storage`; logs: `%APPDATA%\AegisCopilot\logs`.
- The installer is unsigned, so Windows SmartScreen may show an unknown publisher. There is no auto-update channel and no macOS/Linux installer yet.
- The GitHub Release installer is intended for testing and personal use. Configure trusted code signing and verify install, upgrade, and uninstall on a clean Windows host before wider distribution.

## Knowledge and reindexing

Uploaded documents are extracted, split into parent/child chunks, embedded and indexed locally. Documents created before the parent/child index rules were enabled should be rebuilt or reindexed from Knowledge Base Management. A rebuild changes the index revision and safely expires pending command plans that depend on the old evidence.

## Security boundaries

- No automatic or remote command execution.
- Direct commands are written to a local PowerShell/CMD PTY only after explicit confirmation. Templates, clarification cards and legacy answers have no execute button.
- At most four temporary terminal sessions are allowed; sessions are cleaned up on exit and terminal output, input history and working directories are not persisted.
- No credentials, API keys, tokens, private keys, or connection strings in SQLite, logs, long-term memory, or model prompts.
- Deterministic rules label/warn about high-risk commands. Direct plans do not return unresolved placeholders; template fallbacks may retain placeholders. The application is not a sandbox and never executes commands on the user's behalf.
- API keys are encrypted at rest through Electron `safeStorage` / Windows DPAPI.

## Documentation

- [Chinese README](README.md)
- [Project rules](AGENTS.md)
- [v2.9.0 release verification](docs/release-2.9.0.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Development, testing and packaging](docs/DEVELOPMENT.md)
- [RAG evaluation](docs/RAG_EVALUATION.md)
- [Security boundaries](docs/SECURITY.md)
- [Third-party notices](knowledge/THIRD_PARTY_NOTICES.md)

## License

Project code is released under the MIT License. Adapted tldr knowledge is provided under CC BY 4.0; source URLs, revisions, licenses and hashes are recorded in `knowledge/manifest.json` and `knowledge/sources.lock.json`.
