from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .models import (
    Answer,
    ChatMessage,
    Conversation,
    ConversationMemory,
    DEFAULT_KNOWLEDGE_BASE_ID,
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeDocumentContent,
    KnowledgeDocumentDetail,
    LOCAL_USER_PROFILE_ID,
    MemoryMessagePreview,
    MemorySummary,
    UserMemory,
    UserProfile,
    utc_now,
)


BUILTIN_KNOWLEDGE_BASE_NAME = "开发者 IT 知识库"
DATABASE_SCHEMA_VERSION = 4
DOCUMENT_INDEX_SCHEMA_VERSION = 2
GLOBAL_MEMORY_CATEGORIES = {"response_style", "expertise"}
SCOPED_MEMORY_CATEGORIES = {"platform", "toolchain", "project_constraint"}
MEMORY_PROVIDERS = {"local", "deepseek", "manual"}


class ConversationRepository:
    """SQLite repository for conversations, knowledge bases, documents and chunks."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._backup_before_migration()
        self._initialize()

    def _backup_before_migration(self) -> None:
        """Create one consistent SQLite backup before each destructive schema milestone."""
        if not self.database_path.exists() or self.database_path.stat().st_size == 0:
            return
        source = sqlite3.connect(self.database_path, timeout=10)
        try:
            version = int(source.execute("PRAGMA user_version").fetchone()[0])
            has_tables = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
            ).fetchone()
            if version >= DATABASE_SCHEMA_VERSION or not has_tables:
                return
            labels = []
            if version < 3:
                labels.append("v2.3")
            if version < 4:
                labels.append("v2.4")
            for label in labels:
                backup_path = self.database_path.with_name(
                    f"{self.database_path.name}.pre-{label}.backup"
                )
                if self._backup_is_valid(backup_path):
                    continue
                temporary_path = backup_path.with_name(
                    f".{backup_path.name}.{uuid4().hex}.tmp"
                )
                target = sqlite3.connect(temporary_path)
                try:
                    source.backup(target)
                finally:
                    target.close()
                try:
                    if not self._backup_is_valid(temporary_path):
                        raise sqlite3.DatabaseError("迁移备份完整性校验失败")
                    temporary_path.replace(backup_path)
                finally:
                    temporary_path.unlink(missing_ok=True)
        finally:
            source.close()

    @staticmethod
    def _backup_is_valid(path: Path) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return False
        connection = None
        try:
            uri = f"file:{path.as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=10)
            return connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except (OSError, sqlite3.Error):
            return False
        finally:
            if connection is not None:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _checkpoint_wal(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            # Deletion is already committed. A concurrent reader may temporarily
            # prevent truncation, and a later connection will checkpoint the WAL.
            pass

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    def _initialize(self) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    is_builtin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    query TEXT,
                    answer_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_memory (
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
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id TEXT PRIMARY KEY,
                    personalization_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK(personalization_enabled IN (0, 1)),
                    auto_memory_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK(auto_memory_enabled IN (0, 1)),
                    revision INTEGER NOT NULL DEFAULT 0,
                    extraction_epoch INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_memories (
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
                    scope TEXT NOT NULL CHECK(scope IN ('global', 'knowledge_base')),
                    knowledge_base_id TEXT REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    category TEXT NOT NULL CHECK(category IN (
                        'response_style', 'expertise', 'platform', 'toolchain', 'project_constraint'
                    )),
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    display_text TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                    pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0, 1)),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'superseded')),
                    supersedes_id TEXT REFERENCES user_memories(id) ON DELETE SET NULL,
                    source_conversation_id TEXT,
                    source_message_id TEXT,
                    extraction_provider TEXT NOT NULL
                        CHECK(extraction_provider IN ('local', 'deepseek', 'manual')),
                    embedding BLOB,
                    embedding_dimension INTEGER,
                    embedding_version TEXT NOT NULL DEFAULT '',
                    last_used_at TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (scope = 'global' AND knowledge_base_id IS NULL) OR
                        (scope = 'knowledge_base' AND knowledge_base_id IS NOT NULL)
                    )
                );
                CREATE TABLE IF NOT EXISTS user_memory_extraction_tasks (
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    source_message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
                    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'extracting', 'ready', 'failed', 'cancelled')),
                    sanitized_input TEXT NOT NULL,
                    extraction_epoch INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    result_memory_ids_json TEXT NOT NULL DEFAULT '[]',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_documents (
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
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
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
                CREATE TABLE IF NOT EXISTS knowledge_parent_chunks (
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
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_documents_knowledge_base
                    ON knowledge_documents(knowledge_base_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_chunks_knowledge_base
                    ON knowledge_chunks(knowledge_base_id, document_id, chunk_index);
                CREATE INDEX IF NOT EXISTS idx_parent_chunks_knowledge_base
                    ON knowledge_parent_chunks(knowledge_base_id, document_id, parent_index);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_user_memories_active_key
                    ON user_memories(
                        profile_id,
                        scope,
                        ifnull(knowledge_base_id, ''),
                        category,
                        key
                    ) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_user_memories_scope
                    ON user_memories(profile_id, status, scope, knowledge_base_id, category, updated_at);
                CREATE INDEX IF NOT EXISTS idx_user_memory_tasks_status
                    ON user_memory_extraction_tasks(profile_id, status, created_at);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_bases(id, name, is_builtin, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (DEFAULT_KNOWLEDGE_BASE_ID, BUILTIN_KNOWLEDGE_BASE_NAME, now, now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO user_profiles(id, created_at, updated_at) VALUES (?, ?, ?)",
                (LOCAL_USER_PROFILE_ID, now, now),
            )
            columns = self._columns(connection, "conversations")
            if "knowledge_base_id" not in columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN knowledge_base_id TEXT")
            if "knowledge_base_name_snapshot" not in columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN knowledge_base_name_snapshot TEXT")
            document_columns = self._columns(connection, "knowledge_documents")
            document_additions = {
                "index_schema_version": "INTEGER NOT NULL DEFAULT 1",
                "index_revision": "TEXT NOT NULL DEFAULT ''",
                "rebuild_status": "TEXT NOT NULL DEFAULT 'pending'",
                "rebuild_error": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in document_additions.items():
                if name not in document_columns:
                    connection.execute(f"ALTER TABLE knowledge_documents ADD COLUMN {name} {definition}")
            chunk_columns = self._columns(connection, "knowledge_chunks")
            chunk_additions = {
                "stable_id": "TEXT NOT NULL DEFAULT ''",
                "parent_id": "TEXT",
                "retrieval_text": "TEXT NOT NULL DEFAULT ''",
                "locator_json": "TEXT NOT NULL DEFAULT '{}'",
                "content_hash": "TEXT NOT NULL DEFAULT ''",
                "index_revision": "TEXT NOT NULL DEFAULT ''",
                "command_evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for name, definition in chunk_additions.items():
                if name not in chunk_columns:
                    connection.execute(f"ALTER TABLE knowledge_chunks ADD COLUMN {name} {definition}")
            connection.execute(
                "UPDATE conversations SET knowledge_base_id = ? WHERE knowledge_base_id IS NULL OR knowledge_base_id = ''",
                (DEFAULT_KNOWLEDGE_BASE_ID,),
            )
            connection.execute(
                "UPDATE conversations SET knowledge_base_name_snapshot = ? "
                "WHERE knowledge_base_name_snapshot IS NULL OR knowledge_base_name_snapshot = ''",
                (BUILTIN_KNOWLEDGE_BASE_NAME,),
            )
            # A process interrupted during indexing is safely resumed by the worker.
            # A rebuild of an already-ready document deliberately leaves ``status``
            # as ready so its old, usable index remains searchable.  Its progress
            # therefore lives in ``rebuild_status`` and must be recovered too;
            # otherwise a crash leaves it permanently displayed as rebuilding and
            # the startup worker never sees it in list_pending_document_ids().
            connection.execute(
                "UPDATE knowledge_documents SET "
                "status = CASE WHEN status = 'indexing' THEN 'pending' ELSE status END, "
                "error = CASE WHEN status = 'indexing' THEN '' ELSE error END, "
                "rebuild_status = 'pending', rebuild_error = '', updated_at = ? "
                "WHERE status = 'indexing' OR rebuild_status = 'indexing'",
                (now,),
            )
            connection.execute(
                "UPDATE knowledge_documents SET rebuild_status = 'pending', rebuild_error = '' "
                "WHERE index_schema_version < ?",
                (DOCUMENT_INDEX_SCHEMA_VERSION,),
            )
            connection.execute(
                "UPDATE conversation_memory SET status = 'pending' WHERE status = 'summarizing'"
            )
            connection.execute(
                "UPDATE user_memory_extraction_tasks SET status = 'pending', updated_at = ? "
                "WHERE status = 'extracting'",
                (now,),
            )
            connection.execute(
                "UPDATE user_memory_extraction_tasks SET status = 'cancelled', sanitized_input = '', "
                "updated_at = ? WHERE status = 'pending' AND ("
                "extraction_epoch != (SELECT extraction_epoch FROM user_profiles WHERE id = profile_id) "
                "OR (SELECT personalization_enabled FROM user_profiles WHERE id = profile_id) = 0 "
                "OR (SELECT auto_memory_enabled FROM user_profiles WHERE id = profile_id) = 0)",
                (now,),
            )
            connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value)

    def _load_conversation(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Conversation:
        message_rows = connection.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid",
            (row["id"],),
        ).fetchall()
        messages = []
        for message in message_rows:
            answer = Answer.model_validate_json(message["answer_json"]) if message["answer_json"] else None
            messages.append(
                ChatMessage(
                    id=message["id"],
                    role=message["role"],
                    query=message["query"],
                    answer=answer,
                    created_at=self._parse_time(message["created_at"]),
                )
            )
        current_name = row["current_knowledge_base_name"]
        return Conversation(
            id=row["id"],
            title=row["title"],
            messages=messages,
            knowledge_base_id=row["knowledge_base_id"] or DEFAULT_KNOWLEDGE_BASE_ID,
            knowledge_base_name=row["knowledge_base_name_snapshot"] or current_name or BUILTIN_KNOWLEDGE_BASE_NAME,
            knowledge_base_deleted=current_name is None,
            created_at=self._parse_time(row["created_at"]),
            updated_at=self._parse_time(row["updated_at"]),
        )

    @staticmethod
    def _conversation_select() -> str:
        return (
            "SELECT c.*, kb.name AS current_knowledge_base_name FROM conversations c "
            "LEFT JOIN knowledge_bases kb ON kb.id = c.knowledge_base_id"
        )

    def create(self, title: str = "新对话", knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID) -> Conversation:
        knowledge_base = self.get_knowledge_base(knowledge_base_id)
        if knowledge_base is None:
            raise KeyError(knowledge_base_id)
        conversation = Conversation(
            title=title.strip() or "新对话",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_name=knowledge_base.name,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at, knowledge_base_id, knowledge_base_name_snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation.id,
                    conversation.title,
                    conversation.created_at.isoformat(),
                    conversation.updated_at.isoformat(),
                    conversation.knowledge_base_id,
                    conversation.knowledge_base_name,
                ),
            )
            connection.execute(
                "INSERT INTO conversation_memory(conversation_id) VALUES (?)",
                (conversation.id,),
            )
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"{self._conversation_select()} WHERE c.id = ?", (conversation_id,)
            ).fetchone()
            return self._load_conversation(connection, row) if row else None

    def list(self) -> list[Conversation]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(f"{self._conversation_select()} ORDER BY c.updated_at DESC").fetchall()
            return [self._load_conversation(connection, row) for row in rows]

    def delete(self, conversation_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            return cursor.rowcount > 0

    def append_user(self, conversation_id: str, query: str) -> ChatMessage:
        message = ChatMessage(role="user", query=query)
        now = utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            title = row["title"]
            if title == "新对话":
                title = query.strip().replace("\n", " ")[:42] or title
            connection.execute(
                "INSERT INTO messages(id, conversation_id, role, query, answer_json, created_at) "
                "VALUES (?, ?, 'user', ?, NULL, ?)",
                (message.id, conversation_id, query, message.created_at.isoformat()),
            )
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now.isoformat(), conversation_id),
            )
        return message

    def append_answer(self, conversation_id: str, answer: Answer) -> ChatMessage:
        message = ChatMessage(role="assistant", answer=answer)
        with self._lock, self._connect() as connection:
            if connection.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is None:
                raise KeyError(conversation_id)
            connection.execute(
                "INSERT INTO messages(id, conversation_id, role, query, answer_json, created_at) "
                "VALUES (?, ?, 'assistant', NULL, ?, ?)",
                (
                    message.id,
                    conversation_id,
                    json.dumps(answer.model_dump(mode="json"), ensure_ascii=False),
                    message.created_at.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (message.created_at.isoformat(), conversation_id),
            )
        return message

    def _ensure_memory(self, connection: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
        if connection.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is None:
            raise KeyError(conversation_id)
        connection.execute(
            "INSERT OR IGNORE INTO conversation_memory(conversation_id) VALUES (?)",
            (conversation_id,),
        )
        return connection.execute(
            "SELECT * FROM conversation_memory WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()

    def get_memory_record(self, conversation_id: str) -> dict:
        with self._lock, self._connect() as connection:
            return dict(self._ensure_memory(connection, conversation_id))

    def list_pending_memory_ids(self) -> list[str]:
        with self._lock, self._connect() as connection:
            return [
                row["conversation_id"]
                for row in connection.execute(
                    "SELECT conversation_id FROM conversation_memory "
                    "WHERE status = 'pending' AND pending_input_json != '' ORDER BY updated_at"
                ).fetchall()
            ]

    def save_local_memory(
        self,
        conversation_id: str,
        *,
        summary: MemorySummary,
        compacted_through_message_id: str,
        estimated_tokens: int,
        pending_input: dict | None,
    ) -> int:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            current = self._ensure_memory(connection, conversation_id)
            revision = int(current["revision"]) + 1
            status = "pending" if pending_input else "ready"
            connection.execute(
                "UPDATE conversation_memory SET summary_json = ?, summary_provider = 'local', "
                "compacted_through_message_id = ?, status = ?, revision = ?, estimated_tokens = ?, "
                "pending_input_json = ?, last_error = '', updated_at = ? WHERE conversation_id = ?",
                (
                    summary.model_dump_json(),
                    compacted_through_message_id,
                    status,
                    revision,
                    estimated_tokens,
                    json.dumps(pending_input, ensure_ascii=False) if pending_input else "",
                    now,
                    conversation_id,
                ),
            )
            return revision

    def claim_pending_memory(self, conversation_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = self._ensure_memory(connection, conversation_id)
            if row["status"] != "pending" or not row["pending_input_json"]:
                return None
            connection.execute(
                "UPDATE conversation_memory SET status = 'summarizing' WHERE conversation_id = ? AND revision = ?",
                (conversation_id, row["revision"]),
            )
            claimed = dict(row)
            claimed["status"] = "summarizing"
            return claimed

    def complete_model_memory(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
        summary: MemorySummary | None,
        error: str = "",
    ) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            if summary is None:
                cursor = connection.execute(
                    "UPDATE conversation_memory SET status = 'ready', pending_input_json = '', last_error = ?, "
                    "updated_at = ? WHERE conversation_id = ? AND revision = ?",
                    (error[:160], now, conversation_id, expected_revision),
                )
            else:
                cursor = connection.execute(
                    "UPDATE conversation_memory SET summary_json = ?, summary_provider = 'deepseek', status = 'ready', "
                    "estimated_tokens = ?, pending_input_json = '', last_error = '', updated_at = ? "
                    "WHERE conversation_id = ? AND revision = ?",
                    (
                        summary.model_dump_json(),
                        max(1, len(summary.model_dump_json()) // 3),
                        now,
                        conversation_id,
                        expected_revision,
                    ),
                )
            return cursor.rowcount > 0

    def reset_memory(self, conversation_id: str) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            try:
                current = self._ensure_memory(connection, conversation_id)
            except KeyError:
                return False
            last = connection.execute(
                "SELECT id FROM messages WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            connection.execute(
                "UPDATE conversation_memory SET summary_json = '{}', summary_provider = NULL, "
                "compacted_through_message_id = NULL, reset_after_message_id = ?, status = 'idle', "
                "revision = ?, estimated_tokens = 0, pending_input_json = '', last_error = '', updated_at = ? "
                "WHERE conversation_id = ?",
                (last["id"] if last else None, int(current["revision"]) + 1, now, conversation_id),
            )
            return True

    def memory_state(self, conversation_id: str) -> ConversationMemory:
        conversation = self.get(conversation_id)
        if conversation is None:
            raise KeyError(conversation_id)
        record = self.get_memory_record(conversation_id)
        try:
            summary = MemorySummary.model_validate_json(record["summary_json"] or "{}")
        except (ValueError, TypeError):
            summary = MemorySummary()
        visible_messages = conversation.messages
        if record["reset_after_message_id"]:
            boundary_index = next(
                (
                    index
                    for index, message in enumerate(conversation.messages)
                    if message.id == record["reset_after_message_id"]
                ),
                -1,
            )
            visible_messages = conversation.messages[boundary_index + 1 :]
        last_context = next(
            (
                message.answer.context
                for message in reversed(visible_messages)
                if message.role == "assistant" and message.answer is not None
            ),
            None,
        )
        used_ids = set(last_context.used_message_ids if last_context else [])
        previews: list[MemoryMessagePreview] = []
        for message in conversation.messages:
            if message.id not in used_ids:
                continue
            preview = message.query if message.role == "user" else message.answer.summary if message.answer else ""
            previews.append(
                MemoryMessagePreview(
                    id=message.id,
                    role=message.role,
                    preview=(preview or "")[:240],
                    created_at=message.created_at,
                )
            )
        return ConversationMemory(
            conversation_id=conversation_id,
            status=record["status"],
            summary=summary,
            summary_provider=record["summary_provider"],
            summary_updated_at=self._parse_time(record["updated_at"]) if record["updated_at"] else None,
            compacted_through_message_id=record["compacted_through_message_id"],
            reset_after_message_id=record["reset_after_message_id"],
            estimated_tokens=last_context.estimated_tokens if last_context else int(record["estimated_tokens"]),
            recent_messages=previews,
            last_error=record["last_error"],
        )

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> UserProfile:
        return UserProfile(
            id=row["id"],
            personalization_enabled=bool(row["personalization_enabled"]),
            auto_memory_enabled=bool(row["auto_memory_enabled"]),
            revision=int(row["revision"]),
            extraction_epoch=int(row["extraction_epoch"]),
            active_memory_count=int(row["active_memory_count"] or 0),
            pending_task_count=int(row["pending_task_count"] or 0),
            created_at=ConversationRepository._parse_time(row["created_at"]),
            updated_at=ConversationRepository._parse_time(row["updated_at"]),
        )

    @staticmethod
    def _profile_select() -> str:
        return """
            SELECT p.*,
                   (SELECT COUNT(*) FROM user_memories m
                    WHERE m.profile_id = p.id AND m.status = 'active') AS active_memory_count,
                   (SELECT COUNT(*) FROM user_memory_extraction_tasks t
                    WHERE t.profile_id = p.id AND t.status IN ('pending', 'extracting')
                    AND t.extraction_epoch = p.extraction_epoch)
                       AS pending_task_count
            FROM user_profiles p
        """

    def get_profile(self) -> UserProfile:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"{self._profile_select()} WHERE p.id = ?", (LOCAL_USER_PROFILE_ID,)
            ).fetchone()
            if row is None:
                raise KeyError(LOCAL_USER_PROFILE_ID)
            return self._profile_from_row(row)

    def update_profile(
        self,
        *,
        personalization_enabled: bool | None = None,
        auto_memory_enabled: bool | None = None,
    ) -> UserProfile:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_profiles WHERE id = ?", (LOCAL_USER_PROFILE_ID,)
            ).fetchone()
            if row is None:
                raise KeyError(LOCAL_USER_PROFILE_ID)
            personalized = (
                bool(row["personalization_enabled"])
                if personalization_enabled is None
                else bool(personalization_enabled)
            )
            automatic = (
                bool(row["auto_memory_enabled"])
                if auto_memory_enabled is None
                else bool(auto_memory_enabled)
            )
            if (
                personalized == bool(row["personalization_enabled"])
                and automatic == bool(row["auto_memory_enabled"])
            ):
                return self.get_profile()
            extraction_disabled = not personalized or not automatic
            epoch = int(row["extraction_epoch"]) + (1 if extraction_disabled else 0)
            connection.execute(
                "UPDATE user_profiles SET personalization_enabled = ?, auto_memory_enabled = ?, "
                "revision = revision + 1, extraction_epoch = ?, updated_at = ? WHERE id = ?",
                (int(personalized), int(automatic), epoch, now, LOCAL_USER_PROFILE_ID),
            )
            if extraction_disabled:
                connection.execute(
                    "UPDATE user_memory_extraction_tasks SET status = 'cancelled', sanitized_input = '', "
                    "last_error = '', updated_at = ? WHERE profile_id = ? "
                    "AND status IN ('pending', 'extracting')",
                    (now, LOCAL_USER_PROFILE_ID),
                )
        return self.get_profile()

    @staticmethod
    def _user_memory_from_row(row: sqlite3.Row | dict) -> UserMemory:
        return UserMemory(
            id=row["id"],
            scope=row["scope"],
            knowledge_base_id=row["knowledge_base_id"],
            category=row["category"],
            key=row["key"],
            value=row["value"],
            display_text=row["display_text"],
            confidence=float(row["confidence"]),
            pinned=bool(row["pinned"]),
            status=row["status"],
            supersedes_id=row["supersedes_id"],
            source_conversation_id=row["source_conversation_id"],
            source_message_id=row["source_message_id"],
            extraction_provider=row["extraction_provider"],
            last_used_at=(
                ConversationRepository._parse_time(row["last_used_at"])
                if row["last_used_at"]
                else None
            ),
            use_count=int(row["use_count"] or 0),
            created_at=ConversationRepository._parse_time(row["created_at"]),
            updated_at=ConversationRepository._parse_time(row["updated_at"]),
        )

    @staticmethod
    def _normalize_memory_values(
        *,
        scope: str,
        knowledge_base_id: str | None,
        category: str,
        key: str,
        value: str,
        display_text: str,
        confidence: float,
        extraction_provider: str,
    ) -> dict:
        normalized_scope = scope.strip()
        normalized_category = category.strip()
        normalized_key = re.sub(r"[^a-z0-9_.:-]+", "_", key.strip().casefold()).strip("_")
        normalized_value = value.strip()
        normalized_display = display_text.strip()
        normalized_base_id = knowledge_base_id.strip() if knowledge_base_id else None
        if normalized_scope not in {"global", "knowledge_base"}:
            raise ValueError("无效的记忆范围")
        if normalized_scope == "global":
            if normalized_base_id is not None or normalized_category not in GLOBAL_MEMORY_CATEGORIES:
                raise ValueError("全局记忆只允许回答风格和熟练度")
        elif normalized_base_id is None or normalized_category not in SCOPED_MEMORY_CATEGORIES:
            raise ValueError("知识库记忆需要有效知识库以及平台、工具链或项目约束分类")
        if not normalized_key or len(normalized_key) > 80:
            raise ValueError("记忆键不能为空且不能超过 80 个字符")
        if not normalized_value or len(normalized_value) > 160:
            raise ValueError("记忆值不能为空且不能超过 160 个字符")
        if not normalized_display or len(normalized_display) > 240:
            raise ValueError("记忆显示文本不能为空且不能超过 240 个字符")
        normalized_confidence = float(confidence)
        if not 0 <= normalized_confidence <= 1:
            raise ValueError("记忆置信度必须在 0 到 1 之间")
        if extraction_provider not in MEMORY_PROVIDERS:
            raise ValueError("无效的记忆提取来源")
        return {
            "scope": normalized_scope,
            "knowledge_base_id": normalized_base_id,
            "category": normalized_category,
            "key": normalized_key,
            "value": normalized_value,
            "display_text": normalized_display,
            "confidence": normalized_confidence,
            "extraction_provider": extraction_provider,
        }

    @staticmethod
    def _memory_filters(
        *,
        q: str | None,
        scope: str | None,
        category: str | None,
        knowledge_base_id: str | None,
        status: str | None,
        ids: list[str] | None,
        source_message_id: str | None,
    ) -> tuple[str, list[object]]:
        if status not in {None, "active", "superseded"}:
            raise ValueError("无效的记忆状态")
        if scope not in {None, "global", "knowledge_base"}:
            raise ValueError("无效的记忆范围")
        if category is not None and category not in GLOBAL_MEMORY_CATEGORIES | SCOPED_MEMORY_CATEGORIES:
            raise ValueError("无效的记忆分类")
        clauses = ["profile_id = ?"]
        parameters: list[object] = [LOCAL_USER_PROFILE_ID]
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if scope is not None:
            clauses.append("scope = ?")
            parameters.append(scope)
        if category is not None:
            clauses.append("category = ?")
            parameters.append(category)
        if knowledge_base_id is not None:
            clauses.append("knowledge_base_id = ?")
            parameters.append(knowledge_base_id)
        if source_message_id is not None:
            clauses.append("source_message_id = ?")
            parameters.append(source_message_id)
        if q and q.strip():
            clauses.append("(key LIKE ? OR value LIKE ? OR display_text LIKE ?)")
            needle = f"%{q.strip()}%"
            parameters.extend([needle, needle, needle])
        if ids is not None:
            if not ids:
                clauses.append("0")
            else:
                clauses.append(f"id IN ({','.join('?' for _ in ids)})")
                parameters.extend(ids)
        return " AND ".join(clauses), parameters

    def list_user_memory_records(
        self,
        q: str | None = None,
        scope: str | None = None,
        category: str | None = None,
        knowledge_base_id: str | None = None,
        status: str | None = "active",
        ids: list[str] | None = None,
        source_message_id: str | None = None,
    ) -> list[dict]:
        where, parameters = self._memory_filters(
            q=q,
            scope=scope,
            category=category,
            knowledge_base_id=knowledge_base_id,
            status=status,
            ids=ids,
            source_message_id=source_message_id,
        )
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM user_memories WHERE {where} "
                "ORDER BY pinned DESC, updated_at DESC, id",
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_user_memories(
        self,
        q: str | None = None,
        scope: str | None = None,
        category: str | None = None,
        knowledge_base_id: str | None = None,
        status: str | None = "active",
        ids: list[str] | None = None,
        source_message_id: str | None = None,
    ) -> list[UserMemory]:
        return [
            self._user_memory_from_row(row)
            for row in self.list_user_memory_records(
                q=q,
                scope=scope,
                category=category,
                knowledge_base_id=knowledge_base_id,
                status=status,
                ids=ids,
                source_message_id=source_message_id,
            )
        ]

    def get_user_memory_record(self, memory_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_memories WHERE id = ? AND profile_id = ?",
                (memory_id, LOCAL_USER_PROFILE_ID),
            ).fetchone()
            return dict(row) if row else None

    def get_user_memory(self, memory_id: str) -> UserMemory | None:
        record = self.get_user_memory_record(memory_id)
        return self._user_memory_from_row(record) if record else None

    def _upsert_user_memory_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        scope: str,
        knowledge_base_id: str | None,
        category: str,
        key: str,
        value: str,
        display_text: str,
        confidence: float,
        pinned: bool,
        source_conversation_id: str | None,
        source_message_id: str | None,
        extraction_provider: str,
        embedding: bytes | None,
        embedding_dimension: int | None,
        embedding_version: str,
        now: str,
    ) -> tuple[str, bool]:
        normalized = self._normalize_memory_values(
            scope=scope,
            knowledge_base_id=knowledge_base_id,
            category=category,
            key=key,
            value=value,
            display_text=display_text,
            confidence=confidence,
            extraction_provider=extraction_provider,
        )
        knowledge_base_id = normalized["knowledge_base_id"]
        if knowledge_base_id is not None and connection.execute(
            "SELECT 1 FROM knowledge_bases WHERE id = ?", (knowledge_base_id,)
        ).fetchone() is None:
            raise KeyError(knowledge_base_id)
        if embedding is None:
            embedding_dimension = None
            embedding_version = ""
        elif embedding_dimension is None or int(embedding_dimension) <= 0:
            raise ValueError("记忆向量维度无效")
        existing = connection.execute(
            "SELECT * FROM user_memories WHERE profile_id = ? AND scope = ? "
            "AND ifnull(knowledge_base_id, '') = ifnull(?, '') AND category = ? AND key = ? "
            "AND status = 'active'",
            (
                LOCAL_USER_PROFILE_ID,
                normalized["scope"],
                knowledge_base_id,
                normalized["category"],
                normalized["key"],
            ),
        ).fetchone()
        if existing is not None and existing["value"].strip().casefold() == normalized["value"].casefold():
            memory_id = existing["id"]
            connection.execute(
                "UPDATE user_memories SET display_text = ?, confidence = ?, pinned = ?, "
                "source_conversation_id = COALESCE(?, source_conversation_id), "
                "source_message_id = COALESCE(?, source_message_id), extraction_provider = ?, "
                "embedding = COALESCE(?, embedding), "
                "embedding_dimension = CASE WHEN ? IS NULL THEN embedding_dimension ELSE ? END, "
                "embedding_version = CASE WHEN ? IS NULL THEN embedding_version ELSE ? END, "
                "updated_at = ? WHERE id = ?",
                (
                    normalized["display_text"],
                    max(float(existing["confidence"]), normalized["confidence"]),
                    int(bool(existing["pinned"]) or pinned),
                    source_conversation_id,
                    source_message_id,
                    normalized["extraction_provider"],
                    embedding,
                    embedding,
                    embedding_dimension,
                    embedding,
                    embedding_version,
                    now,
                    memory_id,
                ),
            )
            return memory_id, False

        supersedes_id = existing["id"] if existing is not None else None
        if existing is not None:
            connection.execute(
                "UPDATE user_memories SET status = 'superseded', updated_at = ? WHERE id = ?",
                (now, supersedes_id),
            )
        memory_id = str(uuid4())
        connection.execute(
            "INSERT INTO user_memories(id, profile_id, scope, knowledge_base_id, category, key, value, "
            "display_text, confidence, pinned, status, supersedes_id, source_conversation_id, "
            "source_message_id, extraction_provider, embedding, embedding_dimension, embedding_version, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_id,
                LOCAL_USER_PROFILE_ID,
                normalized["scope"],
                knowledge_base_id,
                normalized["category"],
                normalized["key"],
                normalized["value"],
                normalized["display_text"],
                normalized["confidence"],
                int(bool(existing["pinned"]) or pinned) if existing is not None else int(pinned),
                supersedes_id,
                source_conversation_id,
                source_message_id,
                normalized["extraction_provider"],
                embedding,
                embedding_dimension,
                embedding_version if embedding is not None else "",
                now,
                now,
            ),
        )
        return memory_id, True

    def upsert_user_memory(
        self,
        scope: str,
        knowledge_base_id: str | None,
        category: str,
        key: str,
        value: str,
        display_text: str,
        confidence: float,
        pinned: bool = False,
        source_conversation_id: str | None = None,
        source_message_id: str | None = None,
        extraction_provider: str = "local",
        embedding: bytes | None = None,
        embedding_dimension: int | None = None,
        embedding_version: str = "",
    ) -> UserMemory:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            memory_id, _ = self._upsert_user_memory_in_connection(
                connection,
                scope=scope,
                knowledge_base_id=knowledge_base_id,
                category=category,
                key=key,
                value=value,
                display_text=display_text,
                confidence=confidence,
                pinned=pinned,
                source_conversation_id=source_conversation_id,
                source_message_id=source_message_id,
                extraction_provider=extraction_provider,
                embedding=embedding,
                embedding_dimension=embedding_dimension,
                embedding_version=embedding_version,
                now=now,
            )
            connection.execute(
                "UPDATE user_profiles SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (now, LOCAL_USER_PROFILE_ID),
            )
        return self.get_user_memory(memory_id)  # type: ignore[return-value]

    def patch_user_memory(
        self,
        memory_id: str,
        *,
        value: str | None = None,
        display_text: str | None = None,
        pinned: bool | None = None,
        embedding: bytes | None = None,
        embedding_dimension: int | None = None,
        embedding_version: str = "",
    ) -> UserMemory:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM user_memories WHERE id = ? AND profile_id = ?",
                (memory_id, LOCAL_USER_PROFILE_ID),
            ).fetchone()
            if current is None:
                raise KeyError(memory_id)
            if current["status"] != "active":
                raise ValueError("已被替代的记忆不能编辑")
            resolved_value = current["value"] if value is None else value
            resolved_display = current["display_text"] if display_text is None else display_text
            resolved_pinned = bool(current["pinned"]) if pinned is None else bool(pinned)
            if value is None:
                embedding = current["embedding"]
                embedding_dimension = current["embedding_dimension"]
                embedding_version = current["embedding_version"]
            updated_id, _ = self._upsert_user_memory_in_connection(
                connection,
                scope=current["scope"],
                knowledge_base_id=current["knowledge_base_id"],
                category=current["category"],
                key=current["key"],
                value=resolved_value,
                display_text=resolved_display,
                confidence=1.0,
                pinned=resolved_pinned,
                source_conversation_id=current["source_conversation_id"],
                source_message_id=current["source_message_id"],
                extraction_provider="manual",
                embedding=embedding,
                embedding_dimension=embedding_dimension,
                embedding_version=embedding_version,
                now=now,
            )
            if pinned is not None:
                connection.execute(
                    "UPDATE user_memories SET pinned = ? WHERE id = ?",
                    (int(resolved_pinned), updated_id),
                )
            connection.execute(
                "UPDATE user_profiles SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (now, LOCAL_USER_PROFILE_ID),
            )
        return self.get_user_memory(updated_id)  # type: ignore[return-value]

    def update_user_memory(self, memory_id: str, **changes) -> UserMemory:
        return self.patch_user_memory(memory_id, **changes)

    def delete_user_memory_chain(self, memory_id: str) -> int:
        deleted = 0
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT scope, knowledge_base_id, category, key FROM user_memories "
                "WHERE id = ? AND profile_id = ?",
                (memory_id, LOCAL_USER_PROFILE_ID),
            ).fetchone()
            if row is None:
                return 0
            cursor = connection.execute(
                "DELETE FROM user_memories WHERE profile_id = ? AND scope = ? "
                "AND ifnull(knowledge_base_id, '') = ifnull(?, '') AND category = ? AND key = ?",
                (
                    LOCAL_USER_PROFILE_ID,
                    row["scope"],
                    row["knowledge_base_id"],
                    row["category"],
                    row["key"],
                ),
            )
            deleted = cursor.rowcount
            now = utc_now().isoformat()
            connection.execute(
                "UPDATE user_profiles SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (now, LOCAL_USER_PROFILE_ID),
            )
        self._checkpoint_wal()
        return deleted

    def delete_user_memory(self, memory_id: str) -> bool:
        return self.delete_user_memory_chain(memory_id) > 0

    def undo_user_memory(self, memory_id: str) -> UserMemory | None:
        restored_id = None
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM user_memories WHERE id = ? AND profile_id = ?",
                (memory_id, LOCAL_USER_PROFILE_ID),
            ).fetchone()
            if current is None:
                raise KeyError(memory_id)
            if current["status"] != "active":
                raise ValueError("只能撤销当前生效的记忆")
            predecessor = None
            if current["supersedes_id"]:
                predecessor = connection.execute(
                    "SELECT * FROM user_memories WHERE id = ? AND profile_id = ?",
                    (current["supersedes_id"], LOCAL_USER_PROFILE_ID),
                ).fetchone()
            connection.execute("DELETE FROM user_memories WHERE id = ?", (memory_id,))
            if predecessor is not None:
                connection.execute(
                    "UPDATE user_memories SET status = 'active', updated_at = ? WHERE id = ?",
                    (now, predecessor["id"]),
                )
                restored_id = predecessor["id"]
            connection.execute(
                "UPDATE user_profiles SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (now, LOCAL_USER_PROFILE_ID),
            )
        self._checkpoint_wal()
        return self.get_user_memory(restored_id) if restored_id else None

    def reset_user_memories(
        self,
        *,
        scope: str | None = None,
        knowledge_base_id: str | None = None,
    ) -> int:
        if scope not in {None, "global", "knowledge_base"}:
            raise ValueError("无效的记忆范围")
        if scope == "global" and knowledge_base_id is not None:
            raise ValueError("全局记忆不能指定知识库")
        if scope == "knowledge_base" and knowledge_base_id is None:
            raise ValueError("知识库级清空必须指定知识库")
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            clauses = ["profile_id = ?"]
            parameters: list[object] = [LOCAL_USER_PROFILE_ID]
            if scope is not None:
                clauses.append("scope = ?")
                parameters.append(scope)
            if knowledge_base_id is not None:
                clauses.append("knowledge_base_id = ?")
                parameters.append(knowledge_base_id)
            deleted = connection.execute(
                f"DELETE FROM user_memories WHERE {' AND '.join(clauses)}", parameters
            ).rowcount
            # Any in-flight extraction could recreate a just-forgotten item. A
            # reset therefore invalidates every extraction task, even for a
            # narrower scope, while preserving unrelated active memories.
            connection.execute(
                "DELETE FROM user_memory_extraction_tasks WHERE profile_id = ?",
                (LOCAL_USER_PROFILE_ID,),
            )
            connection.execute(
                "UPDATE user_profiles SET revision = revision + 1, extraction_epoch = extraction_epoch + 1, "
                "updated_at = ? WHERE id = ?",
                (now, LOCAL_USER_PROFILE_ID),
            )
        self._checkpoint_wal()
        return deleted

    def mark_user_memories_used(self, memory_ids: list[str]) -> int:
        unique_ids = list(dict.fromkeys(memory_ids))
        if not unique_ids:
            return 0
        now = utc_now().isoformat()
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE user_memories SET use_count = use_count + 1, last_used_at = ? "
                f"WHERE profile_id = ? AND status = 'active' AND id IN ({placeholders})",
                [now, LOCAL_USER_PROFILE_ID, *unique_ids],
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE user_profiles SET revision = revision + 1, updated_at = ? WHERE id = ?",
                    (now, LOCAL_USER_PROFILE_ID),
                )
            return cursor.rowcount

    def count_knowledge_base_memories(self, knowledge_base_id: str) -> int:
        with self._lock, self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM user_memories WHERE profile_id = ? "
                    "AND knowledge_base_id = ? AND status = 'active'",
                    (LOCAL_USER_PROFILE_ID, knowledge_base_id),
                ).fetchone()[0]
            )

    @staticmethod
    def _task_record(row: sqlite3.Row | dict) -> dict:
        record = dict(row)
        try:
            record["result_memory_ids"] = json.loads(record.get("result_memory_ids_json") or "[]")
        except (TypeError, ValueError):
            record["result_memory_ids"] = []
        return record

    def create_memory_extraction_task(
        self,
        conversation_id: str,
        source_message_id: str,
        knowledge_base_id: str,
        sanitized_input: str,
    ) -> dict | None:
        sanitized = sanitized_input.strip()
        if not sanitized:
            raise ValueError("记忆提取输入不能为空")
        if len(sanitized) > 2000:
            sanitized = sanitized[:2000]
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            profile = connection.execute(
                "SELECT * FROM user_profiles WHERE id = ?", (LOCAL_USER_PROFILE_ID,)
            ).fetchone()
            if profile is None:
                raise KeyError(LOCAL_USER_PROFILE_ID)
            if not profile["personalization_enabled"] or not profile["auto_memory_enabled"]:
                return None
            message = connection.execute(
                "SELECT m.role, m.conversation_id, c.knowledge_base_id FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id WHERE m.id = ?",
                (source_message_id,),
            ).fetchone()
            if message is None or message["role"] != "user":
                raise KeyError(source_message_id)
            if message["conversation_id"] != conversation_id:
                raise ValueError("来源消息不属于指定会话")
            if message["knowledge_base_id"] != knowledge_base_id:
                raise ValueError("来源会话与知识库不一致")
            existing = connection.execute(
                "SELECT * FROM user_memory_extraction_tasks WHERE source_message_id = ?",
                (source_message_id,),
            ).fetchone()
            if existing is not None:
                return self._task_record(existing)
            task_id = str(uuid4())
            connection.execute(
                "INSERT INTO user_memory_extraction_tasks(id, profile_id, conversation_id, "
                "source_message_id, knowledge_base_id, status, sanitized_input, extraction_epoch, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    task_id,
                    LOCAL_USER_PROFILE_ID,
                    conversation_id,
                    source_message_id,
                    knowledge_base_id,
                    sanitized,
                    int(profile["extraction_epoch"]),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM user_memory_extraction_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return self._task_record(row)

    def get_memory_extraction_task(self, task_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_memory_extraction_tasks WHERE id = ? AND profile_id = ?",
                (task_id, LOCAL_USER_PROFILE_ID),
            ).fetchone()
            return self._task_record(row) if row else None

    def list_pending_memory_task_ids(self) -> list[str]:
        with self._lock, self._connect() as connection:
            return [
                row["id"]
                for row in connection.execute(
                    "SELECT t.id FROM user_memory_extraction_tasks t "
                    "JOIN user_profiles p ON p.id = t.profile_id "
                    "WHERE t.profile_id = ? AND t.status = 'pending' "
                    "AND p.personalization_enabled = 1 AND p.auto_memory_enabled = 1 "
                    "AND t.extraction_epoch = p.extraction_epoch ORDER BY t.created_at, t.id",
                    (LOCAL_USER_PROFILE_ID,),
                ).fetchall()
            ]

    def list_pending_memory_extraction_task_ids(self) -> list[str]:
        return self.list_pending_memory_task_ids()

    def claim_memory_extraction_task(
        self,
        task_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> dict | None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            task = connection.execute(
                "SELECT t.*, p.personalization_enabled, p.auto_memory_enabled, "
                "p.extraction_epoch AS current_epoch FROM user_memory_extraction_tasks t "
                "JOIN user_profiles p ON p.id = t.profile_id WHERE t.id = ? AND t.profile_id = ?",
                (task_id, LOCAL_USER_PROFILE_ID),
            ).fetchone()
            if task is None or task["status"] != "pending":
                return None
            valid_epoch = int(task["extraction_epoch"]) == int(task["current_epoch"])
            if expected_epoch is not None:
                valid_epoch = valid_epoch and int(task["extraction_epoch"]) == int(expected_epoch)
            if not valid_epoch or not task["personalization_enabled"] or not task["auto_memory_enabled"]:
                connection.execute(
                    "UPDATE user_memory_extraction_tasks SET status = 'cancelled', sanitized_input = '', "
                    "updated_at = ? WHERE id = ?",
                    (now, task_id),
                )
                return None
            cursor = connection.execute(
                "UPDATE user_memory_extraction_tasks SET status = 'extracting', "
                "attempt_count = attempt_count + 1, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (now, task_id),
            )
            if cursor.rowcount == 0:
                return None
            claimed = connection.execute(
                "SELECT * FROM user_memory_extraction_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return self._task_record(claimed)

    def complete_memory_extraction_task(
        self,
        task_id: str,
        candidates: list[dict] | None = None,
        *,
        memory_ids: list[str] | None = None,
        expected_epoch: int | None = None,
        expected_attempt: int | None = None,
    ) -> list[UserMemory]:
        now = utc_now().isoformat()
        result_memory_ids: list[str] = []
        with self._lock, self._connect() as connection:
            task = connection.execute(
                "SELECT t.*, p.personalization_enabled, p.auto_memory_enabled, "
                "p.extraction_epoch AS current_epoch FROM user_memory_extraction_tasks t "
                "JOIN user_profiles p ON p.id = t.profile_id WHERE t.id = ? AND t.profile_id = ?",
                (task_id, LOCAL_USER_PROFILE_ID),
            ).fetchone()
            if task is None or task["status"] != "extracting":
                return []
            valid = int(task["extraction_epoch"]) == int(task["current_epoch"])
            if expected_epoch is not None:
                valid = valid and int(task["extraction_epoch"]) == int(expected_epoch)
            if expected_attempt is not None:
                valid = valid and int(task["attempt_count"]) == int(expected_attempt)
            valid = valid and bool(task["personalization_enabled"]) and bool(task["auto_memory_enabled"])
            if not valid:
                connection.execute(
                    "UPDATE user_memory_extraction_tasks SET status = 'cancelled', sanitized_input = '', "
                    "updated_at = ? WHERE id = ?",
                    (now, task_id),
                )
                return []

            if memory_ids is not None:
                requested_ids = list(dict.fromkeys(memory_ids))
                selected_rows: list[sqlite3.Row] = []
                if requested_ids:
                    placeholders = ",".join("?" for _ in requested_ids)
                    selected_rows = connection.execute(
                            f"SELECT * FROM user_memories WHERE profile_id = ? "
                            f"AND id IN ({placeholders})",
                            [LOCAL_USER_PROFILE_ID, *requested_ids],
                        ).fetchall()
                    selected_by_id = {row["id"]: row for row in selected_rows}
                    result_memory_ids = [value for value in requested_ids if value in selected_by_id]
                    selected_rows = [selected_by_id[value] for value in result_memory_ids]
                connection.execute(
                    "UPDATE user_memory_extraction_tasks SET status = 'ready', sanitized_input = '', "
                    "result_memory_ids_json = ?, last_error = '', updated_at = ? WHERE id = ?",
                    (json.dumps(result_memory_ids, ensure_ascii=False), now, task_id),
                )
                return [self._user_memory_from_row(row) for row in selected_rows]

            changed = False
            for candidate in (candidates or [])[:4]:
                action = str(candidate.get("action") or "ADD").strip().upper()
                if action == "NOOP":
                    continue
                if action not in {"ADD", "UPDATE", "SUPERSEDE"}:
                    continue
                try:
                    confidence = float(candidate.get("confidence", 0))
                except (TypeError, ValueError):
                    continue
                if confidence < 0.80 or confidence > 1:
                    continue
                scope = str(candidate.get("scope") or "")
                category = str(candidate.get("category") or "")
                knowledge_base_id = task["knowledge_base_id"] if scope == "knowledge_base" else None
                try:
                    normalized = self._normalize_memory_values(
                        scope=scope,
                        knowledge_base_id=knowledge_base_id,
                        category=category,
                        key=str(candidate.get("key") or ""),
                        value=str(candidate.get("value") or ""),
                        display_text=str(candidate.get("display_text") or ""),
                        confidence=confidence,
                        extraction_provider="deepseek",
                    )
                except (TypeError, ValueError):
                    continue
                active = connection.execute(
                    "SELECT updated_at FROM user_memories WHERE profile_id = ? AND scope = ? "
                    "AND ifnull(knowledge_base_id, '') = ifnull(?, '') AND category = ? AND key = ? "
                    "AND status = 'active'",
                    (
                        LOCAL_USER_PROFILE_ID,
                        normalized["scope"],
                        normalized["knowledge_base_id"],
                        normalized["category"],
                        normalized["key"],
                    ),
                ).fetchone()
                if active is not None and active["updated_at"] > task["created_at"]:
                    # A manual or later automatic edit wins over this older model call.
                    continue
                try:
                    memory_id, memory_created = self._upsert_user_memory_in_connection(
                        connection,
                        **normalized,
                        pinned=bool(candidate.get("pinned", False)),
                        source_conversation_id=task["conversation_id"],
                        source_message_id=task["source_message_id"],
                        embedding=candidate.get("embedding"),
                        embedding_dimension=candidate.get("embedding_dimension"),
                        embedding_version=str(candidate.get("embedding_version") or ""),
                        now=now,
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                result_memory_ids.append(memory_id)
                changed = changed or memory_created or bool(memory_id)
            result_memory_ids = list(dict.fromkeys(result_memory_ids))
            connection.execute(
                "UPDATE user_memory_extraction_tasks SET status = 'ready', sanitized_input = '', "
                "result_memory_ids_json = ?, last_error = '', updated_at = ? WHERE id = ?",
                (json.dumps(result_memory_ids, ensure_ascii=False), now, task_id),
            )
            if changed:
                connection.execute(
                    "UPDATE user_profiles SET revision = revision + 1, updated_at = ? WHERE id = ?",
                    (now, LOCAL_USER_PROFILE_ID),
                )
        return (
            self.list_user_memories(status=None, ids=result_memory_ids)
            if result_memory_ids
            else []
        )

    def fail_memory_extraction_task(
        self,
        task_id: str,
        error: str,
        *,
        expected_epoch: int | None = None,
        expected_attempt: int | None = None,
    ) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            task = connection.execute(
                "SELECT * FROM user_memory_extraction_tasks WHERE id = ? AND profile_id = ?",
                (task_id, LOCAL_USER_PROFILE_ID),
            ).fetchone()
            if task is None or task["status"] != "extracting":
                return False
            matches = expected_epoch is None or int(task["extraction_epoch"]) == int(expected_epoch)
            matches = matches and (
                expected_attempt is None or int(task["attempt_count"]) == int(expected_attempt)
            )
            if not matches:
                return False
            cursor = connection.execute(
                "UPDATE user_memory_extraction_tasks SET status = 'failed', sanitized_input = '', "
                "last_error = ?, updated_at = ? WHERE id = ? AND status = 'extracting'",
                (error[:160], now, task_id),
            )
            return cursor.rowcount > 0

    def cancel_memory_extraction_tasks(self) -> int:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE user_memory_extraction_tasks SET status = 'cancelled', sanitized_input = '', "
                "last_error = '', updated_at = ? WHERE profile_id = ? "
                "AND status IN ('pending', 'extracting')",
                (now, LOCAL_USER_PROFILE_ID),
            )
            return cursor.rowcount

    def _knowledge_base_from_row(self, row: sqlite3.Row) -> KnowledgeBase:
        return KnowledgeBase(
            id=row["id"],
            name=row["name"],
            is_builtin=bool(row["is_builtin"]),
            document_count=int(row["document_count"] or 0),
            ready_document_count=int(row["ready_document_count"] or 0),
            chunk_count=int(row["chunk_count"] or 0),
            memory_count=int(row["memory_count"] or 0),
            created_at=self._parse_time(row["created_at"]),
            updated_at=self._parse_time(row["updated_at"]),
        )

    @staticmethod
    def _knowledge_base_select() -> str:
        return """
            SELECT kb.*,
                   COUNT(DISTINCT d.id) AS document_count,
                   COUNT(DISTINCT CASE WHEN d.status = 'ready' THEN d.id END) AS ready_document_count,
                   COUNT(DISTINCT ch.id) AS chunk_count,
                   (SELECT COUNT(*) FROM user_memories um
                    WHERE um.knowledge_base_id = kb.id AND um.status = 'active') AS memory_count
            FROM knowledge_bases kb
            LEFT JOIN knowledge_documents d ON d.knowledge_base_id = kb.id
            LEFT JOIN knowledge_chunks ch ON ch.knowledge_base_id = kb.id
        """

    def list_knowledge_bases(self) -> list[KnowledgeBase]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"{self._knowledge_base_select()} GROUP BY kb.id ORDER BY kb.is_builtin DESC, kb.updated_at DESC"
            ).fetchall()
            return [self._knowledge_base_from_row(row) for row in rows]

    def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBase | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"{self._knowledge_base_select()} WHERE kb.id = ? GROUP BY kb.id",
                (knowledge_base_id,),
            ).fetchone()
            return self._knowledge_base_from_row(row) if row else None

    def create_knowledge_base(self, name: str) -> KnowledgeBase:
        now = utc_now()
        knowledge_base_id = str(uuid4())
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO knowledge_bases(id, name, is_builtin, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
                    (knowledge_base_id, name.strip(), now.isoformat(), now.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("知识库名称已存在") from exc
        return self.get_knowledge_base(knowledge_base_id)  # type: ignore[return-value]

    def rename_knowledge_base(self, knowledge_base_id: str, name: str) -> KnowledgeBase:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            try:
                cursor = connection.execute(
                    "UPDATE knowledge_bases SET name = ?, updated_at = ? WHERE id = ?",
                    (name.strip(), now, knowledge_base_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("知识库名称已存在") from exc
            if cursor.rowcount == 0:
                raise KeyError(knowledge_base_id)
        return self.get_knowledge_base(knowledge_base_id)  # type: ignore[return-value]

    def delete_knowledge_base(self, knowledge_base_id: str) -> list[str]:
        deleted_memory_count = 0
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT is_builtin FROM knowledge_bases WHERE id = ?", (knowledge_base_id,)
            ).fetchone()
            if row is None:
                raise KeyError(knowledge_base_id)
            if row["is_builtin"]:
                raise ValueError("内置知识库不能删除")
            paths = [
                item["stored_path"]
                for item in connection.execute(
                    "SELECT stored_path FROM knowledge_documents WHERE knowledge_base_id = ?",
                    (knowledge_base_id,),
                ).fetchall()
            ]
            deleted_memory_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM user_memories WHERE knowledge_base_id = ?",
                    (knowledge_base_id,),
                ).fetchone()[0]
            )
            connection.execute("DELETE FROM knowledge_bases WHERE id = ?", (knowledge_base_id,))
            if deleted_memory_count:
                now = utc_now().isoformat()
                connection.execute(
                    "UPDATE user_profiles SET revision = revision + 1, updated_at = ? WHERE id = ?",
                    (now, LOCAL_USER_PROFILE_ID),
                )
        if deleted_memory_count:
            self._checkpoint_wal()
        return paths

    def create_document(
        self,
        *,
        knowledge_base_id: str,
        filename: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
        stored_path: str,
    ) -> KnowledgeDocument:
        if self.get_knowledge_base(knowledge_base_id) is None:
            raise KeyError(knowledge_base_id)
        document_id = str(uuid4())
        now = utc_now().isoformat()
        title = Path(filename).stem.strip() or filename
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO knowledge_documents(id, knowledge_base_id, filename, title, media_type, size_bytes, "
                    "sha256, stored_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (document_id, knowledge_base_id, filename, title, media_type, size_bytes, sha256, stored_path, now, now),
                )
                connection.execute(
                    "UPDATE knowledge_bases SET updated_at = ? WHERE id = ?", (now, knowledge_base_id)
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("该知识库中已存在内容相同的文件") from exc
        return self.get_document(document_id)  # type: ignore[return-value]

    @staticmethod
    def _document_select() -> str:
        return """
            SELECT d.*,
                   (SELECT COUNT(*) FROM knowledge_chunks ch WHERE ch.document_id = d.id) AS chunk_count,
                   (SELECT COUNT(*) FROM knowledge_parent_chunks p WHERE p.document_id = d.id) AS parent_count
            FROM knowledge_documents d
        """

    def _document_from_row(self, row: sqlite3.Row) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=row["id"],
            knowledge_base_id=row["knowledge_base_id"],
            filename=row["filename"],
            title=row["title"],
            media_type=row["media_type"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            source_kind=row["source_kind"],
            status=row["status"],
            error=row["error"],
            chunk_count=int(row["chunk_count"] or 0),
            parent_count=int(row["parent_count"] or 0),
            index_schema_version=int(row["index_schema_version"] or 1),
            index_revision=row["index_revision"] or "",
            rebuild_status=row["rebuild_status"] or row["status"],
            rebuild_error=row["rebuild_error"] or "",
            created_at=self._parse_time(row["created_at"]),
            updated_at=self._parse_time(row["updated_at"]),
        )

    def list_documents(self, knowledge_base_id: str) -> list[KnowledgeDocument]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"{self._document_select()} WHERE d.knowledge_base_id = ? ORDER BY d.updated_at DESC",
                (knowledge_base_id,),
            ).fetchall()
            return [self._document_from_row(row) for row in rows]

    def get_document(self, document_id: str) -> KnowledgeDocument | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"{self._document_select()} WHERE d.id = ?", (document_id,)
            ).fetchone()
            return self._document_from_row(row) if row else None

    def get_document_detail(self, document_id: str) -> KnowledgeDocumentDetail | None:
        document = self.get_document(document_id)
        if document is None:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT content FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
        return KnowledgeDocumentDetail(**document.model_dump(), content_preview=(row["content"] or "")[:2000])

    def get_document_content(self, document_id: str, chunk_id: str | None = None) -> KnowledgeDocumentContent | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, filename, content, sha256, index_revision FROM knowledge_documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            locator = None
            highlight_start = None
            highlight_end = None
            resolved_chunk_id = None
            version_changed = False
            if chunk_id:
                chunk = connection.execute(
                    "SELECT id, locator_json, index_revision FROM knowledge_chunks "
                    "WHERE document_id = ? AND (id = ? OR stable_id = ?)",
                    (document_id, chunk_id, chunk_id),
                ).fetchone()
                if chunk is not None:
                    locator = json.loads(chunk["locator_json"] or "{}")
                    highlight_start = int(locator.get("char_start") or 0)
                    highlight_end = int(locator.get("char_end") or highlight_start)
                    resolved_chunk_id = chunk["id"]
                    version_changed = bool(
                        row["index_revision"] and chunk["index_revision"] != row["index_revision"]
                    )
            return KnowledgeDocumentContent(
                id=row["id"],
                filename=row["filename"],
                content=row["content"],
                chunk_id=resolved_chunk_id,
                locator=locator,
                highlight_start=highlight_start,
                highlight_end=highlight_end,
                document_sha256=row["sha256"],
                index_revision=row["index_revision"] or "",
                version_changed=version_changed,
            )

    def get_document_source(self, document_id: str) -> tuple[KnowledgeDocument, str] | None:
        document = self.get_document(document_id)
        if document is None:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT stored_path FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
        return document, row["stored_path"]

    def update_document(
        self,
        document_id: str,
        *,
        status: str,
        content: str | None = None,
        error: str = "",
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            if content is None:
                cursor = connection.execute(
                    "UPDATE knowledge_documents SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (status, error, now, document_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE knowledge_documents SET status = ?, content = ?, error = ?, updated_at = ? WHERE id = ?",
                    (status, content, error, now, document_id),
                )
            if cursor.rowcount == 0:
                raise KeyError(document_id)

    def request_document_rebuild(self, document_id: str) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is None:
                raise KeyError(document_id)
            # New uploads have no active index; existing documents stay readable while rebuilding.
            status = "pending" if row["status"] != "ready" else "ready"
            connection.execute(
                "UPDATE knowledge_documents SET status = ?, rebuild_status = 'pending', "
                "rebuild_error = '', error = CASE WHEN ? = 'ready' THEN error ELSE '' END, updated_at = ? "
                "WHERE id = ?",
                (status, status, now, document_id),
            )

    def mark_document_rebuilding(self, document_id: str) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is None:
                raise KeyError(document_id)
            status = "indexing" if row["status"] != "ready" else "ready"
            connection.execute(
                "UPDATE knowledge_documents SET status = ?, rebuild_status = 'indexing', "
                "rebuild_error = '', updated_at = ? WHERE id = ?",
                (status, now, document_id),
            )

    def fail_document_rebuild(self, document_id: str, error: str) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT index_revision FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is None:
                raise KeyError(document_id)
            has_active_index = bool(row["index_revision"])
            connection.execute(
                "UPDATE knowledge_documents SET status = ?, error = ?, rebuild_status = 'failed', "
                "rebuild_error = ?, updated_at = ? WHERE id = ?",
                (
                    "ready" if has_active_index else "failed",
                    "" if has_active_index else error[:500],
                    error[:500],
                    now,
                    document_id,
                ),
            )

    def replace_document_index(
        self,
        *,
        document_id: str,
        knowledge_base_id: str,
        content: str,
        index_revision: str,
        parents: list[dict],
        children: list[dict],
    ) -> None:
        """Atomically replace a document's active parent/child index."""
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM knowledge_documents WHERE id = ? AND knowledge_base_id = ?",
                (document_id, knowledge_base_id),
            ).fetchone()
            if row is None:
                raise KeyError(document_id)
            connection.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
            connection.execute("DELETE FROM knowledge_parent_chunks WHERE document_id = ?", (document_id,))
            connection.executemany(
                "INSERT INTO knowledge_parent_chunks(id, stable_id, document_id, knowledge_base_id, "
                "parent_index, text, locator_json, content_hash, index_revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        parent["id"], parent["stable_id"], document_id, knowledge_base_id,
                        parent["index"], parent["text"], json.dumps(parent["locator"], ensure_ascii=False),
                        parent["content_hash"], index_revision, now,
                    )
                    for parent in parents
                ],
            )
            connection.executemany(
                "INSERT INTO knowledge_chunks(id, stable_id, document_id, knowledge_base_id, parent_id, "
                "chunk_index, text, retrieval_text, locator_json, content_hash, index_revision, "
                "command_evidence_json, tokens_json, embedding, embedding_dimension, embedding_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        child["id"], child["stable_id"], document_id, knowledge_base_id, child["parent_id"],
                        child["index"], child["text"], child["retrieval_text"],
                        json.dumps(child["locator"], ensure_ascii=False), child["content_hash"], index_revision,
                        json.dumps(child.get("command_evidence", []), ensure_ascii=False), child["tokens_json"],
                        child.get("embedding"), child.get("embedding_dimension"), child.get("embedding_version", ""), now,
                    )
                    for child in children
                ],
            )
            connection.execute(
                "UPDATE knowledge_documents SET content = ?, status = 'ready', error = '', "
                "index_schema_version = ?, index_revision = ?, rebuild_status = 'ready', rebuild_error = '', "
                "updated_at = ? WHERE id = ?",
                (content, DOCUMENT_INDEX_SCHEMA_VERSION, index_revision, now, document_id),
            )

    def replace_chunks(
        self,
        document_id: str,
        knowledge_base_id: str,
        chunks: list[tuple[str, str, bytes | None, int | None, str]],
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
            connection.executemany(
                "INSERT INTO knowledge_chunks(id, document_id, knowledge_base_id, chunk_index, text, tokens_json, "
                "embedding, embedding_dimension, embedding_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(uuid4()), document_id, knowledge_base_id, index, text, tokens_json,
                        embedding, dimension, version, now,
                    )
                    for index, (text, tokens_json, embedding, dimension, version) in enumerate(chunks)
                ],
            )

    def list_chunks(self, knowledge_base_id: str) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT ch.*, d.filename, d.title, d.sha256, d.index_revision AS document_index_revision, "
                "p.text AS parent_text, p.locator_json AS parent_locator_json "
                "FROM knowledge_chunks ch "
                "JOIN knowledge_documents d ON d.id = ch.document_id "
                "LEFT JOIN knowledge_parent_chunks p ON p.id = ch.parent_id "
                "WHERE ch.knowledge_base_id = ? AND d.status = 'ready' ORDER BY d.created_at, ch.chunk_index",
                (knowledge_base_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_pending_document_ids(self) -> list[str]:
        with self._lock, self._connect() as connection:
            return [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM knowledge_documents "
                    "WHERE status = 'pending' OR rebuild_status = 'pending' "
                    "ORDER BY created_at"
                ).fetchall()
            ]

    def delete_document(self, document_id: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT stored_path FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))
            return row["stored_path"]
