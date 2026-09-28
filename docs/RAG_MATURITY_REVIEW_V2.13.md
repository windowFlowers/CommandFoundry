# CommandFoundry v2.13 高级查询与检索审查

## 本版本完成

- 确定性查询分析：规范化、意图分类、平台/Shell/领域识别。
- 普通知识问题最多保留四个查询视图；原始查询始终排在第一位。
- 只有检测到两个不同技术域时才做问题分解，并通过现有 RRF 融合。
- 命令、模板、文档定位、凭据和提示注入请求不使用模型改写；复合命令仍只做本地、可审计的分解。
- 在 BM25/向量召回前执行白名单领域、平台和 Shell 过滤；过滤不通过时不把未知命令当作通配符。
- 父子块折叠、`chunk_id`、`parent_id`、locator 和 citation provenance 沿用原有链路。
- 增加确定性轻量 rerank、弱证据置信度和模型调用门禁。
- 增加可选的 FastEmbed `TextCrossEncoder` 适配器。默认关闭、惰性加载、加载或推理失败自动回退，不改变 citation 元数据。
- `/chat/stream` 和离线评测脚本均使用同一高级检索路径；敏感请求绕过查询扩展。

## 可复用组件结论

本项目继续复用已经安装的 `rank-bm25`、`fastembed`、NumPy 和 Pydantic。没有把 LangChain、LlamaIndex 或 Haystack 全套引入运行时，因为它们无法直接保留当前父子块和精确引用契约，并会增加桌面安装包和适配面。

FastEmbed 的 `TextCrossEncoder` 现在通过小型适配器可选接入；必须在项目 qrels 上证明收益后，才考虑打开默认开关。多查询的实现借鉴成熟组件的“原问题 + 受限变体 → 独立召回 → 去重/RRF”模式，但不把模型生成文本当作命令或 citation 来源。

参考：

- [FastEmbed TextCrossEncoder](https://github.com/qdrant/fastembed/blob/main/fastembed/rerank/cross_encoder/text_cross_encoder.py)
- [FastEmbed 支持的模型与许可证](https://qdrant.github.io/fastembed/examples/Supported_Models/)
- [Haystack MultiQueryTextRetriever](https://docs.haystack.deepset.ai/docs/3.2-unstable/multiquerytextretriever)
- [LlamaIndex QueryFusionRetriever](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/retrievers/fusion_retriever.py)

## 当前评测证据

以 v2.12 离线报告为基线，v2.13 高级检索离线全套包含 662 条用例，其中 602 条可评测、60 条按数据集规则跳过；当前结果为：

- 602/602 评测用例通过；
- answerable Recall 1.0000（全体 Recall 0.9983），MRR 0.9892，NDCG 0.9916；
- p95 6.599 ms（重复运行基线为 6.832 ms），当前 v2.13 基线内稳定；
- citation/占位符/凭证/提示注入硬门禁无失败。

报告目录由本地评测命令生成，不应上传未脱敏提示词、凭证或敏感路径。真实 provider 评测仍必须在发布候选阶段运行，并且要使用与桌面端相同的 hybrid 配置；此前 v2.12 的真实模型报告使用的是 BM25 配置，这个差距已经记录为发布前检查项。

## 与成熟产品相比仍缺少的能力

### P0：发布前补齐

1. **真实 hybrid + 可选 reranker 发布评测**：离线门禁已通过，但安装环境中的向量模型缓存、跨语言 reranker 和真实 provider 延迟还没有形成稳定基线。
2. **可校准 no-answer**：当前是 BM25/向量信号的保守启发式，不是按领域、top1-top2 margin 和 rerank 分数校准的概率模型。
3. **检索可观测性**：目前只在内部状态事件中暴露命中数，没有独立的脱敏 retrieval trace、bad-case 导出或 `/knowledge/search` 调试接口。
4. **精确检索模式**：尚未提供 grep/短语/正则的用户可选模式，代码和长日志的精确命中仍依赖 BM25。

### P1：产品化阶段

- Full Context 与 Focused Retrieval 的可选模式；
- 用户可调 similarity threshold、context snippet 数量和 workspace 检索策略；
- metadata filter 的显式 API（文档、来源、revision、标签），而不是只从自然语言推断高信号过滤；
- Git/目录/URL/云盘增量同步、跨知识库搜索和知识库导出；
- 用户反馈、bad-case 回流和基线版本管理；
- 大文档、高并发、离线模型缓存和索引恢复压测；
- 依赖/SBOM/许可证扫描、Windows 签名、升级/卸载/回滚测试；
- 本地数据加密、provider 健康检查、重试、熔断和备用模型。

同类产品能力参照：

- [Open WebUI Knowledge Bases](https://docs.openwebui.com/features/workspace/knowledge/) 与 [RAG](https://docs.openwebui.com/features/chat-conversations/rag/)
- [AnythingLLM 文档与 RAG 设置](https://docs.anythingllm.com/chatting-with-documents/introduction)
- [RAGFlow 索引器](https://ragflow.io/docs/v0.27.2/configure_indexer_component)
- [Datawhale 查询重写与系统评测](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter4/14_query_rewriting.md)

## 结论

CommandFoundry 已从“只有关键词/向量召回”的原型进入本地优先 Beta：命令安全、父子块精确引用和确定性规划仍是差异化优势；高级查询的 P0 召回、过滤、分解和离线门禁已完成。距离正式可用的通用 RAG 产品，下一步应先完成真实 hybrid/reranker 评测、no-answer 校准和检索 trace，再做来源同步、全文/精确检索与运维能力，而不是立即替换向量数据库。
