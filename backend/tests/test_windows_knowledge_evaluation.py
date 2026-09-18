from __future__ import annotations

import json
from pathlib import Path

from app.main import catalog, retriever


ROOT = Path(__file__).resolve().parents[2]
WINDOWS_EVAL = ROOT / "knowledge" / "windows_eval_dataset.json"


def _windows_topics():
    return [topic for topic in catalog.topics if topic.domain == "windows"]


def test_windows_offline_cases_cover_the_seed_set_and_hit_top_three() -> None:
    cases = json.loads(WINDOWS_EVAL.read_text(encoding="utf-8"))
    topics = _windows_topics()
    topic_ids = {topic.id for topic in topics}

    assert 25 <= len(topics) <= 35
    assert len(cases) >= len(topics) * 3
    assert {case["expected_topic_id"] for case in cases} == topic_ids

    for case in cases:
        hits = retriever.retrieve(case["query"], top_k=3)
        hit_ids = [hit.topic.id for hit in hits]
        assert case["expected_topic_id"] in hit_ids, (
            f"Windows query missed expected topic: {case['query']!r}; got {hit_ids}"
        )
        matched = next(hit.topic for hit in hits if hit.topic.id == case["expected_topic_id"])
        actual_shells = {command.shell for command in matched.commands if command.shell}
        assert set(case["expected_shells"]) <= actual_shells


def test_windows_commands_have_explicit_shells_and_robocopy_guardrail() -> None:
    topics = _windows_topics()
    for topic in topics:
        assert topic.source.revision == "v2.7.0"
        assert topic.source.source_url and topic.source.source_url.startswith("https://")
        assert len(topic.aliases) >= 3
        assert len(set(topic.aliases)) == len(topic.aliases)
        for command in topic.commands:
            assert command.shell in {"powershell", "cmd"}
            expected_language = "powershell" if command.shell == "powershell" else "text"
            assert command.language == expected_language

    robocopy = next(topic for topic in topics if topic.id == "windows.robocopy")
    mirror_commands = [command for command in robocopy.commands if "/MIR" in command.code.upper()]
    assert mirror_commands
    assert mirror_commands[0].risk.value == "high"
    assert mirror_commands[0].warning and "删除" in mirror_commands[0].warning
