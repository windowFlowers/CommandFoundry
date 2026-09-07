from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class CommandBlock(BaseModel):
    label: str
    language: Literal["bash", "sql", "powershell", "text"] = "bash"
    code: str = Field(min_length=1)
    platforms: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.low
    warning: str | None = None


class SourceMetadata(BaseModel):
    source_id: str
    title: str
    source_url: str
    license: str
    revision: str
    path: str = ""
    sha256: str = ""
    kind: str = "vendored"


class KnowledgeTopic(BaseModel):
    id: str
    domain: Literal["linux", "git", "sql", "docker", "http", "toolchain"]
    title: str
    aliases: list[str]
    summary: str
    commands: list[CommandBlock]
    notes: list[str] = Field(default_factory=list)
    source: SourceMetadata

    @property
    def retrieval_text(self) -> str:
        command_text = " ".join(f"{item.label} {item.code}" for item in self.commands)
        return " ".join([self.title, *self.aliases, self.summary, command_text])


class RetrievalHit(BaseModel):
    topic: KnowledgeTopic
    score: float
    bm25_score: float = 0.0
    vector_score: float = 0.0


class Citation(BaseModel):
    source_id: str
    title: str
    source_url: str
    license: str
    revision: str
    excerpt: str
    score: float
    domain: str


class Answer(BaseModel):
    mode: Literal["model", "local_fallback"]
    summary: str
    commands: list[CommandBlock]
    notes: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class ChatMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    role: Literal["user", "assistant"]
    query: str | None = None
    answer: Answer | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Conversation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = "新对话"
    messages: list[ChatMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConversationCreateRequest(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=80)


class ConversationListResponse(BaseModel):
    items: list[Conversation]


class KnowledgeStatus(BaseModel):
    ready: bool
    topic_count: int
    domains: dict[str, int]
    retrieval_mode: Literal["hybrid", "bm25"]
    embedding_model: str
    embedding_error: str = ""


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
