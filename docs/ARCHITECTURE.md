# 架构与关键取舍

## 核心链路

```mermaid
flowchart LR
    Q[开发者提问] --> API[FastAPI SSE]
    API --> MEM[会话上下文构建]
    MEM --> RW{需要指代改写?}
    RW -->|否| RET[本地检索]
    RW -->|是| DUAL[原问题 0.35 + 上下文问题 1.0]
    DUAL --> RET
    RET --> BM[BM25]
    RET --> VE[BGE-small-zh 向量]
    BM --> RRF[RRF 融合 Top 4]
    VE --> RRF
    RRF --> GEN{DeepSeek 可用?}
    GEN -->|是| LLM[结构化整理]
    GEN -->|否或失败| FB[确定性本地回答]
    LLM --> VAL[Pydantic 校验]
    VAL --> RISK[命令风险复核]
    FB --> RISK
    RISK --> UI[命令卡 + 来源]
    API <--> DB[(SQLite 会话 / 文档 / 向量)]
    MEM <--> MDB[(conversation_memory)]
    RISK --> COMPACT{超过 6 轮或 1800 token?}
    COMPACT -->|是| LOCAL[本地滚动摘要]
    LOCAL -.可选后台优化.-> SLLM[DeepSeek 摘要优化]
    SLLM --> MDB
    LOCAL --> MDB
```

## 目录职责

```text
backend/app/
  knowledge.py    加载 topics.json 与 manifest 追溯校验
  retrieval.py    分词、BM25、FastEmbed、内存余弦、RRF
  generation.py   DeepSeek OpenAI-compatible 调用与 JSON 校验
  memory.py       上下文预算、指代改写、双查询融合与后台摘要
  answering.py    模型/本地回答路由与引用装配
  risk.py         与模型无关的确定性风险规则
  extraction.py   TXT / Markdown / PDF / DOCX 文本提取
  text.py         标题、段落和代码围栏感知切分
  documents.py    上传文件与可恢复后台索引队列
  repository.py   SQLite 会话、记忆、知识库、文档与分块仓储
  main.py         多知识库 API 与 SSE 事件契约
frontend/src/     问答工作台与知识库管理页
desktop/electron/ 进程、协议、窗口和 safeStorage 边界
knowledge/        300 个结构化主题、来源锁、评测集和声明
```

## 为什么这样设计

### 小数据直接内存检索

内置知识库只有 300 个主题，自定义库也定位于个人文档规模。引入 Milvus、FAISS、Chroma 或 PostgreSQL 会增加部署和打包成本。每个知识库按需加载 BM25 与归一化向量矩阵，文档变更后原子替换缓存。

### BM25 与向量互补

命令问题常含精确 token，例如 `git revert`、`CREATE VIEW`；BM25 对这类词稳定。中文口语改写更适合语义向量。两路结果用 RRF 按排名融合，避免比较不同分数空间。

### LLM 不是单点故障

DeepSeek 只重组本地命中，不能决定来源，也不能绕过风险规则。无 Key、超时、429、非法 JSON 或模型不可用时，命令仍从同一知识主题确定性生成。

### 会话记忆是状态，不是知识

每个会话持有独立的“滚动摘要 + 最近原始轮次”，历史预算最多约 2,400 token。指代改写只影响检索，界面和最终回答仍保留用户原问题。历史摘要和消息在提示中明确标记为不可信；新命令必须来自当前知识库 Top 4 证据。切换知识库会创建新会话，清空上下文只推进边界而不删除可读聊天记录。

达到 6 轮或约 1,800 token 后，除最近两轮外的历史先由本地规则即时压缩。配置 Key 时才排队调用 DeepSeek 优化摘要，后台任务用 revision 乐观锁防止旧结果覆盖新摘要，进程重启会恢复未完成任务。普通回答不会因此增加模型调用。

### 桌面安全边界

Renderer 只得到 API 地址、版本号以及四个模型配置方法。密钥由主进程用 DPAPI 加解密，并仅通过后端子进程环境变量注入；Renderer 无“读取密钥”能力。上传文件只进入本机 FastAPI 和用户数据目录。

## API 契约

`POST /chat/stream` 从 `contextualizing` 状态开始，再进入检索与生成，随后发送 `answer`、`done`，异常使用 `error`。回答对象包含 `mode`、`summary`、`commands`、`notes`、`citations`、`generation` 和 `context`。`GET /conversations/{id}/memory` 返回当前摘要及本次使用的历史预览，`POST /conversations/{id}/memory/reset` 推进上下文边界。对话固定绑定知识库，传入不一致的 `knowledge_base_id` 返回 409。
