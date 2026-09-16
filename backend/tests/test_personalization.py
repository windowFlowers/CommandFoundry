from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from threading import Event

import httpx
import numpy as np

from app.memory import estimate_tokens
from app.generation import (
    DeepSeekGenerator,
    MemoryExtractionDraft,
    MemoryExtractionOperation,
    SYSTEM_PROMPT,
)
from app.models import (
    KnowledgeTopic,
    RetrievalHit,
    SourceMetadata,
    UserMemory,
    UserMemoryCreateRequest,
    UserProfile,
    utc_now,
)
from app.personalization import PersonalizationService, _memory_age_multiplier, local_signal_gate
from app.repository import ConversationRepository


class _NoEmbeddings:
    ready = False
    model_name = "disabled"


class _ExtractionGenerator:
    available = True

    def __init__(self, operations: list[MemoryExtractionOperation] | None = None) -> None:
        self.operations = operations or []
        self.calls = 0
        self.payloads: list[dict] = []

    def extract_user_memories(self, payload: dict) -> MemoryExtractionDraft:
        self.calls += 1
        self.payloads.append(payload)
        return MemoryExtractionDraft(operations=self.operations)


class _BlockingGenerator(_ExtractionGenerator):
    def __init__(self) -> None:
        super().__init__(
            [
                MemoryExtractionOperation(
                    action="ADD",
                    scope="knowledge_base",
                    category="project_constraint",
                    key="deployment_policy",
                    value="优先使用可回滚方案",
                    display_text="项目约束：优先使用可回滚方案",
                    confidence=0.96,
                )
            ]
        )
        self.entered = Event()
        self.release = Event()

    def extract_user_memories(self, payload: dict) -> MemoryExtractionDraft:
        self.calls += 1
        self.payloads.append(payload)
        self.entered.set()
        assert self.release.wait(5), payload
        return MemoryExtractionDraft(operations=self.operations)


def _ambiguous_statement() -> str:
    return "请记住我以后进行变更时优先采用可回滚的保守方案"


def test_ordinary_question_does_not_enqueue_or_call_extraction(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "ordinary.sqlite3")
    conversation = repository.create()
    message = repository.append_user(conversation.id, "Docker 怎么查看最近 100 行日志？")
    generator = _ExtractionGenerator()
    service = PersonalizationService(
        repository=repository,
        generator=generator,  # type: ignore[arg-type]
        embeddings=_NoEmbeddings(),  # type: ignore[arg-type]
    )

    notice = service.capture_user_message(
        conversation_id=conversation.id,
        source_message_id=message.id,
        knowledge_base_id=conversation.knowledge_base_id,
        text=message.query or "",
    )

    assert notice.status == "noop"
    assert generator.calls == 0
    assert repository.list_pending_memory_task_ids() == []
    assert repository.list_user_memories() == []


def test_only_a_genuinely_new_local_memory_is_reported_as_undoable_add(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "local-operation.sqlite3")
    conversation = repository.create()
    generator = _ExtractionGenerator()
    service = PersonalizationService(
        repository=repository,
        generator=generator,  # type: ignore[arg-type]
        embeddings=_NoEmbeddings(),  # type: ignore[arg-type]
    )

    first_message = repository.append_user(conversation.id, "请记住我的系统是 Windows")
    first = service.capture_user_message(
        conversation_id=conversation.id,
        source_message_id=first_message.id,
        knowledge_base_id=conversation.knowledge_base_id,
        text=first_message.query or "",
    )
    assert first.status == "saved"
    assert first.operation == "ADD"
    assert len(first.memory_ids) == 1

    repeated_message = repository.append_user(conversation.id, "请记住我的系统是 Windows")
    repeated = service.capture_user_message(
        conversation_id=conversation.id,
        source_message_id=repeated_message.id,
        knowledge_base_id=conversation.knowledge_base_id,
        text=repeated_message.query or "",
    )
    assert repeated.status == "saved"
    assert repeated.operation == "UPDATE"
    assert repeated.memory_ids == first.memory_ids

    corrected_message = repository.append_user(conversation.id, "不要再用 Windows，改用 Linux")
    corrected = service.capture_user_message(
        conversation_id=conversation.id,
        source_message_id=corrected_message.id,
        knowledge_base_id=conversation.knowledge_base_id,
        text=corrected_message.query or "",
    )
    assert corrected.status == "saved"
    assert corrected.operation == "SUPERSEDE"
    assert corrected.memory_ids != first.memory_ids
    assert repository.get_user_memory(first.memory_ids[0]).status == "superseded"
    assert repository.get_user_memory(corrected.memory_ids[0]).status == "active"
    assert generator.calls == 0


def test_unfenced_commands_and_complete_paths_are_rejected_before_model_extraction(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "sensitive.sqlite3")
    generator = _ExtractionGenerator()
    service = PersonalizationService(
        repository=repository,
        generator=generator,  # type: ignore[arg-type]
        embeddings=_NoEmbeddings(),  # type: ignore[arg-type]
    )
    samples = (
        "以后默认执行 rm -rf /tmp/demo",
        "请记住部署目录是 /var/www/app",
        "我的项目位于 C:/Users/Alice/private-app",
        "以后运行 Remove-Item -Recurse -Force temp",
        "默认用 git reset --hard 回滚",
        "请记住维护时执行 DROP TABLE audit_log",
    )
    for index, text in enumerate(samples):
        conversation = repository.create(title=f"sensitive-{index}")
        message = repository.append_user(conversation.id, text)
        notice = service.capture_user_message(
            conversation_id=conversation.id,
            source_message_id=message.id,
            knowledge_base_id=conversation.knowledge_base_id,
            text=text,
        )
        assert notice.status == "noop", text

    assert generator.calls == 0
    assert repository.list_pending_memory_task_ids() == []
    assert repository.list_user_memories(status=None) == []


def test_chinese_secret_statements_are_rejected_before_model_extraction(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "chinese-sensitive.sqlite3")
    generator = _ExtractionGenerator()
    service = PersonalizationService(
        repository=repository,
        generator=generator,  # type: ignore[arg-type]
        embeddings=_NoEmbeddings(),  # type: ignore[arg-type]
    )
    samples = (
        "请记住我的密码是 abc123",
        "请记住我的密钥为 abc123",
        "请记住我的令牌：abc123",
        "请记住我的 API 密钥是 abc123",
        "我的访问令牌 abc123",
    )
    for index, text in enumerate(samples):
        decision = local_signal_gate(text)
        assert decision.rejected_sensitive is True, text
        conversation = repository.create(title=f"chinese-sensitive-{index}")
        message = repository.append_user(conversation.id, text)
        notice = service.capture_user_message(
            conversation_id=conversation.id,
            source_message_id=message.id,
            knowledge_base_id=conversation.knowledge_base_id,
            text=text,
        )
        assert notice.status == "noop", text

    assert generator.calls == 0
    assert repository.list_pending_memory_task_ids() == []
    assert repository.list_user_memories(status=None) == []


def test_default_technical_question_does_not_become_a_memory_or_model_task(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "default-question.sqlite3")
    conversation = repository.create()
    text = "Docker 默认网络是什么？"
    message = repository.append_user(conversation.id, text)
    generator = _ExtractionGenerator()
    service = PersonalizationService(
        repository=repository,
        generator=generator,  # type: ignore[arg-type]
        embeddings=_NoEmbeddings(),  # type: ignore[arg-type]
    )

    decision = local_signal_gate(text)
    notice = service.capture_user_message(
        conversation_id=conversation.id,
        source_message_id=message.id,
        knowledge_base_id=conversation.knowledge_base_id,
        text=text,
    )

    assert decision.rejected_sensitive is False
    assert decision.local_candidates == ()
    assert decision.infer_with_model is False
    assert notice.status == "noop"
    assert generator.calls == 0
    assert repository.list_pending_memory_task_ids() == []
    assert repository.list_user_memories(status=None) == []


def test_reset_during_model_extraction_cannot_resurrect_memory(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "race.sqlite3")
    conversation = repository.create()
    text = _ambiguous_statement()
    message = repository.append_user(conversation.id, text)
    generator = _BlockingGenerator()
    service = PersonalizationService(
        repository=repository,
        generator=generator,  # type: ignore[arg-type]
        embeddings=_NoEmbeddings(),  # type: ignore[arg-type]
    )

    notice = service.capture_user_message(
        conversation_id=conversation.id,
        source_message_id=message.id,
        knowledge_base_id=conversation.knowledge_base_id,
        text=text,
    )
    assert notice.status == "queued"
    assert generator.entered.wait(5)

    repository.reset_user_memories()
    generator.release.set()
    service._queue.join()

    assert generator.calls == 1
    assert repository.list_user_memories(status=None) == []
    assert repository.get_memory_extraction_task(notice.task_id or "") is None


def test_model_candidates_are_applied_once_by_atomic_completion(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "complete.sqlite3")
    conversation = repository.create()
    text = _ambiguous_statement()
    message = repository.append_user(conversation.id, text)
    generator = _ExtractionGenerator(
        [
            MemoryExtractionOperation(
                action="ADD",
                scope="knowledge_base",
                category="project_constraint",
                key="change_policy",
                value="优先采用可回滚方案",
                display_text="项目约束：优先采用可回滚方案",
                confidence=0.96,
            )
        ]
    )
    service = PersonalizationService(
        repository=repository,
        generator=generator,  # type: ignore[arg-type]
        embeddings=_NoEmbeddings(),  # type: ignore[arg-type]
    )

    notice = service.capture_user_message(
        conversation_id=conversation.id,
        source_message_id=message.id,
        knowledge_base_id=conversation.knowledge_base_id,
        text=text,
    )
    service._queue.join()

    memories = repository.list_user_memories(source_message_id=message.id)
    task = repository.get_memory_extraction_task(notice.task_id or "")
    assert generator.calls == 1
    assert len(memories) == 1
    assert memories[0].extraction_provider == "deepseek"
    assert task and task["status"] == "ready" and task["sanitized_input"] == ""


def test_noncanonical_manual_and_model_keys_obey_current_explicit_platform(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "semantic-keys.sqlite3")
    conversation = repository.create()
    generator = _ExtractionGenerator(
        [
            MemoryExtractionOperation(
                action="ADD",
                scope="knowledge_base",
                # The model mislabeled a Windows environment as toolchain and
                # supplied a nonstandard key.  The service must repair both.
                category="toolchain",
                key="preferred_environment",
                value="Windows",
                display_text="首选环境：Windows",
                confidence=0.96,
            )
        ]
    )
    service = PersonalizationService(
        repository=repository,
        generator=generator,  # type: ignore[arg-type]
        embeddings=_NoEmbeddings(),  # type: ignore[arg-type]
    )

    manual = service.create_manual(
        UserMemoryCreateRequest(
            scope="knowledge_base",
            knowledge_base_id=conversation.knowledge_base_id,
            category="platform",
            key="manual.platform.legacy-looking-key",
            value="Windows 11",
            display_text="我的开发系统：Windows 11",
        )
    )
    assert manual.key == "os"
    assert manual.category == "platform"

    # Simulate an already-saved v2.4 row created before key normalization.
    legacy = repository.upsert_user_memory(
        scope="knowledge_base",
        knowledge_base_id=conversation.knowledge_base_id,
        category="platform",
        key="manual.platform.old-timestamp",
        value="Windows",
        display_text="旧的手动系统偏好：Windows",
        confidence=1,
        extraction_provider="manual",
    )
    overridden = service.build_context(
        query="这次只按 Linux 回答",
        retrieval_query="这次只按 Linux 回答",
        knowledge_base_id=conversation.knowledge_base_id,
    )
    assert manual.id not in overridden.info.memory_ids
    assert legacy.id not in overridden.info.memory_ids
    assert "Windows" not in overridden.retrieval_query

    message = repository.append_user(conversation.id, _ambiguous_statement())
    notice = service.capture_user_message(
        conversation_id=conversation.id,
        source_message_id=message.id,
        knowledge_base_id=conversation.knowledge_base_id,
        text=message.query or "",
    )
    assert notice.status == "queued"
    service._queue.join()
    learned = repository.list_user_memories(source_message_id=message.id)
    assert len(learned) == 1
    assert learned[0].key == "os"
    assert learned[0].category == "platform"
    overridden_after_model = service.build_context(
        query="这次只按 Linux 回答",
        retrieval_query="这次只按 Linux 回答",
        knowledge_base_id=conversation.knowledge_base_id,
    )
    assert learned[0].id not in overridden_after_model.info.memory_ids


def test_model_extraction_receives_only_four_relevant_source_free_memories(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "bounded-context.sqlite3")
    conversation = repository.create()
    for index in range(7):
        repository.upsert_user_memory(
            scope="knowledge_base",
            knowledge_base_id=conversation.knowledge_base_id,
            category="project_constraint",
            key=f"change_policy_{index}",
            value=f"变更方案 {index} 必须可回滚",
            display_text=f"项目约束：变更方案 {index} 必须可回滚",
            confidence=0.98,
            source_conversation_id=f"source-conversation-{index}",
            source_message_id=f"source-message-{index}",
            extraction_provider="local",
        )
    text = _ambiguous_statement()
    message = repository.append_user(conversation.id, text)
    generator = _ExtractionGenerator()
    service = PersonalizationService(
        repository=repository,
        generator=generator,  # type: ignore[arg-type]
        embeddings=_NoEmbeddings(),  # type: ignore[arg-type]
    )

    notice = service.capture_user_message(
        conversation_id=conversation.id,
        source_message_id=message.id,
        knowledge_base_id=conversation.knowledge_base_id,
        text=text,
    )
    assert notice.status == "queued"
    service._queue.join()

    assert generator.calls == 1
    sent = generator.payloads[0]["existing_memories"]
    assert 0 < len(sent) <= 4
    assert estimate_tokens(json.dumps(sent, ensure_ascii=False)) <= 400
    assert all(set(item) == {"scope", "category", "key", "value"} for item in sent)
    assert all("source" not in json.dumps(item, ensure_ascii=False) for item in sent)


class _SelectionRepository:
    def __init__(self, memories: list[UserMemory], records: list[dict] | None = None) -> None:
        self.memories = memories
        self.records = records or []

    def get_profile(self) -> UserProfile:
        now = utc_now()
        return UserProfile(created_at=now, updated_at=now, active_memory_count=len(self.memories))

    def list_user_memories(self, **_: object) -> list[UserMemory]:
        return self.memories

    def list_user_memory_records(self, **_: object) -> list[dict]:
        return self.records

    def list_pending_memory_task_ids(self) -> list[str]:
        return []


def _memory(
    memory_id: str,
    *,
    scope: str,
    knowledge_base_id: str | None,
    category: str,
    key: str,
    value: str,
) -> UserMemory:
    now = utc_now()
    return UserMemory(
        id=memory_id,
        scope=scope,  # type: ignore[arg-type]
        knowledge_base_id=knowledge_base_id,
        category=category,  # type: ignore[arg-type]
        key=key,
        value=value,
        display_text=f"{key}：{value}",
        confidence=0.98,
        extraction_provider="local",
        created_at=now,
        updated_at=now,
    )


def _selection_service(repository, embeddings=None) -> PersonalizationService:
    return PersonalizationService(
        repository=repository,
        generator=_ExtractionGenerator(),  # type: ignore[arg-type]
        embeddings=embeddings or _NoEmbeddings(),  # type: ignore[arg-type]
    )


def test_explicit_linux_filters_windows_and_powershell_and_scoped_precedes_global() -> None:
    memories = [
        _memory("windows", scope="knowledge_base", knowledge_base_id="kb-a", category="platform", key="os", value="Windows"),
        _memory("powershell", scope="knowledge_base", knowledge_base_id="kb-a", category="platform", key="shell", value="PowerShell"),
        _memory("pnpm", scope="knowledge_base", knowledge_base_id="kb-a", category="toolchain", key="package_manager", value="pnpm"),
        _memory("english", scope="global", knowledge_base_id=None, category="response_style", key="language", value="en"),
        _memory("foreign", scope="knowledge_base", knowledge_base_id="kb-b", category="toolchain", key="runtime", value="Python 3.12"),
    ]
    context = _selection_service(_SelectionRepository(memories)).build_context(
        query="Linux 下怎么安装依赖？",
        retrieval_query="Linux 下怎么安装依赖？",
        knowledge_base_id="kb-a",
    )

    assert "windows" not in context.info.memory_ids
    assert "powershell" not in context.info.memory_ids
    assert "foreign" not in context.info.memory_ids
    assert context.info.memory_ids.index("pnpm") < context.info.memory_ids.index("english")


def test_current_language_overrides_saved_language_memory() -> None:
    memories = [
        _memory("chinese", scope="global", knowledge_base_id=None, category="response_style", key="language", value="zh-CN"),
        _memory("detail", scope="global", knowledge_base_id=None, category="response_style", key="detail", value="detailed"),
    ]
    context = _selection_service(_SelectionRepository(memories)).build_context(
        query="这次请用英文回答，并保持简洁",
        retrieval_query="这次请用英文回答，并保持简洁",
        knowledge_base_id="kb-a",
    )
    assert "chinese" not in context.info.memory_ids
    assert "detail" not in context.info.memory_ids


class _VectorEmbeddings:
    ready = True
    model_name = "memory-vector-test"

    def embed(self, texts: list[str]) -> np.ndarray:
        assert len(texts) == 1
        return np.asarray([[1.0, 0.0]], dtype=np.float32)


def test_valid_stored_cosine_signal_orders_otherwise_equal_scoped_memories() -> None:
    first = _memory("orthogonal", scope="knowledge_base", knowledge_base_id="kb-a", category="toolchain", key="tool_a", value="alpha")
    second = _memory("semantic", scope="knowledge_base", knowledge_base_id="kb-a", category="toolchain", key="tool_b", value="beta")
    records = [
        {"id": first.id, "embedding": np.asarray([0.0, 1.0], dtype=np.float32).tobytes(), "embedding_dimension": 2, "embedding_version": "memory-vector-test"},
        {"id": second.id, "embedding": np.asarray([1.0, 0.0], dtype=np.float32).tobytes(), "embedding_dimension": 2, "embedding_version": "memory-vector-test"},
    ]
    context = _selection_service(_SelectionRepository([first, second], records), _VectorEmbeddings()).build_context(
        query="选择合适的工具",
        retrieval_query="选择合适的工具",
        knowledge_base_id="kb-a",
    )
    assert context.info.memory_ids[0] == "semantic"


def test_minimal_noop_is_valid_and_never_becomes_a_candidate() -> None:
    operation = MemoryExtractionOperation.model_validate({"action": "NOOP"})
    assert operation.scope == ""
    assert operation.confidence == 0


def test_soft_decay_and_usage_boost_only_adjust_ranking() -> None:
    now = datetime.now(UTC)
    common = {
        "scope": "global",
        "knowledge_base_id": None,
        "category": "response_style",
        "key": "detail",
        "value": "concise",
        "display_text": "回答偏好：简洁",
        "confidence": 0.98,
        "status": "active",
        "extraction_provider": "local",
        "created_at": now - timedelta(days=360),
    }
    recent = UserMemory(id="recent", updated_at=now, use_count=0, pinned=False, **common)
    old = UserMemory(id="old", updated_at=now - timedelta(days=360), use_count=0, pinned=False, **common)
    old_used = UserMemory(
        id="old-used",
        updated_at=now - timedelta(days=360),
        use_count=10_000,
        pinned=False,
        **common,
    )
    pinned = UserMemory(
        id="pinned",
        updated_at=now - timedelta(days=360),
        use_count=0,
        pinned=True,
        **common,
    )

    assert 0.85 <= _memory_age_multiplier(old) < _memory_age_multiplier(recent) <= 1.05
    assert _memory_age_multiplier(old) < _memory_age_multiplier(old_used) <= 1.05
    assert _memory_age_multiplier(pinned) == 1.0
    assert old.status == "active"  # Decay never hides or deletes the row.


def test_generation_prompt_receives_language_preference_without_forcing_chinese() -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"summary": "Done.", "commands": [], "notes": [], "summary_segments": []})}}]},
        )

    generator = DeepSeekGenerator(
        api_key="test-key-not-secret",
        base_url="https://api.deepseek.example/v1",
        model="deepseek-chat",
        timeout_seconds=2,
        transport=httpx.MockTransport(respond),
    )
    hit = RetrievalHit(
        topic=KnowledgeTopic(
            id="test.topic",
            domain="testing",
            title="Test",
            summary="Grounded fact.",
            source=SourceMetadata(source_id="test.source", title="Test source"),
        ),
        score=1,
    )
    preference = '[{"preference":"回答语言：英文"}]'
    generator.generate("Answer in English", [hit], personalization_context=preference)

    assert preference in requests[0]["messages"][1]["content"]
    assert "回答中文简洁明确" not in SYSTEM_PROMPT
    assert "明确语言、详略偏好" in SYSTEM_PROMPT
