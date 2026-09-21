# CommandFoundry RAG 成熟度路线图

审计日期：2026-09-19  
当前基线：CommandFoundry v2.12 开发分支（本地优先、Windows 单用户桌面端）

评测闭环状态：v2.11 评测基础设施已完成；v2.12 数据清洗和质量报告已接入源码与离线专项测试。当前仍未形成新的发布基线：必须先运行当前 provider 的真实模型候选评测、桌面验收和发布门禁，才允许构建安装包。

## 1. 审计口径

本路线图参考 Datawhale 的 [All-in-RAG 全栈指南](https://github.com/datawhalechina/all-in-rag)，重点覆盖其中的数据加载、文本分块、向量与索引、混合检索、查询构建/重构、格式化生成和系统评估章节。该指南同时把多模态 RAG、知识图谱 RAG 和 Text2SQL 作为高级扩展；它们不自动等同于 CommandFoundry 的当前产品需求。

成熟度分成两个目标，避免把单机产品和 SaaS 平台混为一谈：

- **当前目标：单用户本地成熟产品**。重点是文档质量、检索/回答可靠性、可解释评测、可恢复索引和安全边界。
- **未来目标：团队/服务化产品**。在达到上面目标后，再增加认证、多租户、限流、分布式向量库和可观测性。

## 2. 当前已具备能力

| 指南领域 | 当前实现 | 证据 |
| --- | --- | --- |
| 数据加载 | TXT、Markdown、PDF、DOCX；保留页、行、段落、表格行和标题路径定位 | `backend/app/extraction.py`、`backend/tests/test_parent_child_chunking.py` |
| 分块与上下文 | 结构化父块/子块、稳定 ID、重叠、代码围栏保护、父块上下文回填 | `backend/app/chunking.py`、`docs/ARCHITECTURE.md` |
| 索引与检索 | BM25 + BGE-small-zh-v1.5 + RRF；子块召回、父块聚合、文档配额 | `backend/app/retrieval.py` |
| 引用与溯源 | manifest/source lock、文档哈希、index revision、精确 citation locator、模型引用校验 | `backend/app/evidence.py`、`backend/app/answering.py` |
| 生成安全 | Pydantic 结构化答案、模型不可用时本地回退、占位符/命令/风险审查 | `backend/app/generation.py`、`backend/app/risk.py` |
| 对话能力 | 会话摘要、指代改写、个性化上下文、可恢复命令参数计划 | `backend/app/memory.py`、`backend/app/personalization.py`、`backend/app/command_planner.py` |
| 基线评测 | 300 条单轮、60 组多轮、80 条父子块、60 条个性化和 Windows 专项评测 | `docs/RAG_EVALUATION.md`、`knowledge/*_eval_dataset.json` |

因此，当前更准确的定位是：**功能完整的本地 Beta / 单机生产前版本**，而不是缺少核心 RAG 能力的原型。

## 3. 距离成熟产品的主要缺口

### P0：下一阶段必须补齐（单用户本地产品也需要）

#### P0-1. 从“召回命中率”升级为完整评测闭环

当前评测主要验证预期主题是否进入 Top-1/Top-3，以及父子块 Recall；尚未形成带标注证据、回答质量、成本和回归门槛的一体化评测。指南建议同时观察上下文相关性、忠实度和答案相关性，并把目标、数据、运行记录、分层分析、Bad Case 和回归门槛串成闭环（见[系统评估章节](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter6/18_system_evaluation.md)）。

待做：

- 建立 `qrels`：一个问题可以对应多个相关父块/子块，而不是每题只有一个主题 ID。
- 增加 Precision@k、Recall@k、MRR、NDCG、命中来源覆盖率和无答案拒答率。
- 为最终回答增加 faithfulness、answer relevance、完整性、命令可执行性和引用正确性评测。
- 每次实验记录模型、提示词版本、embedding 版本、chunk 参数、top-k、reranker、延迟、token 和估算成本。
- 把关键 Bad Case 固化为回归集，在 CI 中设置“总体不下降 + 高风险样本不得失败”的门槛。

#### P0-2. 文档解析质量和数据清洗

当前 PDF 依赖文本层提取，扫描型 PDF 被明确拒绝；本项目本阶段不扩展 OCR、网页/HTML、Excel、PPT 和图像解析。指南强调加载阶段需要统一文本、元数据和结构元素，否则会出现“垃圾进，垃圾出”（见[数据加载章节](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter2/04_data_load.md)）。

已完成（v2.12 数据清洗范围）：

- TXT、Markdown、文本型 PDF 和 DOCX 在 extraction 与 chunking 之间执行无模型、确定性的清洗。
- 保留原文件、父子块、标题路径、页码/行号/表格行和精确字符 locator；清洗后的正文单独保存哈希。
- 记录清洗状态、规则 revision、删除统计、警告和质量报告；旧索引在后台重建成功前继续可用。
- SQLite schema v7 迁移前自动备份，重启后继续待重建文档；清洗失败时保留旧 active index。

仍待做（明确延期，不属于 v2.12）：

- 增加扫描 PDF/图片 OCR，并标记 OCR 置信度和人工复核状态。
- 增加 HTML、XLSX、PPTX；保留标题、页码、表格、图片标题、代码块和来源 URL。
- 增加重复/近重复文档检测，避免同一知识在召回和上下文中重复计权。

#### P0-3. 查询理解、重排和无答案判定

当前有会话指代改写和个性化查询补充，但还没有通用的查询翻译、复杂问题分解、查询路由、元数据过滤或 cross-encoder 重排。指南将查询翻译、查询路由、多查询分解和结构化查询视为复杂问题的关键能力（见[查询重构与分发章节](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter4/14_query_rewriting.md) 与[查询构建章节](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter4/12_query_construction.md)）。

待做：

- 增加意图分类：命令生成、知识问答、文档定位、比较/汇总、无关问题和模板请求。
- 对长问题支持多查询分解与去重合并；对短问题保留当前低延迟路径。
- 在 RRF 候选后增加可选 reranker，并用离线数据集选择是否启用。
- 支持按知识库元数据过滤：来源、平台、版本、日期、语言、文档标签和章节。
- 建立“无足够证据”的置信度判定，明确拒答/请求澄清，而不是仅依赖 Top-k 非空。

#### P0-4. 索引生命周期与可恢复性

当前上传文档有后台索引、revision 和重建恢复，但内置主题向量主要在进程中按需构建；embedding 模型变化、批量导入、失败重试、并发索引和索引导出还没有统一的生命周期协议。

待做：

- 将 embedding 模型、维度、归一化方式、chunk schema 和源码 revision 写入可校验的 index manifest。
- 增加增量索引、批量任务、重试退避、并发上限、取消和进度事件。
- embedding 模型或 chunk 规则变化时，提供可预览的迁移/重建计划，不隐式混用旧向量。
- 支持知识库索引导出、备份、恢复和校验；重启后能检测半成品索引。
- 增加大文档、超长代码、并发上传和磁盘不足的测试。

#### P0-5. 命令配方覆盖率与直接可用性

当前知识库有 300 个主题，但可安全渲染的 provenance-backed recipe 只有 21 个；命中其他主题时仍可能只能返回模板。这不是普通 RAG 的必需能力，却是 CommandFoundry 的核心产品承诺：用户希望得到可以直接复制的命令，而不是把尖括号参数留给用户猜。

待做：

- 为高频主题建立 recipe 覆盖率清单，按真实查询频率、风险和平台拆分评测。
- 将“模板、澄清、direct”三种结果纳入端到端指标，统计参数一次提取成功率、追问轮数、无效输入回退率和 direct 占比。
- 为每个 recipe 记录支持的 shell/platform、必填槽位、风险、来源和渲染测试；未知模板继续安全回退，不把所有尖括号自动参数化。
- 为 provider 记录 JSON mode、工具调用、上下文长度、限流和费用能力；能力不足时保持本地规划器结果，不让模型覆盖命令或引用。

### P1：成为团队/服务化产品前补齐

#### P1-1. 可扩展向量后端

当前采用 SQLite 元数据 + 内存 BM25/NumPy 向量矩阵，适合 300 个内置主题和个人文档规模。指南指出，在百万级向量、高并发或复杂元数据过滤时，应考虑 Milvus、Qdrant、Weaviate 等专业向量后端（见[向量数据库章节](https://github.com/datawhalechina/all-in-rag/blob/main/docs/chapter3/08_vector_db.md)）。

待做：

- 定义 `VectorStore` 接口，先保留当前 local provider，再实现一个可选 Qdrant 或 Milvus provider。
- 为不同规模建立基准：块数量、写入吞吐、P95 延迟、内存、重建耗时和过滤性能。
- 当前 `docker-compose.yml` 中的 Postgres/Milvus 配置要么接入真实 adapter，要么删去，避免“配置存在但运行时不生效”。
- 当块数、并发或过滤需求达到阈值时再切换，不为当前单机库提前引入运维负担。

#### P1-2. 来源新鲜度与知识运营

当前内置知识有固定 revision、manifest 和 sources.lock；上传文档主要是本地文件哈希，没有 URL 连接器、定时同步、版本 diff、过期提醒和来源变更通知。

待做：

- 支持可信 URL/仓库连接器和手动同步，保存抓取时间、ETag/Last-Modified、版本和许可证。
- 提供来源 diff、失效/过期标记、重建前后召回变化和回滚。
- 增加知识库标签、批量导入/导出、文件夹和来源管理。

#### P1-3. 可观测性、权限和部署边界

当前 API 默认面向本机 Electron，依赖 Origin 检查而没有用户认证、租户隔离、限流、配额、审计日志、指标和分布式任务队列。

待做：

- 本地模式继续保持无账号；服务模式增加认证、RBAC/知识库 ACL、租户隔离、限流和配额。
- 记录结构化 trace：请求、检索器、命中块、模型、延迟、token、失败原因和成本；默认脱敏且可关闭。
- 增加 `/livez`、`/readyz`、索引队列健康、embedding 健康和磁盘空间检查。
- CI 覆盖桌面测试、知识同步、打包后 smoke、依赖审计和 Windows 构建；当前 CI 主要覆盖后端测试和前端构建。
- 明确 SQLite 单机备份策略，以及服务化部署的 PostgreSQL/对象存储/队列方案。

#### P1-4. 反馈驱动的持续改进

当前没有用户对答案、引用和命令的反馈闭环，也没有把失败样本自动沉淀为评测数据。

待做：

- 支持有用/无用、引用错误、命令不适用和“应该拒答”反馈。
- 反馈记录引用的 query、retrieval config、answer version 和脱敏 Bad Case。
- 由人工审核后进入 qrels、回答评测集或配方测试，不直接让用户反馈改变知识库。

#### P1-5. 发布和升级验收

RAG 质量之外，成熟桌面产品还需要可重复发布。当前文档已记录未完成的 Windows 代码签名、SmartScreen、干净环境安装/覆盖升级/卸载验收；这些不应被“源码测试通过”替代。

待做：

- 在干净 Windows 10/11 x64 环境验证安装、启动、升级、数据迁移、快捷方式、卸载和回滚。
- 配置代码签名、发布校验和安装包 SBOM；保留 SHA-256、构建提交和测试报告。
- CI 增加桌面端测试、知识同步、打包后 smoke 和依赖/许可证扫描。

### P2：按产品方向选择，不是当前阻塞项

- **多模态 RAG**：图像、扫描版表格、流程图、截图和视觉 embedding；只有当用户文档确实包含这类内容时启动。
- **Graph RAG**：实体/关系抽取、图谱存储和 Text2Cypher；适合跨文档关系推理，不适合当前以开发者命令为主的主题库直接引入。
- **Text2SQL/结构化数据检索**：需要 schema 管理、SQL 只读沙箱、行列级权限和结果验证，不能直接把生成 SQL 接入生产库。
- **Agentic RAG**：多路工具调用、规划和自我修正；先完成查询路由、证据门和评测，再考虑增加代理复杂度。

## 4. 分版本实施计划

### v2.11：评测闭环与检索可观测性（P0，基础设施已完成）

- qrels、Precision/Recall/MRR/NDCG、无答案和引用覆盖率。
- 端到端回答评测集：忠实度、相关性、完整性、命令安全。
- 记录脱敏 trace、延迟、token、成本和失败原因。
- 建立 21 条 recipe 的 direct/clarification/template 基线和高频主题覆盖清单。
- CI 回归阈值与 Bad Case 报告。

**当前完成情况**：已提供版本化 qrels/黄金集、答案与安全校验、父子块/来源/引用指标、脱敏 run manifest、离线与真实 `/chat/stream` 评测入口、基线容差门禁、CI 和发布候选工作流。当前离发布还差当前 provider 的真实模型评测、Computer Use 桌面验收和与发布 commit/knowledge revision 匹配的候选报告。

**完成标准**：一次命令能生成可比较的基线报告；任何检索/提示改动都能看到分层指标和失败样本。该标准已由离线核心集验证，真实模型门禁仍按发布候选流程执行。

### v2.12：数据清洗与索引质量（P0，源码已实现，发布门禁待重跑）

- 只覆盖 TXT、Markdown、文本型 PDF 和 DOCX；不新增输入格式。
- 清洗 BOM、换行、控制字符、尾部空格、多余空行、PDF 重复页眉/页脚、相邻重复段落和高置信度近重复。
- 保护代码围栏、命令缩进、表格分隔符、标题路径和父子块精确 locator。
- 生成 `knowledge/quality_report.json`，同步写入 manifest、sources.lock 和 recipes 的 cleaning revision；上传文档暴露脱敏质量统计。
- 后台逐文档重建，原子替换新索引；旧索引、原始文件和原始哈希在失败时保留。

明确延期：OCR、扫描 PDF、HTML、XLSX、PPTX、图片、多模态解析、向量数据库替换、查询改写和 reranker。

**完成标准**：数据质量专项测试、v2.11 离线评测、当前 provider 真实模型评测、风险门禁和桌面验收全部通过后，才允许生成安装包。

### v2.13：查询理解与重排（P0）

- 意图路由、元数据过滤、多查询分解、可选 reranker。
- 无答案置信度、明确拒答和上下文压缩/去重。
- 对命令配方保持确定性规划器优先，模型只负责解释和非权威改写。

**完成标准**：复杂问题召回提升且延迟可控；无证据问题不编造；旧命令规划和精确引用回归不下降。

### v2.14：可扩展后端与服务边界（P1）

- VectorStore provider 接口和 Qdrant/Milvus 任选其一的可选实现。
- 规模/并发/过滤基准，部署文档和真实 Docker smoke。
- 服务模式的认证、ACL、限流、配额、健康检查和结构化 trace。

**完成标准**：local provider 和远程 provider 结果契约一致；有明确的规模切换阈值和回滚方案。

### v2.15：知识运营与反馈闭环（P1）

- URL/仓库同步、版本 diff、过期提醒、批量管理。
- 用户反馈、人工审核、Bad Case 到评测集的流水线。
- 来源许可证、删除请求、备份恢复和审计策略。
- Provider 能力登记、重试/退避/熔断、预算和模型 fallback 策略。
- 干净 Windows 环境的安装、升级、签名和卸载验收。

**完成标准**：知识库能持续更新，失败样本能进入回归集，来源和变更可追踪。

### v2.16+：可选高级能力（P2）

根据实际用户文档和需求选择多模态、Graph RAG、Text2SQL 或 Agentic RAG；每项都必须先补数据集、权限模型、沙箱和专项评测，不以“接入组件”替代效果验证。

## 5. 现在不建议做的事

1. 不因指南介绍了 Milvus 就立即重做当前向量数据库；当前规模使用本地 BM25/NumPy 是合理取舍，先完成基准和 provider 边界。
2. 不把所有模型输出都交给 Agent 自由规划；命令生成继续由来源约束的确定性规划器控制。
3. 不先做 Graph RAG 或多模态展示，再补基础评测；先确保数据质量、召回、忠实度和拒答可量化。
4. 不直接复制 LangChain/LlamaIndex 全套依赖；保留当前轻量管线，只有在某项能力的维护成本明显下降时再局部引入。

## 6. 下一步执行顺序

下一步继续沿着 **评测闭环 → 数据质量 → 评测闭环** 推进，而不是先换向量数据库：

1. 给现有 300/60/80 条数据补充相关块标注、不可回答标签和难度/领域切片。
2. 对 v2.12 清洗结果运行专项质量集，确认 locator、父子块和引用没有漂移。
3. 重新运行 v2.11 离线门禁，并在发布候选阶段运行当前 provider 的真实模型评测。
4. 从 Bad Case 反推是解析、分块、召回、重排还是生成问题；只有数据证明有效才启动后续格式或检索扩展。
