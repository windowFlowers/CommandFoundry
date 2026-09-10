from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from app.models import Answer


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
        assert client.get("/health").json() == {"status": "ok", "version": "2.2.0"}
        payload = client.get("/knowledge/status").json()
    assert payload["ready"] is True
    assert payload["topic_count"] == 300
    assert payload["domains"] == {
        "cicd": 15,
        "docker": 25,
        "git": 30,
        "http": 20,
        "java": 20,
        "kubernetes": 20,
        "linux": 30,
        "network": 20,
        "node": 20,
        "python": 25,
        "redis": 15,
        "sql": 30,
        "testing": 10,
        "windows": 20,
    }
    assert payload["retrieval_mode"] == "bm25"


def test_chat_stream_uses_context_event_contract_and_persists() -> None:
    with TestClient(app) as client:
        events = _events(client.post("/chat/stream", json={"query": "Git 怎么安全回滚一次提交？"}).text)
        assert [name for name, _ in events] == ["status", "status", "status", "answer", "done"]
        assert events[0][1]["stage"] == "contextualizing"
        answer_event = events[3][1]
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
        missing = client.post("/chat/stream", json={"query": "docker logs", "conversation_id": "missing"})
        assert missing.status_code == 404
        assert client.delete(f"/conversations/{created['id']}").status_code == 204
        assert client.get(f"/conversations/{created['id']}").status_code == 404


def test_no_hit_returns_deterministic_empty_answer() -> None:
    with TestClient(app) as client:
        events = _events(client.post("/chat/stream", json={"query": "量子奶茶星云配方"}).text)
    answer = next(data["answer"] for event, data in events if event == "answer")
    assert answer["mode"] == "local_fallback"
    assert answer["commands"] == []
    assert answer["citations"] == []


def test_memory_api_exposes_used_history_and_reset_keeps_messages() -> None:
    with TestClient(app) as client:
        first = _events(client.post("/chat/stream", json={"query": "Linux 怎么查看端口占用？"}).text)
        conversation_id = next(data["conversation_id"] for event, data in first if event == "done")
        second = _events(
            client.post(
                "/chat/stream",
                json={"query": "那 Windows 呢？", "conversation_id": conversation_id},
            ).text
        )
        answer = next(data["answer"] for event, data in second if event == "answer")
        assert answer["context"]["used"] is True
        assert answer["context"]["recent_turn_count"] == 1
        assert answer["context"]["retrieval_query"].startswith("Windows 怎么查看端口占用")

        memory = client.get(f"/conversations/{conversation_id}/memory").json()
        assert len(memory["recent_messages"]) == 2
        assert client.post(f"/conversations/{conversation_id}/memory/reset").status_code == 204
        cleared = client.get(f"/conversations/{conversation_id}/memory").json()
        conversation = client.get(f"/conversations/{conversation_id}").json()

    assert cleared["recent_messages"] == []
    assert cleared["summary"]["current_goal"] == ""
    assert len(conversation["messages"]) == 4


def test_old_answers_without_context_remain_compatible() -> None:
    answer = Answer.model_validate(
        {
            "mode": "local_fallback",
            "summary": "旧回答",
            "commands": [],
            "notes": [],
            "citations": [],
        }
    )
    assert answer.context.used is False
    assert answer.context.strategy == "none"
