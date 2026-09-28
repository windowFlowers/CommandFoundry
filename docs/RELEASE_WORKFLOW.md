# CommandFoundry 版本更新与评测发布流程

本项目以后每个版本都按同一条顺序推进：

```text
代码冻结并提交
  → 离线数据/代码/前端/桌面测试
  → 离线 RAG 评测与基线门禁
  → 当前 provider 的真实模型评测与基线门禁
  → Windows 桌面验收
  → 用户确认后构建安装包
  → SHA-256、报告和发布资产
```

安装包不是普通开发测试的产物。只有前面的 job 全部成功，且完成桌面端人工验收后，才允许构建安装包。

## 为什么普通 CI 不自动调用真实模型

普通 PR/main CI 故意不读取 API Key，避免把凭证带入共享 runner、日志或测试报告。它只执行可重复的离线检查。真实模型评测由 `.github/workflows/evaluation-release.yml` 的手动 `workflow_dispatch` 触发，并从受保护的 GitHub Environment `release-candidate` 读取：

```text
AEGIS_LLM_PROVIDER
AEGIS_LLM_MODEL
AEGIS_LLM_BASE_URL
AEGIS_LLM_API_KEY
```

因此之前没有自动完成真实模型评测，不是离线测试失败，而是当前环境没有被授权把 API Key 注入模型评测步骤。Key 应配置为 GitHub Environment Secret，不能提交到仓库或写入评测报告。DeepSeek、OpenAI 兼容接口和其他已支持的 provider 都沿用应用当前配置；评测脚本不会另起一套生成逻辑，而是调用 `/chat/stream` 使用的后端链路。

本地需要做真实模型评测时，先在当前 PowerShell 会话中设置环境变量，再运行：

```powershell
$env:AEGIS_LLM_PROVIDER = "deepseek"
$env:AEGIS_LLM_MODEL = "deepseek-chat"
$env:AEGIS_LLM_BASE_URL = "https://api.deepseek.com/v1"
$env:AEGIS_LLM_API_KEY = "<只在本地会话中设置，不要写入文件或提交>"
backend\.venv\Scripts\python.exe scripts\run_evaluation.py `
  --suite all --mode model --provider current `
  --hybrid --allow-model-download `
  --baseline evaluation\baselines\v2.13-model.json `
  --output .runtime\model-release-evaluation `
  --redact --fail-on-gate
```

运行完成后应立即清理当前会话中的变量：

```powershell
Remove-Item Env:AEGIS_LLM_API_KEY -ErrorAction SilentlyContinue
```

## 基线管理

评测报告必须和代码、知识库 revision 以及 provider/model profile 对齐。`.runtime/` 是本地临时目录，已被 `.gitignore` 忽略，不能作为 GitHub Actions 的基线路径。批准一个版本后，应把脱敏的基线报告保存到仓库中的明确路径，例如：

```text
evaluation/baselines/v2.13-offline.json
evaluation/baselines/v2.13-model.json
```

基线报告必须由以下命令生成并经过人工检查：

```powershell
backend\.venv\Scripts\python.exe scripts\run_evaluation.py `
  --suite all --mode offline --output .runtime\offline-baseline `
  --redact --fail-on-gate
```

真实模型基线同样必须使用 `--redact`。报告中不得包含 API Key、Authorization Header、密码、Token、私钥、完整敏感路径、完整提示词或终端输出。若 provider、model、base URL、prompt、embedding、reranker、chunk 或 retrieval 参数变化，应建立新的 profile 基线，不能静默覆盖旧基线。

## GitHub Actions 使用方式

### 日常开发和 Pull Request

`.github/workflows/ci.yml` 不使用任何 API Key，固定执行：

- 仓库和评测数据校验；
- backend 测试；
- frontend 测试与 Vite 构建；
- desktop 测试；
- `suite=all` 离线评测和硬门禁。

### 发布候选

1. 在目标发布 commit 上触发 `Release candidate pipeline`。
2. 填写与当前 profile 对应的 `offline_baseline` 和 `model_baseline` 仓库相对路径。第一次建立基线时，必须显式勾选 `allow_new_baseline`，且首个报告仍必须是完整、非空、无硬门禁错误的结果。
3. 离线 job 通过后，model job 才会启动。它从 `release-candidate` Environment 读取密钥，并使用 `mode=model` 执行全套用例。
4. model job 通过后，Windows job 才执行 frontend/desktop 测试和 PyInstaller 后端构建。
5. 需要安装包时才勾选 `build_installer`。该 job 使用 `desktop-release-approval` Environment；仓库管理员应为该 Environment 配置 required reviewer。审核人先在真实 Windows 桌面完成 Computer Use 验收，再批准该 job。
6. 最后一步只上传 Windows x64 安装包和 `SHA256SUMS.txt`。`.exe` 不提交到源码历史。

当前 workflow 不会通过 UI 自动执行助手生成的命令；桌面端人工验收也不能用脚本烟测冒充。

## 门禁失败时的处理

- 离线评测失败：先查看 `bad-cases.jsonl`，修复代码/知识/评测数据后重新提交，不生成安装包。
- 真实模型评测失败：确认 provider 配置、模型可用性、超时/限流和 baseline profile；不要把失败报告强行设为新基线。
- profile 不一致：建立带新 profile 的基线并记录变更原因，不能与旧 profile 直接比较。
- citation、占位符、凭证泄露、Prompt Injection 或高风险命令门禁失败：无论总体质量如何都必须阻断发布。
- 桌面验收失败：保留评测产物，修复后从新 commit 重新跑完整链路。

完整评测脚本、报告格式和门禁说明见 [RAG_EVALUATION.md](RAG_EVALUATION.md)。
