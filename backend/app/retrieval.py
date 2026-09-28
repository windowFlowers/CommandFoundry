from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterable, Sequence

import numpy as np
from rank_bm25 import BM25Okapi

from .knowledge import KnowledgeCatalog
from .models import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    CommandBlock,
    KnowledgeStatus,
    KnowledgeTopic,
    MatchedChunk,
    RetrievalHit,
    SourceLocator,
    SourceMetadata,
)
from .repository import ConversationRepository


LATIN_TOKEN = re.compile(r"[a-zA-Z0-9_+.#:/-]+")
CJK_SEQUENCE = re.compile(r"[\u3400-\u9fff]+")


# These are deliberately conservative.  A metadata filter is only applied
# when the user has named a platform/shell or a technology with a stable
# domain.  Generic words such as "文件" or "日志" must not route a query to
# one domain merely because that domain happens to contain the word.
_DOMAIN_HINTS: dict[str, tuple[str, ...]] = {
    "cicd": ("cicd", "ci/cd", "github actions", "jenkins", "gitlab ci", "pipeline"),
    "docker": ("docker", "容器", "container"),
    "git": ("git", "github", "gitlab", "提交", "commit", "分支", "branch"),
    "http": ("http", "https", "curl", "wget", "接口", "api", "json 请求"),
    "java": ("java", "maven", "gradle", "jar"),
    "kubernetes": ("kubernetes", "k8s", "kubectl", "helm"),
    "linux": ("linux", "ubuntu", "debian", "bash", "shell", "systemd"),
    "network": ("网络", "network", "dns", "端口", "ping", "tcp", "ipconfig"),
    "node": ("node", "node.js", "npm", "npx", "yarn", "pnpm"),
    "python": ("python", "pip", "venv", "virtualenv", "conda"),
    "redis": ("redis", "redis-cli"),
    "sql": ("sql", "mysql", "postgres", "postgresql", "数据库", "查询", "select"),
    "testing": ("测试", "testing", "pytest", "jest", "playwright", "单元测试"),
    "windows": (
        "windows",
        "powershell",
        "cmd.exe",
        "命令提示符",
        "winget",
        "chocolatey",
        "scoop",
        "robocopy",
    ),
}

_QUERY_FILLERS = re.compile(
    r"(?i)(?:请问|请|告诉我|帮我|帮忙|我想|我需要|能否|可以|如何|怎么|怎样|有什么办法|一下|的|是什么|指令|命令|操作|(?<![a-z])how\s+to(?![a-z])|(?<![a-z])how(?![a-z])|(?<![a-z])can\s+you(?![a-z])|(?<![a-z])please(?![a-z]))"
)
_MULTI_QUERY_SPLIT = re.compile(r"\s*(?:以及|并且|同时|另外|然后|和|还有|及|,|，|;|；)\s*")


@dataclass(frozen=True)
class QueryAnalysis:
    """Deterministic query plan used before retrieval.

    This intentionally contains no model-generated text.  ``variants`` are
    bounded, normalized lexical views of the same user query; they are later
    fused with RRF.  The optional metadata fields are strict only when the
    query contains an explicit high-signal hint.
    """

    original: str
    variants: tuple[str, ...]
    intent: str = "knowledge"
    domain: str | None = None
    platform: str | None = None
    shell: str | None = None
    decomposed: bool = False

    @property
    def has_metadata_filter(self) -> bool:
        return bool(self.domain or self.platform or self.shell)


def _normalized_query(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _contains_hint(query: str, hint: str) -> bool:
    value = hint.casefold()
    if re.search(r"[a-z0-9]", value):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", query.casefold()))
    return value in query.casefold()


def _infer_domain(query: str, topics: Sequence[KnowledgeTopic]) -> str | None:
    lowered = query.casefold()
    candidates: dict[str, int] = {}
    known_domains = {topic.domain for topic in topics}
    for domain, hints in _DOMAIN_HINTS.items():
        if domain not in known_domains:
            continue
        score = sum(1 for hint in hints if _contains_hint(lowered, hint))
        if score:
            candidates[domain] = score

    # Catalog titles/aliases provide a small, data-driven extension without
    # turning every stop word into a router.  Only names of at least three
    # meaningful characters participate, and a tie remains unfiltered.
    for topic in topics:
        names = [topic.id.rsplit(".", 1)[-1], topic.title, *topic.aliases]
        for name in names:
            candidate = _normalized_query(name).casefold()
            if len(candidate) < 3 or "<" in candidate:
                continue
            if _contains_hint(lowered, candidate):
                candidates[topic.domain] = candidates.get(topic.domain, 0) + min(len(candidate), 32) / 32

    if not candidates:
        return None
    ordered = sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    if len(ordered) > 1 and abs(ordered[0][1] - ordered[1][1]) < 0.75:
        return None
    return ordered[0][0]


def _infer_shell_and_platform(query: str) -> tuple[str | None, str | None]:
    lowered = query.casefold()
    if any(_contains_hint(lowered, hint) for hint in ("powershell", "pwsh")):
        return "powershell", "windows"
    if any(_contains_hint(lowered, hint) for hint in ("cmd.exe", "cmd", "命令提示符", "批处理")):
        return "cmd", "windows"
    if any(_contains_hint(lowered, hint) for hint in ("windows", "win10", "win11", "windows server")):
        return None, "windows"
    if any(_contains_hint(lowered, hint) for hint in ("bash", "posix", "unix", "linux", "ubuntu", "macos", "mac os")):
        return "posix", "posix"
    if any(_contains_hint(lowered, hint) for hint in ("sql", "mysql", "postgres", "postgresql")):
        return "sql", None
    return None, None


def _focused_variant(query: str) -> str:
    focused = _QUERY_FILLERS.sub(" ", query)
    return _normalized_query(focused).strip(" ，,。.!！？?：:;")


def _classify_intent(query: str) -> str:
    lowered = query.casefold()
    if any(token in lowered for token in ("模板", "范例", "示例", "template", "example")):
        return "template"
    if any(token in lowered for token in ("比较", "区别", "差异", "对比", "compare", "difference")):
        return "compare"
    if any(token in lowered for token in ("文档", "哪一段", "原文", "来源", "引用", "出处")):
        return "document_lookup"
    if any(token in lowered for token in ("命令", "指令", "执行", "运行", "怎么", "如何", "command")) or re.search(
        r"(?<![a-z])how(?![a-z])", lowered
    ):
        return "command"
    return "knowledge"


def analyze_query(query: str, topics: Iterable[KnowledgeTopic]) -> QueryAnalysis:
    """Create a bounded deterministic query plan.

    The original query is always retained.  A focused variant is added only
    when it differs materially, and conjunctions are decomposed only when at
    least two parts contain a known domain signal.  This prevents a generic
    Chinese sentence from unexpectedly becoming a multi-query request.
    """

    original = _normalized_query(query)
    topic_list = list(topics)
    focused = _focused_variant(original)
    variants: list[str] = [original] if original else []
    if focused and focused.casefold() != original.casefold() and len(tokenize(focused)) >= 1:
        variants.append(focused)

    parts = [part for part in _MULTI_QUERY_SPLIT.split(original) if part.strip()]
    part_domains = [_infer_domain(part, topic_list) for part in parts]
    recognized_parts = [part for part, domain in zip(parts, part_domains) if domain]
    decomposed = len(recognized_parts) >= 2 and len(set(domain for domain in part_domains if domain)) >= 2
    if decomposed:
        variants.extend(recognized_parts)

    # De-duplicate while preserving the original-first order and cap work for
    # pathological long input.  The cap also bounds embedding API calls.
    unique_variants = tuple(dict.fromkeys(item for item in variants if item) )[:4]
    shell, platform = _infer_shell_and_platform(original)
    domains = {domain for domain in part_domains if domain}
    domain = _infer_domain(original, topic_list) if len(domains) <= 1 else None
    return QueryAnalysis(
        original=original,
        variants=unique_variants,
        intent=_classify_intent(original),
        domain=domain,
        platform=platform,
        shell=shell,
        decomposed=decomposed,
    )


def _platform_matches(command_platforms: Iterable[str], platform: str) -> bool:
    values = " ".join(str(item).casefold() for item in command_platforms)
    if platform == "windows":
        return any(token in values for token in ("windows", "win32", "win10", "win11"))
    if platform == "posix":
        return any(token in values for token in ("linux", "macos", "mac os", "unix", "posix", "wsl"))
    if platform == "linux":
        return "linux" in values or "wsl" in values
    if platform == "macos":
        return "macos" in values or "mac os" in values
    return platform.casefold() in values


def topic_matches_query_filters(
    topic: KnowledgeTopic,
    *,
    domain: str | None = None,
    platform: str | None = None,
    shell: str | None = None,
) -> bool:
    """Return whether a topic satisfies explicit query metadata filters."""

    if domain and topic.domain.casefold() != domain.casefold():
        return False
    commands = topic.commands
    if shell:
        command_shells = {
            (command.shell or ("posix" if command.language == "bash" else command.language)).casefold()
            for command in commands
        }
        if shell.casefold() not in command_shells:
            return False
    if platform and commands and not any(_platform_matches(command.platforms, platform) for command in commands):
        return False
    return True


def retrieval_confidence(hits: Sequence[RetrievalHit]) -> float:
    """Return a bounded evidence confidence for no-answer routing.

    This is intentionally a retrieval signal, not a claim about answer
    correctness.  It combines the existing BM25 and vector thresholds and is
    useful to keep an empty/weak retrieval result from being handed to a model
    as if it were grounded evidence.
    """

    if not hits:
        return 0.0
    best_lexical = max(0.0, float(max(hit.bm25_score for hit in hits)))
    lexical_confidence = 1.0 - np.exp(-best_lexical / 4.0)
    best_vector = max(0.0, float(max(hit.vector_score for hit in hits)))
    vector_confidence = min(1.0, max(0.0, (best_vector - 0.42) / 0.58))
    return round(float(max(lexical_confidence, vector_confidence)), 6)


def has_sufficient_evidence(hits: Sequence[RetrievalHit], *, minimum_confidence: float = 0.05) -> bool:
    """Conservatively gate model answering on retrieval evidence."""

    return bool(hits) and retrieval_confidence(hits) >= minimum_confidence


def _command_language(code: str) -> str:
    lowered = code.strip().casefold()
    if re.match(r"^(select|insert|update|delete|create|alter|drop|truncate|with)\b", lowered):
        return "sql"
    if re.match(r"^(get-|set-|new-|remove-|start-|stop-|restart-|test-)\w+", lowered):
        return "powershell"
    return "bash"


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
        self._corpus = [tokenize(topic.retrieval_text) for topic in self.catalog.topics]
        self._bm25 = BM25Okapi(self._corpus) if self._corpus else None
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

    def _retrieve_once(
        self,
        query: str,
        *,
        top_k: int,
        domain: str | None = None,
        platform: str | None = None,
        shell: str | None = None,
    ) -> list[RetrievalHit]:
        query_tokens = tokenize(query)
        if not query_tokens or not self.catalog.topics or self._bm25 is None:
            return []
        allowed = [
            index
            for index, topic in enumerate(self.catalog.topics)
            if topic_matches_query_filters(topic, domain=domain, platform=platform, shell=shell)
        ]
        if not allowed:
            return []
        allowed_set = set(allowed)
        raw_bm25 = np.asarray(self._bm25.get_scores(query_tokens), dtype=np.float32)
        normalized_query = re.sub(
            r"\s+", "", unicodedata.normalize("NFKC", query).casefold()
        )
        lexical_bonus = np.zeros(len(self.catalog.topics), dtype=np.float32)
        token_overlap = np.zeros(len(self.catalog.topics), dtype=np.float32)
        query_token_set = set(query_tokens)
        small_corpus = len(self.catalog.topics) <= 4
        for index, topic in enumerate(self.catalog.topics):
            if index not in allowed_set:
                # Do not let filtered-out topics participate in either the
                # candidate list or the confidence threshold.
                raw_bm25[index] = -np.inf
                continue
            overlap = len(query_token_set.intersection(self._corpus[index]))
            token_overlap[index] = overlap
            # rank-bm25 can produce negative IDF in a one-chunk corpus. Explicit
            # token overlap is a deterministic lexical signal and keeps small
            # offline knowledge bases usable without the embedding model.
            if overlap and small_corpus:
                lexical_bonus[index] += 4.0 * overlap / max(1, len(query_token_set))
            names = [topic.title, *topic.aliases]
            normalized_names = [
                re.sub(r"\s+", "", unicodedata.normalize("NFKC", name).casefold())
                for name in names
            ]
            if normalized_query in normalized_names:
                lexical_bonus[index] = 25.0
            elif any(normalized_query and normalized_query in name for name in normalized_names):
                lexical_bonus[index] = 8.0
        bm25_scores = raw_bm25 + lexical_bonus
        bm25_order = [index for index in np.argsort(-bm25_scores).tolist() if index in allowed_set][: self.candidate_k]
        rankings = [bm25_order]
        vector_scores = self._vector_scores(query) if self.embedding_enabled else None
        if vector_scores is not None:
            rankings.append([index for index in np.argsort(-vector_scores).tolist() if index in allowed_set][: self.candidate_k])
        fused = self._rrf(rankings)
        ordered = sorted(fused, key=lambda index: (fused[index], float(bm25_scores[index])), reverse=True)
        best_bm25 = float(bm25_scores[bm25_order[0]]) if bm25_order else 0.0
        best_vector = float(np.max(vector_scores[allowed])) if vector_scores is not None and allowed else 0.0
        best_overlap = float(np.max(token_overlap)) if len(token_overlap) else 0.0
        if best_bm25 <= 0 and best_vector < 0.42 and not (small_corpus and best_overlap > 0):
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

    def retrieve(self, query: str, *, top_k: int = 4) -> list[RetrievalHit]:
        """Backward-compatible single-query hybrid retrieval."""

        return self._retrieve_once(query, top_k=top_k)

    @staticmethod
    def rerank_hits(query: str, hits: Sequence[RetrievalHit], *, top_k: int | None = None) -> list[RetrievalHit]:
        """Apply a lightweight deterministic rerank after RRF.

        The project intentionally does not add a cross-encoder dependency for
        this stage.  Exact title/alias matches and token overlap are stable,
        inspectable tie-breakers; the original RRF/BM25/vector scores remain
        the primary ordering signals.
        """

        query_tokens = set(tokenize(query))

        def key(hit: RetrievalHit) -> tuple[float, float, float, int, float, str]:
            topic = hit.topic
            names = [topic.title, *topic.aliases]
            normalized_query = re.sub(r"\s+", "", unicodedata.normalize("NFKC", query).casefold())
            exact_name = int(
                any(
                    normalized_query == re.sub(r"\s+", "", unicodedata.normalize("NFKC", name).casefold())
                    for name in names
                )
            )
            name_tokens = set(tokenize(" ".join(names)))
            overlap = len(query_tokens.intersection(name_tokens)) / max(1, len(query_tokens))
            return (hit.score, hit.bm25_score, hit.vector_score, exact_name, overlap, topic.id)

        ordered = sorted(hits, key=key, reverse=True)
        return ordered if top_k is None else ordered[:top_k]

    def retrieve_advanced(
        self,
        query: str,
        *,
        top_k: int = 4,
        analysis: QueryAnalysis | None = None,
    ) -> list[RetrievalHit]:
        """Retrieve with bounded query variants, metadata filters and rerank.

        Each lexical view is independently ranked and fused with the same RRF
        primitive already used by the baseline retriever.  This keeps the
        change reversible and prevents a single rewritten query from replacing
        evidence found by the user's original wording.
        """

        plan = analysis or analyze_query(query, self.catalog.topics)
        variants = plan.variants or (query,)
        scores: dict[str, float] = {}
        representatives: dict[str, RetrievalHit] = {}
        for variant_index, variant in enumerate(variants):
            # Original wording is authoritative; derived views are weaker.
            weight = 1.0 if variant_index == 0 else 0.7
            hits = self._retrieve_once(
                variant,
                top_k=max(top_k * 2, self.candidate_k),
                domain=plan.domain,
                platform=plan.platform,
                shell=plan.shell,
            )
            for rank, hit in enumerate(hits, start=1):
                topic_id = hit.topic.id
                scores[topic_id] = scores.get(topic_id, 0.0) + weight / (60 + rank)
                existing = representatives.get(topic_id)
                if existing is None:
                    representatives[topic_id] = hit
                elif hit.matched_chunks:
                    matched = {item.chunk_id: item for item in existing.matched_chunks}
                    matched.update({item.chunk_id: item for item in hit.matched_chunks})
                    representatives[topic_id] = existing.model_copy(update={"matched_chunks": list(matched.values())})

        fused = [
            representatives[topic_id].model_copy(update={"score": round(score, 6)})
            for topic_id, score in scores.items()
        ]
        return self.rerank_hits(plan.original, fused, top_k=top_k)

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
            locator_payload = json.loads(row.get("locator_json") or "{}")
            locator = SourceLocator.model_validate(locator_payload) if locator_payload.get("kind") else None
            command_codes = json.loads(row.get("command_evidence_json") or "[]")
            commands = [
                CommandBlock(
                    label="文档中的命令",
                    language=_command_language(code),
                    code=code,
                    platforms=[],
                    prerequisites=[],
                )
                for code in command_codes
                if str(code).strip()
            ]
            topics.append(
                KnowledgeTopic(
                    id=topic_id,
                    domain="uploaded",
                    title=row["title"],
                    summary=row["text"],
                    commands=commands,
                    index_text=row.get("retrieval_text") or row["text"],
                    parent_context=row.get("parent_text") or row["text"],
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
                        chunk_id=row["id"],
                        parent_id=row.get("parent_id"),
                        index_revision=row.get("index_revision") or "",
                        locator=locator,
                        command_evidence=command_codes,
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

    @staticmethod
    def _collapse_parent_hits(raw_hits: list[RetrievalHit], *, top_k: int) -> list[RetrievalHit]:
        """Collapse child evidence to bounded parent hits after ranking."""

        grouped: dict[str, list[RetrievalHit]] = {}
        standalone: list[RetrievalHit] = []
        for hit in raw_hits:
            source = hit.topic.source
            if source.kind != "upload" or not source.parent_id:
                standalone.append(hit)
                continue
            grouped.setdefault(source.parent_id, []).append(hit)

        parents: list[RetrievalHit] = []
        for parent_id, children in grouped.items():
            children.sort(key=lambda item: (item.score, item.bm25_score, item.vector_score), reverse=True)
            best = children[0]
            matched = [
                MatchedChunk(
                    chunk_id=child.topic.source.chunk_id or child.topic.id,
                    parent_id=parent_id,
                    text=child.topic.summary,
                    score=child.score,
                    locator=child.topic.source.locator,
                    command_evidence=child.topic.source.command_evidence,
                )
                for child in children
            ]
            parent_commands: list[CommandBlock] = []
            seen_codes: set[str] = set()
            for child in children:
                for code in child.topic.source.command_evidence:
                    if code in seen_codes:
                        continue
                    seen_codes.add(code)
                    parent_commands.append(
                        CommandBlock(
                            label="文档中的命令",
                            language=_command_language(code),
                            code=code,
                        )
                    )
            # Extra matches are only a deterministic tie-breaker, never a length reward.
            tie_bonus = min(len(children) - 1, 3) * 0.000001
            parents.append(
                RetrievalHit(
                    topic=best.topic.model_copy(
                        update={
                            "id": f"parent.{parent_id}",
                            "summary": best.topic.parent_context or best.topic.summary,
                            "commands": parent_commands,
                        }
                    ),
                    score=round(best.score + tie_bonus, 6),
                    bm25_score=best.bm25_score,
                    vector_score=best.vector_score,
                    parent_text=best.topic.parent_context or best.topic.summary,
                    matched_chunks=matched,
                )
            )

        ordered = sorted(
            [*standalone, *parents],
            key=lambda item: (item.score, item.bm25_score, item.vector_score),
            reverse=True,
        )
        selected: list[RetrievalHit] = []
        per_document: Counter[str] = Counter()
        for hit in ordered:
            document_id = hit.topic.source.document_id
            if document_id and per_document[document_id] >= 2:
                continue
            selected.append(hit)
            if document_id:
                per_document[document_id] += 1
            if len(selected) >= top_k:
                break
        return selected

    def retrieve(self, knowledge_base_id: str, query: str, *, top_k: int) -> list[RetrievalHit]:
        # Child chunks are ranked first, then collapsed to bounded parent evidence.
        raw_hits = self.get(knowledge_base_id).retrieve(query, top_k=max(20, top_k))
        return self._collapse_parent_hits(raw_hits, top_k=top_k)

    def retrieve_advanced(
        self,
        knowledge_base_id: str,
        query: str,
        *,
        top_k: int,
        analysis: QueryAnalysis | None = None,
    ) -> list[RetrievalHit]:
        """Advanced query path preserving the existing parent/child contract."""

        raw_hits = self.get(knowledge_base_id).retrieve_advanced(
            query,
            top_k=max(20, top_k),
            analysis=analysis,
        )
        return self._collapse_parent_hits(raw_hits, top_k=top_k)

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
