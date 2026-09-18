from __future__ import annotations

import asyncio
import ipaddress
import json
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .answering import AnswerService
from .command_planner import CommandPlanner, contains_sensitive_input, redact_sensitive_input
from .config import settings
from .documents import MAX_UPLOAD_BYTES, DocumentIndexService
from .extraction import ExtractionError
from .generation import DeepSeekGenerator
from .knowledge import KnowledgeCatalog
from .memory import ConversationMemoryService, merge_contextual_hits
from .models import (
    ChatRequest,
    Conversation,
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationMemory,
    DEFAULT_KNOWLEDGE_BASE_ID,
    HealthResponse,
    KnowledgeBase,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseListResponse,
    KnowledgeBaseUpdateRequest,
    KnowledgeDocument,
    KnowledgeDocumentContent,
    KnowledgeDocumentDetail,
    KnowledgeDocumentListResponse,
    KnowledgeStatus,
    UserMemory,
    UserMemoryCreateRequest,
    UserMemoryExtractionTaskStatusResponse,
    UserMemoryListResponse,
    UserMemoryUpdateRequest,
    UserProfile,
    UserProfileUpdateRequest,
)
from .personalization import PersonalizationService, memory_update_for_response
from .repository import ConversationRepository
from .retrieval import EmbeddingEngine, KnowledgeIndexManager


catalog = KnowledgeCatalog(settings.knowledge_dir)
catalog.load()
repository = ConversationRepository(settings.storage_dir / "aegis-v2.sqlite3")
embedding_engine = EmbeddingEngine(
    settings.embedding_model,
    settings.model_cache_dir,
    enabled=settings.embedding_enabled,
    local_files_only=settings.embedding_local_only,
)
index_manager = KnowledgeIndexManager(
    catalog=catalog,
    repository=repository,
    embedding_engine=embedding_engine,
    candidate_k=settings.retrieval_candidate_k,
)
# Compatibility handle used by the retrieval evaluation tests and local scripts.
retriever = index_manager.get(DEFAULT_KNOWLEDGE_BASE_ID)
generator = DeepSeekGenerator(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    model=settings.llm_model,
    provider=settings.llm_provider,
    timeout_seconds=settings.llm_timeout_seconds,
)
answer_service = AnswerService(generator)
command_planner = CommandPlanner(catalog=catalog, repository=repository)
memory_service = ConversationMemoryService(repository=repository, generator=generator)
personalization_service = PersonalizationService(
    repository=repository,
    generator=generator,
    embeddings=embedding_engine,
)
document_service = DocumentIndexService(
    repository=repository,
    indexes=index_manager,
    embeddings=embedding_engine,
    upload_dir=settings.upload_dir,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if settings.embedding_enabled:
        task = asyncio.create_task(asyncio.to_thread(retriever.initialize_embeddings))
    yield
    if task and not task.done():
        task.cancel()


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
TRUSTED_APP_ORIGINS = {"aegis://app/localhost"}
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys([*settings.allowed_origins, *TRUSTED_APP_ORIGINS])),
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def _is_trusted_mutation_origin(origin: str) -> bool:
    if origin in settings.allowed_origins or origin in TRUSTED_APP_ORIGINS:
        return True
    try:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return False
        hostname = (parsed.hostname or "").rstrip(".").casefold()
        # Accessing .port also rejects malformed/out-of-range ports.
        _ = parsed.port
        if hostname == "localhost":
            return True
        return bool(hostname and ipaddress.ip_address(hostname).is_loopback)
    except (ValueError, UnicodeError):
        return False


@app.middleware("http")
async def reject_untrusted_browser_mutations(request: Request, call_next):
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        # Native Electron/main-process and local CLI calls do not carry Origin.
        # A browser request does; reject it before endpoint code can mutate data.
        if origin is not None and not _is_trusted_mutation_origin(origin.strip()):
            return JSONResponse(status_code=403, content={"detail": "不受信任的请求来源"})
    return await call_next(request)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/", response_model=HealthResponse)
@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(version=settings.app_version)


@app.get("/knowledge/status", response_model=KnowledgeStatus)
def knowledge_status(
    knowledge_base_id: str = Query(default=DEFAULT_KNOWLEDGE_BASE_ID),
) -> KnowledgeStatus:
    try:
        return index_manager.status(knowledge_base_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="知识库不存在") from exc


@app.get("/knowledge-bases", response_model=KnowledgeBaseListResponse)
def list_knowledge_bases() -> KnowledgeBaseListResponse:
    return KnowledgeBaseListResponse(items=repository.list_knowledge_bases())


@app.post("/knowledge-bases", response_model=KnowledgeBase, status_code=status.HTTP_201_CREATED)
def create_knowledge_base(request: KnowledgeBaseCreateRequest) -> KnowledgeBase:
    try:
        return repository.create_knowledge_base(request.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBase)
def rename_knowledge_base(knowledge_base_id: str, request: KnowledgeBaseUpdateRequest) -> KnowledgeBase:
    knowledge_base = repository.get_knowledge_base(knowledge_base_id)
    if knowledge_base is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if knowledge_base.is_builtin:
        raise HTTPException(status_code=409, detail="内置知识库不能重命名")
    try:
        return repository.rename_knowledge_base(knowledge_base_id, request.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/knowledge-bases/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge_base(knowledge_base_id: str) -> None:
    try:
        document_service.delete_knowledge_base(knowledge_base_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="知识库不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/documents", response_model=KnowledgeDocumentListResponse)
def list_documents(knowledge_base_id: str) -> KnowledgeDocumentListResponse:
    if repository.get_knowledge_base(knowledge_base_id) is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return KnowledgeDocumentListResponse(items=repository.list_documents(knowledge_base_id))


@app.post(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=KnowledgeDocument,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(knowledge_base_id: str, file: UploadFile = File(...)) -> KnowledgeDocument:
    if repository.get_knowledge_base(knowledge_base_id) is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        return document_service.create_upload(knowledge_base_id, file.filename or "", content)
    except ExtractionError as exc:
        code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if len(content) > MAX_UPLOAD_BYTES else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-documents/{document_id}", response_model=KnowledgeDocumentDetail)
def get_document(document_id: str) -> KnowledgeDocumentDetail:
    document = repository.get_document_detail(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return document


@app.get("/knowledge-documents/{document_id}/content", response_model=KnowledgeDocumentContent)
def get_document_content(
    document_id: str,
    chunk_id: str | None = Query(default=None),
) -> KnowledgeDocumentContent:
    document = repository.get_document_content(document_id, chunk_id=chunk_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if chunk_id and document.chunk_id is None:
        raise HTTPException(status_code=404, detail="引用片段不存在或索引版本已变化")
    return document


@app.delete("/knowledge-documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: str) -> None:
    if not document_service.delete_document(document_id):
        raise HTTPException(status_code=404, detail="文档不存在")


@app.post("/knowledge-documents/{document_id}/reindex", response_model=KnowledgeDocument, status_code=202)
def reindex_document(document_id: str) -> KnowledgeDocument:
    try:
        return document_service.reindex(document_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc


@app.post("/conversations", response_model=Conversation, status_code=status.HTTP_201_CREATED)
def create_conversation(request: ConversationCreateRequest | None = None) -> Conversation:
    payload = request or ConversationCreateRequest()
    try:
        return repository.create(payload.title, payload.knowledge_base_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="知识库不存在") from exc


@app.get("/conversations", response_model=ConversationListResponse)
def list_conversations() -> ConversationListResponse:
    return ConversationListResponse(items=repository.list())


@app.get("/conversations/{conversation_id}", response_model=Conversation)
def get_conversation(conversation_id: str) -> Conversation:
    conversation = repository.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conversation


@app.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: str) -> None:
    if not repository.delete(conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")


@app.get("/conversations/{conversation_id}/memory", response_model=ConversationMemory)
def get_conversation_memory(conversation_id: str) -> ConversationMemory:
    try:
        return memory_service.get_state(conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc


@app.post("/conversations/{conversation_id}/memory/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_conversation_memory(conversation_id: str) -> None:
    if not memory_service.reset(conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")


@app.get("/profile", response_model=UserProfile)
def get_profile() -> UserProfile:
    return repository.get_profile()


@app.patch("/profile", response_model=UserProfile)
def update_profile(request: UserProfileUpdateRequest) -> UserProfile:
    return repository.update_profile(
        personalization_enabled=request.personalization_enabled,
        auto_memory_enabled=request.auto_memory_enabled,
    )


def _memory_extraction_task_response(task: dict) -> UserMemoryExtractionTaskStatusResponse:
    """Translate a persisted task into the small status view safe for the UI.

    The task table intentionally keeps the sanitized source statement private.
    A completed task retains only result ids, so whether a result was new,
    updated, or superseded is reconstructed from the resulting memory row.
    """

    task_status = str(task.get("status") or "cancelled")
    result_ids = list(dict.fromkeys(str(value) for value in task.get("result_memory_ids", []) if value))
    memories_by_id = {
        memory.id: memory
        for memory in repository.list_user_memories(status=None, ids=result_ids)
    }
    memories = [memories_by_id[memory_id] for memory_id in result_ids if memory_id in memories_by_id]

    operation = "NOOP"
    undoable_ids: list[str] = []
    if task_status == "ready" and memories:
        task_created_at = datetime.fromisoformat(str(task["created_at"]))
        operations: list[str] = []
        for memory in memories:
            if memory.supersedes_id:
                operations.append("SUPERSEDE")
            elif memory.created_at >= task_created_at:
                operations.append("ADD")
                if memory.status == "active":
                    undoable_ids.append(memory.id)
            else:
                operations.append("UPDATE")
        if "SUPERSEDE" in operations:
            operation = "SUPERSEDE"
        elif operations and all(item == "ADD" for item in operations):
            operation = "ADD"
        else:
            operation = "UPDATE"

    return UserMemoryExtractionTaskStatusResponse(
        id=str(task["id"]),
        status=task_status,
        source_message_id=str(task["source_message_id"]),
        memory_ids=[memory.id for memory in memories] if task_status == "ready" else [],
        undoable_memory_ids=undoable_ids,
        operation=operation,
        created_at=task["created_at"],
        updated_at=task["updated_at"],
    )


@app.get(
    "/profile/memory-extractions/{task_id}",
    response_model=UserMemoryExtractionTaskStatusResponse,
)
def get_profile_memory_extraction(task_id: str) -> UserMemoryExtractionTaskStatusResponse:
    task = repository.get_memory_extraction_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="记忆提取任务不存在或已取消")
    return _memory_extraction_task_response(task)


@app.get("/profile/memories", response_model=UserMemoryListResponse)
def list_profile_memories(
    q: str | None = Query(default=None, max_length=160),
    scope: str | None = Query(default=None),
    category: str | None = Query(default=None),
    knowledge_base_id: str | None = Query(default=None),
    status_filter: str = Query(default="active", alias="status"),
    ids: str | None = Query(default=None),
    source_message_id: str | None = Query(default=None),
) -> UserMemoryListResponse:
    requested_ids = [value for value in (ids or "").split(",") if value] or None
    normalized_status = status_filter.strip().casefold()
    repository_status = None if normalized_status == "all" else normalized_status
    try:
        items = repository.list_user_memories(
            q=q,
            scope=scope,
            category=category,
            knowledge_base_id=knowledge_base_id,
            status=repository_status,
            ids=requested_ids,
            source_message_id=source_message_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UserMemoryListResponse(items=items)


@app.post("/profile/memories", response_model=UserMemory, status_code=status.HTTP_201_CREATED)
def create_profile_memory(request: UserMemoryCreateRequest) -> UserMemory:
    try:
        return personalization_service.create_manual(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="知识库不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/profile/memories/{memory_id}", response_model=UserMemory)
def update_profile_memory(memory_id: str, request: UserMemoryUpdateRequest) -> UserMemory:
    try:
        return personalization_service.update_manual(memory_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="记忆不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/profile/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile_memory(memory_id: str) -> None:
    if not repository.delete_user_memory_chain(memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")


@app.post("/profile/memories/{memory_id}/undo", response_model=UserMemory | None)
def undo_profile_memory(memory_id: str) -> UserMemory | None:
    try:
        return repository.undo_user_memory(memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="记忆不存在") from exc


@app.post("/profile/memories/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_profile_memories(
    scope: str | None = Query(default=None),
    knowledge_base_id: str | None = Query(default=None),
) -> None:
    try:
        repository.reset_user_memories(scope=scope, knowledge_base_id=knowledge_base_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    conversation = repository.get(request.conversation_id) if request.conversation_id else None
    if request.conversation_id and conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if conversation and conversation.knowledge_base_deleted:
        raise HTTPException(status_code=409, detail="该会话绑定的知识库已删除，不能继续提问")
    requested_base_id = request.knowledge_base_id or (
        conversation.knowledge_base_id if conversation else DEFAULT_KNOWLEDGE_BASE_ID
    )
    if conversation and requested_base_id != conversation.knowledge_base_id:
        raise HTTPException(status_code=409, detail="会话与所选知识库不一致，请开始新对话")
    if repository.get_knowledge_base(requested_base_id) is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    def generate_events():
        current = conversation or repository.create(knowledge_base_id=requested_base_id)
        # Read the pending plan before building contextual retrieval input.  The
        # planner owns the next slot; conversational memory must not silently
        # fill it or let an unrelated model answer replace it.
        pending_plan = repository.get_pending_command_plan(
            current.id,
            knowledge_base_id=requested_base_id,
        )
        credential_reply = command_planner.pending_slot_is_sensitive(pending_plan)
        sensitive_input = credential_reply or contains_sensitive_input(request.query)
        safe_query = redact_sensitive_input(request.query, whole=credential_reply)
        memory = memory_service.build_context(current, safe_query)
        personalization = personalization_service.build_context(
            query=safe_query,
            retrieval_query=memory.info.retrieval_query or safe_query,
            knowledge_base_id=requested_base_id,
        )
        yield sse(
            "status",
            {
                "stage": "contextualizing",
                "message": "正在整理会话上下文",
                "memory_used": memory.info.used,
                "recent_turn_count": memory.info.recent_turn_count,
                "summary_used": memory.info.summary_used,
                "personalization_count": len(personalization.info.memory_ids),
            },
        )
        # Keep credential values out of conversation history as well as the
        # command-plan table.  The planner still receives the raw turn below
        # only long enough to decide whether to reject it or render a safe
        # command; no cloud call receives it.
        user_message = repository.append_user(current.id, safe_query)
        try:
            yield sse("status", {"stage": "retrieving", "message": "正在检索本地知识库"})
            retrieval_query = personalization.retrieval_query
            if retrieval_query != safe_query.strip():
                candidate_k = max(settings.retrieval_top_k * 2, 8)
                raw_hits = index_manager.retrieve(requested_base_id, safe_query, top_k=candidate_k)
                contextual_hits = index_manager.retrieve(requested_base_id, retrieval_query, top_k=candidate_k)
                hits = merge_contextual_hits(
                    raw_hits,
                    contextual_hits,
                    top_k=settings.retrieval_top_k,
                )
            else:
                hits = index_manager.retrieve(
                    requested_base_id, safe_query, top_k=settings.retrieval_top_k
                )
            # Curated command plans are local-first.  Do not advertise or call
            # The configured OpenAI-compatible model is used for a pending/recognised recipe, and keep explicit
            # template requests deterministic as well.
            planner_recipe = command_planner._recipe_for(request.query, hits, pending_plan)
            planner_owned = planner_recipe is not None or command_planner.is_template_request(request.query)
            use_model = generator.available and bool(hits) and not planner_owned and not sensitive_input
            yield sse(
                "status",
                {
                    "stage": "generating" if use_model else "fallback",
                    "message": "正在请求已配置模型" if use_model else "正在生成本地确定性答案",
                    "hit_count": len(hits),
                },
            )
            planned_answer = command_planner.handle(
                request.query,
                hits,
                conversation_id=current.id,
                knowledge_base_id=requested_base_id,
                context=memory.info,
                personalization=personalization.info,
            )
            if planned_answer is not None:
                answer = planned_answer
            elif command_planner.is_template_request(request.query):
                # An explicit request for a template is always deterministic;
                # it must not spend a cloud call merely to repeat source text.
                answer = answer_service.local_fallback(
                    hits,
                    reason="no_api_key",
                    context=memory.info,
                    personalization=personalization.info,
                    query=safe_query,
                    personalization_context=personalization.prompt_payload,
                    single_template=True,
                )
            elif planner_owned or sensitive_input:
                # A recognised plan can intentionally return ``None`` for a
                # cancellation or a stale/unsafe slot.  Keep that path local;
                # the cloud model must never receive a credential-bearing turn
                # or replace the planner's deterministic fallback.
                answer = answer_service.local_fallback(
                    hits,
                    reason="no_api_key",
                    context=memory.info,
                    personalization=personalization.info,
                    query=safe_query,
                    personalization_context=personalization.prompt_payload,
                    single_template=bool(planner_owned or sensitive_input),
                )
            else:
                answer, _ = answer_service.answer(
                    safe_query,
                    hits,
                    context=memory.info,
                    memory_context=memory.prompt_payload,
                    personalization=personalization.info,
                    personalization_context=personalization.prompt_payload,
                )
            assistant_message = repository.append_answer(current.id, answer)
            if answer.personalization.memory_ids:
                repository.mark_user_memories_used(answer.personalization.memory_ids)
            try:
                memory_update = personalization_service.capture_user_message(
                    conversation_id=current.id,
                    source_message_id=user_message.id,
                    knowledge_base_id=requested_base_id,
                    text=safe_query,
                )
            except Exception:
                # Automatic extraction is a non-blocking side channel. It must
                # never turn an already generated answer into an SSE failure.
                memory_update = None
            memory_service.compact_if_needed(current.id)
            memory_update_payload = (
                memory_update_for_response(memory_update) if memory_update is not None else None
            )
            yield sse(
                "answer",
                {
                    "conversation_id": current.id,
                    "user_message_id": user_message.id,
                    "message_id": assistant_message.id,
                    "answer": answer.model_dump(mode="json"),
                    "memory_update": memory_update_payload,
                },
            )
            yield sse(
                "done",
                {
                    "conversation_id": current.id,
                    "message_id": assistant_message.id,
                    "memory_update": memory_update_payload,
                },
            )
        except Exception as exc:
            yield sse("error", {"message": f"回答生成失败：{type(exc).__name__}"})

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
