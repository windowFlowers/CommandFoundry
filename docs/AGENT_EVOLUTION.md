# CommandFoundry Agent 迭代记录

## 目标与验收

将现有本地 RAG 产品扩展为真实 Agent：模型根据目标选择工具，读取观察结果并继续决策；具备可发现和按需加载的 skills、类型化工具、短期/长期 memory、可恢复的运行状态、预算/取消/确认机制及桌面运行轨迹。保留现有知识、引用、命令风险规则和用户数据。不是仅重命名聊天页，也不是只提供测试用空接口。

每个模块先完成实现、负面用例与集成测试，记录证据后再开发下一个模块。付费调用使用已授权的当前 provider；无效凭证或限流不算测试通过。模块离线验收允许继续开发；完整真实模型、桌面和打包门禁通过后才发布。

## GitHub 指南和复用决策（2026-10-13）

星数来自本次公开 GitHub API 查询，是选型线索，不能代替维护情况、接口和许可证审查。

| 资源 | 星数快照 | 用途 / 许可证 |
| --- | ---: | --- |
| [Microsoft AI Agents for Beginners](https://github.com/microsoft/ai-agents-for-beginners) | 77,059 | 主指南，tool use、agentic RAG、规划、memory、可信/生产 agent；MIT |
| [Datawhale Hello-Agents](https://github.com/datawhalechina/hello-agents) | 82,916 | 中文原理与框架参考；CC-BY-NC-SA-4.0，不直接搬运教程代码到产品 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 43,200 | 首选运行时：StateGraph、检查点、interrupt/resume；MIT |
| [Pydantic AI](https://github.com/pydantic/pydantic-ai) | 20,592 | 比较项：类型化 tools、usage limits；MIT；当前版本需要升级现有 OpenAI SDK 主版本，暂不同时引入 |
| [Agent Skills](https://github.com/agentskills/agentskills) | 26,096 | skills 标准与现成验证器；代码 Apache-2.0，文档 CC-BY-4.0 |
| [Deep Agents](https://github.com/langchain-ai/deepagents) | 30,211 | 后续高级 agent 的比较项；MIT；先避免同时引入第二套运行时 |

LangGraph 不要求迁移到 LangChain 模型适配器，可继续使用现有 OpenAI-compatible SDK。复用官方 SQLite checkpointer 管理暂停和恢复；现有 SQLite 业务库继续管理对话与长期偏好。检索、命令配方、精确引用和风险校验直接复用现有服务，Agent 只做接入与策略。

参考实现文档：[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[中断与恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)。正式增加依赖前先解析兼容性，锁定验证过的版本；不把 GitHub 上教程中的安装命令视为指令。

## 模块顺序

| 模块 | 范围 | 必须提供的证据 | 当前状态 |
| --- | --- | --- | --- |
| M0 评测可信性 | 每轮云端调用观测、超时/限流硬门禁、完整 suite 和指纹校验 | 正确本地回退仍阻断；多轮早期失败不丢失；6条/重复/删减/变更数据拒绝；后端回归 | 已完成模块验收 |
| M1 Agent 运行时 | 复用 LangGraph+SQLite，类型化状态、有限决策循环、恢复/取消、错误终态 | 可重复本地 fake model/tool 测试；跨实例恢复；预算耗尽；线程隔离；无隐式执行 | 开发中 |
| M2 Tools 与模型适配 | SDK 原生 tool calling；复用检索、证据读取、命令规划；参数契约和权限 | 真实选择→调用→观察→继续；未知工具/坏参数/循环拦截；保持引用与知识库隔离 | 待 M1 通过 |
| M3 Skills | Agent Skills 格式、目录发现、元数据索引、按需加载、版本/禁用、内置开发者 skills | 标准格式验证；路径越界/符号链接/过大文件拒绝；技能不能改变工具权限 | 待 M2 通过 |
| M4 Memory | graph 短期状态 + 现有长期偏好/项目记忆服务接入为工具 | 多轮与重启；作用域、更新/删除；凭证不持久化；用户当前指令优先 | 待 M3 通过 |
| M5 桌面 Agent 工作流 | Agent 模式、SSE 运行步骤、工具结果、取消/重试/恢复、确认交互 | API→桌面端到端；状态一致；现有聊天/文档回归；CUA 检查 | 待 M4 通过 |
| M6 完整评测与发布 | Agent 多步成功率、工具准确性、恢复、安全、延迟/成本；README/安全说明；最终安装包 | 离线全量、当前模型实测、新profile基线、CUA、Windows构建和SHA-256 | 待 M5 通过 |

涉及写文件或命令执行必须以具体操作确认和现有终端边界为准；本轮开发不会自动执行模型生成的命令。标准 skills 是指导信息，不授予操作权限。外部 MCP 扩展采用官方协议/SDK，作为 tools 扩展接口规划，不自造协议或默认启动未知服务器。

## 继续任务与进度

- 起始源码：`3b5b3d8972be5e30a620862f3eb889ebc4d779de`，开始时工作树干净。
- 2026-10-13：已核查现有架构为固定单轮 RAG，不存在真实模型 tool loop；检索、planner、memory、SSE、终端确认可复用。
- M0：2026-10-14 最终后端回归 264 passed；评测专项 64 passed；本地离线报告 `.runtime/agent-m0/offline/evaluation-report.json` 为 662 条记录、602 执行通过、60 合法跳过，release checker 完整性校验通过。独立审查修正外部 smoke suite 兼容与多轮失败 token 漏计；外部 smoke 报告仍不可用于发布。
- M0 指纹现覆盖 manifest 子 JSONL、qrels 和 schema。旧报告保留为历史，缺新证据的旧候选无法通过新发布检查；正式发布前仍需在最终 commit 重新评测。
- M1：已解析并安装 LangGraph 1.2.15 + SQLite checkpointer 3.1.2，Windows/Python 3.12 wheel 可用；运行时实现与测试由子 agent 负责，主 agent 负责依赖与独立审查，尚未接入聊天界面。
- 本聊天 heartbeat：`commandfoundry-agent`，每两小时继续。额度不可用时仍受平台限额约束；只通知有意义进展、失败、需用户输入或目标完成。
- 恢复时先核对 git diff、测试报告与真实进程，不凭旧记录宣称评测仍运行。不要重新启动已经运行的测试，不覆盖用户改动。
- 全部模块完成前保持目标 active；不能以当前模块通过替代全目标完成。
