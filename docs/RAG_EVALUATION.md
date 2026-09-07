# RAG 评测报告

评测日期：2026-09-08

## 数据集

`knowledge/eval_dataset.json` 共 100 条查询，每个知识主题 1 条，覆盖六个领域。每条记录只包含查询、预期主题 ID 和领域，不使用模型生成结果判定检索命中。

| 领域 | 数量 |
|---|---:|
| Linux / Shell | 20 |
| Git | 20 |
| SQL / MySQL | 15 |
| Docker | 15 |
| HTTP / cURL | 10 |
| Python / Node | 20 |
| 合计 | 100 |

## 结果

| 检索配置 | Top-1 | Top-3 |
|---|---:|---:|
| BM25 本地降级 | 100% | 100% |
| BM25 + BGE-small-zh-v1.5 + RRF | 92% | 98% |

目标为 Top-3 不低于 85%，当前混合检索高出 13 个百分点。三个面试核心问题均 Top-1 命中：

- Linux 切换目录 → `linux.cd`
- Git 安全回滚 → `git.git-revert`
- MySQL 创建视图 → `sql.create-view`

混合检索的 2 个 Top-3 失配样例是“查看未提交修改”和“MySQL JOIN”。它们在向量路由中被邻近主题挤出前三，但精确别名场景的 BM25 降级路径均命中。首版保留该结果并在后续通过补充对比式问法评测、调整向量候选权重来改进，不为 100 个主题引入复杂 reranker。

## 安全与失败测试

- `rm -rf`、`git reset --hard`、强制推送、`DROP/TRUNCATE`、Docker prune、Kubernetes delete、`curl | sh` 全部被提升为高风险并带警告。
- `sudo` 至少为中风险。
- 无 Key、无命中和模型非法 JSON 均进入 `local_fallback`。
- 所有引用的 `source_id` 均存在于 manifest，每条来源有固定 revision、URL、license 和 SHA-256。

## 复现

```powershell
.\backend\.venv\Scripts\python.exe scripts\evaluate_rag.py
```

输出为 JSON，分别给出 BM25 与混合检索的 Top-1 / Top-3 命中数和命中率。完整测试仍可通过 `backend\.venv\Scripts\python.exe -m pytest -q` 运行。
