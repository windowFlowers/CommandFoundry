from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .answering import AnswerService
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
)
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
    timeout_seconds=settings.llm_timeout_seconds,
)
answer_service = AnswerService(generator)
memory_service = ConversationMemoryService(repository=repository, generator=generator)
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


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
        memory = memory_service.build_context(current, request.query)
        yield sse(
            "status",
            {
                "stage": "contextualizing",
                "message": "正在整理会话上下文",
                "memory_used": memory.info.used,
                "recent_turn_count": memory.info.recent_turn_count,
                "summary_used": memory.info.summary_used,
            },
        )
        user_message = repository.append_user(current.id, request.query)
        try:
            yield sse("status", {"stage": "retrieving", "message": "正在检索本地知识库"})
            retrieval_query = memory.info.retrieval_query or request.query
            if retrieval_query != request.query.strip():
                candidate_k = max(settings.retrieval_top_k * 2, 8)
                raw_hits = index_manager.retrieve(requested_base_id, request.query, top_k=candidate_k)
                contextual_hits = index_manager.retrieve(requested_base_id, retrieval_query, top_k=candidate_k)
                hits = merge_contextual_hits(
                    raw_hits,
                    contextual_hits,
                    top_k=settings.retrieval_top_k,
                )
            else:
                hits = index_manager.retrieve(
                    requested_base_id, request.query, top_k=settings.retrieval_top_k
                )
            use_model = generator.available and bool(hits)
            yield sse(
                "status",
                {
                    "stage": "generating" if use_model else "fallback",
                    "message": "正在请求 DeepSeek" if use_model else "正在生成本地确定性答案",
                    "hit_count": len(hits),
                },
            )
            answer, _ = answer_service.answer(
                request.query,
                hits,
                context=memory.info,
                memory_context=memory.prompt_payload,
            )
            assistant_message = repository.append_answer(current.id, answer)
            memory_service.compact_if_needed(current.id)
            yield sse(
                "answer",
                {
                    "conversation_id": current.id,
                    "user_message_id": user_message.id,
                    "message_id": assistant_message.id,
                    "answer": answer.model_dump(mode="json"),
                },
            )
            yield sse("done", {"conversation_id": current.id, "message_id": assistant_message.id})
        except Exception as exc:
            yield sse("error", {"message": f"回答生成失败：{type(exc).__name__}"})

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
