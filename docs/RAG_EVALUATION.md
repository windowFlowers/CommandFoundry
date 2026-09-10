# RAG 评测报告

评测日期：2026-09-10（单轮/多轮基线于 2026-09-08 建立）

## 数据集

`knowledge/eval_dataset.json` 共 300 条独立自然语言查询，每个知识主题一条。`knowledge/memory_eval_dataset.json` 另含 60 组多轮查询，覆盖平台替换、代词、序号指针、风险与约束继承、新主题切换、无命中和提示注入。

每条记录只包含查询、预期主题 ID 和领域，不使用大模型结果判定检索命中。构建脚本同时校验主题 ID、HTTPS 来源、固定 revision、许可证、SHA-256、占位符和命令代码。

## 结果

| 检索配置 | 单轮 Top-1 | 单轮 Top-3 | 多轮 Top-1 | 多轮 Top-3 |
|---|---:|---:|---:|---:|
| BM25 本地降级 | 98.00%（294/300） | 100%（300/300） | 98.33%（59/60） | 100%（60/60） |
| BM25 + BGE-small-zh-v1.5 + RRF | 93.67%（281/300） | 99.67%（299/300） | 95.00%（57/60） | 98.33%（59/60） |

单轮与多轮混合检索 Top-3 均高于 90% 目标。多轮集合唯一未命中项是刻意设计的完全无命中问题：向量模型仍返回语义候选，回答层会遵守知识证据约束，避免据此编造命令。

三个单轮面试问题与三个核心追问均 Top-1 命中：

- Linux 切换目录 → `linux.cd`
- Git 安全回滚 → `git.git-revert`
- MySQL 创建视图 → `sql.create-view`
- Linux 查看端口 → “那 Windows 呢？” → `windows.netstat`
- Git 回滚 → “第二种会丢代码吗？” → `git.git-reset`
- MySQL View → “PostgreSQL 怎么写？” → `sql.postgres-create-view`

## v2.3 父子检索离线评测

2026-09-10 使用 20 个结构化 IT 主题，每个主题动态生成 4 条自然语言改写，共 80 条查询。每个主题均先经过正式的结构化提取和父子分块，写入临时 SQLite 索引；评测随后调用应用的 `HybridRetriever` 执行 BM25 子块检索，再调用 `KnowledgeIndexManager` 按父块聚合。向量检索在此项测试中明确关闭，以保证结果离线、确定且可复现。

| 指标 | 目标 | 实测 |
|---|---:|---:|
| 父块 Recall@3 | ≥ 92% | 100.00%（80/80） |
| 精确子块 Recall@5 | ≥ 90% | 100.00%（80/80） |

复现命令：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_parent_child_retrieval_evaluation.py -q -s
```

测试运行约 1 秒，输出 `cases=80, parent_recall_at_3=100.00%, child_recall_at_5=100.00%`。数据集和四种改写模板固定在测试文件中，不依赖 API Key、网络或模型缓存。

## v2.3 自动化回归

| 范围 | 结果 |
|---|---:|
| FastAPI / SQLite / RAG | 70 passed |
| React 数据与引用交互 | 18 passed |
| Electron 安全与打包边界 | 16 passed |
| Vite 生产构建 | 通过 |

## 功能与失败验证

- 多知识库隔离、会话绑定、已删除知识库历史只读和 409 冲突均有自动化测试。
- 会话记忆覆盖 2,400 token 上限、最近轮次优先、reset 边界、跨会话/知识库零泄漏、本地摘要、DeepSeek 摘要成功/失败、revision 并发保护、进程恢复与级联删除。
- 普通回答验证为一次模型调用；只有超过摘要阈值才产生后台摘要调用。摘要不保存回答中的命令正文，并对常见 API Key、Token 和密码格式脱敏。
- TXT/Markdown/PDF/DOCX 已完成真实文件上传、提取、后台索引和内容预览验收；20 MB 上限、重复文件、空文本、扫描型 PDF 与加密 PDF 均有确定性拒绝路径。
- v2.3 增加结构化提取、父子块、稳定 ID 和精确引用专项测试：Markdown/TXT 行号、PDF 页码、DOCX 原序段落与表格行均能回溯到规范化字符区间。
- 父块目标/上限为 1,800/2,400 字符，子块为 420/800 字符并重叠 80；超长代码围栏只按完整行切分，每个片段自动闭合与重开围栏。
- 子块候选池为 20，按父块最佳命中聚合，最终每份文档最多 2 个父块；事务故障测试验证旧 parent/child/content/revision 会整体回滚。
- 模型未知引用会被移除，上传命令必须精确匹配命令证据；旧 v2.2 回答缺少新字段时仍能加载。
- `rm -rf`、`git reset --hard`、强推、`DROP/TRUNCATE`、Docker prune、Kubernetes delete、`curl | sh` 全部提升为高风险并带警告。
- 无 Key、无命中、401/403、429、超时、网络失败和非法 JSON 均映射到稳定的 `fallback_reason`。
- 使用 Windows 安全存储中的 Key 完成真实 Chat Completions 与完整 RAG 验收：最小连接测试返回 45 token、716 ms；最近一次完整链路返回 `mode=model`、真实响应 ID、2,981 token、2,216 ms、3 条命令、4 条引用和 3 个带引用正文段（均为单次实测值，不作为性能承诺）。
- 所有 300 个来源都能同时追溯到 `manifest.json` 与 `sources.lock.json`；构建期缓存不进入运行时。

## 复现

```powershell
.\backend\.venv\Scripts\python.exe scripts\evaluate_rag.py

cd desktop
npm run verify:deepseek
npm run verify:rag
npm run verify:uploads
```

真实模型验证只输出请求证据，不输出 API Key、Authorization、完整请求正文或模型原始响应。
