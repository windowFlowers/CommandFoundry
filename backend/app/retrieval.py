from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from threading import RLock

import numpy as np
from rank_bm25 import BM25Okapi

from .knowledge import KnowledgeCatalog
from .models import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    KnowledgeStatus,
    KnowledgeTopic,
    RetrievalHit,
    SourceMetadata,
)
from .repository import ConversationRepository


LATIN_TOKEN = re.compile(r"[a-zA-Z0-9_+.#:/-]+")
CJK_SEQUENCE = re.compile(r"[\u3400-\u9fff]+")


def tokenize(text: str) -> list[str]:
    normalized = text.casefold()
    tokens = LATIN_TOKEN.findall(normalized)
    for sequence in CJK_SEQUENCE.findall(normalized):
        tokens.append(sequence)
        tokens.extend(sequence[index : index + 2] for index in range(max(len(sequence) - 1, 0)))
        if len(sequence) <= 6:
            tokens.extend(sequence)
    return [token for token in tokens if token.strip()]


class EmbeddingEngine:
    """One lazily loaded FastEmbed runtime shared by every knowledge-base index."""

    def __init__(self, model_name: str, cache_dir: Path, *, enabled: bool, local_files_only: bool) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.enabled = enabled
        self.local_files_only = local_files_only
        self._model = None
        self._lock = RLock()
        self.error = ""

    @property
    def ready(self) -> bool:
        return self._model is not None

    def initialize(self) -> bool:
        if not self.enabled:
            self.error = "已通过配置关闭向量检索"
            return False
        with self._lock:
            if self.ready:
                return True
            try:
                from fastembed import TextEmbedding

                self.cache_dir.mkdir(parents=True, exist_ok=True)
                try:
                    self._model = TextEmbedding(
                        model_name=self.model_name,
                        cache_dir=str(self.cache_dir),
                        local_files_only=self.local_files_only,
                    )
                except TypeError:
                    self._model = TextEmbedding(model_name=self.model_name, cache_dir=str(self.cache_dir))
                self.error = ""
                return True
            except Exception as exc:
                self._model = None
                self.error = f"{type(exc).__name__}: {exc}"
                return False

    def embed(self, texts: list[str]) -> np.ndarray | None:
        if not texts or not self.initialize():
            return None
        assert self._model is not None
        try:
            vectors = np.asarray(list(self._model.embed(texts)), dtype=np.float32)
            if vectors.ndim != 2 or vectors.shape[0] != len(texts):
                raise ValueError("向量模型返回了无效维度")
            return vectors
        except Exception as exc:
            self.error = f"向量化失败：{type(exc).__name__}: {exc}"
            return None


class _CatalogView:
    def __init__(self, topics: list[KnowledgeTopic]) -> None:
        self.topics = topics

    @property
    def domain_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(topic.domain for topic in self.topics).items()))


class HybridRetriever:
    def __init__(
        self,
        catalog: KnowledgeCatalog | _CatalogView,
        *,
        embedding_model: str,
        model_cache_dir: Path,
        embedding_enabled: bool = True,
        local_files_only: bool = False,
        candidate_k: int = 12,
        embedding_engine: EmbeddingEngine | None = None,
        precomputed_vectors: dict[str, np.ndarray] | None = None,
    ) -> None:
        self.catalog = catalog
        self.embedding_model_name = embedding_model
        self.model_cache_dir = model_cache_dir
        self.embedding_enabled = embedding_enabled
        self.local_files_only = local_files_only
        self.candidate_k = candidate_k
        self.engine = embedding_engine or EmbeddingEngine(
            embedding_model, model_cache_dir, enabled=embedding_enabled, local_files_only=local_files_only
        )
        self.precomputed_vectors = precomputed_vectors or {}
        self._lock = RLock()
        corpus = [tokenize(topic.retrieval_text) for topic in self.catalog.topics]
        self._bm25 = BM25Okapi(corpus) if corpus else None
        self._topic_vectors: np.ndarray | None = None
        self.embedding_error = ""

    @property
    def embedding_ready(self) -> bool:
        return self._topic_vectors is not None

    def initialize_embeddings(self) -> bool:
        if not self.embedding_enabled or not self.catalog.topics:
            self.embedding_error = "已通过配置关闭向量检索" if not self.embedding_enabled else ""
            return False
        with self._lock:
            if self.embedding_ready:
                return True
            vectors: list[np.ndarray | None] = []
            missing_texts: list[str] = []
            missing_indices: list[int] = []
            for index, topic in enumerate(self.catalog.topics):
                vector = self.precomputed_vectors.get(topic.id)
                if vector is None:
                    vectors.append(None)
                    missing_texts.append(topic.retrieval_text)
                    missing_indices.append(index)
                else:
                    vectors.append(np.asarray(vector, dtype=np.float32))
            embedded = self.engine.embed(missing_texts)
            if missing_texts and embedded is None:
                self.embedding_error = self.engine.error
                return False
            for row, index in enumerate(missing_indices):
                vectors[index] = embedded[row]  # type: ignore[index]
            try:
                matrix = np.asarray(vectors, dtype=np.float32)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                self._topic_vectors = matrix / np.clip(norms, 1e-12, None)
                self.embedding_error = ""
                return True
            except Exception as exc:
                self._topic_vectors = None
                self.embedding_error = f"{type(exc).__name__}: {exc}"
                return False

    @staticmethod
    def _rrf(rankings: list[list[int]], *, constant: int = 60) -> dict[int, float]:
        fused: dict[int, float] = {}
        for ranking in rankings:
            for rank, index in enumerate(ranking, start=1):
                fused[index] = fused.get(index, 0.0) + 1.0 / (constant + rank)
        return fused

    def _vector_scores(self, query: str) -> np.ndarray | None:
        if not self.embedding_ready:
            self.initialize_embeddings()
        if not self.embedding_ready:
            return None
        assert self._topic_vectors is not None
        query_matrix = self.engine.embed([query])
        if query_matrix is None:
            self.embedding_error = self.engine.error
            return None
        query_vector = query_matrix[0]
        query_vector /= max(float(np.linalg.norm(query_vector)), 1e-12)
        return self._topic_vectors @ query_vector

    def retrieve(self, query: str, *, top_k: int = 4) -> list[RetrievalHit]:
        query_tokens = tokenize(query)
        if not query_tokens or not self.catalog.topics or self._bm25 is None:
            return []
        raw_bm25 = np.asarray(self._bm25.get_scores(query_tokens), dtype=np.float32)
        normalized_query = re.sub(r"\s+", "", query.casefold())
        lexical_bonus = np.zeros(len(self.catalog.topics), dtype=np.float32)
        for index, topic in enumerate(self.catalog.topics):
            names = [topic.title, *topic.aliases]
            normalized_names = [re.sub(r"\s+", "", name.casefold()) for name in names]
            if normalized_query in normalized_names:
                lexical_bonus[index] = 25.0
            elif any(normalized_query and normalized_query in name for name in normalized_names):
                lexical_bonus[index] = 8.0
        bm25_scores = raw_bm25 + lexical_bonus
        bm25_order = np.argsort(-bm25_scores)[: self.candidate_k].tolist()
        rankings = [bm25_order]
        vector_scores = self._vector_scores(query) if self.embedding_enabled else None
        if vector_scores is not None:
            rankings.append(np.argsort(-vector_scores)[: self.candidate_k].tolist())
        fused = self._rrf(rankings)
        ordered = sorted(fused, key=lambda index: (fused[index], float(bm25_scores[index])), reverse=True)
        best_bm25 = float(bm25_scores[bm25_order[0]]) if bm25_order else 0.0
        best_vector = float(np.max(vector_scores)) if vector_scores is not None else 0.0
        if best_bm25 <= 0 and best_vector < 0.42:
            return []
        return [
            RetrievalHit(
                topic=self.catalog.topics[index],
                score=round(float(fused[index]), 6),
                bm25_score=round(float(bm25_scores[index]), 6),
                vector_score=round(float(vector_scores[index]), 6) if vector_scores is not None else 0.0,
            )
            for index in ordered[:top_k]
        ]

    def status(self) -> KnowledgeStatus:
        return KnowledgeStatus(
            ready=bool(self.catalog.topics),
            topic_count=len(self.catalog.topics),
            domains=self.catalog.domain_counts,
            retrieval_mode="hybrid" if self.embedding_ready else "bm25",
            embedding_model=self.embedding_model_name,
            embedding_error=self.embedding_error or self.engine.error,
        )


class KnowledgeIndexManager:
    def __init__(
        self,
        *,
        catalog: KnowledgeCatalog,
        repository: ConversationRepository,
        embedding_engine: EmbeddingEngine,
        candidate_k: int,
    ) -> None:
        self.catalog = catalog
        self.repository = repository
        self.engine = embedding_engine
        self.candidate_k = candidate_k
        self._cache: dict[str, HybridRetriever] = {}
        self._lock = RLock()

    def _uploaded_topics(self, knowledge_base_id: str) -> tuple[list[KnowledgeTopic], dict[str, np.ndarray]]:
        topics: list[KnowledgeTopic] = []
        vectors: dict[str, np.ndarray] = {}
        for row in self.repository.list_chunks(knowledge_base_id):
            topic_id = f"upload.{row['id']}"
            topics.append(
                KnowledgeTopic(
                    id=topic_id,
                    domain="uploaded",
                    title=row["title"],
                    summary=row["text"],
                    source=SourceMetadata(
                        source_id=f"upload.{row['document_id']}",
                        title=row["filename"],
                        source_url=None,
                        license="User provided",
                        revision=row["sha256"][:12],
                        sha256=row["sha256"],
                        kind="upload",
                        document_id=row["document_id"],
                        knowledge_base_id=knowledge_base_id,
                    ),
                )
            )
            if row["embedding"] and row["embedding_dimension"]:
                vector = np.frombuffer(row["embedding"], dtype=np.float32)
                if len(vector) == int(row["embedding_dimension"]):
                    vectors[topic_id] = vector
        return topics, vectors

    def get(self, knowledge_base_id: str) -> HybridRetriever:
        with self._lock:
            cached = self._cache.get(knowledge_base_id)
            if cached is not None:
                return cached
            if self.repository.get_knowledge_base(knowledge_base_id) is None:
                raise KeyError(knowledge_base_id)
            uploaded, vectors = self._uploaded_topics(knowledge_base_id)
            builtin = self.catalog.topics if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID else []
            retriever = HybridRetriever(
                _CatalogView([*builtin, *uploaded]),
                embedding_model=self.engine.model_name,
                model_cache_dir=self.engine.cache_dir,
                embedding_enabled=self.engine.enabled,
                local_files_only=self.engine.local_files_only,
                candidate_k=self.candidate_k,
                embedding_engine=self.engine,
                precomputed_vectors=vectors,
            )
            self._cache[knowledge_base_id] = retriever
            return retriever

    def invalidate(self, knowledge_base_id: str) -> None:
        with self._lock:
            self._cache.pop(knowledge_base_id, None)

    def retrieve(self, knowledge_base_id: str, query: str, *, top_k: int) -> list[RetrievalHit]:
        return self.get(knowledge_base_id).retrieve(query, top_k=top_k)

    def status(self, knowledge_base_id: str) -> KnowledgeStatus:
        knowledge_base = self.repository.get_knowledge_base(knowledge_base_id)
        if knowledge_base is None:
            raise KeyError(knowledge_base_id)
        retriever_status = self.get(knowledge_base_id).status()
        documents = self.repository.list_documents(knowledge_base_id)
        return KnowledgeStatus(
            **retriever_status.model_dump(exclude={"knowledge_base_id", "knowledge_base_name", "document_count", "pending_document_count", "failed_document_count"}),
            knowledge_base_id=knowledge_base_id,
            knowledge_base_name=knowledge_base.name,
            document_count=len(documents),
            pending_document_count=sum(item.status in {"pending", "indexing"} for item in documents),
            failed_document_count=sum(item.status == "failed" for item in documents),
        )
