# CommandFoundry

CommandFoundry is a local-first command RAG desktop application for developers, currently focused on Windows.

## What it does

- Turns natural-language operations into source-grounded, copyable commands; asks for required details such as paths, shells, and ports when they are missing.
- Searches local knowledge with parent/child chunk retrieval and precise citations. It supports TXT, Markdown, PDF, and DOCX uploads, as well as multiple knowledge bases.
- Covers common PowerShell, CMD, and POSIX developer operations, including Python, Git, Docker, and Windows file, process, service, and networking topics.
- Uses the official OpenAI SDKs to connect to DeepSeek, OpenAI, Qwen, Moonshot, SiliconFlow, Ollama, and other OpenAI-compatible APIs.
- Provides local retrieval, personalized context, resumable command-parameter planning, and a side terminal. Local commands are run only after explicit user confirmation.

## Troubleshooting agent (experimental)

`backend/app/troubleshoot` is a developer troubleshooting agent. It uses tools to find and fix service faults, write actions go through a host policy and approval, and host probes decide whether a fix worked. It is not part of the desktop app yet; it runs in an isolated sandbox through `scripts/run_memory_bench.py`.

- Long-term memory: the host decides what is written, and only runs it verified add project facts and fixes. Memory only informs the model and cannot relax the safety policy. [Mem0](https://github.com/mem0ai/mem0) can store and retrieve the records (`pip install -e ".[memory]"`), but no model extracts memories from a run: in the comparison, extraction stored the contents of secret files.
- Trajectory distillation: Qwen3.5-4B is LoRA-tuned on troubleshooting trajectories from a teacher model.

Task success rates below use apps and fault types absent from training, 100 sessions per cell, temperature 0.7, skills off:

| Model | No memory | With memory |
| --- | --- | --- |
| Qwen3.5-4B base | 75% | 92% |
| Qwen3.5-4B distilled | 91% | 92% |
| Qwen3.5-9B base (FP8) | 96% | 93% |

The scenarios are built by this project and cover 2 apps; repeating one configuration can differ by about 6 points.

The memory gain above does not generalize. A later run on 2 new apps, 240 sessions per row, Qwen3.5-4B base:

| Memory | Success | Tool calls per session | Secrets stored |
| --- | --- | --- | --- |
| None | 85.0% | 12.6 | - |
| Own (SQLite, lexical match) | 83.3% | 10.5 | None |
| Mem0 retrieval, host-written records | 87.5% | 10.3 | None |

Mem0 retrieval beats the own version by 4.2 points (95% interval 0.4 to 7.9) and no memory by 2.5 points (-3.3 to 7.5, not significant). The established benefit so far is 18% fewer tool calls. There is also a known side effect: listed project facts make small models overlook what is not listed, and launcher-script faults drop from 28/40 to 18/40.

Methods, the full comparison and known issues are in [AGENT_EVOLUTION.md](docs/AGENT_EVOLUTION.md) (Chinese).

## Technology stack

| Layer | Technology |
| --- | --- |
| Desktop | Electron, electron-builder / NSIS, Node.js |
| Frontend | React, Vite, Lucide React |
| Backend | Python, FastAPI, Pydantic, SQLite, SSE |
| Retrieval | BM25, FastEmbed, ONNX Runtime, RRF |
| Model APIs | Official OpenAI Python/Node SDKs, OpenAI-compatible APIs |
| Packaging | PyInstaller, Windows x64 NSIS |

## Screenshots

### Command workspace

![Command workspace](docs/screenshots/answer-1080x720.png)

### Knowledge-base management

![Knowledge-base management](docs/screenshots/knowledge-1440x920.png)

### Personalized context

![Personalized context](docs/screenshots/personalization-1440x920.png)

中文版本：[README.md](README.md)
