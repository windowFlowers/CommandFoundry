from __future__ import annotations

from app.main import catalog, retriever
from app.models import RetrievalHit
from app.retrieval import (
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


def test_retrieval_confidence_is_a_conservative_no_answer_signal() -> None:
    assert retrieval_confidence([]) == 0.0
    assert has_sufficient_evidence([]) is False

    hit = RetrievalHit(topic=catalog.topics[0], score=0.02, bm25_score=0.0, vector_score=0.1)
    assert retrieval_confidence([hit]) == 0.0
    assert has_sufficient_evidence([hit]) is False

    grounded = hit.model_copy(update={"bm25_score": 2.0})
    assert retrieval_confidence([grounded]) > 0.05
    assert has_sufficient_evidence([grounded]) is True
