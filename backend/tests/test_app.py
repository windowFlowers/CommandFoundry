from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app


def _events(raw: str) -> list[tuple[str, dict]]:
    parsed = []
    for frame in raw.strip().split("\n\n"):
        lines = frame.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        parsed.append((event, data))
    return parsed


def test_health_and_knowledge_status() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "version": "2.0.0"}
        payload = client.get("/knowledge/status").json()
    assert payload["ready"] is True
    assert payload["topic_count"] == 100
    assert payload["domains"] == {
        "docker": 15,
        "git": 20,
        "http": 10,
        "linux": 20,
        "sql": 15,
        "toolchain": 20,
    }
    assert payload["retrieval_mode"] == "bm25"


def test_chat_stream_uses_four_event_contract_and_persists() -> None:
    with TestClient(app) as client:
        events = _events(client.post("/chat/stream", json={"query": "Git 怎么安全回滚一次提交？"}).text)
        assert [name for name, _ in events] == ["status", "status", "answer", "done"]
        answer_event = events[2][1]
        answer = answer_event["answer"]
        assert answer["mode"] == "local_fallback"
        assert "git revert" in "\n".join(item["code"] for item in answer["commands"])
        assert answer["citations"][0]["source_id"] == "git.git-revert"
        conversation = client.get(f"/conversations/{answer_event['conversation_id']}").json()
    assert [message["role"] for message in conversation["messages"]] == ["user", "assistant"]


def test_conversation_crud_and_missing_stream_target() -> None:
    with TestClient(app) as client:
        created = client.post("/conversations", json={"title": "Docker 排查"}).json()
        assert any(item["id"] == created["id"] for item in client.get("/conversations").json()["items"])
        events = _events(client.post("/chat/stream", json={"query": "docker logs", "conversation_id": "missing"}).text)
        assert events == [("error", {"message": "会话不存在"})]
        assert client.delete(f"/conversations/{created['id']}").status_code == 204
        assert client.get(f"/conversations/{created['id']}").status_code == 404


def test_no_hit_returns_deterministic_empty_answer() -> None:
    with TestClient(app) as client:
        events = _events(client.post("/chat/stream", json={"query": "量子奶茶星云配方"}).text)
    answer = next(data["answer"] for event, data in events if event == "answer")
    assert answer["mode"] == "local_fallback"
    assert answer["commands"] == []
    assert answer["citations"] == []
