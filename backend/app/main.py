from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .answering import AnswerService
from .config import settings
from .generation import DeepSeekGenerator
from .knowledge import KnowledgeCatalog
from .models import (
    ChatRequest,
    Conversation,
    ConversationCreateRequest,
    ConversationListResponse,
    HealthResponse,
    KnowledgeStatus,
)
from .repository import ConversationRepository
from .retrieval import HybridRetriever


catalog = KnowledgeCatalog(settings.knowledge_dir)
catalog.load()
retriever = HybridRetriever(
    catalog,
    embedding_model=settings.embedding_model,
    model_cache_dir=settings.model_cache_dir,
    embedding_enabled=settings.embedding_enabled,
    local_files_only=settings.embedding_local_only,
    candidate_k=settings.retrieval_candidate_k,
)
repository = ConversationRepository(settings.storage_dir / "aegis-v2.sqlite3")
generator = DeepSeekGenerator(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    model=settings.llm_model,
    timeout_seconds=settings.llm_timeout_seconds,
)
answer_service = AnswerService(generator)


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
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/", response_model=HealthResponse)
@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(version=settings.app_version)


@app.get("/knowledge/status", response_model=KnowledgeStatus)
def knowledge_status() -> KnowledgeStatus:
    return retriever.status()


@app.post("/conversations", response_model=Conversation, status_code=status.HTTP_201_CREATED)
def create_conversation(request: ConversationCreateRequest | None = None) -> Conversation:
    return repository.create(request.title if request else "新对话")


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


@app.post("/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    def generate_events():
        conversation = repository.get(request.conversation_id) if request.conversation_id else None
        if request.conversation_id and conversation is None:
            yield sse("error", {"message": "会话不存在"})
            return
        if conversation is None:
            conversation = repository.create()
        repository.append_user(conversation.id, request.query)
        try:
            yield sse("status", {"stage": "retrieving", "message": "正在检索本地知识库"})
            hits = retriever.retrieve(request.query, top_k=settings.retrieval_top_k)
            use_model = generator.available and bool(hits)
            yield sse(
                "status",
                {
                    "stage": "generating" if use_model else "fallback",
                    "message": "正在由 DeepSeek 整理答案" if use_model else "正在生成本地确定性答案",
                    "hit_count": len(hits),
                },
            )
            answer, reason = answer_service.answer(request.query, hits)
            assistant_message = repository.append_answer(conversation.id, answer)
            yield sse(
                "answer",
                {
                    "conversation_id": conversation.id,
                    "message_id": assistant_message.id,
                    "answer": answer.model_dump(mode="json"),
                    "fallback_reason": reason if answer.mode == "local_fallback" else None,
                },
            )
            yield sse("done", {"conversation_id": conversation.id, "message_id": assistant_message.id})
        except Exception as exc:
            yield sse("error", {"message": f"回答生成失败：{type(exc).__name__}"})

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
