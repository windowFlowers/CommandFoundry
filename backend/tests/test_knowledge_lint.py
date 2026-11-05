from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.sync_knowledge import TOPIC_CUES, normalize_placeholders, risk_for  # noqa: E402

TOPICS = json.loads((ROOT / "knowledge" / "topics.json").read_text(encoding="utf-8"))
COMMANDS = [(topic, command) for topic in TOPICS for command in topic["commands"]]


def test_commands_have_no_leftover_tldr_syntax() -> None:
    leftovers = [
        command["code"]
        for _, command in COMMANDS
        if "\\{" in command["code"] or "\\}" in command["code"] or re.search(r"<\[[^\]]*\|", command["code"])
    ]
    assert leftovers == []


def test_every_topic_has_real_commands() -> None:
    assert [topic["id"] for topic in TOPICS if not topic["commands"]] == []
    assert [command["code"] for _, command in COMMANDS if command["code"].startswith("tldr ")] == []


def test_each_tldr_page_backs_one_topic_per_name() -> None:
    names = [topic["id"].split(".", 1)[1] for topic in TOPICS if topic["source"]["kind"] == "vendored"]
    assert sorted(name for name in set(names) if names.count(name) > 1) == []


def test_topic_cues_reference_existing_topics() -> None:
    assert sorted(set(TOPIC_CUES) - {topic["id"] for topic in TOPICS}) == []


@pytest.mark.parametrize(
    "pattern",
    [
        r"^kubectl (delete|drain)\b",
        r"^terraform destroy",
        r"docker (volume|network|system|image) prune",
        r"docker compose down .*--volumes",
        r"^git stash (clear|drop)",
        r"^git push .*--delete",
        r"^rsync .*--delete",
        r"^find .*-delete",
        r"^gh .*\b(delete|remove)\b",
        r"^rm .*--recursive",
        r"^(DEL|FLUSHDB)\b",
    ],
)
def test_destructive_commands_are_high_risk(pattern: str) -> None:
    matches = [command for _, command in COMMANDS if re.search(pattern, command["code"])]
    assert matches
    assert [command["code"] for command in matches if command["risk"] != "high"] == []


def test_linux_only_tools_do_not_claim_windows() -> None:
    linux_only = {"network.ip", "network.ss", "network.ufw", "testing.strace", "network.firewall-cmd", "network.hostnamectl", "network.certbot"}
    by_id = {topic["id"]: topic for topic in TOPICS}
    for topic_id in linux_only:
        for command in by_id[topic_id]["commands"]:
            assert not any(platform.startswith("Windows") for platform in command["platforms"]), topic_id


def test_redis_repl_commands_are_not_labelled_bash() -> None:
    for topic, command in COMMANDS:
        if topic["domain"] == "redis" and topic["source"]["kind"] == "generated_seed" and "redis-cli" not in command["code"]:
            assert command["language"] == "text", command["code"]


def test_redis_subscribe_and_publish_are_separate_sessions() -> None:
    pubsub = next(topic for topic in TOPICS if topic["id"] == "redis.pubsub")
    assert [command["code"].split()[0] for command in pubsub["commands"]] == ["SUBSCRIBE", "PUBLISH"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("kubectl get {{[po|pods]}}", "kubectl get pods"),
        ("ls {{[-la|-l --all]}}", "ls -l --all"),
        ("docker inspect --format '\\{\\{.LogPath\\}\\}' {{container}}", "docker inspect --format '{{.LogPath}}' <container>"),
        ('docker search --format "{{.Name}}" {{keyword}}', 'docker search --format "{{.Name}}" <keyword>'),
        ("go fmt {{./...}}", "go fmt <./...>"),
        ("mkdir {{path/to/{a,b}}}", "mkdir <path/to/{a,b}>"),
    ],
)
def test_normalize_placeholders(source: str, expected: str) -> None:
    assert normalize_placeholders(source) == expected


def test_risk_for_sees_flags_written_in_long_form() -> None:
    assert risk_for("rm --recursive --force <path>")[0] == "high"
    assert risk_for("curl -fsSL <url> | sudo bash")[0] == "high"
    assert risk_for("git status")[0] == "low"
    assert risk_for("git branch --delete <branch>")[0] == "medium"


def _codes(topic_id: str) -> list[str]:
    return [command["code"] for command in next(topic for topic in TOPICS if topic["id"] == topic_id)["commands"]]


def test_dry_run_templates_cannot_select_a_real_run() -> None:
    assert [command["code"] for _, command in COMMANDS if re.search(r"--dry-run=<[^>]*none", command["code"])] == []


def test_topics_do_not_repeat_another_topics_commands() -> None:
    sequences: dict[tuple[str, ...], list[str]] = {}
    for topic in TOPICS:
        sequences.setdefault(tuple(command["code"] for command in topic["commands"]), []).append(topic["id"])
    assert [ids for ids in sequences.values() if len(ids) > 1] == []


def test_topics_lead_with_their_own_tool() -> None:
    assert _codes("linux.gunzip")[0].startswith("gunzip ")
    assert _codes("node.npm-test")[0] == "npm test"
    assert _codes("python.http-server")[0] == "python -m http.server"
    assert _codes("node.http-server")[0] == "http-server"


@pytest.mark.parametrize(
    ("topic_id", "removed"),
    [
        ("cicd.helm", "helm repo add <repository_name>"),
        ("cicd.helm", "helm get <chart_release_name>"),
        ("node.next", "next export"),
        ("python.pipenv", "pipenv --three"),
        ("python.pipenv", "pipenv lock --requirements"),
        ("node.webpack", "webpack <app.js> <bundle.js>"),
        ("java.jmap", "jmap -heap <filename.jar> <java_pid>"),
        ("linux.zip", "zip --split-size --freshen <path/to/compressed.zip>"),
    ],
)
def test_broken_upstream_examples_stay_corrected(topic_id: str, removed: str) -> None:
    assert removed not in _codes(topic_id)


def test_shell_handover_through_netcat_is_high_risk() -> None:
    risky = [command for topic, command in COMMANDS if topic["id"] == "network.nc" and "<shell_executable>" in command["code"]]
    assert len(risky) == 2
    assert {command["risk"] for command in risky} == {"high"}
    assert all("远程命令执行" in command["warning"] for command in risky)


def test_posix_only_syntax_is_not_offered_to_native_windows() -> None:
    offenders = [
        command["code"]
        for _, command in COMMANDS
        if "Windows" in command["platforms"] and re.search(r"(?:^|\s)(?:sudo\s|export\s+\w+=|eval\s|source\s)|\$\(", command["code"])
    ]
    assert offenders == []
