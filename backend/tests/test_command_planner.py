from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.command_planner import (
    contains_sensitive_input,
    detect_shell,
    extract_path,
    infer_platform,
    parse_slot_value,
    redact_sensitive_input,
    render_virtualenv,
)
from app.main import app
from app.repository import ConversationRepository


def _events(raw: str) -> list[tuple[str, dict]]:
    result = []
    for frame in raw.strip().split("\n\n"):
        lines = frame.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        result.append((event, json.loads(next(line[6:] for line in lines if line.startswith("data: ")))))
    return result


def _answer(response) -> tuple[str, dict]:
    events = _events(response.text)
    return next(data["conversation_id"] for event, data in events if event == "done"), next(
        data["answer"] for event, data in events if event == "answer"
    )


def test_path_platform_and_shell_parsing() -> None:
    path = extract_path(r"F:\SOT tracker improvment\outputs\reports，我要创建环境")
    assert path == r"F:\SOT tracker improvment\outputs\reports"
    assert infer_platform(path) == "windows"
    assert infer_platform("/srv/project") == "posix"
    assert infer_platform("PowerShell") == "windows"
    assert extract_path("参考 https://example.com/docs/venv") is None
    assert parse_slot_value("path", "https://example.com/docs/venv") is None
    assert detect_shell("PowerShell") == "powershell"
    assert detect_shell("命令提示符") == "cmd"
    assert detect_shell("相对路径") is None
    assert parse_slot_value("port", "Windows 11 查看端口 8080") == "8080"
    assert parse_slot_value("port", "Windows 11 查看端口占用") is None
    assert parse_slot_value("port", "8080 端口") == "8080"
    assert parse_slot_value("commit", "git revert previous commit safely") is None
    assert parse_slot_value("commit", "git revert refs/heads/release") == "refs/heads/release"
    assert parse_slot_value("image", "docker run nginx:latest") == "nginx:latest"
    assert parse_slot_value("container_name", "docker run --name web nginx:latest") == "web"
    # Unknown Docker options must not shift their value into the image slot.
    assert parse_slot_value("image", "docker run --unknown value nginx:latest") is None
    assert parse_slot_value("image", "Docker 镜像 nginx:latest") == "nginx:latest"
    assert parse_slot_value("image", "nginx:latest 镜像") == "nginx:latest"
    assert parse_slot_value("image", "我想运行 https://registry.example/nginx") is None
    assert parse_slot_value("path", "Python create and activate virtualenv") is None
    assert parse_slot_value("path", "./reports") == "./reports"
    assert parse_slot_value("path", "reports") is None


def test_catalog_slots_extract_the_value_after_the_matching_marker() -> None:
    """Natural-language facts must not all collapse to the last token."""

    from app.command_planner import SlotSpec, _parse_recipe_slot

    query = "MySQL 主机 db 用户 root 数据库 app"
    assert _parse_recipe_slot(SlotSpec("host", "text", "", slot_type="identifier"), query) == "db"
    assert _parse_recipe_slot(SlotSpec("user", "text", "", slot_type="identifier"), query) == "root"
    assert _parse_recipe_slot(SlotSpec("database", "text", "", slot_type="identifier"), query) == "app"
    seconds = SlotSpec("seconds", "text", "", slot_type="value", validation="positive_integer")
    assert _parse_recipe_slot(seconds, "过期秒数 60 秒") == "60"
    assert _parse_recipe_slot(seconds, "不是数字", allow_bare=True) is None
    assert parse_slot_value("environment_name", "在这个目录创建 .venv") == ".venv"
    assert _parse_recipe_slot(SlotSpec("target_path", "path", ""), "目标目录 reports") == "reports"


def test_sensitive_connection_string_is_rejected_from_recipe_values() -> None:
    from app.command_planner import SlotSpec, _parse_recipe_slot

    spec = SlotSpec("host", "text", "", slot_type="identifier")
    assert _parse_recipe_slot(spec, "mysql://user:secret@db.example", allow_bare=True) is None


def test_virtualenv_renderer_quotes_windows_path() -> None:
    rendered = render_virtualenv(r"F:\SOT tracker improvment\reports", ".venv", "powershell")
    assert rendered is not None
    assert rendered.shell == "powershell"
    assert rendered.code == (
        "python -m venv 'F:\\SOT tracker improvment\\reports\\.venv'\n"
        "& 'F:\\SOT tracker improvment\\reports\\.venv\\Scripts\\Activate.ps1'"
    )
    assert "<" not in rendered.code and ">" not in rendered.code


def test_virtualenv_renderer_handles_cmd_and_posix_special_characters() -> None:
    cmd = render_virtualenv(r"C:\项目\reports", ".venv", "cmd")
    assert cmd is not None
    assert cmd.shell == "cmd"
    assert cmd.language == "text"
    assert "python -m venv C:\\项目\\reports\\.venv" in cmd.code
    assert "activate.bat" in cmd.code

    posix = render_virtualenv("/tmp/O'Reilly project", ".venv", "posix")
    assert posix is not None
    assert posix.shell == "posix"
    assert "O'" in posix.code and "Reilly project" in posix.code
    assert "<" not in posix.code and ">" not in posix.code

    # cmd.exe metacharacters cannot be made safe by ordinary quoting, so the
    # planner must refuse a direct variant and let the source template win.
    assert render_virtualenv(r"C:\work&drop", ".venv", "cmd") is None
    assert render_virtualenv(r"C:\work!drop", ".venv", "cmd") is None
    relative = render_virtualenv("reports", ".venv", "powershell")
    assert relative is not None
    assert "reports\\.venv" in relative.code


def test_sensitive_values_are_never_forwarded_or_persisted() -> None:
    text = "连接服务 password=super-secret-token"
    assert contains_sensitive_input(text)
    assert "super-secret-token" not in redact_sensitive_input(text)
    assert "password" in redact_sensitive_input(text)
    assert contains_sensitive_input("eyJheaderxx.payloadxx.signaturexx")
    assert contains_sensitive_input("-----BEGIN PRIVATE KEY-----")


def test_virtualenv_stream_asks_one_slot_at_a_time_then_renders_direct_command() -> None:
    with TestClient(app) as client:
        conversation_id, first = _answer(
            client.post("/chat/stream", json={"query": "Python 怎么创建并激活虚拟环境？"})
        )
        assert first["answer_kind"] == "clarification"
        assert first["clarification"]["slot"] == "target_path"
        assert first["commands"] == []

        conversation_id, second = _answer(
            client.post(
                "/chat/stream",
                json={
                    "query": r"F:\SOT tracker improvment\outputs\anti_uav_v0\reports",
                    "conversation_id": conversation_id,
                },
            )
        )
        assert second["answer_kind"] == "clarification"
        assert second["clarification"]["slot"] == "shell"

        conversation_id, third = _answer(
            client.post(
                "/chat/stream",
                json={"query": "PowerShell", "conversation_id": conversation_id},
            )
        )
        assert third["clarification"]["slot"] == "environment_name"
        assert third["clarification"]["options"][0]["value"] == ".venv"

        _, final = _answer(
            client.post(
                "/chat/stream",
                json={"query": ".venv", "conversation_id": conversation_id},
            )
        )
        assert final["answer_kind"] == "direct"
        assert len(final["commands"]) == 1
        assert final["commands"][0]["shell"] == "powershell"
        assert "F:\\SOT tracker improvment\\outputs\\anti_uav_v0\\reports\\.venv" in final["commands"][0]["code"]
        assert "<" not in final["commands"][0]["code"] and ">" not in final["commands"][0]["code"]
        assert final["commands"][0]["citation_ids"]
        assert final["command_plan"]["citation_ids"]


def test_contextual_followup_after_direct_command_does_not_reopen_recipe_plan() -> None:
    with TestClient(app) as client:
        conversation_id, _ = _answer(
            client.post("/chat/stream", json={"query": "Python 怎么创建并激活虚拟环境？"})
        )
        for query in (r"C:\workspace", "PowerShell", ".venv"):
            _answer(
                client.post(
                    "/chat/stream",
                    json={"query": query, "conversation_id": conversation_id},
                )
            )
        _, followup = _answer(
            client.post(
                "/chat/stream",
                json={"query": "这个命令安全吗？", "conversation_id": conversation_id},
            )
        )
    assert (followup.get("clarification") or {}).get("slot") != "target_path"


def test_invalid_slot_falls_back_to_one_template() -> None:
    with TestClient(app) as client:
        conversation_id, first = _answer(
            client.post("/chat/stream", json={"query": "Python 创建虚拟环境"})
        )
        assert first["clarification"]["slot"] == "target_path"
        _, fallback = _answer(
            client.post(
                "/chat/stream",
                json={"query": "不知道", "conversation_id": conversation_id},
            )
        )
    assert fallback["answer_kind"] == "template"
    assert len(fallback["commands"]) == 1


def test_windows_path_only_offers_windows_shells_and_template_request_closes_plan() -> None:
    with TestClient(app) as client:
        conversation_id, first = _answer(
            client.post(
                "/chat/stream",
                json={"query": r"F:\workspace，我要创建并激活 Python 虚拟环境"},
            )
        )
        assert first["answer_kind"] == "clarification"
        assert first["clarification"]["slot"] == "shell"
        assert {item["value"] for item in first["clarification"]["options"]} == {"powershell", "cmd"}

        _, template = _answer(
            client.post(
                "/chat/stream",
                json={"query": "给我一个模板示例", "conversation_id": conversation_id},
            )
        )
        assert template["answer_kind"] == "template"
        assert len(template["commands"]) == 1

        # The template request is terminal; a later message starts a normal
        # retrieval/planning turn instead of consuming the old shell slot.
        _, fresh = _answer(
            client.post(
                "/chat/stream",
                json={"query": "Git 怎么安全回滚一次提交？", "conversation_id": conversation_id},
            )
        )
        assert (fresh.get("clarification") or {}).get("slot") != "shell"


def test_catalog_recipe_renders_posix_shell_and_does_not_use_natural_language_as_image() -> None:
    with TestClient(app) as client:
        conversation_id, first = _answer(
            client.post("/chat/stream", json={"query": "Docker 怎么运行镜像？"})
        )
        assert first["clarification"]["slot"] == "image"
        _, second = _answer(
            client.post(
                "/chat/stream",
                json={"query": "nginx:latest", "conversation_id": conversation_id},
            )
        )
        assert second["clarification"]["slot"] == "shell"
        assert any(item["value"] == "posix" and "Bash" in item["label"] for item in second["clarification"]["options"])
        _, third = _answer(
            client.post(
                "/chat/stream",
                json={"query": "Bash", "conversation_id": conversation_id},
            )
        )
        assert third["answer_kind"] == "direct"
        assert third["commands"][0]["shell"] == "posix"
        assert "nginx:latest" in third["commands"][0]["code"]
        assert third["commands"][0]["code"] == "docker run nginx:latest"


def test_docker_run_accepts_explicit_cli_facts_and_omits_optional_name() -> None:
    with TestClient(app) as client:
        conversation_id, first = _answer(
            client.post("/chat/stream", json={"query": "docker run nginx:latest"})
        )
        assert first["answer_kind"] == "clarification"
        assert first["clarification"]["slot"] == "shell"
        assert first["command_plan"]["collected_slots"]["image"] == "nginx:latest"

        _, final = _answer(
            client.post(
                "/chat/stream",
                json={"query": "Bash", "conversation_id": conversation_id},
            )
        )
    assert final["answer_kind"] == "direct"
    assert final["commands"][0]["shell"] == "posix"
    assert final["commands"][0]["code"] == "docker run nginx:latest"
    assert "<" not in final["commands"][0]["code"]


def test_docker_run_preserves_explicit_name_and_shell_alias() -> None:
    with TestClient(app) as client:
        conversation_id, first = _answer(
            client.post("/chat/stream", json={"query": "docker run --name web nginx:latest"})
        )
        assert first["clarification"]["slot"] == "shell"
        _, final = _answer(
            client.post(
                "/chat/stream",
                json={"query": "bash", "conversation_id": conversation_id},
            )
        )
    assert final["answer_kind"] == "direct"
    assert final["commands"][0]["shell"] == "posix"
    assert "--name 'web'" in final["commands"][0]["code"]


def test_windows_file_recipe_renders_one_quoted_command() -> None:
    with TestClient(app) as client:
        conversation_id, first = _answer(
            client.post("/chat/stream", json={"query": "Windows PowerShell 怎么创建一个文件？"})
        )
        assert first["answer_kind"] == "clarification"
        assert first["clarification"]["slot"] == "path"
        _, final = _answer(
            client.post(
                "/chat/stream",
                json={"query": r"C:\项目\空白.txt", "conversation_id": conversation_id},
            )
        )
    assert final["answer_kind"] == "direct"
    assert len(final["commands"]) == 1
    assert final["commands"][0]["shell"] == "powershell"
    assert "New-Item" in final["commands"][0]["code"]
    assert "空白.txt" in final["commands"][0]["code"]


def test_command_plan_table_is_created_and_cascades(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "plans.sqlite3")
    conversation = repository.create()
    plan = repository.create_command_plan(
        conversation.id,
        conversation.knowledge_base_id,
        "python.virtualenv.create_activate",
        {"target_path": {"input_type": "path"}},
        values={"target_path": r"C:\workspace"},
        next_slot="shell",
        citation_ids=["cit-example"],
    )
    assert repository.get_pending_command_plan(conversation.id)["id"] == plan["id"]
    assert repository.get_pending_command_plan(conversation.id)["citation_ids"] == ["cit-example"]
    assert repository.update_command_plan(
        plan["id"], values={"shell": "powershell"}, next_slot=None,
        status="completed", expected_state_version=1,
    )["values"]["target_path"] == r"C:\workspace"
    assert repository.delete(conversation.id)
    assert repository.get_command_plan(conversation.id) is None


def test_command_plan_filters_credential_values(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "secret-plans.sqlite3")
    conversation = repository.create()
    plan = repository.create_command_plan(
        conversation.id,
        conversation.knowledge_base_id,
        "custom.credential",
        {"password": {"input_type": "text", "secret": True}},
        values={"password": "do-not-store", "host": "localhost"},
        next_slot="password",
    )
    assert plan["values"] == {"host": "localhost"}
    assert "do-not-store" not in json.dumps(plan)


def test_command_plan_filters_sensitive_value_patterns(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "secret-value-plans.sqlite3")
    conversation = repository.create()
    plan = repository.create_command_plan(
        conversation.id,
        conversation.knowledge_base_id,
        "custom.connection",
        {"host": {"input_type": "text"}},
        values={"host": "mysql://user:secret@db.example", "safe": "localhost"},
        next_slot="host",
    )
    assert plan["values"] == {"safe": "localhost"}
    assert "secret" not in json.dumps(plan)


def test_document_rebuild_expires_pending_plans_for_the_knowledge_base(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "rebuild-plans.sqlite3")
    conversation = repository.create()
    document = repository.create_document(
        knowledge_base_id=conversation.knowledge_base_id,
        filename="notes.md",
        media_type="text/markdown",
        size_bytes=10,
        sha256="a" * 64,
        stored_path=str(tmp_path / "notes.md"),
    )
    plan = repository.create_command_plan(
        conversation.id,
        conversation.knowledge_base_id,
        "python.virtualenv.create_activate",
        {"target_path": {"input_type": "path"}},
        next_slot="target_path",
    )
    repository.request_document_rebuild(document.id)
    current = repository.get_command_plan(conversation.id)
    assert current is not None
    assert current["id"] == plan["id"]
    assert current["status"] == "expired"
    assert repository.get_pending_command_plan(conversation.id) is None
