from __future__ import annotations

import json
from pathlib import Path

from app.main import catalog, retriever


def test_all_300_evaluation_queries_hit_expected_topic_in_top_three() -> None:
    dataset_path = Path(__file__).resolve().parents[2] / "knowledge" / "eval_dataset.json"
    cases = json.loads(dataset_path.read_text(encoding="utf-8"))
    assert len(cases) == 300
    hits = 0
    for case in cases:
        result_ids = [item.topic.id for item in retriever.retrieve(case["query"], top_k=3)]
        hits += case["expected_topic_id"] in result_ids
    assert hits / len(cases) >= 0.90


def test_required_showcase_queries_hit_correct_topic_first() -> None:
    cases = {
        "在 Linux 中切换文件目录的指令是什么？": "linux.cd",
        "Git 怎么安全回滚一次提交？": "git.git-revert",
        "MySQL 中怎么创建一个视图？": "sql.create-view",
    }
    for query, expected in cases.items():
        assert retriever.retrieve(query, top_k=3)[0].topic.id == expected


def test_catalog_sources_are_traceable_to_manifest() -> None:
    source_ids = {item["source_id"] for item in catalog.manifest["sources"]}
    assert len(catalog.topics) == 300
    assert all(topic.source.source_id in source_ids for topic in catalog.topics)
    assert all(len(topic.source.sha256) == 64 for topic in catalog.topics)
    source_lock = json.loads((Path(__file__).resolve().parents[2] / "knowledge" / "sources.lock.json").read_text(encoding="utf-8"))
    assert len(source_lock["sources"]) == 300
    assert {item["source_id"] for item in source_lock["sources"]} == source_ids
    assert all(item["source_url"].startswith("https://") for item in source_lock["sources"])


def test_command_placeholders_and_fences_are_intact() -> None:
    for topic in catalog.topics:
        for command in topic.commands:
            assert "```" not in command.code
            assert command.code.strip()
            if topic.source.kind == "generated_seed":
                without_redirects = command.code.replace(" > ", " ").replace(" < ", " ")
                assert without_redirects.count("<") == without_redirects.count(">")
