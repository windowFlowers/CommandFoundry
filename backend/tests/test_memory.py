from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.answering import AnswerService
from app.generation import DeepSeekGenerator
from app.main import retriever
from app.memory import (
    ConversationMemoryService,
    ConversationTurn,
    build_local_summary,
    estimate_tokens,
    merge_contextual_hits,
    resolve_contextual_query,
)
from app.models import Answer, ChatMessage, CommandBlock, MemorySummary
from app.repository import ConversationRepository


class OfflineGenerator:
    available = False
    model = "deepseek-chat"


class SummaryGenerator:
    available = True
    model = "deepseek-chat"

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def summarize_memory(self, payload: dict) -> MemorySummary:
        self.calls += 1
        if self.failure:
            raise self.failure
        return MemorySummary(
            current_goal="排查服务端口",
            environments=["Windows"],
            constraints=["不要终止进程"],
            history_topics=["端口占用"],
        )


class CountingGenerator(SummaryGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.answer_calls = 0

    def generate(self, query, hits, *, memory_context=""):
        self.answer_calls += 1
        from app.generation import ModelAnswerDraft

        return ModelAnswerDraft(summary="已根据知识库整理。", commands=hits[0].topic.commands[:1])


def _answer(*labels: str, summary: str = "已找到可执行方案。") -> Answer:
    return Answer(
        mode="local_fallback",
        summary=summary,
        commands=[CommandBlock(label=label, code="echo <value>") for label in labels],
        notes=["先确认当前环境。"],
        citations=[],
    )


def _append_turn(
    repository: ConversationRepository,
    conversation_id: str,
    query: str,
    *labels: str,
    summary: str = "已找到可执行方案。",
) -> tuple[ChatMessage, ChatMessage]:
    user = repository.append_user(conversation_id, query)
    assistant = repository.append_answer(conversation_id, _answer(*labels, summary=summary))
    return user, assistant


def _turn(previous_query: str, *labels: str) -> ConversationTurn:
    return ConversationTurn(
        user=ChatMessage(role="user", query=previous_query),
        assistant=ChatMessage(role="assistant", answer=_answer(*labels)),
    )


def test_local_reference_resolution_handles_platform_pointer_and_new_topic() -> None:
    linux = _turn("Linux 怎么查看端口占用？", "查看监听端口", "查看占用进程")
    assert resolve_contextual_query("那 Windows 呢？", linux).startswith("Windows 怎么查看端口占用")

    git = _turn("Git 怎么回滚一次提交？", "git revert 安全撤销", "git reset --hard 强制重置")
    pointer = resolve_contextual_query("第二种会丢代码吗？", git)
    assert pointer.startswith("Git；")
    assert "git reset --hard 强制重置" in pointer

    explicit = "Docker 怎么查看容器日志？"
    assert resolve_contextual_query(explicit, linux) == explicit


@pytest.mark.parametrize(
    ("previous", "labels", "follow_up", "expected"),
    [
        ("Linux 怎么查看端口占用？", ["ss 查看监听端口"], "那 Windows 呢？", "windows.netstat"),
        (
            "Git 怎么回滚一次提交？",
            ["git revert 安全撤销", "git reset --hard 强制重置"],
            "第二种会丢代码吗？",
            "git.git-reset",
        ),
        ("MySQL 中怎么创建一个视图？", ["创建 MySQL 视图"], "PostgreSQL 怎么写？", "sql.postgres-create-view"),
    ],
)
def test_core_follow_ups_retrieve_the_expected_topic_first(
    previous: str,
    labels: list[str],
    follow_up: str,
    expected: str,
) -> None:
    contextual = resolve_contextual_query(follow_up, _turn(previous, *labels))
    hits = merge_contextual_hits(
        retriever.retrieve(follow_up, top_k=8),
        retriever.retrieve(contextual, top_k=8),
        top_k=4,
    )
    assert hits[0].topic.id == expected


def test_60_case_multi_turn_evaluation_meets_top_three_target() -> None:
    dataset = json.loads(
        (Path(__file__).resolve().parents[2] / "knowledge" / "memory_eval_dataset.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(dataset) == 60
    assert {case["category"] for case in dataset} >= {
        "platform-replace",
        "ordinal-pointer",
        "risk-pronoun",
        "constraint-pronoun",
        "new-topic",
        "no-hit",
        "prompt-injection",
    }
    top_three_hits = 0
    for case in dataset:
        contextual = resolve_contextual_query(
            case["follow_up"],
            _turn(case["previous_query"], *case["previous_command_labels"]),
        )
        if contextual == case["follow_up"].strip():
            hits = retriever.retrieve(case["follow_up"], top_k=3)
        else:
            hits = merge_contextual_hits(
                retriever.retrieve(case["follow_up"], top_k=8),
                retriever.retrieve(contextual, top_k=8),
                top_k=3,
            )
        result_ids = [hit.topic.id for hit in hits]
        expected = case["expected_topic_id"]
        top_three_hits += (not result_ids) if expected is None else expected in result_ids
    assert top_three_hits / len(dataset) >= 0.90


def test_context_is_isolated_by_conversation_and_knowledge_base(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "memory.sqlite3")
    custom = repository.create_knowledge_base("项目运行手册")
    first = repository.create(knowledge_base_id="developer-it")
    second = repository.create(knowledge_base_id=custom.id)
    first_ids = _append_turn(repository, first.id, "Linux 怎么查看端口？", "ss 查看端口")
    _append_turn(repository, second.id, "内部服务怎么部署？", "部署服务")
    service = ConversationMemoryService(repository=repository, generator=OfflineGenerator())

    first_context = service.build_context(repository.get(first.id), "那 Windows 呢？")
    second_context = service.build_context(repository.get(second.id), "需要回滚吗？")

    assert set(first_context.info.used_message_ids) == {message.id for message in first_ids}
    assert not set(first_context.info.used_message_ids).intersection(second_context.info.used_message_ids)
    assert "内部服务" not in first_context.prompt_payload


def test_reset_sets_a_boundary_without_deleting_chat_history(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "memory.sqlite3")
    conversation = repository.create()
    old_ids = _append_turn(repository, conversation.id, "Linux 怎么查看端口？", "ss 查看端口")
    service = ConversationMemoryService(repository=repository, generator=OfflineGenerator())
    assert service.build_context(repository.get(conversation.id), "那 Windows 呢？").info.used

    assert service.reset(conversation.id)
    assert len(repository.get(conversation.id).messages) == 2
    cleared = service.build_context(repository.get(conversation.id), "从头开始")
    assert cleared.info.used is False
    assert service.get_state(conversation.id).recent_messages == []

    new_ids = _append_turn(repository, conversation.id, "Docker 怎么看日志？", "docker logs")
    rebuilt = service.build_context(repository.get(conversation.id), "最近 100 行呢？")
    assert set(rebuilt.info.used_message_ids) == {message.id for message in new_ids}
    assert not set(message.id for message in old_ids).intersection(rebuilt.info.used_message_ids)


def test_context_budget_is_strict_and_keeps_the_latest_turn(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "memory.sqlite3")
    conversation = repository.create()
    latest = None
    for index in range(5):
        latest = _append_turn(
            repository,
            conversation.id,
            f"第 {index} 轮：" + "很长的上下文" * 140,
            "查看状态",
            "执行操作",
            summary="回答摘要" * 90,
        )
    service = ConversationMemoryService(repository=repository, generator=OfflineGenerator())
    context = service.build_context(repository.get(conversation.id), "继续")

    assert context.info.estimated_tokens == estimate_tokens(context.prompt_payload)
    assert context.info.estimated_tokens <= 2400
    assert latest is not None
    assert {message.id for message in latest}.issubset(context.info.used_message_ids)


def test_local_summary_compacts_all_but_the_latest_two_turns(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "memory.sqlite3")
    conversation = repository.create()
    turns = [
        _append_turn(repository, conversation.id, f"第 {index} 轮 Linux 排查", "查看状态")
        for index in range(5)
    ]
    service = ConversationMemoryService(
        repository=repository,
        generator=OfflineGenerator(),
        summary_trigger_turns=2,
    )
    service.compact_if_needed(conversation.id)
    state = service.get_state(conversation.id)
    context = service.build_context(repository.get(conversation.id), "继续排查")

    assert state.summary_provider == "local"
    assert state.status == "ready"
    assert state.compacted_through_message_id == turns[-3][1].id
    assert state.summary.current_goal == "第 2 轮 Linux 排查"
    assert context.info.strategy == "summary_recent"
    assert context.info.recent_turn_count == 2


@pytest.mark.parametrize("failure", [None, ValueError("invalid summary JSON")])
def test_background_summary_preserves_local_result_on_success_or_failure(
    tmp_path: Path,
    failure: Exception | None,
) -> None:
    repository = ConversationRepository(tmp_path / "memory.sqlite3")
    conversation = repository.create()
    for index in range(4):
        _append_turn(repository, conversation.id, f"第 {index} 轮端口排查", "查看端口")
    generator = SummaryGenerator(failure=failure)
    service = ConversationMemoryService(
        repository=repository,
        generator=generator,
        summary_trigger_turns=1,
    )
    service.compact_if_needed(conversation.id)
    service._queue.join()
    state = service.get_state(conversation.id)

    assert generator.calls == 1
    assert state.status == "ready"
    if failure is None:
        assert state.summary_provider == "deepseek"
        assert state.summary.current_goal == "排查服务端口"
        assert state.last_error == ""
    else:
        assert state.summary_provider == "local"
        assert state.summary.current_goal == "第 1 轮端口排查"
        assert state.last_error == "ValueError"


def test_revision_lock_rejects_stale_model_summary(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "memory.sqlite3")
    conversation = repository.create()
    revision = repository.save_local_memory(
        conversation.id,
        summary=MemorySummary(current_goal="旧任务"),
        compacted_through_message_id="message-1",
        estimated_tokens=10,
        pending_input={"turns": []},
    )
    assert repository.claim_pending_memory(conversation.id) is not None
    repository.save_local_memory(
        conversation.id,
        summary=MemorySummary(current_goal="新任务"),
        compacted_through_message_id="message-2",
        estimated_tokens=10,
        pending_input=None,
    )

    updated = repository.complete_model_memory(
        conversation.id,
        expected_revision=revision,
        summary=MemorySummary(current_goal="过期模型结果"),
    )
    assert updated is False
    assert repository.memory_state(conversation.id).summary.current_goal == "新任务"


def test_interrupted_summary_is_recovered_after_repository_restart(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    first_repository = ConversationRepository(database)
    conversation = first_repository.create()
    first_repository.save_local_memory(
        conversation.id,
        summary=MemorySummary(current_goal="本地任务"),
        compacted_through_message_id="message-1",
        estimated_tokens=10,
        pending_input={"turns": []},
    )
    assert first_repository.claim_pending_memory(conversation.id)["status"] == "summarizing"

    restarted_repository = ConversationRepository(database)
    generator = SummaryGenerator()
    service = ConversationMemoryService(repository=restarted_repository, generator=generator)
    service._queue.join()

    assert service.get_state(conversation.id).summary_provider == "deepseek"
    assert generator.calls == 1


def test_conversation_delete_cascades_to_memory(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    repository = ConversationRepository(database)
    conversation = repository.create()
    assert repository.get_memory_record(conversation.id)
    assert repository.delete(conversation.id)
    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM conversation_memory WHERE conversation_id = ?",
            (conversation.id,),
        ).fetchone()[0]
    assert count == 0


def test_ordinary_answer_does_not_trigger_a_summary_call_before_threshold(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "memory.sqlite3")
    conversation = repository.create()
    generator = CountingGenerator()
    service = ConversationMemoryService(repository=repository, generator=generator)
    hits = retriever.retrieve("Git 安全回滚", top_k=4)

    answer, mode = AnswerService(generator).answer("Git 安全回滚", hits)
    repository.append_user(conversation.id, "Git 安全回滚")
    repository.append_answer(conversation.id, answer)
    service.compact_if_needed(conversation.id)

    assert mode == "model"
    assert generator.answer_calls == 1
    assert generator.calls == 0


def test_deepseek_memory_summary_uses_json_mode_and_rejects_invalid_json() -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "current_goal": "查看端口",
                                    "environments": ["Linux"],
                                    "constraints": [],
                                    "decisions": [],
                                    "open_questions": [],
                                    "history_topics": ["网络排查"],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    generator = DeepSeekGenerator(
        api_key="test-key-not-a-secret",
        base_url="https://api.deepseek.example/v1",
        model="deepseek-chat",
        timeout_seconds=2,
        transport=httpx.MockTransport(respond),
    )
    summary = generator.summarize_memory({"turns": []})
    assert summary.current_goal == "查看端口"
    assert requests[0]["max_tokens"] == 800
    assert requests[0]["response_format"] == {"type": "json_object"}

    invalid = DeepSeekGenerator(
        api_key="test-key-not-a-secret",
        base_url="https://api.deepseek.example/v1",
        model="deepseek-chat",
        timeout_seconds=2,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})
        ),
    )
    with pytest.raises(ValueError, match="会话摘要"):
        invalid.summarize_memory({"turns": []})


def test_local_summary_redacts_secrets_and_pasted_code() -> None:
    summary = build_local_summary(
        MemorySummary(),
        [_turn("API key=sk-secretvalue123456 然后运行 `rm -rf /tmp/demo`")],
    )
    serialized = summary.model_dump_json()
    assert "sk-secretvalue" not in serialized
    assert "rm -rf" not in serialized
    assert "[REDACTED]" in serialized
