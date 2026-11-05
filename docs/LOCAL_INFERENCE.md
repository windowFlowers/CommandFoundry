# 私有化部署：Qwen3.5-4B 替换云端 API

本文记录把生成模型从 DeepSeek 云端 API 换成自部署 Qwen3.5-4B 的方案、评测与压测结果。RAG 检索、引用校验和安全门禁保持不变，只替换 OpenAI-compatible 生成端点。

## 部署架构

```text
CommandFoundry（Windows）──SSH 隧道──▶ GPU 服务器（离线，4× RTX 5090 32GB）
                                     ├─ llama.cpp llama-server  Qwen3.5-4B Q4_K_M GGUF  127.0.0.1:18080  GPU 1
                                     └─ vLLM 0.32.0             Qwen3.5-4B BF16         127.0.0.1:18000  GPU 0
```

- 两个引擎都只监听服务器本机地址，客户端通过 `ssh -N -L <本地端口>:127.0.0.1:<端口> <服务器>` 访问，不对局域网暴露未鉴权端口。
- 服务器无外网：vLLM 及 198 个依赖在 Windows 上用 `uv pip compile --python-platform x86_64-manylinux_2_28` 按 Linux 解析，再 `pip download --no-deps` 下载 wheel，离线安装到独立 venv；模型权重从 ModelScope 下载后传输并按 SHA-256 校验。
- vLLM 启动参数：`--max-model-len 16384 --gpu-memory-utilization 0.6 --limit-mm-per-prompt '{"image":0,"video":0}'`（RAG 只用文本，关闭视觉输入以节省显存）。

## 接入方式

新增 `llamacpp`、`vllm` 两个 provider（后端 `generation.py`、桌面端预设、前端选项）：

- 无需 API Key（`KEYLESS_PROVIDERS`）。
- Qwen3.5 默认开启思考模式，会把 token 预算耗在推理上导致 JSON 为空，因此对这两个 provider 自动发送 `chat_template_kwargs: {"enable_thinking": false}`（`THINKING_TOGGLE_PROVIDERS`）。

```powershell
$env:AEGIS_LLM_PROVIDER = "vllm"            # 或 llamacpp
$env:AEGIS_LLM_BASE_URL = "http://127.0.0.1:18000/v1"
$env:AEGIS_LLM_MODEL = "qwen3.5-4b"
$env:AEGIS_LLM_TIMEOUT_SECONDS = "120"
```

## 质量评测（662 条用例，602 条参与评分，`--suite all --mode model --hybrid`）

| 指标 | DeepSeek-chat（云端） | Qwen3.5-4B llama.cpp Q4 | Qwen3.5-4B vLLM BF16 |
| --- | --- | --- | --- |
| 通过率 | 100%（602/602） | 95.2%（573/602） | 94.0%（566/602） |
| 检索 MRR | 0.977 | 0.985 | 0.985 |
| 忠实度 / 有效引用率 / 安全通过率 / 拒答准确率 | 100% | 100% | 100% |
| 模型调用成功 / 尝试 | — | 539 / 572 | 529 / 572 |
| 回退原因：invalid_json / ungrounded | — | 18 / 9 | 26 / 10 |
| 平均 / p95 延迟（串行） | 2.4s / 4.9s | 2.8s / 5.5s | 4.8s / 10.1s* |

\* 在空闲 GPU 上重测结果相同（94.2%，平均 4.5s），延迟差异并非来自 GPU 共用。评测是串行的单请求解码，受显存带宽限制：BF16 每生成一个 token 要读约 8GB 权重，Q4 约 2.7GB，因此单请求时 llama.cpp Q4 解码更快（本次评测约 187 vs 130 输出 tok/s）；vLLM 的优势在并发场景，见下方压测。

结论：

- 失败几乎全部来自小模型输出不满足结构化契约（JSON 解析/校验失败、引用不在证据中），系统按设计回退到本地确定性答案，**没有任何安全、引用或注入门禁失败**。
- Q4 量化与 BF16 的通过率差异在随机波动范围内（两次运行共同失败 21 例，各自独有 8 / 15 例），4-bit 量化对该任务没有可观测的质量损失。
- `no_api_key` 回退（4 例）是模板请求、命令规划和敏感输入等**有意不调用模型**的确定性路径，与 provider 无关。

## 并发压测（`scripts/benchmark_serving.py`，在服务器本机运行）

请求形态：系统提示 + 约 1140 token 证据 + 问题，`max_tokens=256`，`temperature=0`，关闭思考；每档请求数 `max(8, 4×并发)`。

| 并发 | llama.cpp 请求/s | vLLM 请求/s | llama.cpp 输出 tok/s | vLLM 输出 tok/s | llama.cpp p95 延迟 | vLLM p95 延迟 | llama.cpp p95 TTFT | vLLM p95 TTFT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.64 | 1.15 | 105 | 148 | 2.1s | 1.3s | 0.47s | 0.08s |
| 4 | 0.70 | 2.99 | 109 | 382 | 6.5s | 2.1s | 6.1s | 0.35s |
| 8 | 0.68 | 4.65 | 104 | 635 | 12.9s | 2.3s | 12.4s | 0.43s |
| 16 | 0.67 | 7.20 | 104 | 960 | 25.8s | 3.3s | 24.8s | 0.85s |

- llama.cpp 以 `--parallel 1` 运行，请求排队，吞吐不随并发增长，延迟随并发线性上升。
- vLLM 依靠连续批处理与 PagedAttention，16 并发下吞吐为 llama.cpp 的 10.8 倍，p95 延迟约为其 1/8。
- 压测时 llama.cpp 所在 GPU 1 上有其他任务（约 46% 利用率），其数据可能偏低。

选型建议：单用户本地/桌面场景用 llama.cpp Q4（显存约 3.4GB）；多用户服务端场景用 vLLM。

## 复现

```bash
# 服务器
/data_disk/czx/qwen35_4b_vllm/start.sh      # 启动 vLLM（stop.sh 停止）
python3 benchmark_serving.py --base-url http://127.0.0.1:18000/v1 --model qwen3.5-4b --label vllm --output vllm.json
```

```powershell
# Windows：先开隧道 ssh -N -L 18000:127.0.0.1:18000 <服务器>，再按“接入方式”设置环境变量
backend\.venv\Scripts\python.exe scripts\run_evaluation.py --suite all --mode model --provider current `
  --hybrid --output .runtime\qwen35-4b-vllm\evaluation-report.json --redact
```

## LoRA 蒸馏（DeepSeek-V4.1-Flash → Qwen3.5-4B）

1. **出题**：本地 Qwen 按知识库主题生成 3715 道问题，用 bge-small-zh 与评测集问题及主题别名去重（余弦 ≥ 0.85 剔除），保留 3702 道（`scripts/distill/build_questions.py`）。
2. **收集老师回答**：在进程内运行真实的 `/chat/stream` 链路（同样的检索、提示词与校验），把生成器换成 DeepSeek-V4.1-Flash 并记录每次调用（`scripts/distill/collect_teacher.py`）。只保留应用判定为模型回答（`mode == "model"`）的样本，共 2782 条。
3. **构建 SFT 集**：输入为应用实际发送的 system/user 提示词，目标为老师的最终 JSON（不含推理过程），按主题划出 4% 作验证集（`scripts/distill/build_sft.py`，2670 / 112）。
4. **训练**：LoRA r=16、α=32，覆盖语言模型的全注意力、线性注意力（Gated DeltaNet）与 MLP 投影，3047 万可训练参数；只对回答部分计损失，2 epoch，单张 RTX 5090，62 分钟，显存峰值 13.9 GB（`scripts/distill/train_lora.py`）。验证集损失 0.653 → 0.383（epoch 1）→ 0.381（epoch 2）。
5. **部署**：vLLM `--enable-lora --lora-modules qwen3.5-4b-distill=<adapter>`，基座与 LoRA 版本由同一服务按模型名切换。

同一 vLLM 配置（`--enable-lora`）、空闲的 RTX 5090、串行评测；基座与 LoRA 版本由同一服务按模型名切换。LoRA 版本独立运行两次以估计波动。

| 指标 | 基座 | LoRA 第 1 次 | LoRA 第 2 次 |
|---|---|---|---|
| 通过率 | 94.2%（567/602） | **98.3%（592/602）** | **97.5%（587/602）** |
| `invalid_json` 失败 | 26 | 8 | 12 |
| 回退率 | 6.0% | 2.7% | 3.3% |
| 忠实度 / 安全通过率 / 拒答准确率 | 100% | 100% | 100% |
| 每例输出 token（模型回答） | 628 | 405 | 409 |
| 平均 / p95 延迟 | 4.5 s / 10.3 s | 3.0 s / 5.8 s | 3.5 s / 6.5 s |

- 平均通过率 94.2% → 97.9%（+3.7 个百分点），失败用例 35 → 12.5（−64%）；基座的 35 个失败中有 31 个在两次 LoRA 运行中都通过。
- 同一模型两次运行相差约 1 个百分点（基座两次 94.0% / 94.2%，LoRA 两次 98.3% / 97.5%），提升幅度明显大于波动。
- 学生模型学会了老师紧凑的 JSON 风格，每例输出 token 减少约 35%，延迟下降主要来自输出变短（LoRA 本身会让单请求解码慢约 20%）。
- 训练问题与评测问题覆盖同一知识库（这正是领域蒸馏的用法），但已按语义相似度去重，不含评测原题。
- 以上为蒸馏时的 `json_object` 结果；加入提示词与 schema 约束后的对比见下一节。

## 结构化输出（提示词约束 + JSON Schema 约束解码）

**根因诊断**：在评测中记录每次回退的异常与模型原始输出（基座模型 35 次回退），发现被统计为 `invalid_json` 的失败**全部是合法 JSON**，原因均为 `commands` 超过 `ModelAnswerDraft` 的 6 条上限——模型在 Docker、SSH、Maven、kubectl 等命令密集的主题上把证据中的命令全部列出（7–16 条）。该上限只存在于 Pydantic 校验中，提示词和 `json_object` 都没有约束它。

**修复（两层）**：

1. **提示词层**：`SYSTEM_PROMPT` 增加一行"commands 最多 6 条；候选命令较多时只保留与当前问题最相关的几条"。只写命令上限：把 notes / segments / summary 长度上限也写进提示词后，模型反而写得更长（每例输出 938 token），未转义引号导致的 JSON 截断随之增多（通过率 99.2%）。
2. **解码层**：对 `vllm` / `llamacpp` provider，`response_format` 改为从 `ModelAnswerDraft` 生成的 JSON Schema（`SCHEMA_DECODING_PROVIDERS`），由 vLLM（xgrammar）与 llama.cpp（GBNF）在解码时强制字段、枚举与数量上限；输出上限 2048 token（`SCHEMA_ANSWER_MAX_TOKENS`）。云端 provider 保持 `json_object`。

**Schema 的两个坑**（第一版 schema 评测结果因此作废）：

- **字段顺序**：语法引擎按 schema 中的属性顺序生成，而 Pydantic 生成的顺序（summary、commands、notes、summary_segments）与提示词要求的顺序（summary、summary_segments、commands、notes）不一致。只有 `summary` 必填时，模型按提示词先写 `summary_segments`，之后语法不允许再回到 `commands`，于是直接结束对象——**回答中带命令的比例为 0%**，通过率却显示 99.7%、输出 token "减少 60%"（减少的正是缺失的命令）。全部字段改为必填后，模型写完 `notes` 想结束而语法要求 `summary_segments`，只能不断输出空白直到上限（4.3% 用例失败）。修复：schema 属性顺序与提示词一致（`ANSWER_FIELD_ORDER`），全部字段必填（允许空数组），并有测试保证两者一致。
- **空白死循环**：模型在字符串中写出未转义的 `"`（如 `tasklist /FI "`）会提前闭合字符串，此后语法只允许空白或分隔符，模型持续输出空白直到上限。2048 token 上限把这类失败从约 100 s 缩短到 20 s 以内。vLLM 的 `disable_any_whitespace` 能消除空白，但会明显扭曲输出（摘要变成英文、`summary_segments` 被无意义短语填满、不输出命令），已弃用。

**结果**（同一评测集，602 例计分，串行；"含命令"= 证据中有命令的回答里至少给出 1 条命令的比例）：

| 配置 | 通过率 | 失败 | 含命令 | 中文摘要 | 每例输出 token | 平均 / p95 延迟 |
|---|---|---|---|---|---|---|
| vLLM 基座 · 原始（json_object，无上限提示） | 94.2% | 35 | — | — | 628 | 4.5 s / 10.3 s |
| vLLM 基座 · 提示词 + json_object | 99.8% | 1 | 94.0% | 99.5% | 655 | — ¹ |
| vLLM 基座 · 提示词 + schema（第一版，作废） | 99.7% | 2 | **0%** | 99.5% | 239 | 2.3 s / 4.7 s ¹ |
| vLLM 基座 · **提示词 + schema（修复后）** | **99.8%** | 1 | 94.2% | 99.5% | 656 | 4.7 s / 8.8 s |
| vLLM + LoRA · 提示词 + schema（修复后） | 99.7% | 2 | 92.5% | 96.5% | 412 | 3.7 s / 7.2 s ² |
| llama.cpp Q4 · 提示词 + schema（修复后） | 99.8% | 1 | 93.5% | 99.1% | 596 | 3.3 s / 6.4 s ² |

¹ 三组评测同时压在同一 vLLM 上，延迟不可比。² LoRA 与 llama.cpp 两组同时运行（不同 GPU）。

- 通过率 94.2% → 99.8%，主要来自提示词层：单靠提示词 + `json_object` 已达 99.8%。schema 在本评测集上没有额外的通过率提升，它的价值是把"最多 6 条命令"、字段与枚举从"模型大概率遵守"变成解码时的硬保证，并给最坏情况设了上限。
- 修复后输出长度与原始配置相当（656 vs 628 token）：早先"约束解码减少 60% token、延迟下降 60%"的结论来自缺失命令的回答，已撤回。
- 在两层约束下，LoRA 蒸馏的通过率不再领先（99.7% vs 99.8%）；剩余收益是输出短约 37%、平均延迟低约 20%，代价是中文摘要比例略低（96.5% vs 99.5%）、给出的命令略少（2.5 vs 2.9 条）。回答内容质量还需通过/失败以外的评测（如 LLM 评审）衡量。
- 教训：通过/失败评测不检查"回答里有没有命令"，token 大幅下降应先核对内容是否缺失。评测脚本（`.runtime/eval_variant.py` 的 `EVAL_ANSWER_LOG`）现记录每个回答的命令数、段落数与中文比例。
## 知识库修复与扩充

`scripts/sync_knowledge.py` 生成全部知识文件，`backend/tests/test_knowledge_lint.py` 防止下列错误回归。

**修复**：

- 23 个主题没有真实命令：tldr 的别名页只有一句 `tldr docker container run` 式指引，`docker run`、`docker ps`、`docker logs`、`docker exec` 等都在其中。现在会跟随别名解析到实际页面。
- 风险分级原来按子串匹配，漏掉了长选项和多数破坏性命令，改为按命令结构匹配后 `kubectl delete`、`terraform destroy`、`git stash clear`、`rsync --delete`、`find -delete`、Redis `DEL` 等都标为高风险。
- 清除 tldr 语法残留：`\{\{` 转义、`<[ls|list]>` 备选写法；恢复被改坏的 Go 模板（`docker ... --format '{{.Name}}'`）。
- 按 tldr 页面目录修正平台（`ip`、`ss`、`ufw`、`strace` 等不再标 Windows），合并 9 组重复主题。
- 修正种子命令：Redis 订阅与发布拆成两个会话，Redis 命令不再标为 bash，`Copy-Item` 复制目录补 `-Recurse`，`sc.exe stop && start` 的竞态改用 `net stop && net start`，`taskkill /PID` 不再写“强制”。
- 约一半命令说明改用 tldr `pages.zh` 的中文（只在命令与英文页一致时采用，翻译滞后的页面保留英文）。

**扩充**：主题 300 → 514，命令 1436 → 约 2600，领域 14 → 20（新增 Go、Rust、C/C++、云平台、SQLite/MongoDB、音视频与文档处理）。选题依据是常见开发和运维任务，不是真实使用统计。

**覆盖率**（`scripts/evaluate_coverage.py`，91 条不与主题别名重复的日常问题，混合检索）：

| 问题类型 | 扩充前 Top-4 | 扩充后 Top-4 |
|---|---|---|
| 原有主题（20 条） | 80.0% | 90.0% |
| 新增主题（71 条） | 1.4% | 88.7% |
| 合计 | 18.7% | **89.0%** |

这组问题与扩充内容出自同一作者，只能说明大致改善；未命中的 10 条没有针对性调整别名，其中“CentOS 上安装 git”被路由到 Git 领域，属于检索路由问题。

**模型评测**（提示词 + schema；514 主题对应 900 例计分，基座与 LoRA 两组同时运行，延迟偏高）：

| 配置 | 通过率 | 失败 | 含命令 | 中文摘要 | 每例输出 token | 平均 / p95 延迟 |
|---|---|---|---|---|---|---|
| vLLM 基座 | 98.9% | 10 | 95.5% | 99.2% | 636 | 6.0 s / 11.3 s |
| vLLM + LoRA | 99.2% | 7 | 94.7% | 97.3% | 393 | 4.2 s / 7.8 s |

- 通过率低于 300 主题时的 99.8%，但评测集不同（900 例 vs 602 例），不能直接比较。新增主题带来的相近候选也确实让检索更难。
- 扩充后第一轮评测为 97.6%（22 例失败），暴露出两个实际问题并已修复：Windows 命令模板按关键词抢答（“防火墙端口”触发 netstat 模板），以及追问时被泛化的命令说明带偏（上一轮问 curl，追问查到 xh）。
- 基座 10 例失败中 6 例是 `invalid_json`，其中 3 例是 `tasklist /FI "` 这类引号问题（根因和修复见下一节）。`删除文件` 未指定平台时回答可能引用 Windows 主题，追问随之沿用该主题，而评测标注为 `linux.rm`。
- 知识库扩充后不需要重新蒸馏：知识来自检索，LoRA 学到的是输出格式和风格。两者通过率差 3 例，在波动范围内，LoRA 输出 token 仍少约 38%。
## 知识库审核与引号问题

**外部审核**：另一个 agent 审查 `topics.json`，列出 22 组问题和 6 组待核查项。逐条核对后全部属实，修复写入 `sync_knowledge.py` 的 `COMMAND_CORRECTIONS`（更正匹配不到原文时构建直接失败）。主要包括：`--dry-run=none` 会真实执行、`helm repo add` 缺 URL、`nc -e` 风险过低、`awk` 把 GID 当 UID、已移除的 `next export` / `pipenv --three` / `jhat`，以及错误展开的 `zip --split-size --freshen`。

同类扫描额外修复：17 条风险低估的命令升为 medium，13 条 POSIX 语法命令不再标原生 Windows，`linux.gunzip`、`node.npm-test`、`python.http-server` 重写，新增 `node.http-server`（主题 515 个）。这些修复对照官方文档完成，没有在真实环境逐条运行；低风险命令只做过关键词扫描，不是全量复审。

**引号问题根因**：vLLM 使用的 xgrammar 在字符串带 `minLength` / `maxLength` 时不接受 JSON 转义，`\"` 无法生成。同一 schema 下 llama.cpp 正常：

| schema 中 `code` 的约束 | vLLM | llama.cpp |
|---|---|---|
| 无长度约束 | `tasklist /FI \"PID eq <pid>\"` | 同左 |
| `minLength` 或 `maxLength` | 在 `tasklist /FI "` 处关闭字符串 | `tasklist /FI \"PID eq <pid>\"` |

这造成两种现象：`summary` 写到引号就断开，之后补空白直到 2048 token 上限（`invalid_json`）；`code` 里带 `"` 的命令被改写，再被逐字匹配过滤。修复：

1. schema 不再给 `code` 设长度约束，Pydantic 解析时仍校验。
2. `summary`、`text`、`notes` 用 `pattern` 规定只能在句末标点后结束，模型不写裸引号（`text`、`notes` 没有长度约束时也出现过裸引号）。
3. 解析时允许字符串内的原始制表符。
4. `grounded_commands` 比对时忽略引号差异，优先精确匹配，返回的始终是知识库原文。这也修复了只差占位符的命令互相覆盖的问题（知识库中 55 组，如 `git checkout <branch_name>` 与 `git checkout <path/to/file>`）。

**最终结果**（515 主题，901 例计分）。vLLM 两组同时运行，llama.cpp 与修复 `code` 前的 vLLM 两组同时运行，延迟只能组内比较；首例预热约 68 s，未计入最长延迟：

| 配置 | 通过率 | 失败 | `invalid_json` | 过滤后含命令 | 命令保留率 | 每例输出 token | 平均 / p95 / 最长延迟 |
|---|---|---|---|---|---|---|---|
| vLLM 基座 | 99.56% | 4 | 0 | 95.2% | 98.0% | 644 | 5.2 / 10.0 / 17.5 s |
| vLLM + LoRA | 99.56% | 4 | 0 | 93.5% | 98.5% | 397 | 3.8 / 7.1 / 13.3 s |
| llama.cpp Q4 | 99.56% | 4 | 0 | 94.4% | 98.7% | 580 | 3.9 / 7.0 / 12.3 s |

- “过滤后含命令”按 `grounded_commands` 之后统计，是用户实际看到的结果。上文表格的“含命令”都是过滤前的口径，偏高。修复前 vLLM 基座为 87.5%（旧匹配规则），修复后 95.2%。
- 这个问题不影响通过率：修 `code` 前后都是 99.56%。只看通过率发现不了命令被丢弃。
- 三组失败相同：memory-029、memory-030（未指定平台的追问）、memory-060（无答案问题未拒答）、windows-171（检索未命中）。
- 剩余失败都不是输出格式问题。LoRA 仍少约 38% 输出 token，含命令比例略低于基座。

## 已知问题与下一步

- vLLM 日志提示 FlashInfer 内核未启用（缺少 `flashinfer-cubin`），启用后延迟可能进一步下降。
- Agent 工具调用（`agent_model.py`）尚未在本地模型上验证。