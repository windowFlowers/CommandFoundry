from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.models import Answer, DEFAULT_KNOWLEDGE_BASE_ID
from app.repository import DATABASE_SCHEMA_VERSION, ConversationRepository


def _conversation_with_user_message(
    repository: ConversationRepository,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    query: str = "请记住我使用 PowerShell",
):
    conversation = repository.create(knowledge_base_id=knowledge_base_id)
    message = repository.append_user(conversation.id, query)
    return conversation, message


def test_v24_migration_creates_independent_backup_and_secure_schema(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('v2.3-data')")
        connection.execute("PRAGMA user_version = 3")

    repository = ConversationRepository(database)
    backup = tmp_path / "legacy.sqlite3.pre-v2.4.backup"

    assert backup.exists()
    assert not (tmp_path / "legacy.sqlite3.pre-v2.3.backup").exists()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "v2.3-data"
    with repository._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION == 5
        assert connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"user_profiles", "user_memories", "user_memory_extraction_tasks"} <= tables


def test_old_answer_defaults_to_unused_personalization() -> None:
    answer = Answer.model_validate(
        {
            "mode": "local_fallback",
            "summary": "旧回答",
            "commands": [],
            "notes": [],
            "citations": [],
        }
    )
    assert answer.personalization.used is False
    assert answer.personalization.memory_ids == []
    assert answer.personalization.sent_to_model is False


def test_profile_switches_cancel_tasks_and_advance_extraction_epoch(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "profile.sqlite3")
    original = repository.get_profile()
    conversation, message = _conversation_with_user_message(repository)
    task = repository.create_memory_extraction_task(
        conversation.id,
        message.id,
        conversation.knowledge_base_id,
        "我使用 PowerShell",
    )
    assert task is not None
    assert repository.get_profile().pending_task_count == 1

    disabled = repository.update_profile(auto_memory_enabled=False)

    assert disabled.personalization_enabled is True
    assert disabled.auto_memory_enabled is False
    assert disabled.extraction_epoch == original.extraction_epoch + 1
    assert disabled.pending_task_count == 0
    assert repository.get_memory_extraction_task(task["id"])["status"] == "cancelled"
    assert repository.create_memory_extraction_task(
        conversation.id,
        message.id,
        conversation.knowledge_base_id,
        "不会创建",
    ) is None


def test_upsert_supersedes_atomically_supports_vectors_undo_and_chain_delete(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "memories.sqlite3")
    first = repository.upsert_user_memory(
        "global",
        None,
        "response_style",
        "language",
        "zh-CN",
        "回答使用中文",
        0.98,
        embedding=b"vector-one",
        embedding_dimension=3,
        embedding_version="test-v1",
    )
    repeated = repository.upsert_user_memory(
        "global",
        None,
        "response_style",
        "language",
        "zh-CN",
        "始终使用中文回答",
        0.99,
    )
    assert repeated.id == first.id
    assert repeated.confidence == 0.99
    record = repository.list_user_memory_records(ids=[first.id])[0]
    assert record["embedding"] == b"vector-one"
    assert record["embedding_dimension"] == 3

    second = repository.upsert_user_memory(
        "global",
        None,
        "response_style",
        "language",
        "en",
        "Answer in English",
        1.0,
        extraction_provider="manual",
    )
    assert second.id != first.id
    assert second.supersedes_id == first.id
    assert repository.get_user_memory(first.id).status == "superseded"
    assert repository.list_user_memories(category="response_style") == [second]

    restored = repository.undo_user_memory(second.id)
    assert restored is not None
    assert restored.id == first.id
    assert restored.status == "active"

    third = repository.patch_user_memory(
        first.id,
        value="de",
        display_text="Antworten auf Deutsch",
        pinned=True,
        embedding=b"vector-three",
        embedding_dimension=3,
        embedding_version="test-v1",
    )
    assert third.id != first.id
    assert third.supersedes_id == first.id
    assert third.pinned is True
    assert repository.delete_user_memory_chain(third.id) == 2
    assert repository.list_user_memories(status=None) == []


def test_scoped_memory_filters_usage_and_knowledge_base_count(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "scoped.sqlite3")
    knowledge_base = repository.create_knowledge_base("项目手册")
    first = repository.upsert_user_memory(
        "knowledge_base",
        knowledge_base.id,
        "platform",
        "operating_system",
        "Windows",
        "项目运行在 Windows",
        0.98,
        pinned=True,
    )
    second = repository.upsert_user_memory(
        "knowledge_base",
        knowledge_base.id,
        "toolchain",
        "package_manager",
        "pnpm",
        "项目使用 pnpm",
        0.98,
    )

    assert repository.get_knowledge_base(knowledge_base.id).memory_count == 2
    assert repository.count_knowledge_base_memories(knowledge_base.id) == 2
    assert repository.list_user_memories(q="pnpm", knowledge_base_id=knowledge_base.id) == [second]
    assert repository.list_user_memories(ids=[]) == []
    assert repository.mark_user_memories_used([first.id, first.id, "missing"]) == 1
    used = repository.get_user_memory(first.id)
    assert used.use_count == 1
    assert used.last_used_at is not None


def test_task_completion_is_idempotent_filtered_and_atomic(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "tasks.sqlite3")
    conversation, message = _conversation_with_user_message(repository)
    task = repository.create_memory_extraction_task(
        conversation.id,
        message.id,
        conversation.knowledge_base_id,
        "我平时使用 PowerShell",
    )
    duplicate = repository.create_memory_extraction_task(
        conversation.id,
        message.id,
        conversation.knowledge_base_id,
        "重复任务",
    )
    assert task is not None and duplicate is not None
    assert duplicate["id"] == task["id"]
    assert repository.list_pending_memory_task_ids() == [task["id"]]

    claimed = repository.claim_memory_extraction_task(
        task["id"], expected_epoch=task["extraction_epoch"]
    )
    assert claimed is not None
    assert claimed["status"] == "extracting"
    assert claimed["attempt_count"] == 1
    memories = repository.complete_memory_extraction_task(
        task["id"],
        [
            {
                "action": "ADD",
                "scope": "knowledge_base",
                "category": "platform",
                "key": "shell",
                "value": "PowerShell",
                "display_text": "默认使用 PowerShell",
                "confidence": 0.93,
                "embedding": b"powershell-vector",
                "embedding_dimension": 4,
                "embedding_version": "test-v1",
            },
            {
                "action": "ADD",
                "scope": "knowledge_base",
                "category": "toolchain",
                "key": "runtime",
                "value": "unreliable",
                "display_text": "低置信度内容",
                "confidence": 0.79,
            },
            {"action": "NOOP"},
        ],
        expected_epoch=claimed["extraction_epoch"],
        expected_attempt=claimed["attempt_count"],
    )

    assert len(memories) == 1
    assert memories[0].source_conversation_id == conversation.id
    assert memories[0].source_message_id == message.id
    assert memories[0].extraction_provider == "deepseek"
    assert repository.list_user_memory_records(ids=[memories[0].id])[0]["embedding"] == b"powershell-vector"
    completed = repository.get_memory_extraction_task(task["id"])
    assert completed["status"] == "ready"
    assert completed["sanitized_input"] == ""
    assert completed["result_memory_ids"] == [memories[0].id]
    assert repository.complete_memory_extraction_task(task["id"], []) == []


def test_interrupted_task_recovers_and_stale_task_cannot_write_after_reset(tmp_path: Path) -> None:
    database = tmp_path / "recovery.sqlite3"
    first_repository = ConversationRepository(database)
    conversation, message = _conversation_with_user_message(first_repository)
    task = first_repository.create_memory_extraction_task(
        conversation.id,
        message.id,
        conversation.knowledge_base_id,
        "我的环境偏向 Windows",
    )
    assert task is not None
    claimed = first_repository.claim_memory_extraction_task(task["id"])
    assert claimed is not None

    restarted = ConversationRepository(database)
    recovered = restarted.get_memory_extraction_task(task["id"])
    assert recovered["status"] == "pending"
    claimed_again = restarted.claim_memory_extraction_task(task["id"])
    assert claimed_again["attempt_count"] == 2

    before_epoch = restarted.get_profile().extraction_epoch
    restarted.reset_user_memories()
    assert restarted.get_profile().extraction_epoch == before_epoch + 1
    assert restarted.get_memory_extraction_task(task["id"]) is None
    assert restarted.complete_memory_extraction_task(
        task["id"],
        [
            {
                "action": "ADD",
                "scope": "knowledge_base",
                "category": "platform",
                "key": "operating_system",
                "value": "Windows",
                "display_text": "使用 Windows",
                "confidence": 0.99,
            }
        ],
        expected_epoch=claimed_again["extraction_epoch"],
    ) == []


def test_task_completion_accepts_precreated_memory_ids_for_service_compatibility(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "task-compat.sqlite3")
    conversation, message = _conversation_with_user_message(repository)
    task = repository.create_memory_extraction_task(
        conversation.id,
        message.id,
        conversation.knowledge_base_id,
        "我希望回答更简洁",
    )
    assert task is not None
    claimed = repository.claim_memory_extraction_task(task["id"])
    memory = repository.upsert_user_memory(
        "global",
        None,
        "response_style",
        "detail",
        "concise",
        "回答偏好：简洁直接",
        0.95,
        source_conversation_id=conversation.id,
        source_message_id=message.id,
        extraction_provider="deepseek",
    )

    completed = repository.complete_memory_extraction_task(
        task["id"],
        memory_ids=[memory.id],
        expected_epoch=claimed["extraction_epoch"],
    )

    assert [item.id for item in completed] == [memory.id]
    assert repository.get_memory_extraction_task(task["id"])["status"] == "ready"


def test_task_failure_and_knowledge_base_delete_cascade(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "cascade.sqlite3")
    knowledge_base = repository.create_knowledge_base("临时项目")
    memory = repository.upsert_user_memory(
        "knowledge_base",
        knowledge_base.id,
        "toolchain",
        "package_manager",
        "pnpm",
        "临时项目使用 pnpm",
        0.99,
    )
    conversation, message = _conversation_with_user_message(
        repository, knowledge_base_id=knowledge_base.id
    )
    task = repository.create_memory_extraction_task(
        conversation.id,
        message.id,
        knowledge_base.id,
        "这个项目使用 pnpm",
    )
    assert task is not None
    claimed = repository.claim_memory_extraction_task(task["id"])
    assert repository.fail_memory_extraction_task(
        task["id"],
        "ValueError: provider included private output",
        expected_epoch=claimed["extraction_epoch"],
        expected_attempt=claimed["attempt_count"],
    )
    failed = repository.get_memory_extraction_task(task["id"])
    assert failed["status"] == "failed"
    assert failed["sanitized_input"] == ""

    repository.delete_knowledge_base(knowledge_base.id)

    assert repository.get_user_memory(memory.id) is None
    assert repository.get_memory_extraction_task(task["id"]) is None
    historical = repository.get(conversation.id)
    assert historical is not None
    assert historical.knowledge_base_deleted is True


def test_scope_validation_and_reset_are_deterministic(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "validation.sqlite3")
    try:
        repository.upsert_user_memory(
            "global",
            None,
            "platform",
            "operating_system",
            "Windows",
            "使用 Windows",
            1.0,
        )
    except ValueError as exc:
        assert "全局记忆" in str(exc)
    else:
        raise AssertionError("global platform memory must be rejected")

    memory = repository.upsert_user_memory(
        "global",
        None,
        "expertise",
        "level",
        "beginner",
        "开发经验为初学者",
        1.0,
    )
    scoped = repository.upsert_user_memory(
        "knowledge_base",
        DEFAULT_KNOWLEDGE_BASE_ID,
        "platform",
        "operating_system",
        "Windows",
        "项目运行在 Windows",
        1.0,
    )
    previous_epoch = repository.get_profile().extraction_epoch
    assert repository.reset_user_memories(scope="global") == 1
    assert repository.get_user_memory(memory.id) is None
    assert repository.get_user_memory(scoped.id) is not None
    assert repository.reset_user_memories(
        scope="knowledge_base", knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID
    ) == 1
    assert repository.get_profile().active_memory_count == 0
    assert repository.get_profile().extraction_epoch == previous_epoch + 2
