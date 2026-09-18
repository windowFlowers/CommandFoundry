from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.command_planner import extract_paths
from app.main import app


def _events(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in raw.strip().split("\n\n"):
        lines = frame.splitlines()
        event = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data_line = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if event and data_line:
            events.append((event, json.loads(data_line)))
    return events


def _answer(response) -> tuple[str, dict]:
    events = _events(response.text)
    return (
        next(data["conversation_id"] for event, data in events if event == "done"),
        next(data["answer"] for event, data in events if event == "answer"),
    )


def test_windows_path_extraction_keeps_two_paths_and_drops_shell_label() -> None:
    paths = extract_paths(r"Windows 复制 C:\work\a.txt 到 C:\backup\a.txt PowerShell")
    assert paths == [r"C:\work\a.txt", r"C:\backup\a.txt"]


def test_windows_and_posix_path_quoting_covers_unc_unicode_quotes_and_trailing_separator() -> None:
    assert extract_paths("\\\\server\\share\\中文 目录\\") == ["\\\\server\\share\\中文 目录\\"]
    assert extract_paths("C:\\O'Reilly\\资料.txt") == ["C:\\O'Reilly\\资料.txt"]

    from app.command_planner import render_virtualenv

    rendered = render_virtualenv("C:\\O'Reilly\\项目", ".venv", "powershell")
    assert rendered is not None
    assert "C:\\O''Reilly\\项目\\.venv" in rendered.code


def test_windows_copy_renders_direct_command_from_two_first_turn_paths() -> None:
    with TestClient(app) as client:
        _, answer = _answer(
            client.post(
                "/chat/stream",
                json={"query": r"Windows 复制 C:\work\a.txt 到 C:\backup\a.txt PowerShell"},
            )
        )

    assert answer["answer_kind"] == "direct"
    assert len(answer["commands"]) == 1
    command = answer["commands"][0]
    assert command["shell"] == "powershell"
    assert command["code"] == "Copy-Item -LiteralPath 'C:\\work\\a.txt' -Destination 'C:\\backup\\a.txt'"
    assert "<" not in command["code"] and ">" not in command["code"]


def test_windows_stop_process_asks_shell_then_renders_medium_risk_command() -> None:
    with TestClient(app) as client:
        conversation_id, first = _answer(
            client.post("/chat/stream", json={"query": "Windows 停止 PID 1234 的进程"})
        )
        assert first["answer_kind"] == "clarification"
        assert first["clarification"]["slot"] == "shell"

        _, final = _answer(
            client.post(
                "/chat/stream",
                json={"query": "PowerShell", "conversation_id": conversation_id},
            )
        )

    assert final["answer_kind"] == "direct"
    assert final["commands"][0]["code"] == "Stop-Process -Id 1234"
    assert final["commands"][0]["risk"] == "medium"


def test_windows_test_netconnection_collects_host_and_port_on_first_turn() -> None:
    with TestClient(app) as client:
        _, answer = _answer(
            client.post(
                "/chat/stream",
                json={"query": "Windows 测试主机 example.com 端口 443"},
            )
        )

    assert answer["answer_kind"] == "direct"
    assert answer["commands"][0]["code"] == "Test-NetConnection -ComputerName 'example.com' -Port 443"


def test_winget_install_keeps_package_and_asks_only_shell() -> None:
    with TestClient(app) as client:
        conversation_id, first = _answer(
            client.post("/chat/stream", json={"query": "Windows 安装 winget 包 Git.Git"})
        )
        assert first["answer_kind"] == "clarification"
        assert first["clarification"]["slot"] == "shell"
        assert first["command_plan"]["collected_slots"]["package_id"] == "Git.Git"

        _, final = _answer(
            client.post(
                "/chat/stream",
                json={"query": "cmd", "conversation_id": conversation_id},
            )
        )

    assert final["answer_kind"] == "direct"
    assert final["commands"][0]["shell"] == "cmd"
    assert final["commands"][0]["code"] == "winget install --id Git.Git --exact"
