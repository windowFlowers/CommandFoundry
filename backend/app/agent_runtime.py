"""Local, bounded LangGraph runtime; no provider, shell or UI integration.

All operations use a fail-fast database-wide file lock (also across processes).
Cancel applies to idle/interrupted runs; it cannot interrupt a running callback.
The execution ledger never retries a started call without a recorded outcome:
after a crash that window is indeterminate and requires human reconciliation.
This is an at-most-once attempt policy, not an exactly-once side-effect promise.
Tool implementers must change ``revision`` whenever executor behavior changes.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Literal, TypedDict
from uuid import uuid4

from filelock import FileLock, Timeout
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, field_validator, model_validator

from .command_planner import contains_sensitive_input
from .evaluation_gate import find_credential_leaks, redact_report


class RuntimeErrorBase(ValueError):
    """Public errors contain a fixed code, never callback inputs or exceptions."""


class RunBusyError(RuntimeErrorBase):
    pass


DecisionFailureCode = Literal["provider_timeout", "provider_rate_limit", "provider_authentication",
                              "provider_error", "invalid_model_decision"]


class DecisionError(RuntimeErrorBase):
    """A provider failure with a fixed public code, never its raw exception."""
    def __init__(self, code: DecisionFailureCode):
        if code not in {"provider_timeout", "provider_rate_limit", "provider_authentication",
                        "provider_error", "invalid_model_decision"}:
            raise RuntimeErrorBase("invalid_error_code")
        self.code = code
        super().__init__(code)


class Contract(BaseModel):
    # Pydantic otherwise trusts existing instances, including model_copy and
    # model_construct values that never passed their field validators.
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


class ToolCall(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class AgentDecision(Contract):
    kind: Literal["tool", "final"]
    tool_call: ToolCall | None = None
    answer: str | None = None

    @model_validator(mode="after")
    def check_kind(self):
        if self.kind == "tool" and (self.tool_call is None or self.answer is not None):
            raise ValueError("invalid_tool_decision")
        if self.kind == "final" and (self.tool_call is not None or not self.answer):
            raise ValueError("invalid_final_decision")
        return self


class ToolSpec(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    arguments_model: type[BaseModel]
    executor: Callable[[BaseModel], JsonValue]
    requires_approval: StrictBool = False
    revision: str = Field(min_length=1, max_length=80)

    @field_validator("arguments_model")
    @classmethod
    def check_arguments_model(cls, value):
        if value.model_config.get("extra") != "forbid":
            raise ValueError("tool_schema_must_forbid_extra_arguments")
        return value

    def fingerprint(self) -> str:
        return _hash({"name": self.name, "schema": self.arguments_model.model_json_schema(),
                      "approval": self.requires_approval, "revision": self.revision})


class Observation(Contract):
    call_id: str
    name: str
    output: JsonValue
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class PendingCall(Contract):
    call_id: str
    tool_call: ToolCall
    tool_fingerprint: str
    approval_id: str
    requires_approval: StrictBool
    approved: StrictBool = False


class ApprovalDecision(Contract):
    approval_id: str
    approved: StrictBool


class AgentView(Contract):
    query: str
    observations: list[Observation]
    steps: int
    max_steps: int


class AgentRun(AgentView):
    run_id: str
    status: Literal["created", "running", "awaiting_approval", "completed", "failed", "cancelled", "budget_exhausted"]
    max_output_chars: int
    pending: PendingCall | None = None
    answer: str | None = None
    error_code: str | None = None
    scope_fingerprint: str = ""


class _State(TypedDict):
    run_id: str
    query: str
    observations: list[dict]
    steps: int
    max_steps: int
    max_output_chars: int
    status: str
    pending: dict | None
    answer: str | None
    error_code: str | None
    scope_fingerprint: str


_TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted"}
_SECRET_KEY = re.compile(r"password|passwd|secret|token|credential|authorization|api.?key|private.?key", re.I)
_TOKEN_METRICS = {"prompt_tokens", "completion_tokens", "total_tokens", "estimated_tokens"}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _safe(value, *, limit: int) -> None:
    """Reject, rather than silently changing tool parameters or model facts."""
    serialized = _json(value)
    if len(serialized) > limit:
        raise RuntimeErrorBase("content_limit")
    def sensitive_keys(item):
        if isinstance(item, dict):
            return any((bool(_SECRET_KEY.search(str(key))) and not (
                key in _TOKEN_METRICS and (child is None or type(child) is int and child >= 0)))
                or sensitive_keys(child) for key, child in item.items())
        return isinstance(item, list) and any(sensitive_keys(child) for child in item)
    if (sensitive_keys(value) or contains_sensitive_input(serialized)
            or find_credential_leaks({"value": value}) or redact_report(value) != value):
        raise RuntimeErrorBase("sensitive_content")


class AgentRuntime:
    def __init__(self, database_path: Path, *, decider: Callable[[AgentView], AgentDecision],
                 tools: list[ToolSpec], max_steps: int = 8, max_output_chars: int = 16000,
                 scope_fingerprint: str = "", before_tool: Callable[[AgentView], None] | None = None):
        if type(max_steps) is not int or not 1 <= max_steps <= 64:
            raise RuntimeErrorBase("invalid_step_budget")
        if type(max_output_chars) is not int or not 64 <= max_output_chars <= 100000:
            raise RuntimeErrorBase("invalid_output_budget")
        if not isinstance(scope_fingerprint, str) or (scope_fingerprint and not re.fullmatch(r"[a-f0-9]{64}", scope_fingerprint)):
            raise RuntimeErrorBase("invalid_scope")
        self.scope_fingerprint = scope_fingerprint
        self.before_tool = before_tool
        self.path = Path(database_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.decider = decider
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise RuntimeErrorBase("duplicate_tool_name")
        self.max_steps, self.max_output_chars = max_steps, max_output_chars
        self.closed = False
        with self._locked():
            self.connection = sqlite3.connect(str(self.path), check_same_thread=False)
            self.connection.execute("CREATE TABLE IF NOT EXISTS agent_tool_attempts (run_id TEXT, call_id TEXT, fingerprint TEXT NOT NULL, status TEXT NOT NULL, output TEXT, PRIMARY KEY(run_id, call_id))")
            self.connection.commit()
            self.saver = SqliteSaver(self.connection)
            self.saver.setup()
        builder = StateGraph(_State)
        builder.add_node("create", lambda state: {})
        builder.add_node("decide", self._decide)
        builder.add_node("approval", self._approval)
        builder.add_node("execute", self._execute)
        builder.add_node("finish", lambda state: {})
        builder.add_edge(START, "create")
        builder.add_edge("create", "decide")
        builder.add_conditional_edges("decide", lambda state: "approval" if state["pending"] else END)
        builder.add_conditional_edges("approval", lambda state: "execute" if state["status"] == "running" else END)
        builder.add_conditional_edges("execute", lambda state: "decide" if state["status"] == "running" else END)
        builder.add_edge("finish", END)
        self.graph = builder.compile(checkpointer=self.saver)

    @contextmanager
    def _locked(self):
        if self.closed:
            raise RuntimeErrorBase("runtime_closed")
        # FileLock's per-instance reentrancy is not a concurrency guard; use a
        # fresh instance each operation so another thread must acquire OS lock.
        lock = FileLock(str(self.path) + ".agent.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise RunBusyError("runtime_busy") from None
        try:
            with tracing_context(enabled=False):
                yield
        finally:
            lock.release()

    @staticmethod
    def _config(run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise RuntimeErrorBase("invalid_run_id")
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 300}

    def _get(self, run_id) -> AgentRun:
        snapshot = self.graph.get_state(self._config(run_id))
        if not snapshot.values:
            raise RuntimeErrorBase("run_not_found")
        value = dict(snapshot.values)
        if value["status"] not in _TERMINAL and any(task.interrupts for task in snapshot.tasks):
            value["status"] = "awaiting_approval"
        run = AgentRun.model_validate(value)
        if run.scope_fingerprint != self.scope_fingerprint:
            raise RuntimeErrorBase("run_scope_mismatch")
        return run

    def create(self, query: str) -> AgentRun:
        if not isinstance(query, str) or not query.strip():
            raise RuntimeErrorBase("invalid_query")
        _safe(query, limit=8192)
        run = AgentRun(run_id=uuid4().hex, query=query, observations=[], steps=0,
                       max_steps=self.max_steps, max_output_chars=self.max_output_chars,
                       status="created", scope_fingerprint=self.scope_fingerprint)
        with self._locked():
            self.graph.update_state(self._config(run.run_id), run.model_dump(mode="json"), as_node="create")
        return run

    def get(self, run_id: str) -> AgentRun:
        with self._locked():
            return self._get(run_id)

    def run(self, run_id: str) -> AgentRun:
        with self._locked():
            run = self._get(run_id)
            if run.status in _TERMINAL or run.status == "awaiting_approval":
                raise RuntimeErrorBase("run_not_runnable")
            self.graph.invoke(None, self._config(run_id), durability="sync")
            return self._get(run_id)

    def resume(self, run_id: str, decision: ApprovalDecision) -> AgentRun:
        try:
            decision = ApprovalDecision.model_validate(decision)
            payload = decision.model_dump(mode="json")
            _safe(payload, limit=8192)
        except Exception:
            # Never expose a ValidationError containing approval input or pass
            # unchecked values to Command: resume values are checkpointed.
            raise RuntimeErrorBase("invalid_approval") from None
        with self._locked():
            run = self._get(run_id)
            if run.status != "awaiting_approval" or run.pending is None:
                raise RuntimeErrorBase("run_not_awaiting_approval")
            if decision.approval_id != run.pending.approval_id:
                raise RuntimeErrorBase("approval_mismatch")
            self.graph.invoke(Command(resume=payload), self._config(run_id), durability="sync")
            return self._get(run_id)

    def cancel(self, run_id: str) -> AgentRun:
        with self._locked():
            run = self._get(run_id)
            if run.status in _TERMINAL:
                raise RuntimeErrorBase("run_already_terminal")
            self.graph.update_state(self._config(run_id), {"status": "cancelled", "pending": None}, as_node="finish")
            return self._get(run_id)

    def _tool(self, pending: PendingCall) -> ToolSpec:
        tool = self.tools.get(pending.tool_call.name)
        if tool is None or tool.fingerprint() != pending.tool_fingerprint:
            raise RuntimeErrorBase("tool_contract_changed")
        return tool

    @staticmethod
    def _failed(code: str):
        return {"status": "failed", "error_code": code, "pending": None}

    def _decide(self, state):
        if state["steps"] >= state["max_steps"]:
            return {"status": "budget_exhausted", "pending": None}
        try:
            view = AgentView.model_validate({key: state[key] for key in AgentView.model_fields})
            decision = AgentDecision.model_validate(self.decider(view))
            if decision.kind == "final":
                _safe(decision.answer, limit=state["max_output_chars"])
                return {"status": "completed", "answer": decision.answer, "pending": None, "steps": state["steps"] + 1}
            call = decision.tool_call
            _safe(call.model_dump(mode="json"), limit=8192)
            tool = self.tools.get(call.name)
            if tool is None:
                return self._failed("unknown_tool")
            args = tool.arguments_model.model_validate(call.arguments, strict=True)
            # Do not allow host schema coercion/defaults to smuggle secrets.
            arguments = args.model_dump(mode="json", by_alias=True)
            _safe(arguments, limit=8192)
            pending = PendingCall(call_id=uuid4().hex,
                tool_call=ToolCall(name=call.name, arguments=arguments),
                tool_fingerprint=tool.fingerprint(), approval_id=uuid4().hex,
                requires_approval=tool.requires_approval)
            return {"status": "running", "steps": state["steps"] + 1, "pending": pending.model_dump(mode="json")}
        except DecisionError as exc:
            return self._failed(exc.code)
        except Exception:
            return self._failed("invalid_decision")

    def _approval(self, state):
        pending = PendingCall.model_validate(state["pending"])
        try:
            self._tool(pending)
        except Exception:
            return self._failed("tool_contract_changed")
        if pending.requires_approval:
            response = interrupt({"approval_id": pending.approval_id, "tool_call": pending.tool_call.model_dump(mode="json")})
            try:
                decision = ApprovalDecision.model_validate(response)
                if decision.approval_id != pending.approval_id:
                    return self._failed("approval_mismatch")
                if not decision.approved:
                    return {"status": "cancelled", "pending": None}
            except Exception:
                return self._failed("invalid_approval")
        pending.approved = True
        return {"status": "running", "pending": pending.model_dump(mode="json")}

    def _execute(self, state):
        pending = PendingCall.model_validate(state["pending"])
        try:
            if self.before_tool:
                self.before_tool(AgentView.model_validate({key: state[key] for key in AgentView.model_fields}))
            tool = self._tool(pending)
            if not pending.approved:
                return self._failed("approval_required")
            args = tool.arguments_model.model_validate(pending.tool_call.arguments, strict=True)
            normalized = args.model_dump(mode="json", by_alias=True)
            _safe(normalized, limit=8192)
            if _json(normalized) != _json(pending.tool_call.arguments):
                return self._failed("tool_arguments_changed")
            fingerprint = _hash(pending.model_dump(mode="json"))
            # SqliteSaver has its own background checkpoint writes. Share its
            # cursor lock so ledger commits never interleave on this connection.
            with self.saver.cursor() as cursor:
                row = cursor.execute("SELECT fingerprint, status, output FROM agent_tool_attempts WHERE run_id=? AND call_id=?", (state["run_id"], pending.call_id)).fetchone()
            if row:
                if row[0] != fingerprint or row[1] != "completed":
                    return self._failed("tool_outcome_indeterminate")
                output = json.loads(row[2])
            else:
                with self.saver.cursor() as cursor:
                    cursor.execute("INSERT INTO agent_tool_attempts VALUES (?, ?, ?, 'started', NULL)", (state["run_id"], pending.call_id, fingerprint))
                output = tool.executor(args)
                _safe(output, limit=state["max_output_chars"])
                observation = Observation(call_id=pending.call_id, name=tool.name, output=output,
                                          arguments=pending.tool_call.arguments)
                output = observation.model_dump(mode="json")["output"]
                with self.saver.cursor() as cursor:
                    cursor.execute("UPDATE agent_tool_attempts SET status='completed', output=? WHERE run_id=? AND call_id=?", (_json(output), state["run_id"], pending.call_id))
            observation = Observation(call_id=pending.call_id, name=tool.name, output=output,
                                      arguments=pending.tool_call.arguments)
            return {"status": "running", "pending": None, "observations": [*state["observations"], observation.model_dump(mode="json")]}
        except DecisionError as exc:
            return self._failed(exc.code)
        except Exception:
            return self._failed("tool_execution_failed")

    def close(self):
        if not self.closed:
            with self._locked():
                self.connection.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
