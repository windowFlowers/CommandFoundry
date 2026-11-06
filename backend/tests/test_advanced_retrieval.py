from __future__ import annotations

from app import main as main_module
from app.main import catalog, retriever
from app.models import RetrievalHit
from app.retrieval import (
    FastEmbedReranker,
    HybridRetriever,
    QueryAnalysis,
    analyze_query,
    has_sufficient_evidence,
    retrieval_confidence,
    topic_matches_query_filters,
)


def test_query_analysis_extracts_explicit_shell_and_platform() -> None:
    analysis = analyze_query("请用 PowerShell 在 Windows 中查看端口", catalog.topics)

    assert isinstance(analysis, QueryAnalysis)
    assert analysis.domain == "windows"
    assert analysis.platform == "windows"
    assert analysis.shell == "powershell"
    assert analysis.intent == "knowledge"
    assert analysis.variants[0].startswith("请用 PowerShell")
    assert len(analysis.variants) >= 2


def test_query_analysis_decomposes_only_distinct_known_domains() -> None:
    analysis = analyze_query("Docker 查看日志，并且 Git 怎么安全回滚", catalog.topics)

    assert analysis.decomposed is True
    assert analysis.domain is None
    assert any("Docker" in variant for variant in analysis.variants[1:])
    assert any("Git" in variant for variant in analysis.variants[1:])

    generic = analyze_query("请告诉我怎么处理文件和日志", catalog.topics)
    assert generic.decomposed is False


def test_exact_topic_alias_overrides_token_domain_routing() -> None:
    query = "where python 路径"

    assert analyze_query(query, catalog.topics).domain == "windows"
    assert retriever.retrieve_advanced(query, top_k=4)[0].topic.id == "windows.where"
    assert analyze_query("python where 路径", catalog.topics).domain != "windows"


def test_explicit_metadata_filter_keeps_shell_specific_topics() -> None:
    powershell_topic = next(topic for topic in catalog.topics if topic.id == "windows.new-item")
    linux_topic = next(topic for topic in catalog.topics if topic.id == "linux.cd")

    assert topic_matches_query_filters(powershell_topic, domain="windows", platform="windows", shell="powershell")
    assert not topic_matches_query_filters(linux_topic, domain="windows", platform="windows", shell="powershell")


def test_advanced_retrieval_preserves_showcase_first_hits() -> None:
    cases = {
        "在 Linux 中切换文件目录的指令是什么？": "linux.cd",
        "Git 怎么安全回滚一次提交？": "git.git-revert",
        "MySQL 中怎么创建一个视图？": "sql.create-view",
        "如何用 cURL 发送带 JSON 的 POST 请求？": "http.curl",
    }
    for query, expected in cases.items():
        hits = retriever.retrieve_advanced(query, top_k=3)
        assert hits
        assert hits[0].topic.id == expected


def test_hybrid_retrieval_keeps_explicit_ascii_topic_identifiers() -> None:
    cases = {
        "Get-ChildItem 怎么用": "windows.get-childitem",
        "Resolve-DnsName 怎么用": "windows.resolve-dnsname",
        "Restart-Service 怎么用": "windows.restart-service",
        "PostgreSQL CREATE VIEW": "sql.postgres-create-view",
        "PostgreSQL EXPLAIN ANALYZE": "sql.postgres-explain",
        "PostgreSQL INSERT RETURNING": "sql.postgres-insert",
    }
    for query, expected in cases.items():
        hits = retriever.retrieve_advanced(query, top_k=3)
        assert hits
        assert hits[0].topic.id == expected


def test_index_manager_preserves_advanced_order_after_parent_collapse() -> None:
    """The API-facing manager must not undo lexical identifier ranking."""

    hits = main_module.index_manager.retrieve_advanced(
        "developer-it",
        "Get-ChildItem 怎么用",
        top_k=4,
    )

    assert hits
    assert hits[0].topic.id == "windows.get-childitem"


def test_retrieval_confidence_is_a_conservative_no_answer_signal() -> None:
    assert retrieval_confidence([]) == 0.0
    assert has_sufficient_evidence([]) is False

    hit = RetrievalHit(topic=catalog.topics[0], score=0.02, bm25_score=0.0, vector_score=0.1)
    assert retrieval_confidence([hit]) == 0.0
    assert has_sufficient_evidence([hit]) is False

    grounded = hit.model_copy(update={"bm25_score": 2.0})
    assert retrieval_confidence([grounded]) > 0.05
    assert has_sufficient_evidence([grounded]) is True


def test_optional_cross_encoder_reranker_only_reorders_existing_hits() -> None:
    class FakeReranker:
        name = "fake-cross-encoder"

        def score(self, query: str, documents: list[str]) -> list[float]:
            assert query == "rerank"
            return [float(index) for index in range(len(documents))]

    base = HybridRetriever(
        catalog,
        embedding_model="disabled",
        model_cache_dir=main_module.settings.model_cache_dir,
        embedding_enabled=False,
        reranker=FakeReranker(),
    )
    hits = [
        RetrievalHit(topic=catalog.topics[0], score=0.9),
        RetrievalHit(topic=catalog.topics[1], score=0.8),
    ]
    ordered = base._rerank_hits("rerank", hits, top_k=2)

    assert [item.topic.id for item in ordered] == [catalog.topics[1].id, catalog.topics[0].id]
    assert [item.topic.source.source_id for item in ordered] == [
        hits[1].topic.source.source_id,
        hits[0].topic.source.source_id,
    ]


def test_cross_encoder_failure_falls_back_to_deterministic_order() -> None:
    class FailedReranker:
        name = "failed"

        def score(self, query: str, documents: list[str]) -> None:
            return None

    base = HybridRetriever(
        catalog,
        embedding_model="disabled",
        model_cache_dir=main_module.settings.model_cache_dir,
        embedding_enabled=False,
        reranker=FailedReranker(),
    )
    hits = [
        RetrievalHit(topic=catalog.topics[0], score=0.1),
        RetrievalHit(topic=catalog.topics[1], score=0.9),
    ]

    assert base._rerank_hits("fallback", hits, top_k=2) == HybridRetriever.rerank_hits(
        "fallback", hits, top_k=2
    )


def test_fastembed_reranker_is_lazy_and_does_not_load_on_construction(tmp_path) -> None:
    reranker = FastEmbedReranker("Xenova/ms-marco-MiniLM-L-6-v2", tmp_path)

    assert reranker._model is None
    assert reranker.error == ""
