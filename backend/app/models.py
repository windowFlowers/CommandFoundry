from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


DEFAULT_KNOWLEDGE_BASE_ID = "developer-it"


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
    source_url: str | None = None
    license: str = "User provided"
    revision: str = "local"
    path: str = ""
    sha256: str = ""
    kind: str = "vendored"
    document_id: str | None = None
    knowledge_base_id: str | None = None


class KnowledgeTopic(BaseModel):
    id: str
    domain: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    summary: str
    commands: list[CommandBlock] = Field(default_factory=list)
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
    source_url: str | None = None
    license: str
    revision: str
    excerpt: str
    score: float
    domain: str
    document_id: str | None = None
    knowledge_base_id: str | None = None
    source_kind: str = "vendored"


FallbackReason = Literal[
    "no_api_key",
    "no_hits",
    "timeout",
    "rate_limit",
    "authentication",
    "invalid_json",
    "provider_error",
]


class GenerationInfo(BaseModel):
    attempted: bool = False
    provider: Literal["deepseek", "local"] = "local"
    model: str | None = None
    request_id: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    fallback_reason: FallbackReason | None = None


class Answer(BaseModel):
    mode: Literal["model", "local_fallback"]
    summary: str
    commands: list[CommandBlock]
    notes: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    generation: GenerationInfo = Field(default_factory=GenerationInfo)


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    knowledge_base_id: str | None = None


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
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID
    knowledge_base_name: str = "开发者 IT 知识库"
    knowledge_base_deleted: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConversationCreateRequest(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=80)
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID


class ConversationListResponse(BaseModel):
    items: list[Conversation]


DocumentStatus = Literal["pending", "indexing", "ready", "failed"]


class KnowledgeBase(BaseModel):
    id: str
    name: str
    is_builtin: bool = False
    document_count: int = 0
    ready_document_count: int = 0
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class KnowledgeBaseUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBase]


class KnowledgeDocument(BaseModel):
    id: str
    knowledge_base_id: str
    filename: str
    title: str
    media_type: str
    size_bytes: int
    sha256: str
    source_kind: Literal["upload"] = "upload"
    status: DocumentStatus = "pending"
    error: str = ""
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocument):
    content_preview: str = ""


class KnowledgeDocumentContent(BaseModel):
    id: str
    filename: str
    content: str


class KnowledgeDocumentListResponse(BaseModel):
    items: list[KnowledgeDocument]


class KnowledgeStatus(BaseModel):
    ready: bool
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID
    knowledge_base_name: str = "开发者 IT 知识库"
    topic_count: int
    document_count: int = 0
    pending_document_count: int = 0
    failed_document_count: int = 0
    domains: dict[str, int]
    retrieval_mode: Literal["hybrid", "bm25"]
    embedding_model: str
    embedding_error: str = ""


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
