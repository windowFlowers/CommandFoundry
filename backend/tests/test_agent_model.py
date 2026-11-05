"""Real OpenAI SDK requests against a fake transport, never a live provider."""
import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from app.agent_model import AgentModelDecider
from app.agent_runtime import AgentView, DecisionError, Observation, ToolSpec
from app.generation import OpenAICompatibleGenerator


class SearchArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    query: str


def spec(name="knowledge_search"):
    return ToolSpec(name=name, arguments_model=SearchArgs, executor=lambda _: "unused", revision="1")


def view(observations=None):
    return AgentView(query="Find the command", observations=observations or [], steps=0, max_steps=8)


def tool_call(name="knowledge_search", arguments=None):
    return {"id": "provider_tool_id", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {"query": "explicit words"})}}


def response(calls=None, *, content=None, finish_reason="tool_calls"):
    return {"id": "provider_response_id", "object": "chat.completion", "created": 1,
        "model": "test-model", "choices": [{"index": 0, "finish_reason": finish_reason,
            "message": {"role": "assistant", "content": content,
                        "tool_calls": calls if calls is not None else [tool_call()]}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}}


def make(handler, *, tools=None, resolver=None):
    generator = OpenAICompatibleGenerator(api_key="sk-fixture-secret-not-real-123456789",
        base_url="https://example.test/v1", model="test-model", provider="custom",
        timeout_seconds=7, transport=httpx.MockTransport(handler))
    return AgentModelDecider(generator, tools if tools is not None else [spec()],
        resolver or (lambda state, call_id: "host verified answer"))


def test_native_sdk_request_has_strict_tools_and_required_choice():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response())
    decider = make(handler)
    result = decider(view())
    assert result.kind == "tool"
    assert result.tool_call.name == "knowledge_search"
    assert result.tool_call.arguments == {"query": "explicit words"}
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/chat/completions"
    assert payload["tool_choice"] == "required"
    assert payload["parallel_tool_calls"] is False
    assert all(tool["function"]["strict"] for tool in payload["tools"])
    assert {item["function"]["name"] for item in payload["tools"]} == {"knowledge_search", "agent_finish"}
    assert payload["messages"][1] == {"role": "user", "content": "Find the command"}
    assert "不可信" in payload["messages"][0]["content"]
    assert decider.calls[0]["total_tokens"] == 15
    assert decider.calls[0]["error_code"] is None
    assert "sk-fixture" not in json.dumps(decider.calls)
    assert "Find the command" not in json.dumps(decider.calls)
    assert requests[0].extensions["timeout"]["read"] == 7


def test_tool_history_reuses_stored_ids_and_arguments():
    seen = []
    stored = Observation(call_id="stored_call_1", name="knowledge_search",
        arguments={"query": "original words"}, output={"excerpt": "untrusted fact"})
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response([tool_call("agent_finish", {"answer_call_id": stored.call_id})], content="untrusted model command"))
    resolved = []
    def resolve(state, call_id):
        resolved.append((state, call_id))
        return "host verified answer"
    decider = make(handler, resolver=resolve)
    result = decider(view([stored]))
    assert result.answer == "host verified answer"
    assert resolved[0][1] == stored.call_id
    messages = seen[0]["messages"]
    assert messages[2]["tool_calls"][0]["id"] == stored.call_id
    assert json.loads(messages[2]["tool_calls"][0]["function"]["arguments"]) == stored.arguments
    assert messages[3] == {"role": "tool", "tool_call_id": stored.call_id, "content": json.dumps(stored.output)}
    assert "untrusted model command" not in json.dumps(decider.calls)


@pytest.mark.parametrize("kind", ["none", "multiple", "unknown", "invalid_json", "non_object", "duplicate_key", "nan", "wrong_type", "extra_arg", "nonstandard_name", "truncated", "multiple_choices"])
def test_invalid_native_tool_responses_fail_closed(kind):
    payload = response()
    message = payload["choices"][0]["message"]
    if kind == "none":
        message.update(tool_calls=[], content="Run this unverified command")
        payload["choices"][0]["finish_reason"] = "stop"
    elif kind == "multiple":
        message["tool_calls"].append(tool_call())
    elif kind == "unknown":
        message["tool_calls"] = [tool_call("arbitrary_execute")]
    elif kind == "nonstandard_name":
        message["tool_calls"] = [tool_call("knowledge.search")]
    elif kind == "truncated":
        payload["choices"][0]["finish_reason"] = "length"
    elif kind == "multiple_choices":
        payload["choices"].append(payload["choices"][0])
    else:
        arguments = {
            "invalid_json": "not json", "non_object": "[]",
            "duplicate_key": '{"query":"first","query":"second"}',
            "nan": '{"query":NaN}', "wrong_type": '{"query":42}',
            "extra_arg": '{"query":"safe","knowledge_base_id":"other"}',
        }[kind]
        message["tool_calls"][0]["function"]["arguments"] = arguments
    decider = make(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(DecisionError) as error:
        decider(view())
    assert error.value.code == "invalid_model_decision"
    assert decider.calls[-1]["attempted"] is True
    assert decider.calls[-1]["error_code"] == "invalid_model_decision"


@pytest.mark.parametrize("failure", ["timeout", "connection", "rate_limit", "authentication", "server"])
def test_sdk_provider_failures_are_classified_without_retry_or_raw_errors(failure):
    count = []
    secret = "sk-fixture-error-message-123456789"
    def handler(request):
        count.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout(secret, request=request)
        if failure == "connection":
            raise httpx.ConnectError(secret, request=request)
        return httpx.Response({"rate_limit": 429, "authentication": 401, "server": 500}[failure], json={"error": {"message": secret}})
    decider = make(handler)
    with pytest.raises(DecisionError) as error:
        decider(view())
    expected = {"timeout": "provider_timeout", "connection": "provider_error", "rate_limit": "provider_rate_limit",
                "authentication": "provider_authentication", "server": "provider_error"}[failure]
    assert error.value.code == expected
    assert len(count) == 1
    assert secret not in str(error.value)
    assert secret not in json.dumps(decider.calls)


def test_unsupported_tool_protocol_does_not_retry_as_plain_text():
    count = []
    def handler(request):
        count.append(1)
        return httpx.Response(400, json={"error": {"message": "tools unsupported"}})
    decider = make(handler)
    with pytest.raises(DecisionError) as error:
        decider(view())
    assert error.value.code == "provider_error"
    assert count == [1]


@pytest.mark.parametrize("reason", ["missing_observation", "resolver_rejection", "extra_answer"])
def test_fake_finish_cannot_create_final_answer(reason):
    stored = Observation(call_id="stored_call", name="knowledge_search", arguments={"query": "q"}, output="fact")
    arguments = {"answer_call_id": "invented" if reason == "missing_observation" else stored.call_id}
    if reason == "extra_answer":
        arguments["answer"] = "arbitrary command"
    def reject(state, call_id):
        raise ValueError("host refused this unverified answer")
    decider = make(lambda request: httpx.Response(200, json=response([tool_call("agent_finish", arguments)])),
                   resolver=reject if reason == "resolver_rejection" else None)
    with pytest.raises(DecisionError) as error:
        decider(view([stored]))
    assert error.value.code == "invalid_model_decision"


@pytest.mark.parametrize("name", ["agent_finish", "knowledge.search"])
def test_reserved_or_nonstandard_registry_name_rejected(name):
    with pytest.raises(DecisionError, match="invalid_model_decision"):
        make(lambda request: pytest.fail("unexpected request"), tools=[spec(name)])


@pytest.mark.parametrize("source", ["input", "history", "model_arguments"])
def test_secret_shapes_do_not_enter_observations_or_provider_input(source):
    secret = "sk-fixture-secret-12345678901234567890123"
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response([tool_call(arguments={"query": secret})]))
    decider = make(handler)
    state = view()
    if source == "input":
        state.query = secret
    elif source == "history":
        state.observations = [Observation(call_id="old", name="knowledge_search", output=secret)]
    with pytest.raises(DecisionError):
        decider(state)
    assert secret not in json.dumps(decider.calls)
    assert len(requests) == (1 if source == "model_arguments" else 0)


def test_observation_list_has_only_bounded_safe_scalar_metadata():
    decider = make(lambda request: httpx.Response(200, json=response()))
    decider(view())
    assert set(decider.calls[0]) == {"attempted", "error_code", "latency_ms", "prompt_tokens", "completion_tokens", "total_tokens", "failure_stage"}
    assert all(not isinstance(value, (dict, list)) for value in decider.calls[0].values())
