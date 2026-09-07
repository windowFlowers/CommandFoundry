from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock

from .models import Answer, ChatMessage, Conversation, utc_now


class ConversationRepository:
    """Small SQLite repository for the only persistent user data in v2."""

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

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
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
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, created_at);
                """
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
        return Conversation(
            id=row["id"],
            title=row["title"],
            messages=messages,
            created_at=self._parse_time(row["created_at"]),
            updated_at=self._parse_time(row["updated_at"]),
        )

    def create(self, title: str = "新对话") -> Conversation:
        conversation = Conversation(title=title.strip() or "新对话")
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (
                    conversation.id,
                    conversation.title,
                    conversation.created_at.isoformat(),
                    conversation.updated_at.isoformat(),
                ),
            )
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            return self._load_conversation(connection, row) if row else None

    def list(self) -> list[Conversation]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
            return [self._load_conversation(connection, row) for row in rows]

    def delete(self, conversation_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            return cursor.rowcount > 0

    def append_user(self, conversation_id: str, query: str) -> ChatMessage:
        message = ChatMessage(role="user", query=query)
        now = utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
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
            if connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone() is None:
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
