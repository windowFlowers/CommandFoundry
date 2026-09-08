from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .models import (
    Answer,
    ChatMessage,
    Conversation,
    DEFAULT_KNOWLEDGE_BASE_ID,
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeDocumentContent,
    KnowledgeDocumentDetail,
    utc_now,
)


BUILTIN_KNOWLEDGE_BASE_NAME = "开发者 IT 知识库"


class ConversationRepository:
    """SQLite repository for conversations, knowledge bases, documents and chunks."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    def _initialize(self) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(knowledge_base_id, sha256)
                );
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    tokens_json TEXT NOT NULL,
                    embedding BLOB,
                    embedding_dimension INTEGER,
                    embedding_version TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(document_id, chunk_index)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_documents_knowledge_base
                    ON knowledge_documents(knowledge_base_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_chunks_knowledge_base
                    ON knowledge_chunks(knowledge_base_id, document_id, chunk_index);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_bases(id, name, is_builtin, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (DEFAULT_KNOWLEDGE_BASE_ID, BUILTIN_KNOWLEDGE_BASE_NAME, now, now),
            )
            columns = self._columns(connection, "conversations")
            if "knowledge_base_id" not in columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN knowledge_base_id TEXT")
            if "knowledge_base_name_snapshot" not in columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN knowledge_base_name_snapshot TEXT")
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
            connection.execute(
                "UPDATE knowledge_documents SET status = 'pending', error = '', updated_at = ? "
                "WHERE status = 'indexing'",
                (now,),
            )

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

    def _knowledge_base_from_row(self, row: sqlite3.Row) -> KnowledgeBase:
        return KnowledgeBase(
            id=row["id"],
            name=row["name"],
            is_builtin=bool(row["is_builtin"]),
            document_count=int(row["document_count"] or 0),
            ready_document_count=int(row["ready_document_count"] or 0),
            chunk_count=int(row["chunk_count"] or 0),
            created_at=self._parse_time(row["created_at"]),
            updated_at=self._parse_time(row["updated_at"]),
        )

    @staticmethod
    def _knowledge_base_select() -> str:
        return """
            SELECT kb.*,
                   COUNT(DISTINCT d.id) AS document_count,
                   COUNT(DISTINCT CASE WHEN d.status = 'ready' THEN d.id END) AS ready_document_count,
                   COUNT(DISTINCT ch.id) AS chunk_count
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
            connection.execute("DELETE FROM knowledge_bases WHERE id = ?", (knowledge_base_id,))
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
            SELECT d.*, COUNT(ch.id) AS chunk_count
            FROM knowledge_documents d
            LEFT JOIN knowledge_chunks ch ON ch.document_id = d.id
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
            created_at=self._parse_time(row["created_at"]),
            updated_at=self._parse_time(row["updated_at"]),
        )

    def list_documents(self, knowledge_base_id: str) -> list[KnowledgeDocument]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"{self._document_select()} WHERE d.knowledge_base_id = ? GROUP BY d.id ORDER BY d.updated_at DESC",
                (knowledge_base_id,),
            ).fetchall()
            return [self._document_from_row(row) for row in rows]

    def get_document(self, document_id: str) -> KnowledgeDocument | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"{self._document_select()} WHERE d.id = ? GROUP BY d.id", (document_id,)
            ).fetchone()
            return self._document_from_row(row) if row else None

    def get_document_detail(self, document_id: str) -> KnowledgeDocumentDetail | None:
        document = self.get_document(document_id)
        if document is None:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT content FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
        return KnowledgeDocumentDetail(**document.model_dump(), content_preview=(row["content"] or "")[:2000])

    def get_document_content(self, document_id: str) -> KnowledgeDocumentContent | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, filename, content FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
            return KnowledgeDocumentContent(**dict(row)) if row else None

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
                "SELECT ch.*, d.filename, d.title, d.sha256 FROM knowledge_chunks ch "
                "JOIN knowledge_documents d ON d.id = ch.document_id "
                "WHERE ch.knowledge_base_id = ? AND d.status = 'ready' ORDER BY d.created_at, ch.chunk_index",
                (knowledge_base_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_pending_document_ids(self) -> list[str]:
        with self._lock, self._connect() as connection:
            return [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM knowledge_documents WHERE status = 'pending' ORDER BY created_at"
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
