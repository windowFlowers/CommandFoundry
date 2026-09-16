from __future__ import annotations

import json
from pathlib import Path

from app.memory import estimate_tokens
from app.models import UserMemory, UserProfile, utc_now
from app.personalization import PersonalizationService, local_signal_gate
from app.repository import ConversationRepository


DATASET = json.loads(
    (Path(__file__).resolve().parents[2] / "knowledge" / "personalization_eval_dataset.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _matches(candidate, case: dict) -> bool:
    if candidate.scope != case.get("scope") or candidate.category != case.get("category"):
        return False
    if case.get("key") and candidate.key != case["key"]:
        return False
    if case.get("key_prefix") and not candidate.key.startswith(case["key_prefix"]):
        return False
    if case.get("value") and candidate.value != case["value"]:
        return False
    if case.get("value_contains") and case["value_contains"] not in candidate.value:
        return False
    if case.get("operation") and candidate.action != case["operation"]:
        return False
    return True


def _explicit_override_for(candidate) -> str | None:
    alternatives = {
        "os": "这次只按 Linux 回答" if candidate.value != "Linux" else "这次只按 Windows 回答",
        "shell": "这次只用 bash" if candidate.value.casefold() != "bash" else "这次只用 PowerShell",
        "package_manager": "这次只用 npm" if candidate.value.casefold() != "npm" else "这次只用 pnpm",
        "database": "这次只讲 MySQL" if candidate.value.casefold() != "mysql" else "这次只讲 PostgreSQL",
        "runtime": "这次使用 Node.js" if not candidate.value.startswith("Node") else "这次使用 Python",
        "container": "这次只用 Docker" if candidate.value != "Docker" else "这次只用 Kubernetes",
    }
    return alternatives.get(candidate.key)


class _NoModel:
    available = False


class _NoVectors:
    ready = False
    model_name = "disabled"


def test_sixty_cross_conversation_personalization_scenarios(tmp_path) -> None:
    """Exercise every dataset row through persistence and a fresh conversation.

    Positive scenarios learn a fact in one conversation and select it in a new
    conversation. Scoped facts are also checked against another knowledge base,
    explicit conflicts are checked when meaningful, and every scenario ends in
    a physical reset. No-op and attack rows prove that ordinary questions and
    unsafe input cannot create cross-conversation state.
    """

    repository = ConversationRepository(tmp_path / "personalization-evaluation.sqlite3")
    other_base = repository.create_knowledge_base("隔离评测知识库")
    service = PersonalizationService(
        repository=repository,
        generator=_NoModel(),  # type: ignore[arg-type]
        embeddings=_NoVectors(),  # type: ignore[arg-type]
    )
    true_positive = 0
    predicted_positive = 0
    scoped_correct = 0
    positive_count = 0
    scenario_passes = 0
    for case in DATASET:
        repository.reset_user_memories()
        decision = local_signal_gate(case["message"])
        source_conversation = repository.create(title=f"source-{case['id']}")
        source_message = repository.append_user(source_conversation.id, case["message"])
        predecessor = None
        if case.get("operation") == "SUPERSEDE":
            predecessor = repository.upsert_user_memory(
                scope=case["scope"],
                knowledge_base_id=source_conversation.knowledge_base_id,
                category=case["category"],
                key=case["key"],
                value="npm",
                display_text="首选包管理器：npm",
                confidence=0.99,
            )
        notice = service.capture_user_message(
            conversation_id=source_conversation.id,
            source_message_id=source_message.id,
            knowledge_base_id=source_conversation.knowledge_base_id,
            text=case["message"],
        )
        if case["expect"] == "extract":
            positive_count += 1
            predicted_positive += len(decision.local_candidates)
            matching = [candidate for candidate in decision.local_candidates if _matches(candidate, case)]
            assert matching, case["id"]
            true_positive += 1
            scoped_correct += int(matching[0].scope == case["scope"])
            assert decision.rejected_sensitive is False
            assert notice.status == "saved", case["id"]
            stored = repository.list_user_memories(status="active")
            learned = next(
                memory
                for memory in stored
                if memory.category == case["category"]
                and (
                    memory.key == case.get("key")
                    or memory.key.startswith(case.get("key_prefix", "\0"))
                )
            )
            assert learned.source_conversation_id == source_conversation.id
            assert learned.source_message_id == source_message.id

            # The target represents a new conversation: only durable user
            # memory, never the source conversation history, is available.
            repository.create(title=f"target-{case['id']}")
            generic = service.build_context(
                query="后续开发任务应该采用什么环境？",
                retrieval_query="后续开发任务应该采用什么环境？",
                knowledge_base_id=source_conversation.knowledge_base_id,
            )
            assert learned.id in generic.info.memory_ids, case["id"]
            if learned.scope == "knowledge_base":
                isolated = service.build_context(
                    query="后续开发任务应该采用什么环境？",
                    retrieval_query="后续开发任务应该采用什么环境？",
                    knowledge_base_id=other_base.id,
                )
                assert learned.id not in isolated.info.memory_ids, case["id"]
                override_query = _explicit_override_for(matching[0])
                if override_query:
                    overridden = service.build_context(
                        query=override_query,
                        retrieval_query=override_query,
                        knowledge_base_id=source_conversation.knowledge_base_id,
                    )
                    assert learned.id not in overridden.info.memory_ids, case["id"]
            else:
                across_base = service.build_context(
                    query="后续开发任务应该采用什么环境？",
                    retrieval_query="后续开发任务应该采用什么环境？",
                    knowledge_base_id=other_base.id,
                )
                assert learned.id in across_base.info.memory_ids, case["id"]
            if predecessor is not None:
                assert notice.operation == "SUPERSEDE"
                assert repository.get_user_memory(predecessor.id).status == "superseded"
        elif case["expect"] == "noop":
            predicted_positive += len(decision.local_candidates)
            assert decision.local_candidates == (), case["id"]
            assert decision.infer_with_model is False, case["id"]
            assert decision.rejected_sensitive is False
            assert notice.status == "noop", case["id"]
            assert repository.list_user_memories(status=None) == []
        else:
            assert decision.rejected_sensitive is True, case["id"]
            assert decision.local_candidates == ()
            assert notice.status == "noop", case["id"]
            assert repository.list_user_memories(status=None) == []

        repository.reset_user_memories()
        after_reset = service.build_context(
            query="继续上一个开发任务",
            retrieval_query="继续上一个开发任务",
            knowledge_base_id=source_conversation.knowledge_base_id,
        )
        assert after_reset.info.used is False, case["id"]
        scenario_passes += 1

    precision = true_positive / max(1, predicted_positive)
    scope_accuracy = scoped_correct / max(1, positive_count)
    assert len(DATASET) == 60
    assert scenario_passes == 60
    assert precision >= 0.90
    assert scope_accuracy >= 0.95


class _ProfileRepository:
    def __init__(self, memories: list[UserMemory]) -> None:
        self.memories = memories

    def get_profile(self) -> UserProfile:
        now = utc_now()
        return UserProfile(created_at=now, updated_at=now, active_memory_count=len(self.memories))

    def list_user_memories(self, **_: object) -> list[UserMemory]:
        return self.memories

    def list_pending_memory_task_ids(self) -> list[str]:
        return []


class _OfflineGenerator:
    available = False


class _DisabledEmbeddings:
    ready = False
    model_name = "disabled"


def _memory(
    memory_id: str,
    *,
    scope: str,
    knowledge_base_id: str | None,
    category: str,
    key: str,
    value: str,
    display: str,
    pinned: bool = False,
) -> UserMemory:
    now = utc_now()
    return UserMemory(
        id=memory_id,
        scope=scope,
        knowledge_base_id=knowledge_base_id,
        category=category,
        key=key,
        value=value,
        display_text=display,
        confidence=0.98,
        pinned=pinned,
        extraction_provider="local",
        created_at=now,
        updated_at=now,
    )


def _service(memories: list[UserMemory]) -> PersonalizationService:
    return PersonalizationService(
        repository=_ProfileRepository(memories),  # type: ignore[arg-type]
        generator=_OfflineGenerator(),  # type: ignore[arg-type]
        embeddings=_DisabledEmbeddings(),  # type: ignore[arg-type]
    )


def test_current_platform_overrides_saved_preference_and_generic_query_uses_it() -> None:
    memories = [
        _memory(
            "windows",
            scope="knowledge_base",
            knowledge_base_id="kb-a",
            category="platform",
            key="os",
            value="Windows",
            display="首选系统：Windows",
        ),
        _memory(
            "concise",
            scope="global",
            knowledge_base_id=None,
            category="response_style",
            key="detail",
            value="concise",
            display="回答偏好：简洁直接",
        ),
    ]
    service = _service(memories)

    explicit = service.build_context(
        query="Linux 怎么查看端口？",
        retrieval_query="Linux 怎么查看端口？",
        knowledge_base_id="kb-a",
    )
    assert "windows" not in explicit.info.memory_ids
    assert explicit.info.retrieval_query_enriched is False

    generic = service.build_context(
        query="怎么查看端口？",
        retrieval_query="怎么查看端口？",
        knowledge_base_id="kb-a",
    )
    assert "windows" in generic.info.memory_ids
    assert "Windows" in generic.retrieval_query
    assert generic.info.retrieval_query_enriched is True


def test_knowledge_base_scope_isolated_and_prompt_budget_is_bounded() -> None:
    memories = [
        _memory(
            "pnpm-a",
            scope="knowledge_base",
            knowledge_base_id="kb-a",
            category="toolchain",
            key="package_manager",
            value="pnpm",
            display="首选包管理器：pnpm",
        ),
        *[
            _memory(
                f"style-{index}",
                scope="global",
                knowledge_base_id=None,
                category="response_style",
                key=f"style_{index}",
                value="concise",
                display=f"回答风格 {index}：保持简洁且给出必要依据",
            )
            for index in range(8)
        ],
    ]
    service = _service(memories)
    other = service.build_context(
        query="怎么安装依赖？",
        retrieval_query="怎么安装依赖？",
        knowledge_base_id="kb-b",
    )
    assert "pnpm-a" not in other.info.memory_ids
    assert len(other.info.memory_ids) <= 4
    assert estimate_tokens(other.prompt_payload) <= 400
