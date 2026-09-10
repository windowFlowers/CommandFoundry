from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


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
    citation_ids: list[str] = Field(default_factory=list)


class SourceLocator(BaseModel):
    kind: Literal["markdown", "text", "pdf", "docx"]
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] = Field(default_factory=list)
    line_start: int | None = None
    line_end: int | None = None
    paragraph_start: int | None = None
    paragraph_end: int | None = None
    table_start: int | None = None
    table_end: int | None = None
    table_row_start: int | None = None
    table_row_end: int | None = None
    element_start: int | None = None
    element_end: int | None = None
    char_start: int = 0
    char_end: int = 0


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
    chunk_id: str | None = None
    parent_id: str | None = None
    index_revision: str = ""
    locator: SourceLocator | None = None
    command_evidence: list[str] = Field(default_factory=list)


class KnowledgeTopic(BaseModel):
    id: str
    domain: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    summary: str
    commands: list[CommandBlock] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    source: SourceMetadata
    index_text: str | None = Field(default=None, exclude=True)
    parent_context: str | None = Field(default=None, exclude=True)

    @property
    def retrieval_text(self) -> str:
        if self.index_text:
            return self.index_text
        command_text = " ".join(f"{item.label} {item.code}" for item in self.commands)
        return " ".join([self.title, *self.aliases, self.summary, command_text])


class RetrievalHit(BaseModel):
    topic: KnowledgeTopic
    score: float
    bm25_score: float = 0.0
    vector_score: float = 0.0
    parent_text: str | None = None
    matched_chunks: list["MatchedChunk"] = Field(default_factory=list)


class MatchedChunk(BaseModel):
    chunk_id: str
    parent_id: str | None = None
    text: str
    score: float = 0.0
    locator: SourceLocator | None = None
    command_evidence: list[str] = Field(default_factory=list)


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
    citation_id: str | None = None
    chunk_id: str | None = None
    parent_id: str | None = None
    document_sha256: str = ""
    index_revision: str = ""
    locator: SourceLocator | None = None


class AnswerSegment(BaseModel):
    text: str
    citation_ids: list[str] = Field(default_factory=list)


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


class ContextInfo(BaseModel):
    used: bool = False
    strategy: Literal["none", "recent", "summary_recent"] = "none"
    recent_turn_count: int = 0
    used_message_ids: list[str] = Field(default_factory=list)
    summary_used: bool = False
    retrieval_query: str = ""
    estimated_tokens: int = 0


class Answer(BaseModel):
    mode: Literal["model", "local_fallback"]
    summary: str
    commands: list[CommandBlock]
    notes: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    generation: GenerationInfo = Field(default_factory=GenerationInfo)
    context: ContextInfo = Field(default_factory=ContextInfo)
    summary_segments: list[AnswerSegment] = Field(default_factory=list)


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


class MemorySummary(BaseModel):
    current_goal: str = ""
    environments: list[str] = Field(default_factory=list, max_length=12)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    decisions: list[str] = Field(default_factory=list, max_length=12)
    open_questions: list[str] = Field(default_factory=list, max_length=12)
    history_topics: list[str] = Field(default_factory=list, max_length=12)

    @staticmethod
    def _sanitize(value: object, *, limit: int) -> str:
        text = str(value or "")
        text = re.sub(r"```[\s\S]*?```", "[代码块已省略]", text)
        text = re.sub(r"`[^`\r\n]+`", "[命令已省略]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text, flags=re.IGNORECASE)
        text = re.sub(
            r"(?i)\b(password|passwd|token|api[_ -]?key)\s*[:=]\s*\S+",
            r"\1=[REDACTED]",
            text,
        )
        return re.sub(r"\s+", " ", text).strip()[:limit]

    @field_validator("current_goal", mode="before")
    @classmethod
    def sanitize_goal(cls, value: object) -> str:
        return cls._sanitize(value, limit=240)

    @field_validator(
        "environments",
        "constraints",
        "decisions",
        "open_questions",
        "history_topics",
        mode="before",
    )
    @classmethod
    def sanitize_lists(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = [value] if isinstance(value, str) else list(value)
        return [item for item in (cls._sanitize(entry, limit=160) for entry in values) if item]


class MemoryMessagePreview(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    preview: str
    created_at: datetime


class ConversationMemory(BaseModel):
    conversation_id: str
    status: Literal["idle", "pending", "summarizing", "ready"] = "idle"
    summary: MemorySummary = Field(default_factory=MemorySummary)
    summary_provider: Literal["local", "deepseek"] | None = None
    summary_updated_at: datetime | None = None
    compacted_through_message_id: str | None = None
    reset_after_message_id: str | None = None
    estimated_tokens: int = 0
    recent_messages: list[MemoryMessagePreview] = Field(default_factory=list)
    last_error: str = ""


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
    parent_count: int = 0
    index_schema_version: int = 1
    index_revision: str = ""
    rebuild_status: DocumentStatus = "pending"
    rebuild_error: str = ""
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocument):
    content_preview: str = ""


class KnowledgeDocumentContent(BaseModel):
    id: str
    filename: str
    content: str
    chunk_id: str | None = None
    locator: SourceLocator | None = None
    highlight_start: int | None = None
    highlight_end: int | None = None
    document_sha256: str = ""
    index_revision: str = ""
    version_changed: bool = False


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
