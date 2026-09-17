from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from app import main as main_module
from app.main import _is_trusted_mutation_origin, app
from app.models import Answer
from app.repository import ConversationRepository


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
        assert client.get("/health").json() == {"status": "ok", "version": "2.5.0"}
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


def test_untrusted_browser_origin_cannot_mutate_loopback_state() -> None:
    memory_id = None
    conversation_id = None
    with TestClient(app) as client:
        created = client.post(
            "/profile/memories",
            json={
                "scope": "global",
                "knowledge_base_id": None,
                "category": "response_style",
                "key": f"csrf-test-{uuid4().hex}",
                "value": "concise",
                "display_text": "回答偏好：简洁",
                "pinned": False,
            },
        )
        assert created.status_code == 201
        memory_id = created.json()["id"]

        blocked = client.post(
            "/profile/memories/reset",
            content="",
            headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
        )
        assert blocked.status_code == 403
        remaining = client.get(f"/profile/memories?ids={memory_id}").json()["items"]
        assert [item["id"] for item in remaining] == [memory_id]

        allowed = client.post(
            "/conversations",
            json={"title": "可信桌面来源"},
            headers={"Origin": "aegis://app/localhost"},
        )
        assert allowed.status_code == 201
        conversation_id = allowed.json()["id"]

        assert client.delete(f"/profile/memories/{memory_id}").status_code == 204
        assert client.delete(f"/conversations/{conversation_id}").status_code == 204


def test_mutation_origin_validation_rejects_lookalike_hosts() -> None:
    assert _is_trusted_mutation_origin("aegis://app")
    assert _is_trusted_mutation_origin("aegis://app/localhost")
    assert _is_trusted_mutation_origin("http://127.0.0.1:5173")
    assert _is_trusted_mutation_origin("https://localhost:5173")
    assert not _is_trusted_mutation_origin("https://127.0.0.1.evil.example")
    assert not _is_trusted_mutation_origin("https://localhost.evil.example")
    assert not _is_trusted_mutation_origin("null")


def test_memory_extraction_status_endpoint_is_terminal_safe_and_undoable(
    monkeypatch, tmp_path
) -> None:
    repository = ConversationRepository(tmp_path / "profile-task-status.sqlite3")
    conversation = repository.create()
    message = repository.append_user(conversation.id, "请记住部署时优先使用可回滚方案")
    task = repository.create_memory_extraction_task(
        conversation.id,
        message.id,
        conversation.knowledge_base_id,
        "请记住部署时优先使用可回滚方案",
    )
    assert task is not None
    claimed = repository.claim_memory_extraction_task(task["id"])
    assert claimed is not None
    saved = repository.complete_memory_extraction_task(
        task["id"],
        [
            {
                "action": "ADD",
                "scope": "knowledge_base",
                "category": "project_constraint",
                "key": "deployment_policy",
                "value": "优先使用可回滚方案",
                "display_text": "部署优先使用可回滚方案",
                "confidence": 0.98,
            }
        ],
        expected_epoch=claimed["extraction_epoch"],
        expected_attempt=claimed["attempt_count"],
    )
    assert len(saved) == 1
    monkeypatch.setattr(main_module, "repository", repository)

    with TestClient(app) as client:
        response = client.get(f"/profile/memory-extractions/{task['id']}")
        missing = client.get("/profile/memory-extractions/not-a-real-task")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == task["id"]
    assert payload["status"] == "ready"
    assert payload["source_message_id"] == message.id
    assert payload["memory_ids"] == [saved[0].id]
    assert payload["undoable_memory_ids"] == [saved[0].id]
    assert payload["operation"] == "ADD"
    assert "sanitized_input" not in payload
    assert "last_error" not in payload
    assert missing.status_code == 404


def test_memory_extraction_status_endpoint_reports_update_noop_and_failure(
    monkeypatch, tmp_path
) -> None:
    repository = ConversationRepository(tmp_path / "profile-task-outcomes.sqlite3")
    conversation = repository.create()
    existing = repository.upsert_user_memory(
        "knowledge_base",
        conversation.knowledge_base_id,
        "platform",
        "operating_system",
        "Windows",
        "项目运行在 Windows",
        0.95,
    )

    update_message = repository.append_user(conversation.id, "项目仍然使用 Windows")
    update_task = repository.create_memory_extraction_task(
        conversation.id,
        update_message.id,
        conversation.knowledge_base_id,
        "项目仍然使用 Windows",
    )
    assert update_task is not None
    claimed_update = repository.claim_memory_extraction_task(update_task["id"])
    assert claimed_update is not None
    repository.complete_memory_extraction_task(
        update_task["id"],
        [
            {
                "action": "UPDATE",
                "scope": "knowledge_base",
                "category": "platform",
                "key": "operating_system",
                "value": "Windows",
                "display_text": "项目运行在 Windows",
                "confidence": 0.96,
            }
        ],
        expected_epoch=claimed_update["extraction_epoch"],
        expected_attempt=claimed_update["attempt_count"],
    )

    supersede_message = repository.append_user(conversation.id, "项目已经改用 Linux")
    supersede_task = repository.create_memory_extraction_task(
        conversation.id,
        supersede_message.id,
        conversation.knowledge_base_id,
        "项目已经改用 Linux",
    )
    assert supersede_task is not None
    claimed_supersede = repository.claim_memory_extraction_task(supersede_task["id"])
    assert claimed_supersede is not None
    replacement = repository.complete_memory_extraction_task(
        supersede_task["id"],
        [
            {
                "action": "SUPERSEDE",
                "scope": "knowledge_base",
                "category": "platform",
                "key": "operating_system",
                "value": "Linux",
                "display_text": "项目运行在 Linux",
                "confidence": 0.97,
            }
        ],
        expected_epoch=claimed_supersede["extraction_epoch"],
        expected_attempt=claimed_supersede["attempt_count"],
    )
    assert len(replacement) == 1

    noop_message = repository.append_user(conversation.id, "这句没有长期偏好")
    noop_task = repository.create_memory_extraction_task(
        conversation.id,
        noop_message.id,
        conversation.knowledge_base_id,
        "这句没有长期偏好",
    )
    assert noop_task is not None
    claimed_noop = repository.claim_memory_extraction_task(noop_task["id"])
    assert claimed_noop is not None
    repository.complete_memory_extraction_task(
        noop_task["id"],
        [{"action": "NOOP"}],
        expected_epoch=claimed_noop["extraction_epoch"],
        expected_attempt=claimed_noop["attempt_count"],
    )

    failed_message = repository.append_user(conversation.id, "这项提取会失败")
    failed_task = repository.create_memory_extraction_task(
        conversation.id,
        failed_message.id,
        conversation.knowledge_base_id,
        "这项提取会失败",
    )
    assert failed_task is not None
    claimed_failed = repository.claim_memory_extraction_task(failed_task["id"])
    assert claimed_failed is not None
    assert repository.fail_memory_extraction_task(
        failed_task["id"],
        "ProviderPrivateError: do not expose this",
        expected_epoch=claimed_failed["extraction_epoch"],
        expected_attempt=claimed_failed["attempt_count"],
    )
    monkeypatch.setattr(main_module, "repository", repository)

    with TestClient(app) as client:
        update_payload = client.get(f"/profile/memory-extractions/{update_task['id']}").json()
        supersede_payload = client.get(
            f"/profile/memory-extractions/{supersede_task['id']}"
        ).json()
        noop_payload = client.get(f"/profile/memory-extractions/{noop_task['id']}").json()
        failed_payload = client.get(f"/profile/memory-extractions/{failed_task['id']}").json()

    assert update_payload["operation"] == "UPDATE"
    assert update_payload["memory_ids"] == [existing.id]
    assert update_payload["undoable_memory_ids"] == []
    assert supersede_payload["operation"] == "SUPERSEDE"
    assert supersede_payload["memory_ids"] == [replacement[0].id]
    assert supersede_payload["undoable_memory_ids"] == []
    assert noop_payload["status"] == "ready"
    assert noop_payload["operation"] == "NOOP"
    assert noop_payload["memory_ids"] == []
    assert failed_payload["status"] == "failed"
    assert failed_payload["operation"] == "NOOP"
    assert failed_payload["memory_ids"] == []
    assert "ProviderPrivateError" not in json.dumps(failed_payload)
