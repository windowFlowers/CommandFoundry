from __future__ import annotations

import sqlite3
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.answering import AnswerService
from app.documents import DocumentIndexService
from app.evidence import citation_id
from app.generation import ModelAnswerDraft
from app.models import (
    Answer,
    AnswerSegment,
    CommandBlock,
    DEFAULT_KNOWLEDGE_BASE_ID,
    KnowledgeTopic,
    MatchedChunk,
    RetrievalHit,
    SourceLocator,
    SourceMetadata,
)
from app.repository import ConversationRepository, DATABASE_SCHEMA_VERSION
from app.retrieval import KnowledgeIndexManager


def _locator(*, start: int = 5, end: int = 15) -> dict:
    return {
        "kind": "text",
        "page_start": None,
        "page_end": None,
        "heading_path": [],
        "line_start": 2,
        "line_end": 2,
        "paragraph_start": None,
        "paragraph_end": None,
        "char_start": start,
        "char_end": end,
    }


def _create_document(repository: ConversationRepository, tmp_path: Path):
    return repository.create_document(
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        filename="guide.txt",
        media_type="text/plain",
        size_bytes=64,
        sha256="a" * 64,
        stored_path=str(tmp_path / "guide.txt"),
    )


def _parent(parent_id: str, *, index: int, text: str = "parent context") -> dict:
    return {
        "id": parent_id,
        "stable_id": f"stable-{parent_id}",
        "index": index,
        "text": text,
        "locator": _locator(start=0, end=len(text)),
        "content_hash": f"hash-{parent_id}",
    }


def _child(
    child_id: str,
    *,
    parent_id: str,
    index: int,
    stable_id: str | None = None,
    text: str = "target text",
    command_evidence: list[str] | None = None,
) -> dict:
    return {
        "id": child_id,
        "stable_id": stable_id or f"stable-{child_id}",
        "parent_id": parent_id,
        "index": index,
        "text": text,
        "retrieval_text": text,
        "locator": _locator(),
        "content_hash": f"hash-{child_id}",
        "command_evidence": command_evidence or [],
        "tokens_json": '["target", "text"]',
        "embedding": None,
        "embedding_dimension": None,
        "embedding_version": "",
    }


def _replace_index(
    repository: ConversationRepository,
    *,
    document_id: str,
    content: str,
    revision: str,
    parents: list[dict],
    children: list[dict],
) -> None:
    repository.replace_document_index(
        document_id=document_id,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        content=content,
        index_revision=revision,
        parents=parents,
        children=children,
    )


def test_v22_answer_and_citation_payloads_remain_compatible() -> None:
    answer = Answer.model_validate(
        {
            "mode": "local_fallback",
            "summary": "legacy answer",
            "commands": [{"label": "status", "code": "git status"}],
            "citations": [
                {
                    "source_id": "git.status",
                    "title": "Git status",
                    "license": "CC BY 4.0",
                    "revision": "legacy",
                    "excerpt": "Show working tree status.",
                    "score": 1.0,
                    "domain": "git",
                }
            ],
        }
    )

    assert answer.summary_segments == []
    assert answer.commands[0].citation_ids == []
    assert answer.citations[0].citation_id is None
    assert answer.citations[0].chunk_id is None
    assert answer.citations[0].parent_id is None
    assert answer.citations[0].locator is None
    assert answer.citations[0].document_sha256 == ""
    assert answer.citations[0].index_revision == ""


def test_migration_sets_user_version_and_creates_one_consistent_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE legacy_sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_sentinel(value) VALUES ('before-v2.3')")
        connection.execute("PRAGMA user_version = 1")

    ConversationRepository(database_path)
    backup_path = tmp_path / "legacy.sqlite3.pre-v2.3.backup"

    assert backup_path.exists()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT value FROM legacy_sentinel").fetchone()[0] == "before-v2.3"

    # Initializing again must not replace the pre-migration snapshot.
    ConversationRepository(database_path)
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_replace_document_index_rolls_back_the_entire_old_index_on_insert_failure(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "index.sqlite3")
    document = _create_document(repository, tmp_path)
    _replace_index(
        repository,
        document_id=document.id,
        content="old content",
        revision="revision-old",
        parents=[_parent("parent-old", index=0)],
        children=[_child("child-old", parent_id="parent-old", index=0)],
    )

    duplicate = _child("child-new", parent_id="parent-new", index=0)
    with pytest.raises(sqlite3.IntegrityError):
        _replace_index(
            repository,
            document_id=document.id,
            content="new content",
            revision="revision-new",
            parents=[_parent("parent-new", index=0)],
            children=[duplicate, {**duplicate, "index": 1}],
        )

    active = repository.get_document(document.id)
    preview = repository.get_document_content(document.id, "child-old")
    assert active is not None
    assert active.index_revision == "revision-old"
    assert active.parent_count == 1
    assert active.chunk_count == 1
    assert [row["id"] for row in repository.list_chunks(DEFAULT_KNOWLEDGE_BASE_ID)] == ["child-old"]
    assert preview is not None
    assert preview.content == "old content"
    assert preview.chunk_id == "child-old"


def test_repository_resolves_chunk_id_and_stable_id_to_exact_highlight(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "content.sqlite3")
    document = _create_document(repository, tmp_path)
    content = "zero\nTARGET-TEN\nend"
    _replace_index(
        repository,
        document_id=document.id,
        content=content,
        revision="revision-1",
        parents=[_parent("parent-1", index=0, text=content)],
        children=[
            _child(
                "child-1",
                stable_id="stable-child-1",
                parent_id="parent-1",
                index=0,
                text="TARGET-TEN",
            )
        ],
    )

    for requested_id in ("child-1", "stable-child-1"):
        result = repository.get_document_content(document.id, requested_id)
        assert result is not None
        assert result.chunk_id == "child-1"
        assert result.locator == SourceLocator.model_validate(_locator())
        assert result.highlight_start == 5
        assert result.highlight_end == 15
        assert result.content[result.highlight_start : result.highlight_end] == "TARGET-TEN"
        assert result.document_sha256 == "a" * 64
        assert result.index_revision == "revision-1"
        assert result.version_changed is False


def test_content_endpoint_forwards_optional_chunk_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "api-content.sqlite3")
    document = _create_document(repository, tmp_path)
    _replace_index(
        repository,
        document_id=document.id,
        content="zero\nTARGET-TEN\nend",
        revision="revision-api",
        parents=[_parent("parent-api", index=0)],
        children=[_child("child-api", parent_id="parent-api", index=0, text="TARGET-TEN")],
    )
    monkeypatch.setattr(main_module, "repository", repository)

    with TestClient(main_module.app) as client:
        response = client.get(
            f"/knowledge-documents/{document.id}/content",
            params={"chunk_id": "child-api"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["chunk_id"] == "child-api"
    assert payload["highlight_start"] == 5
    assert payload["highlight_end"] == 15
    assert payload["locator"]["line_start"] == 2


class _RawRetriever:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self.hits = hits
        self.requested_top_k: list[int] = []

    def retrieve(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        self.requested_top_k.append(top_k)
        return self.hits


def _upload_hit(
    *,
    document_id: str,
    parent_id: str,
    chunk_id: str,
    score: float,
    command_evidence: list[str] | None = None,
) -> RetrievalHit:
    locator = SourceLocator.model_validate(_locator())
    source = SourceMetadata(
        source_id=f"upload.{document_id}",
        title=f"{document_id}.txt",
        kind="upload",
        document_id=document_id,
        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
        chunk_id=chunk_id,
        parent_id=parent_id,
        index_revision="revision-1",
        sha256="b" * 64,
        locator=locator,
        command_evidence=command_evidence or [],
    )
    topic = KnowledgeTopic(
        id=f"upload.{chunk_id}",
        domain="uploaded",
        title=source.title,
        summary=f"child excerpt {chunk_id}",
        parent_context=f"parent context {parent_id}",
        source=source,
    )
    return RetrievalHit(topic=topic, score=score, bm25_score=score)


def test_parent_aggregation_uses_twenty_candidates_and_limits_each_document_to_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _RawRetriever(
        [
            _upload_hit(document_id="doc-a", parent_id="p1", chunk_id="c1", score=0.90),
            _upload_hit(document_id="doc-a", parent_id="p1", chunk_id="c2", score=0.89),
            _upload_hit(document_id="doc-a", parent_id="p2", chunk_id="c3", score=0.80),
            _upload_hit(document_id="doc-a", parent_id="p3", chunk_id="c4", score=0.70),
            _upload_hit(document_id="doc-b", parent_id="p4", chunk_id="c5", score=0.60),
        ]
    )
    manager = KnowledgeIndexManager(
        catalog=object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        embedding_engine=object(),  # type: ignore[arg-type]
        candidate_k=20,
    )
    monkeypatch.setattr(manager, "get", lambda _: raw)

    results = manager.retrieve(DEFAULT_KNOWLEDGE_BASE_ID, "target", top_k=4)

    assert raw.requested_top_k == [20]
    assert [hit.topic.source.parent_id for hit in results] == ["p1", "p2", "p4"]
    assert sum(hit.topic.source.document_id == "doc-a" for hit in results) == 2
    first = results[0]
    assert first.score == pytest.approx(0.900001)
    assert [item.chunk_id for item in first.matched_chunks] == ["c1", "c2"]


def test_unknown_citations_are_removed_and_upload_commands_require_exact_command_evidence() -> None:
    hit = _upload_hit(
        document_id="doc-command",
        parent_id="parent-command",
        chunk_id="chunk-command",
        score=0.9,
        command_evidence=["npm ci"],
    )
    hit = hit.model_copy(
        update={
            "matched_chunks": [
                MatchedChunk(
                    chunk_id="chunk-command",
                    parent_id="parent-command",
                    text="The prose also mentions npm run deploy, but only npm ci is code evidence.",
                    score=0.9,
                    locator=hit.topic.source.locator,
                    command_evidence=["npm ci"],
                )
            ]
        }
    )
    valid_id = citation_id(hit.topic.source.source_id, "chunk-command")

    class Generator:
        available = True
        model = "deepseek-chat"

        def generate(self, query, hits):
            return ModelAnswerDraft(
                summary="Grounded fact. Forged fact.",
                summary_segments=[
                    AnswerSegment(
                        text="Grounded fact.",
                        citation_ids=[valid_id, "cit-forged", valid_id],
                    ),
                    AnswerSegment(text="Forged fact.", citation_ids=["cit-forged"]),
                ],
                commands=[
                    CommandBlock(label="exact", code="npm ci", citation_ids=["cit-forged"]),
                    CommandBlock(label="spacing differs", code="npm  ci"),
                    CommandBlock(label="prose only", code="npm run deploy"),
                ],
            )

    answer, reason = AnswerService(Generator()).answer("install", [hit])

    assert reason == "model"
    assert answer.summary == "Grounded fact."
    assert answer.summary_segments == [AnswerSegment(text="Grounded fact.", citation_ids=[valid_id])]
    assert [command.code for command in answer.commands] == ["npm ci"]
    assert answer.commands[0].citation_ids == [valid_id]
    assert [citation.citation_id for citation in answer.citations] == [valid_id]


def test_uploaded_document_chat_returns_parent_context_with_precise_command_citation() -> None:
    source = b"""# Kubernetes running pods\n\nUse the field selector for the current namespace.\n\n```bash\nkubectl get pods --field-selector=status.phase=Running\n```\n"""
    with TestClient(main_module.app) as client:
        knowledge_base = client.post("/knowledge-bases", json={"name": "v2.3 precise citation"}).json()
        uploaded = client.post(
            f"/knowledge-bases/{knowledge_base['id']}/documents",
            files={"file": ("pods.md", source, "text/markdown")},
        )
        assert uploaded.status_code == 202
        document_id = uploaded.json()["id"]
        for _ in range(100):
            document = client.get(f"/knowledge-documents/{document_id}").json()
            if document["status"] in {"ready", "failed"}:
                break
            time.sleep(0.02)
        assert document["status"] == "ready"
        response = client.post(
            "/chat/stream",
            json={"query": "show running Kubernetes pods with a field selector", "knowledge_base_id": knowledge_base["id"]},
        )
        answer_payload = None
        for frame in response.text.split("\n\n"):
            if "event: answer" not in frame:
                continue
            data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
            answer_payload = json.loads(data_line[6:])["answer"]
        assert answer_payload is not None
        assert answer_payload["commands"], answer_payload
        assert answer_payload["commands"][0]["code"] == "kubectl get pods --field-selector=status.phase=Running"
        citation_ids = {item["citation_id"] for item in answer_payload["citations"]}
        assert answer_payload["commands"][0]["citation_ids"][0] in citation_ids
        assert answer_payload["summary_segments"][0]["citation_ids"][0] in citation_ids
        citation = answer_payload["citations"][0]
        assert citation["document_sha256"]
        assert citation["index_revision"]
        assert citation["locator"]["heading_path"] == ["Kubernetes running pods"]
        assert client.delete(f"/knowledge-bases/{knowledge_base['id']}").status_code == 204


def test_restart_requeues_interrupted_rebuild_while_preserving_active_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "rebuild-recovery.sqlite3"
    repository = ConversationRepository(database_path)
    document = _create_document(repository, tmp_path)
    _replace_index(
        repository,
        document_id=document.id,
        content="old content remains available",
        revision="revision-old",
        parents=[_parent("parent-old", index=0)],
        children=[_child("child-old", parent_id="parent-old", index=0)],
    )

    repository.request_document_rebuild(document.id)
    repository.mark_document_rebuilding(document.id)
    interrupted = repository.get_document(document.id)
    assert interrupted is not None
    assert interrupted.status == "ready"
    assert interrupted.rebuild_status == "indexing"
    assert [row["id"] for row in repository.list_chunks(interrupted.knowledge_base_id)] == ["child-old"]

    restarted = ConversationRepository(database_path)
    recovered = restarted.get_document(document.id)

    assert recovered is not None
    assert recovered.status == "ready"
    assert recovered.rebuild_status == "pending"
    assert recovered.rebuild_error == ""
    assert restarted.list_pending_document_ids() == [document.id]
    # The worker can safely re-index from this queue without making the old
    # index unavailable in the meantime.
    assert [row["id"] for row in restarted.list_chunks(recovered.knowledge_base_id)] == ["child-old"]

    # DocumentIndexService reads that recovered pending state during startup.
    # Keep its worker inert so the test can observe the enqueue itself rather
    # than racing an actual extraction job.
    monkeypatch.setattr(DocumentIndexService, "_worker_loop", lambda self: None)
    service = DocumentIndexService(
        repository=restarted,
        indexes=object(),  # type: ignore[arg-type]
        embeddings=object(),  # type: ignore[arg-type]
        upload_dir=tmp_path / "uploads",
    )
    assert document.id in service._queued


def test_uncitable_model_summary_uses_deterministic_grounded_fallback() -> None:
    raw_hit = _upload_hit(
        document_id="doc-uncitable",
        parent_id="parent-uncitable",
        chunk_id="chunk-uncitable",
        score=0.9,
    )
    hit = raw_hit.model_copy(
        update={
            "matched_chunks": [
                MatchedChunk(
                    chunk_id="chunk-uncitable",
                    parent_id="parent-uncitable",
                    text="The exact, locally indexed evidence.",
                    score=0.9,
                    locator=SourceLocator.model_validate(_locator()),
                )
            ]
        }
    )
    valid_citation_id = citation_id(hit.topic.source.source_id, "chunk-uncitable")

    class Generator:
        available = True
        model = "deepseek-chat"

        def generate(self, query: str, hits: list[RetrievalHit]) -> ModelAnswerDraft:
            return ModelAnswerDraft(
                summary="This model claim has no valid source binding.",
                summary_segments=[
                    AnswerSegment(
                        text="This model claim has no valid source binding.",
                        citation_ids=["cit-forged"],
                    )
                ],
            )

    answer, reason = AnswerService(Generator()).answer("explain", [hit])

    assert reason == "ungrounded"
    assert answer.mode == "local_fallback"
    assert answer.generation.attempted is True
    assert answer.generation.fallback_reason == "ungrounded"
    assert answer.summary != "This model claim has no valid source binding."
    assert answer.summary_segments == [
        AnswerSegment(text=answer.summary, citation_ids=[valid_citation_id])
    ]
