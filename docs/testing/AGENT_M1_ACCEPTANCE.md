# Agent M1 运行时模块验收

日期：2026-10-14。范围：本地同步 LangGraph 运行时；模型与产品 UI 接入尚未完成。

实际复用 LangGraph StateGraph、官方 SqliteSaver、interrupt/Command 和 filelock。

验证命令：`backend/.venv/Scripts/python.exe -m pytest backend/tests -q`

结果：308 passed，23.21s；1 项现有 Starlette/AnyIO 弃用警告。运行时两个测试文件共 44 项；`pip check` 无冲突，`git diff --check` 通过。

已验证：模型决策→类型化工具→观察→后续决策；步数和输出预算；未知工具和坏参数拒绝；审批发生在工具效果前；重启恢复；审批拒绝/取消/错误 ID/跨运行审批拒绝；schema/revision 变更失效；两个独立进程锁冲突；凭证不进入 checkpoint/ledger；宿主环境打开 LangSmith 时仍关闭 tracing。

独立审查实际复现并修复：Pydantic model_copy/model_construct 实例绕过字段校验；非幂等 validator 改变已审批参数；字段 alias 在审批和执行之间丢失。回归用例已纳入测试。

效果恢复策略：执行前持久化 started；完成后持久化输出；完成记录可以重读而不重试效果，started 缺结果则失败关闭。该策略保证最多尝试一次，不能保证外部效果 exactly-once。

限制：同步回调执行期间无法即时取消。未调用真实 API、未操作桌面 UI、未执行助手生成命令，未生成安装包。本记录只证明 M1 验收，不构成发布报告。
