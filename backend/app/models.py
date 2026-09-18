from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


DEFAULT_KNOWLEDGE_BASE_ID = "developer-it"
LOCAL_USER_PROFILE_ID = "local-user"


def utc_now() -> datetime:
    return datetime.now(UTC)


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class CommandBlock(BaseModel):
    label: str
    language: Literal["bash", "sql", "powershell", "text"] = "bash"
    # ``language`` describes the syntax highlighter while ``shell`` describes
    # the program that should receive the copied command.  They are deliberately
    # separate: a command from a tldr page is often labelled bash even when the
    # executable variant is PowerShell or cmd.exe.
    shell: Literal["bash", "powershell", "cmd", "posix", "sql", "text"] | None = None
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
    "ungrounded",
    "provider_error",
]


class GenerationInfo(BaseModel):
    attempted: bool = False
    # Provider ids are configured by the desktop app.  Keep this open-ended so
    # OpenAI-compatible services (OpenAI, Qwen, Moonshot, Ollama, custom) can
    # be reported without changing the answer contract again.
    provider: str = "local"
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


class PersonalizationInfo(BaseModel):
    used: bool = False
    memory_ids: list[str] = Field(default_factory=list)
    global_count: int = 0
    scoped_count: int = 0
    sent_to_model: bool = False
    retrieval_query_enriched: bool = False


AnswerKind = Literal["direct", "clarification", "template"]
ClarificationInputType = Literal["text", "path", "number", "select"]


class ClarificationOption(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=240)


class ClarificationInfo(BaseModel):
    """One safe, user-visible question in a command plan.

    The backend intentionally exposes only the current slot.  The remaining
    slots stay server-side so model output or a stale UI cannot skip required
    values.
    """

    plan_id: str = Field(min_length=1)
    slot: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=600)
    input_type: ClarificationInputType = "text"
    options: list[ClarificationOption] = Field(default_factory=list, max_length=12)
    attempt: int = Field(default=1, ge=1, le=10)
    max_attempts: int = Field(default=1, ge=1, le=10)
    total_slots: int = Field(default=1, ge=1, le=20)


class CommandPlanInfo(BaseModel):
    """Public, non-sensitive snapshot of the command planner state."""

    plan_id: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    source_revision: str = ""
    citation_ids: list[str] = Field(default_factory=list)
    collected_slots: dict[str, str] = Field(default_factory=dict)
    unresolved_slots: list[str] = Field(default_factory=list)


class Answer(BaseModel):
    mode: Literal["model", "local_fallback"]
    # Old answer_json rows do not contain this field.  ``template`` is the
    # conservative default so loading a v2.4 row can never make an old command
    # look like a fully rendered direct command.
    answer_kind: AnswerKind = "template"
    summary: str
    commands: list[CommandBlock]
    notes: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    generation: GenerationInfo = Field(default_factory=GenerationInfo)
    context: ContextInfo = Field(default_factory=ContextInfo)
    personalization: PersonalizationInfo = Field(default_factory=PersonalizationInfo)
    summary_segments: list[AnswerSegment] = Field(default_factory=list)
    clarification: ClarificationInfo | None = None
    command_plan: CommandPlanInfo | None = None


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
    # Provider ids are configured by the desktop app.  Keep this open-ended so
    # summaries created with OpenAI, Qwen, Ollama or a custom compatible
    # endpoint remain truthful after the model provider changes.
    summary_provider: str | None = None
    summary_updated_at: datetime | None = None
    compacted_through_message_id: str | None = None
    reset_after_message_id: str | None = None
    estimated_tokens: int = 0
    recent_messages: list[MemoryMessagePreview] = Field(default_factory=list)
    last_error: str = ""


MemoryScope = Literal["global", "knowledge_base"]
MemoryCategory = Literal[
    "response_style",
    "expertise",
    "platform",
    "toolchain",
    "project_constraint",
]
MemoryStatus = Literal["active", "superseded"]
# Manual/local are retained for backwards compatibility.  Automatic
# extraction may be produced by any configured OpenAI-compatible provider.
MemoryExtractionProvider = str


class UserProfile(BaseModel):
    id: str = LOCAL_USER_PROFILE_ID
    personalization_enabled: bool = True
    auto_memory_enabled: bool = True
    revision: int = 0
    extraction_epoch: int = 0
    active_memory_count: int = 0
    pending_task_count: int = 0
    created_at: datetime
    updated_at: datetime


class UserProfileUpdateRequest(BaseModel):
    personalization_enabled: bool | None = None
    auto_memory_enabled: bool | None = None


class UserMemory(BaseModel):
    id: str
    scope: MemoryScope
    knowledge_base_id: str | None = None
    category: MemoryCategory
    key: str
    value: str
    display_text: str
    confidence: float = Field(ge=0, le=1)
    pinned: bool = False
    status: MemoryStatus = "active"
    supersedes_id: str | None = None
    source_conversation_id: str | None = None
    source_message_id: str | None = None
    extraction_provider: MemoryExtractionProvider = "manual"
    last_used_at: datetime | None = None
    use_count: int = 0
    created_at: datetime
    updated_at: datetime


class UserMemoryCreateRequest(BaseModel):
    scope: MemoryScope
    knowledge_base_id: str | None = None
    category: MemoryCategory
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=160)
    display_text: str = Field(min_length=1, max_length=240)
    pinned: bool = False


class UserMemoryUpdateRequest(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=160)
    display_text: str | None = Field(default=None, min_length=1, max_length=240)
    pinned: bool | None = None


class UserMemoryListResponse(BaseModel):
    items: list[UserMemory]


MemoryExtractionTaskStatus = Literal["pending", "extracting", "ready", "failed", "cancelled"]
MemoryExtractionTaskOperation = Literal["ADD", "UPDATE", "SUPERSEDE", "NOOP"]


class UserMemoryExtractionTaskStatusResponse(BaseModel):
    """Public, source-text-free state for one automatic memory extraction task."""

    id: str
    status: MemoryExtractionTaskStatus
    source_message_id: str
    memory_ids: list[str] = Field(default_factory=list)
    # Only genuinely new, still-active memories can safely be removed by the
    # one-click undo affordance.  Replacements deliberately restore their
    # predecessor through the memory manager instead.
    undoable_memory_ids: list[str] = Field(default_factory=list)
    operation: MemoryExtractionTaskOperation = "NOOP"
    created_at: datetime
    updated_at: datetime


DocumentStatus = Literal["pending", "indexing", "ready", "failed"]


class KnowledgeBase(BaseModel):
    id: str
    name: str
    is_builtin: bool = False
    document_count: int = 0
    ready_document_count: int = 0
    chunk_count: int = 0
    memory_count: int = 0
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
