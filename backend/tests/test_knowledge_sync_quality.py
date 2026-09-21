from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.sync_knowledge import CLEANING_REVISION, clean_and_validate_topics, clean_topic_payload  # noqa: E402


def _topic(topic_id: str, *, source_id: str | None = None) -> dict[str, object]:
    return {
        "id": topic_id,
        "domain": "demo",
        "title": "  示例标题  ",
        "aliases": [" 示例查询 ", "示例查询", "另一个查询"],
        "summary": " 示例摘要\r\n\r\n\r\n ",
        "commands": [
            {
                "label": "执行命令  ",
                "language": "bash",
                "code": "  printf 'ok'  \r\n",
                "platforms": ["Linux", "Linux"],
                "prerequisites": ["已安装工具", "已安装工具"],
                "risk": "low",
                "warning": "  ",
            },
            {
                "label": "重复命令",
                "language": "bash",
                "code": "printf 'ok'",
                "platforms": ["Linux"],
                "prerequisites": [],
                "risk": "low",
                "warning": None,
            },
        ],
        "notes": [" 说明 ", "说明"],
        "source": {
            "source_id": source_id or topic_id,
            "title": "示例来源",
            "source_url": "https://example.com/source",
            "license": "MIT",
            "revision": "r1",
            "path": "generated/demo",
            "sha256": "a" * 64,
            "kind": "generated_seed",
        },
    }


def test_sync_cleaning_is_deterministic_and_deduplicates_within_topic() -> None:
    cleaned, stats = clean_topic_payload(_topic("demo.one"))

    assert cleaned["title"] == "示例标题"
    assert cleaned["aliases"] == ["示例查询", "另一个查询"]
    assert len(cleaned["commands"]) == 1
    assert cleaned["commands"][0]["code"] == "  printf 'ok'"
    assert cleaned["source"]["cleaning_revision"] == CLEANING_REVISION
    assert stats == {"removed_aliases": 1, "removed_notes": 1, "removed_commands": 1}


def test_sync_does_not_merge_same_command_across_topic_ids() -> None:
    topics, report = clean_and_validate_topics([_topic("demo.one"), _topic("demo.two")])

    assert [topic["id"] for topic in topics] == ["demo.one", "demo.two"]
    assert report["topic_count"] == 2
    assert report["command_count"] == 2
    assert report["rules"]["cross_topic_deduplication"] is False


def test_sync_rejects_duplicate_topic_or_source_ids() -> None:
    with pytest.raises(ValueError, match="duplicate topic id"):
        clean_and_validate_topics([_topic("demo.one"), _topic("demo.one", source_id="demo.two")])
    with pytest.raises(ValueError, match="duplicate source id"):
        clean_and_validate_topics([_topic("demo.one"), _topic("demo.two", source_id="demo.one")])


def test_sync_rejects_inconsistent_shell_and_platform() -> None:
    topic = _topic("demo.windows")
    topic["domain"] = "windows"
    topic["commands"][0]["shell"] = "powershell"
    topic["commands"][0]["language"] = "powershell"
    with pytest.raises(ValueError, match="Windows platform"):
        clean_topic_payload(topic)


def test_generated_quality_report_records_cleaning_revision() -> None:
    report = json.loads((ROOT / "knowledge" / "quality_report.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "knowledge" / "manifest.json").read_text(encoding="utf-8"))

    assert report["cleaning_revision"] == CLEANING_REVISION
    assert report["status"] in {"clean", "warning"}
    assert report["topic_count"] == manifest["topic_count"] == 300
    assert manifest["cleaning_revision"] == CLEANING_REVISION
    assert manifest["quality_report_sha256"] == report["report_sha256"]
