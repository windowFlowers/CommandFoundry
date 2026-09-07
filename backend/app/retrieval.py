from __future__ import annotations

import re
from threading import RLock

import numpy as np
from rank_bm25 import BM25Okapi

from .knowledge import KnowledgeCatalog
from .models import KnowledgeStatus, RetrievalHit


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


class HybridRetriever:
    def __init__(
        self,
        catalog: KnowledgeCatalog,
        *,
        embedding_model: str,
        model_cache_dir,
        embedding_enabled: bool = True,
        local_files_only: bool = False,
        candidate_k: int = 12,
    ) -> None:
        self.catalog = catalog
        self.embedding_model_name = embedding_model
        self.model_cache_dir = model_cache_dir
        self.embedding_enabled = embedding_enabled
        self.local_files_only = local_files_only
        self.candidate_k = candidate_k
        self._lock = RLock()
        self._bm25 = BM25Okapi([tokenize(topic.retrieval_text) for topic in self.catalog.topics])
        self._embedding_model = None
        self._topic_vectors: np.ndarray | None = None
        self.embedding_error = ""

    @property
    def embedding_ready(self) -> bool:
        return self._embedding_model is not None and self._topic_vectors is not None

    def initialize_embeddings(self) -> bool:
        if not self.embedding_enabled:
            self.embedding_error = "已通过配置关闭向量检索"
            return False
        with self._lock:
            if self.embedding_ready:
                return True
            try:
                from fastembed import TextEmbedding

                self.model_cache_dir.mkdir(parents=True, exist_ok=True)
                try:
                    model = TextEmbedding(
                        model_name=self.embedding_model_name,
                        cache_dir=str(self.model_cache_dir),
                        local_files_only=self.local_files_only,
                    )
                except TypeError:
                    model = TextEmbedding(
                        model_name=self.embedding_model_name,
                        cache_dir=str(self.model_cache_dir),
                    )
                vectors = np.asarray(
                    list(model.embed([topic.retrieval_text for topic in self.catalog.topics])),
                    dtype=np.float32,
                )
                if vectors.ndim != 2 or vectors.shape[0] != len(self.catalog.topics):
                    raise ValueError("向量模型返回了无效维度")
                norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                self._topic_vectors = vectors / np.clip(norms, 1e-12, None)
                self._embedding_model = model
                self.embedding_error = ""
                return True
            except Exception as exc:
                self._embedding_model = None
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
            return None
        assert self._embedding_model is not None and self._topic_vectors is not None
        try:
            query_vector = np.asarray(list(self._embedding_model.embed([query]))[0], dtype=np.float32)
            query_vector /= max(float(np.linalg.norm(query_vector)), 1e-12)
            return self._topic_vectors @ query_vector
        except Exception as exc:
            self.embedding_error = f"查询向量化失败：{type(exc).__name__}: {exc}"
            return None

    def retrieve(self, query: str, *, top_k: int = 4) -> list[RetrievalHit]:
        query_tokens = tokenize(query)
        if not query_tokens or not self.catalog.topics:
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

        vector_scores = self._vector_scores(query)
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
            embedding_error=self.embedding_error,
        )
