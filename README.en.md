# AegisCopilot 2.5

Local-first command RAG for developers on Windows.

AegisCopilot turns a natural-language operation into grounded, copyable commands. It retrieves local knowledge, preserves parent/child document context, cites the exact evidence range, and asks for missing command parameters one at a time instead of inventing paths, ports, shells, or credentials.

> AegisCopilot only displays commands for copying. It never executes commands, opens a terminal, or remotely operates the operating system.

## Highlights

- **Direct command planning**: complete facts produce one command block without placeholders; missing facts become one clarification question at a time.
- **Safe fallback**: skipped, refused, or invalid input falls back to one clearly labelled template instead of asking indefinitely.
- **Shell-aware rendering**: PowerShell, CMD, and POSIX variants use dedicated quoting rules. Windows paths with spaces, Unicode, UNC paths, and trailing separators are covered by tests.
- **Curated recipes**: provenance-backed recipes cover Python virtual environments, Git revert, Docker run, Windows file/port operations, MySQL, and Redis.
- **Parent/child retrieval**: small child chunks drive retrieval while bounded parent chunks preserve context for generation; citations point to the exact child/parent locator.
- **Personalized context**: explicit user preferences can be scoped globally or to a knowledge base. Sensitive values, credentials, private keys, and connection strings are rejected before persistence or model calls.
- **Offline-first operation**: BM25 and local FastEmbed retrieval work without a cloud key. DeepSeek is optional and may only explain already grounded evidence.
- **Desktop safety**: API keys are protected by Electron `safeStorage` / Windows DPAPI and are never exposed to the renderer or SQLite.

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

Do not execute the generated command during a UI test.

## Architecture

| Layer | Technology |
|---|---|
| Desktop | Electron 39, custom protocol, electron-builder / NSIS |
| Frontend | React 18, Vite 5, Lucide React |
| Backend | Python 3.11+, FastAPI, Pydantic, SQLite, SSE |
| Retrieval | rank-bm25, FastEmbed `BAAI/bge-small-zh-v1.5`, ONNX Runtime, RRF |
| Generation | Optional DeepSeek OpenAI-compatible API |
| Packaging | PyInstaller onedir, electron-builder Windows x64 |

The command planner persists resumable plans in SQLite schema v5. Plans include a source revision and citation IDs, expire after 24 hours by default, and are invalidated when the knowledge base is rebuilt. Credentials are deliberately excluded from plan values and model input.

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
npm.cmd run dist:win
```

The v2.5.0 release gate passed:

- Backend: 130 tests passed.
- Command planner: 18 tests passed; recipe catalog: 4 tests passed.
- Frontend: 34 tests passed; Vite production build passed.
- Desktop: 16 tests passed.
- Packaged backend smoke test passed, including profile/memory CRUD, hybrid retrieval, offline SSE, and the four-step clarification flow.

The Windows x64 installer is generated at `desktop/dist/AegisCopilot Setup 2.5.0.exe` and is intended to be distributed as a GitHub Release asset rather than committed to source history.

## Knowledge and reindexing

Uploaded documents are extracted, split into parent/child chunks, embedded and indexed locally. Documents created before the parent/child index rules were enabled should be rebuilt or reindexed from Knowledge Base Management. A rebuild changes the index revision and safely expires pending command plans that depend on the old evidence.

## Security boundaries

- No automatic command execution.
- No terminal automation by the desktop app.
- No credentials, API keys, tokens, private keys, or connection strings in SQLite, logs, long-term memory, or model prompts.
- Risk review rejects unsafe commands and any unresolved placeholders before a direct command is returned.
- API keys are encrypted at rest through Electron `safeStorage` / Windows DPAPI.

## Documentation

- [Chinese README](README.md)
- [Project rules](AGENTS.md)
- [Release verification](docs/release-2.5.0.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Development, testing and packaging](docs/DEVELOPMENT.md)
- [RAG evaluation](docs/RAG_EVALUATION.md)
- [Security boundaries](docs/SECURITY.md)
- [Third-party notices](knowledge/THIRD_PARTY_NOTICES.md)

## License

Project code is released under the MIT License. Adapted tldr knowledge is provided under CC BY 4.0; source URLs, revisions, licenses and hashes are recorded in `knowledge/manifest.json` and `knowledge/sources.lock.json`.
