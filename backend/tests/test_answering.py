from __future__ import annotations

import httpx
import pytest

from app.answering import AnswerService
from app.generation import DeepSeekGenerator, ModelAnswerDraft
from app.main import retriever
from app.models import CommandBlock


class FailedGenerator:
    available = True

    def generate(self, query, hits):
        raise ValueError("invalid JSON")


class SuccessfulGenerator:
    available = True

    def generate(self, query, hits):
        return ModelAnswerDraft(
            summary="使用 git revert 创建一个反向提交。",
            commands=[
                CommandBlock(
                    label="安全回滚",
                    code="git revert <commit>",
                    platforms=["Windows", "macOS", "Linux"],
                    prerequisites=["Git 仓库工作区干净"],
                )
            ],
            notes=["该方式保留历史。"],
        )


def _generator(handler: httpx.MockTransport) -> DeepSeekGenerator:
    return DeepSeekGenerator(
        api_key="test-key-not-a-secret",
        base_url="https://api.deepseek.example/v1/",
        model="deepseek-chat",
        timeout_seconds=2,
        transport=handler,
    )


def _git_hits():
    return retriever.retrieve("Git 安全回滚提交", top_k=4)


def test_invalid_model_output_falls_back_without_losing_citations() -> None:
    hits = retriever.retrieve("Git 安全回滚提交", top_k=4)
    answer, reason = AnswerService(FailedGenerator()).answer("Git 安全回滚提交", hits)
    assert answer.mode == "local_fallback"
    assert reason == "invalid_json"
    assert answer.generation.attempted is True
    assert answer.generation.fallback_reason == "invalid_json"
    assert answer.citations


def test_valid_model_output_is_structured_and_grounded() -> None:
    hits = retriever.retrieve("Git 安全回滚提交", top_k=4)
    answer, reason = AnswerService(SuccessfulGenerator()).answer("Git 安全回滚提交", hits)
    assert answer.mode == "model"
    assert reason == "model"
    assert answer.commands[0].code == "git revert <0c01a9>"
    assert answer.citations[0].source_id == "git.git-revert"


def test_model_commands_not_present_in_current_evidence_are_removed() -> None:
    class HallucinatingGenerator:
        available = True
        model = "deepseek-chat"

        def generate(self, query, hits):
            return ModelAnswerDraft(
                summary="模型给出了不在当前知识证据中的命令。",
                commands=[CommandBlock(label="危险历史命令", code="rm -rf /")],
            )

    answer, reason = AnswerService(HallucinatingGenerator()).answer("Git 安全回滚提交", _git_hits())

    assert reason == "model"
    assert answer.mode == "model"
    assert answer.commands == []


def test_deepseek_success_response_is_validated() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.deepseek.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key-not-a-secret"
        payload = {
            "id": "chatcmpl-test",
            "model": "deepseek-chat",
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
            "choices": [
                {
                    "message": {
                        "content": """```json
                        {
                          "summary": "使用反向提交安全撤销变更。",
                          "commands": [{
                            "label": "撤销提交",
                            "language": "bash",
                            "code": "git revert <commit>",
                            "platforms": ["Windows", "macOS", "Linux"],
                            "prerequisites": ["当前目录是 Git 仓库"],
                            "risk": "low",
                            "warning": null
                          }],
                          "notes": ["会保留历史记录。"]
                        }
                        ```"""
                    }
                }
            ]
        }
        return httpx.Response(200, json=payload)

    draft = _generator(httpx.MockTransport(respond)).generate("如何安全回滚？", _git_hits())
    assert draft.commands[0].code == "git revert <commit>"
    assert draft.summary == "使用反向提交安全撤销变更。"
    assert draft.generation.request_id == "chatcmpl-test"
    assert draft.generation.total_tokens == 100
    assert draft.generation.latency_ms > 0


def test_deepseek_timeout_is_exposed_for_fallback() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mock timeout", request=request)

    with pytest.raises(httpx.ReadTimeout):
        _generator(httpx.MockTransport(timeout)).generate("如何安全回滚？", _git_hits())


def test_deepseek_rate_limit_is_exposed_for_fallback() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(429, json={"error": {"message": "rate limited"}}))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        _generator(transport).generate("如何安全回滚？", _git_hits())
    assert caught.value.response.status_code == 429


def test_deepseek_invalid_json_is_rejected() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})
    )
    with pytest.raises(ValueError, match="结构化答案"):
        _generator(transport).generate("如何安全回滚？", _git_hits())


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ReadTimeout("timeout"), "timeout"),
        (httpx.ConnectError("dns"), "provider_error"),
    ],
)
def test_generation_failures_are_normalized(failure: Exception, expected: str) -> None:
    request = httpx.Request("POST", "https://api.deepseek.example/v1/chat/completions")
    if isinstance(failure, httpx.RequestError):
        failure._request = request

    class Generator:
        available = True
        model = "deepseek-chat"

        def generate(self, query, hits):
            raise failure

    answer, reason = AnswerService(Generator()).answer("Git 安全回滚", _git_hits())
    assert reason == expected
    assert answer.generation.fallback_reason == expected
    assert answer.generation.attempted is True


@pytest.mark.parametrize(("status_code", "expected"), [(401, "authentication"), (403, "authentication"), (429, "rate_limit"), (500, "provider_error")])
def test_http_failures_are_normalized(status_code: int, expected: str) -> None:
    request = httpx.Request("POST", "https://api.deepseek.example/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    failure = httpx.HTTPStatusError("failed", request=request, response=response)

    class Generator:
        available = True
        model = "deepseek-chat"

        def generate(self, query, hits):
            raise failure

    answer, reason = AnswerService(Generator()).answer("Git 安全回滚", _git_hits())
    assert reason == expected
    assert answer.generation.fallback_reason == expected
