# RAG 评测报告

评测日期：2026-09-08

## 数据集

`knowledge/eval_dataset.json` 共 300 条独立自然语言查询，每个知识主题一条。主题分布为：Linux 30、Git 30、SQL 30、Docker 25、HTTP 20、Python 25、Node 20、Windows 20、Kubernetes 20、网络 20、Redis 15、Java 20、CI/CD 15、测试与调试 10。

每条记录只包含查询、预期主题 ID 和领域，不使用大模型结果判定检索命中。构建脚本同时校验主题 ID、HTTPS 来源、固定 revision、许可证、SHA-256、占位符和命令代码。

## 结果

| 检索配置 | Top-1 | Top-3 |
|---|---:|---:|
| BM25 本地降级 | 97.00%（291/300） | 100%（300/300） |
| BM25 + BGE-small-zh-v1.5 + RRF | 92.00%（276/300） | 99.33%（298/300） |

目标为混合检索 Top-3 不低于 90%，当前高出 9.33 个百分点。三个面试核心问题均 Top-1 命中：

- Linux 切换目录 → `linux.cd`
- Git 安全回滚 → `git.git-revert`
- MySQL 创建视图 → `sql.create-view`

## 功能与失败验证

- 多知识库隔离、会话绑定、已删除知识库历史只读和 409 冲突均有自动化测试。
- TXT/Markdown/PDF/DOCX 已完成真实文件上传、提取、后台索引和内容预览验收；20 MB 上限、重复文件、空文本、扫描型 PDF 与加密 PDF 均有确定性拒绝路径。
- 分块按标题、段落和代码围栏执行；超长代码围栏保持为一个完整块。
- `rm -rf`、`git reset --hard`、强推、`DROP/TRUNCATE`、Docker prune、Kubernetes delete、`curl | sh` 全部提升为高风险并带警告。
- 无 Key、无命中、401/403、429、超时、网络失败和非法 JSON 均映射到稳定的 `fallback_reason`。
- 使用 Windows 安全存储中的 Key 完成真实 Chat Completions 与完整 RAG 验收；最近一次完整链路返回 `mode=model`、响应 ID、2210 token、3250 ms 和 4 条引用（单次实测值，不作为性能承诺）。
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
