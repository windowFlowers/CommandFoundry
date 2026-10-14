from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from langsmith import get_tracing_context
from pydantic import BaseModel, ConfigDict, ValidationError

from app.agent_runtime import (
    AgentDecision, AgentRuntime, ApprovalDecision, RunBusyError, RuntimeErrorBase,
    ToolCall, ToolSpec,
)


class LookupArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


def tool_spec(executor, *, approval=False, revision="1"):
    return ToolSpec(name="lookup", arguments_model=LookupArgs, executor=executor,
                    requires_approval=approval, revision=revision)


def once(view):
    if view.observations:
        return AgentDecision(kind="final", answer="Evidence found")
    return AgentDecision(kind="tool", tool_call=ToolCall(name="lookup", arguments={"value": "test"}))


def test_real_langgraph_loop_observes_tool_then_finishes(tmp_path):
    seen = []
    with AgentRuntime(tmp_path / "runs.db", decider=once, tools=[tool_spec(lambda arg: seen.append(arg.value) or {"result": "found"})]) as runtime:
        created = runtime.create("find evidence")
        assert created.status == "created"
        assert seen == []
        final = runtime.run(created.run_id)
        assert final.status == "completed"
        assert final.steps == 2
        assert final.observations[0].output == {"result": "found"}
        assert seen == ["test"]
        assert "approval" in runtime.graph.get_graph().nodes
        with pytest.raises(RuntimeErrorBase, match="not_runnable"):
            runtime.run(created.run_id)


def test_approval_persists_before_effect_and_resumes_after_restart(tmp_path):
    db = tmp_path / "runs.db"
    effects = []
    spec = tool_spec(lambda arg: effects.append(arg.value) or "ok", approval=True)
    with AgentRuntime(db, decider=once, tools=[spec]) as runtime:
        created = runtime.create("request action")
        waiting = runtime.run(created.run_id)
        assert waiting.status == "awaiting_approval"
        assert effects == []
        with pytest.raises(RuntimeErrorBase, match="not_runnable"):
            runtime.run(created.run_id)
    with AgentRuntime(db, decider=once, tools=[spec]) as restarted:
        assert restarted.get(created.run_id).pending == waiting.pending
        final = restarted.resume(created.run_id, ApprovalDecision(approval_id=waiting.pending.approval_id, approved=True))
        assert final.status == "completed"
        assert effects == ["test"]
        with pytest.raises(RuntimeErrorBase, match="not_awaiting"):
            restarted.resume(created.run_id, ApprovalDecision(approval_id=waiting.pending.approval_id, approved=True))
        assert effects == ["test"]


@pytest.mark.parametrize("action", ["deny", "cancel", "wrong_approval"])
def test_no_effect_for_rejection_cancellation_or_wrong_approval(tmp_path, action):
    effects = []
    with AgentRuntime(tmp_path / "runs.db", decider=once, tools=[tool_spec(lambda arg: effects.append(1), approval=True)]) as runtime:
        waiting = runtime.run(runtime.create("test").run_id)
        if action == "deny":
            final = runtime.resume(waiting.run_id, ApprovalDecision(approval_id=waiting.pending.approval_id, approved=False))
            assert final.status == "cancelled"
        elif action == "cancel":
            assert runtime.cancel(waiting.run_id).status == "cancelled"
        else:
            with pytest.raises(RuntimeErrorBase, match="approval_mismatch"):
                runtime.resume(waiting.run_id, ApprovalDecision(approval_id="wrong", approved=True))
            assert runtime.get(waiting.run_id).status == "awaiting_approval"
        assert effects == []


def test_approval_boolean_is_strict():
    with pytest.raises(ValidationError):
        ApprovalDecision(approval_id="id", approved="false")


@pytest.mark.parametrize("changed", ["revision", "approval", "schema", "removed"])
def test_registry_change_invalidates_stored_approval(tmp_path, changed):
    db = tmp_path / "runs.db"
    effects = []
    spec = tool_spec(lambda arg: effects.append(1) or "ok", approval=True)
    with AgentRuntime(db, decider=once, tools=[spec]) as runtime:
        waiting = runtime.run(runtime.create("test").run_id)
    if changed == "revision":
        spec = spec.model_copy(update={"revision": "2"})
    elif changed == "approval":
        spec = spec.model_copy(update={"requires_approval": False})
    elif changed == "schema":
        class NewArgs(LookupArgs):
            other: str = "default"
        spec = spec.model_copy(update={"arguments_model": NewArgs})
    with AgentRuntime(db, decider=once, tools=[] if changed == "removed" else [spec]) as runtime:
        final = runtime.resume(waiting.run_id, ApprovalDecision(approval_id=waiting.pending.approval_id, approved=True))
        assert final.status == "failed"
        assert final.error_code == "tool_contract_changed"
        assert not effects


def test_step_budget_survives_restart_with_larger_defaults(tmp_path):
    db = tmp_path / "runs.db"
    calls = []
    spec = tool_spec(lambda arg: calls.append(1) or "ok", approval=True)
    with AgentRuntime(db, decider=once, tools=[spec], max_steps=1) as runtime:
        waiting = runtime.run(runtime.create("test").run_id)
    with AgentRuntime(db, decider=once, tools=[spec], max_steps=64) as runtime:
        final = runtime.resume(waiting.run_id, ApprovalDecision(approval_id=waiting.pending.approval_id, approved=True))
        assert final.status == "budget_exhausted"
        assert final.max_steps == 1
        assert calls == [1]


def test_endless_decisions_are_bounded(tmp_path):
    calls = []
    with AgentRuntime(tmp_path / "runs.db", decider=lambda view: once(view.model_copy(update={"observations": []})),
                      tools=[tool_spec(lambda arg: calls.append(1) or "ok")], max_steps=3) as runtime:
        final = runtime.run(runtime.create("test").run_id)
        assert final.status == "budget_exhausted"
        assert len(calls) == 3


@pytest.mark.parametrize("name,args", [("unregistered", {"value": "test"}), ("lookup", {"value": 123}), ("lookup", {"value": "test", "unexpected": True})])
def test_unknown_tools_and_invalid_arguments_never_execute(tmp_path, name, args):
    effects = []
    decide = lambda view: AgentDecision(kind="tool", tool_call=ToolCall(name=name, arguments=args))
    with AgentRuntime(tmp_path / "runs.db", decider=decide, tools=[tool_spec(lambda arg: effects.append(1))]) as runtime:
        assert runtime.run(runtime.create("test").run_id).status == "failed"
        assert not effects


@pytest.mark.parametrize("source", ["query", "arguments", "output", "answer", "exception"])
def test_secret_shapes_never_enter_database(tmp_path, source):
    secret = "sk-" + "never_persist_this_value_" * 3
    def decide(view):
        if source == "answer":
            return AgentDecision(kind="final", answer=secret)
        if source == "exception":
            raise RuntimeError(secret)
        return AgentDecision(kind="tool", tool_call=ToolCall(name="lookup", arguments={"value": secret if source == "arguments" else "test"}))
    db = tmp_path / "runs.db"
    with AgentRuntime(db, decider=decide, tools=[tool_spec(lambda arg: secret if source == "output" else "ok")]) as runtime:
        if source == "query":
            with pytest.raises(RuntimeErrorBase, match="sensitive_content"):
                runtime.create(secret)
        else:
            assert runtime.run(runtime.create("test").run_id).status == "failed"
    for path in tmp_path.iterdir():
        assert secret.encode() not in path.read_bytes()


def test_output_budget_persists_across_restart(tmp_path):
    db = tmp_path / "runs.db"
    spec = tool_spec(lambda arg: "x" * 500, approval=True)
    with AgentRuntime(db, decider=once, tools=[spec], max_output_chars=100) as runtime:
        waiting = runtime.run(runtime.create("test").run_id)
    with AgentRuntime(db, decider=once, tools=[spec], max_output_chars=10000) as runtime:
        result = runtime.resume(waiting.run_id, ApprovalDecision(approval_id=waiting.pending.approval_id, approved=True))
        assert result.status == "failed"
        assert result.max_output_chars == 100
        assert not result.observations


def test_completed_tool_ledger_prevents_reexecution_after_checkpoint_gap(tmp_path):
    effects = []
    db = tmp_path / "runs.db"
    spec = tool_spec(lambda arg: effects.append(1) or "ok", approval=True)
    with AgentRuntime(db, decider=once, tools=[spec]) as runtime:
        waiting = runtime.run(runtime.create("test").run_id)
        approved = waiting.pending.model_copy(update={"approved": True})
        # Model a crash after execute's durable ledger write but before its graph checkpoint.
        state = waiting.model_dump(mode="json")
        state.update(status="running", pending=approved.model_dump(mode="json"))
        assert runtime._execute(state)["status"] == "running"
        runtime.graph.update_state(runtime._config(waiting.run_id), state, as_node="approval")
    with AgentRuntime(db, decider=once, tools=[spec]) as runtime:
        assert runtime.run(waiting.run_id).status == "completed"
        assert effects == [1]


def test_started_call_without_result_is_not_replayed(tmp_path):
    effects = []
    spec = tool_spec(lambda arg: effects.append(1) or "ok", approval=True)
    with AgentRuntime(tmp_path / "runs.db", decider=once, tools=[spec]) as runtime:
        waiting = runtime.run(runtime.create("test").run_id)
        runtime.connection.execute("INSERT INTO agent_tool_attempts VALUES (?, ?, ?, 'started', NULL)", (waiting.run_id, waiting.pending.call_id, "unknown"))
        runtime.connection.commit()
        final = runtime.resume(waiting.run_id, ApprovalDecision(approval_id=waiting.pending.approval_id, approved=True))
        assert final.error_code == "tool_outcome_indeterminate"
        assert not effects


def test_same_database_rejects_concurrent_operations(tmp_path):
    entered, release = Event(), Event()
    def blocking(view):
        entered.set()
        assert release.wait(5)
        return AgentDecision(kind="final", answer="done")
    db = tmp_path / "runs.db"
    with AgentRuntime(db, decider=blocking, tools=[]) as first, AgentRuntime(db, decider=once, tools=[]) as second:
        created = first.create("test")
        with ThreadPoolExecutor() as pool:
            future = pool.submit(first.run, created.run_id)
            try:
                assert entered.wait(3)
                with pytest.raises(RunBusyError):
                    first.get(created.run_id)
                with pytest.raises(RunBusyError):
                    second.cancel(created.run_id)
            finally:
                release.set()
            assert future.result().status == "completed"


def test_host_tracing_env_cannot_enable_agent_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "fake-for-test-only")
    import langsmith.run_trees
    monkeypatch.setattr(langsmith.run_trees.RunTree, "post", lambda *args, **kwargs: pytest.fail("telemetry was enabled"))
    observed = []
    def decide(view):
        observed.append(get_tracing_context()["enabled"])
        return once(view)
    def execute(arg):
        observed.append(get_tracing_context()["enabled"])
        return "ok"
    with AgentRuntime(tmp_path / "runs.db", decider=decide, tools=[tool_spec(execute)]) as runtime:
        created = runtime.create("test")
        assert runtime.run(created.run_id).status == "completed"
        runtime.get(created.run_id)
    assert observed and all(value is False for value in observed)
