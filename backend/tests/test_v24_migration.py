from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import pytest

from app.repository import ConversationRepository, DATABASE_SCHEMA_VERSION


# This is the schema shipped by the tagged v2.3.0 repository (schema version
# 3), kept here as a migration contract rather than synthesizing a reduced
# approximation of the old database.
V23_SCHEMA = """
CREATE TABLE knowledge_bases (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    knowledge_base_id TEXT,
    knowledge_base_name_snapshot TEXT
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    query TEXT,
    answer_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE conversation_memory (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    summary_json TEXT NOT NULL DEFAULT '{}',
    summary_provider TEXT,
    compacted_through_message_id TEXT,
    reset_after_message_id TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    revision INTEGER NOT NULL DEFAULT 0,
    estimated_tokens INTEGER NOT NULL DEFAULT 0,
    pending_input_json TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
CREATE TABLE knowledge_documents (
    id TEXT PRIMARY KEY,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'upload',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    stored_path TEXT NOT NULL,
    index_schema_version INTEGER NOT NULL DEFAULT 1,
    index_revision TEXT NOT NULL DEFAULT '',
    rebuild_status TEXT NOT NULL DEFAULT 'pending',
    rebuild_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(knowledge_base_id, sha256)
);
CREATE TABLE knowledge_parent_chunks (
    id TEXT PRIMARY KEY,
    stable_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    parent_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    locator_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    index_revision TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(document_id, stable_id)
);
CREATE TABLE knowledge_chunks (
    id TEXT PRIMARY KEY,
    stable_id TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES knowledge_parent_chunks(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    retrieval_text TEXT NOT NULL DEFAULT '',
    locator_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    index_revision TEXT NOT NULL DEFAULT '',
    command_evidence_json TEXT NOT NULL DEFAULT '[]',
    tokens_json TEXT NOT NULL,
    embedding BLOB,
    embedding_dimension INTEGER,
    embedding_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(document_id, chunk_index)
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX idx_documents_knowledge_base ON knowledge_documents(knowledge_base_id, updated_at);
CREATE INDEX idx_chunks_knowledge_base ON knowledge_chunks(knowledge_base_id, document_id, chunk_index);
CREATE INDEX idx_parent_chunks_knowledge_base
    ON knowledge_parent_chunks(knowledge_base_id, document_id, parent_index);
"""

NOW = "2026-09-09T12:00:00+00:00"
VECTOR = struct.pack("<3f", 0.25, -0.5, 1.0)
OLD_ANSWER = {
    "mode": "local_fallback",
    "summary": "来自 v2.3 的回答",
    "commands": [],
    "notes": ["旧回答仍可阅读"],
    "citations": [],
    "generation": {"attempted": False, "provider": "local", "fallback_reason": "no_api_key"},
    "context": {
        "used": True,
        "strategy": "recent",
        "recent_turn_count": 1,
        "used_message_ids": ["msg-user-v23"],
        "summary_used": False,
        "retrieval_query": "Git 安全回滚",
        "estimated_tokens": 24,
    },
    "summary_segments": [],
}
OLD_SUMMARY = {
    "current_goal": "安全回滚提交",
    "environments": ["Windows"],
    "constraints": ["保留工作区"],
    "decisions": ["优先 git revert"],
    "open_questions": [],
    "history_topics": ["Git"],
}
LOCATOR = {
    "kind": "markdown",
    "page_start": None,
    "page_end": None,
    "heading_path": ["Git", "回滚"],
    "line_start": 3,
    "line_end": 5,
    "paragraph_start": None,
    "paragraph_end": None,
    "char_start": 8,
    "char_end": 35,
}


def _seed_real_v23(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(V23_SCHEMA)
        connection.execute(
            "INSERT INTO knowledge_bases VALUES (?, ?, ?, ?, ?)",
            ("kb-v23", "v2.3 上传知识库", 0, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)",
            ("conv-v23", "迁移保留测试", NOW, NOW, "kb-v23", "v2.3 上传知识库"),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, 'user', ?, NULL, ?)",
            ("msg-user-v23", "conv-v23", "Git 怎么安全回滚？", NOW),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, 'assistant', NULL, ?, ?)",
            ("msg-answer-v23", "conv-v23", json.dumps(OLD_ANSWER, ensure_ascii=False), NOW),
        )
        connection.execute(
            "INSERT INTO conversation_memory VALUES (?, ?, 'local', ?, NULL, 'ready', 7, 88, '', '', ?)",
            (
                "conv-v23",
                json.dumps(OLD_SUMMARY, ensure_ascii=False),
                "msg-answer-v23",
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_documents VALUES (?, ?, ?, ?, ?, ?, ?, 'upload', 'ready', '', ?, ?, 2, ?, 'ready', '', ?, ?)",
            (
                "doc-v23",
                "kb-v23",
                "git-guide.md",
                "Git 指南",
                "text/markdown",
                128,
                "a" * 64,
                "# Git\n\n使用 git revert 安全回滚。",
                "C:/Aegis/uploads/git-guide.md",
                "revision-v23",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_parent_chunks VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (
                "parent-v23",
                "stable-parent-v23",
                "doc-v23",
                "kb-v23",
                "使用 git revert 安全回滚。",
                json.dumps(LOCATOR, ensure_ascii=False),
                "parent-hash-v23",
                "revision-v23",
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "chunk-v23",
                "stable-child-v23",
                "doc-v23",
                "kb-v23",
                "parent-v23",
                "git revert <commit>",
                "Git 回滚 git revert <commit>",
                json.dumps(LOCATOR, ensure_ascii=False),
                "child-hash-v23",
                "revision-v23",
                json.dumps(["git revert <commit>"], ensure_ascii=False),
                json.dumps(["git", "revert"], ensure_ascii=False),
                VECTOR,
                3,
                "bge-v23",
                NOW,
            ),
        )
        connection.execute("PRAGMA user_version = 3")


def _raw_snapshot(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        return {
            "version": connection.execute("PRAGMA user_version").fetchone()[0],
            "conversation_count": connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "message_count": connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "memory_summary": connection.execute(
                "SELECT summary_json FROM conversation_memory WHERE conversation_id = 'conv-v23'"
            ).fetchone()[0],
            "document_count": connection.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0],
            "parent_count": connection.execute("SELECT COUNT(*) FROM knowledge_parent_chunks").fetchone()[0],
            "child_count": connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0],
            "embedding": connection.execute(
                "SELECT embedding FROM knowledge_chunks WHERE id = 'chunk-v23'"
            ).fetchone()[0],
        }


def test_real_v23_database_migrates_to_v24_without_losing_rag_or_conversation_state(tmp_path: Path) -> None:
    database = tmp_path / "aegis.sqlite3"
    _seed_real_v23(database)
    before = _raw_snapshot(database)

    repository = ConversationRepository(database)

    after = _raw_snapshot(database)
    assert after == {**before, "version": DATABASE_SCHEMA_VERSION}
    conversation = repository.get("conv-v23")
    assert conversation is not None
    assert conversation.knowledge_base_id == "kb-v23"
    assert [message.role for message in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[1].answer is not None
    assert conversation.messages[1].answer.summary == "来自 v2.3 的回答"
    assert conversation.messages[1].answer.personalization.used is False

    memory = repository.memory_state("conv-v23")
    assert memory.summary.current_goal == "安全回滚提交"
    assert memory.summary.decisions == ["优先 git revert"]
    assert memory.summary_provider == "local"
    assert memory.compacted_through_message_id == "msg-answer-v23"

    knowledge_base = repository.get_knowledge_base("kb-v23")
    assert knowledge_base is not None
    assert knowledge_base.document_count == 1
    assert knowledge_base.chunk_count == 1
    document = repository.get_document("doc-v23")
    assert document is not None
    assert document.status == "ready"
    assert document.parent_count == 1
    assert document.index_schema_version == 2
    assert document.index_revision == "revision-v23"
    content = repository.get_document_content("doc-v23", chunk_id="stable-child-v23")
    assert content is not None
    assert content.chunk_id == "chunk-v23"
    assert content.locator is not None
    assert content.locator.heading_path == ["Git", "回滚"]
    assert content.highlight_start == 8
    assert content.highlight_end == 35

    backup = database.with_name(f"{database.name}.pre-v2.4.backup")
    assert ConversationRepository._backup_is_valid(backup)
    assert _raw_snapshot(backup) == before
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"user_profiles", "user_memories", "user_memory_extraction_tasks"} <= tables
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_failed_v24_schema_upgrade_rolls_back_and_leaves_v23_backup_readable(tmp_path: Path) -> None:
    database = tmp_path / "rollback.sqlite3"
    _seed_real_v23(database)
    with sqlite3.connect(database) as connection:
        # A corrupt/foreign object colliding with the new v2.4 table forces the
        # migration transaction to fail after BEGIN IMMEDIATE.
        connection.execute("CREATE VIEW user_profiles AS SELECT id FROM conversations")
    before = _raw_snapshot(database)

    with pytest.raises(sqlite3.OperationalError):
        ConversationRepository(database)

    assert _raw_snapshot(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'user_profiles'"
        ).fetchone()[0] == "view"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_memories'"
        ).fetchone() is None
    backup = database.with_name(f"{database.name}.pre-v2.4.backup")
    assert ConversationRepository._backup_is_valid(backup)
    assert _raw_snapshot(backup) == before


def test_corrupt_existing_pre_v24_backup_is_atomically_regenerated(tmp_path: Path) -> None:
    database = tmp_path / "recover-backup.sqlite3"
    _seed_real_v23(database)
    before = _raw_snapshot(database)
    backup = database.with_name(f"{database.name}.pre-v2.4.backup")
    backup.write_bytes(b"not-a-sqlite-database")

    ConversationRepository(database)

    assert ConversationRepository._backup_is_valid(backup)
    assert _raw_snapshot(backup) == before
    assert not list(tmp_path.glob(f".{backup.name}.*.tmp"))
