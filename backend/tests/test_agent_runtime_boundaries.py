"""Independent integration checks for the M1 persistence boundary."""
import subprocess
import sys

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent_runtime import (
    AgentDecision, AgentRuntime, ApprovalDecision, RunBusyError, RuntimeErrorBase, ToolCall, ToolSpec,
)


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


def decide(view):
    if view.observations:
        return AgentDecision(kind="final", answer=view.observations[0].output)
    return AgentDecision(kind="tool", tool_call=ToolCall(
        name="echo", arguments={"value": view.query}))


def spec(effects, output=None):
    def execute(args):
        effects.append(args.value)
        return args.value if output is None else output
    return ToolSpec(name="echo", arguments_model=Args, executor=execute,
                    requires_approval=True, revision="boundary-v1")


def test_runs_and_approval_ids_remain_isolated_after_restart(tmp_path):
    db, effects = tmp_path / "agent.db", []
    with AgentRuntime(db, decider=decide, tools=[spec(effects)]) as runtime:
        first = runtime.run(runtime.create("first request").run_id)
        second = runtime.run(runtime.create("second request").run_id)
    with AgentRuntime(db, decider=decide, tools=[spec(effects)]) as runtime:
        with pytest.raises(RuntimeErrorBase, match="approval_mismatch"):
            runtime.resume(second.run_id, ApprovalDecision(
                approval_id=first.pending.approval_id, approved=True))
        assert effects == []
        final = runtime.resume(second.run_id, ApprovalDecision(
            approval_id=second.pending.approval_id, approved=True))
        assert final.answer == "second request"
        assert effects == ["second request"]
        untouched = runtime.get(first.run_id)
        assert untouched.status == "awaiting_approval"
        assert untouched.observations == []
        assert runtime.cancel(first.run_id).status == "cancelled"
        assert runtime.get(second.run_id).answer == final.answer


def test_cancelled_created_run_never_calls_decider_after_restart(tmp_path):
    db = tmp_path / "agent.db"
    def forbidden(_):
        pytest.fail("cancelled run invoked a callback")
    with AgentRuntime(db, decider=forbidden, tools=[]) as runtime:
        created = runtime.create("queued request")
        runtime.cancel(created.run_id)
    with AgentRuntime(db, decider=forbidden, tools=[]) as runtime:
        assert runtime.get(created.run_id).status == "cancelled"
        with pytest.raises(RuntimeErrorBase, match="not_runnable"):
            runtime.run(created.run_id)


@pytest.mark.parametrize("output", [float("nan"), float("inf"), {"password": "fixture-only"}])
def test_invalid_tool_result_is_not_checkpointed_or_retried(tmp_path, output):
    db, effects = tmp_path / "agent.db", []
    with AgentRuntime(db, decider=decide, tools=[spec(effects, output)]) as runtime:
        waiting = runtime.run(runtime.create("safe request").run_id)
        final = runtime.resume(waiting.run_id, ApprovalDecision(
            approval_id=waiting.pending.approval_id, approved=True))
        assert final.status == "failed"
        assert final.error_code == "tool_execution_failed"
        assert final.observations == []
        row = runtime.connection.execute(
            "SELECT status, output FROM agent_tool_attempts WHERE run_id=?", (final.run_id,)
        ).fetchone()
        assert row == ("started", None)
    with AgentRuntime(db, decider=decide, tools=[spec(effects, output)]) as runtime:
        with pytest.raises(RuntimeErrorBase, match="not_runnable"):
            runtime.run(final.run_id)
        assert effects == ["safe request"]


@pytest.mark.parametrize("budget", [0, 65, True, 1.5])
def test_invalid_budget_rejected_before_database_creation(tmp_path, budget):
    db = tmp_path / "agent.db"
    with pytest.raises(RuntimeErrorBase, match="invalid_step_budget"):
        AgentRuntime(db, decider=decide, tools=[], max_steps=budget)
    assert not db.exists()


def test_plain_model_text_cannot_be_interpreted_as_a_tool(tmp_path):
    effects = []
    with AgentRuntime(tmp_path / "agent.db", decider=lambda _: "echo arbitrary text",
                      tools=[spec(effects)]) as runtime:
        final = runtime.run(runtime.create("safe request").run_id)
        assert final.status == "failed"
        assert final.error_code == "invalid_decision"
        assert not effects


@pytest.mark.parametrize("construction", ["copy", "construct", "dict"])
def test_unvalidated_approval_cannot_enter_checkpoint(tmp_path, construction):
    effects, db = [], tmp_path / "agent.db"
    secret = "sk-" + "fixture_unvalidated_approval_" * 3
    with AgentRuntime(db, decider=decide, tools=[spec(effects)]) as runtime:
        waiting = runtime.run(runtime.create("safe request").run_id)
        values = {"approval_id": waiting.pending.approval_id, "approved": secret}
        if construction == "copy":
            approval = ApprovalDecision(approval_id=waiting.pending.approval_id,
                                        approved=False).model_copy(update=values)
        elif construction == "construct":
            approval = ApprovalDecision.model_construct(**values)
        else:
            approval = values
        with pytest.raises(RuntimeErrorBase, match="^invalid_approval$") as error:
            runtime.resume(waiting.run_id, approval)
        assert secret not in str(error.value)
        assert runtime.get(waiting.run_id).status == "awaiting_approval"
        assert effects == []
    for path in tmp_path.iterdir():
        assert secret.encode() not in path.read_bytes()


@pytest.mark.parametrize("construction", ["nested", "final"])
def test_unvalidated_model_instances_are_rechecked(tmp_path, construction):
    effects, db = [], tmp_path / "agent.db"
    if construction == "nested":
        malformed = AgentDecision(kind="tool", tool_call=ToolCall(name="echo",
            arguments={"value": "safe"})).model_copy(update={"tool_call":
                ToolCall.model_construct(name="echo", arguments="not an object")})
    else:
        malformed = AgentDecision.model_construct(kind="final", answer=123, tool_call=None)
    with AgentRuntime(db, decider=lambda _: malformed, tools=[spec(effects)]) as runtime:
        result = runtime.run(runtime.create("safe request").run_id)
        assert result.status == "failed"
        assert result.error_code == "invalid_decision"
        assert result.answer is None
        assert not effects


def test_non_idempotent_normalization_cannot_change_approved_arguments(tmp_path):
    class ChangingArgs(Args):
        @field_validator("value")
        @classmethod
        def change(cls, value):
            return value + "!"
    effects = []
    tool = ToolSpec(name="echo", arguments_model=ChangingArgs,
                    executor=lambda args: effects.append(args.value) or args.value,
                    requires_approval=True, revision="validator-v1")
    with AgentRuntime(tmp_path / "agent.db", decider=decide, tools=[tool]) as runtime:
        waiting = runtime.run(runtime.create("safe request").run_id)
        assert waiting.pending.tool_call.arguments == {"value": "safe request!"}
        result = runtime.resume(waiting.run_id, ApprovalDecision(
            approval_id=waiting.pending.approval_id, approved=True))
        assert result.error_code == "tool_arguments_changed"
        assert effects == []
        assert runtime.connection.execute("SELECT count(*) FROM agent_tool_attempts").fetchone()[0] == 0


def test_published_argument_alias_is_preserved_through_approval(tmp_path):
    class AliasedArgs(Args):
        value: str = Field(alias="query")
    tool = ToolSpec(name="echo", arguments_model=AliasedArgs,
                    executor=lambda args: args.value,
                    requires_approval=True, revision="alias-v1")
    def choose(view):
        if view.observations:
            return AgentDecision(kind="final", answer=view.observations[0].output)
        return AgentDecision(kind="tool", tool_call=ToolCall(name="echo",
                             arguments={"query": view.query}))
    with AgentRuntime(tmp_path / "agent.db", decider=choose, tools=[tool]) as runtime:
        waiting = runtime.run(runtime.create("safe alias request").run_id)
        assert waiting.pending.tool_call.arguments == {"query": "safe alias request"}
        final = runtime.resume(waiting.run_id, ApprovalDecision(
            approval_id=waiting.pending.approval_id, approved=True))
        assert final.answer == "safe alias request"


def test_tool_schema_cannot_silently_ignore_unknown_arguments():
    class IgnoringArgs(BaseModel):
        value: str
    with pytest.raises(ValidationError, match="tool_schema_must_forbid"):
        ToolSpec(name="echo", arguments_model=IgnoringArgs, executor=lambda _: "ok", revision="1")


def test_other_process_holding_database_lock_blocks_mutations(tmp_path):
    db = tmp_path / "agent.db"
    with AgentRuntime(db, decider=decide, tools=[]) as runtime:
        created = runtime.create("queued request")
        worker = subprocess.Popen([sys.executable, "-u", "-c",
            "import sys; from filelock import FileLock; "
            "lock=FileLock(sys.argv[1]); lock.acquire(timeout=3); "
            "print('locked', flush=True); input(); lock.release()",
            str(db) + ".agent.lock"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            assert worker.stdout.readline().strip() == "locked"
            with pytest.raises(RunBusyError, match="runtime_busy"):
                runtime.cancel(created.run_id)
        finally:
            worker.communicate("\n", timeout=5)
        assert worker.returncode == 0
        assert runtime.get(created.run_id).status == "created"
        assert runtime.cancel(created.run_id).status == "cancelled"
