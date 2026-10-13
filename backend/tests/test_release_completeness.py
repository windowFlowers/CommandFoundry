"""Release checks must not accept a passing subset as a full evaluation."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_script("check_release_evaluation")
workflow = _load_script("run_release_workflow")


@pytest.fixture
def full_report():
    cases = [case for path in checker.RELEASE_SUITE_PATHS for case in checker.load_evaluation_suite(path)]
    return {
        "manifest": {
            "mode": "offline",
            "suite_paths": [path.relative_to(ROOT).as_posix() for path in checker.RELEASE_SUITE_PATHS],
            "dataset_fingerprints": checker.dataset_fingerprints_for_suites(checker.RELEASE_SUITE_PATHS),
        },
        "cases": [
            {
                "case_id": case.id,
                "passed": True,
                "skipped": not case.relevant_ids and case.metadata.get("evaluation_kind") == "personalization",
            }
            for case in cases
        ],
    }


def _as_model(report):
    report = copy.deepcopy(report)
    report["manifest"].update(mode="model", parameters={"model_observability": "per-turn-v1"})
    cases = {case.id: case for path in checker.RELEASE_SUITE_PATHS for case in checker.load_evaluation_suite(path)}
    for item in report["cases"]:
        if not item["skipped"]:
            item["model_calls"] = [
                {"attempted": True, "fallback_reason": None, "total_tokens": 10}
                for _ in range(checker._turn_count(cases[item["case_id"]]))
            ]
    return report


def test_complete_local_release_suite_passes(full_report):
    checker.validate_release_completeness(full_report)
    checker.validate_release_completeness(_as_model(full_report), required_mode="model")


@pytest.mark.parametrize("mutation", ["six_only", "missing", "duplicate", "substitution", "skip"])
def test_passing_metrics_cannot_hide_incomplete_case_inventory(full_report, mutation):
    # Keep a forged perfect summary: only the actual case list establishes coverage.
    full_report["metrics"] = {"pass_rate": 1.0, "cases": len(full_report["cases"])}
    if mutation == "six_only":
        full_report["cases"] = full_report["cases"][:6]
    elif mutation == "missing":
        full_report["cases"].pop()
    elif mutation == "duplicate":
        full_report["cases"][-1] = copy.deepcopy(full_report["cases"][0])
    elif mutation == "substitution":
        full_report["cases"][-1]["case_id"] = "fabricated-case"
    else:
        full_report["cases"][0]["skipped"] = True
    with pytest.raises(ValueError, match="case IDs|may not be skipped"):
        checker.validate_release_completeness(full_report)


def test_named_subset_cannot_claim_full_release(full_report):
    full_report["manifest"]["suite_paths"] = ["evaluation/fixtures/v2.11/answer_golden.jsonl"]
    with pytest.raises(ValueError, match="suite all inventory"):
        checker.validate_release_completeness(full_report)


def test_manifest_only_hashes_are_insufficient(full_report):
    full_report["manifest"]["dataset_fingerprints"].pop("evaluation/fixtures/v2.11/answer.qrels")
    with pytest.raises(ValueError, match="fingerprints"):
        checker.validate_release_completeness(full_report)


@pytest.mark.parametrize("changed_file", ["nested.jsonl", "nested.qrels", "schema.json"])
def test_nested_fixture_change_invalidates_unchanged_manifest(tmp_path, changed_file):
    fixture = tmp_path / "nested.jsonl"
    fixture.write_text(json.dumps({"id": "example", "query": "sample", "expected_topic_id": "topic"}) + "\n", encoding="utf-8")
    (tmp_path / "nested.qrels").write_text("example 0 topic 1\n", encoding="utf-8")
    (tmp_path / "schema.json").write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema": "schema.json", "datasets": [{"path": "nested.jsonl", "qrels": "nested.qrels"}]}), encoding="utf-8")
    report = {
        "manifest": {
            "mode": "offline", "suite_paths": ["manifest.json"],
            "dataset_fingerprints": checker.dataset_fingerprints_for_suites([manifest], root=tmp_path),
        },
        "cases": [{"case_id": "example", "passed": True}],
    }
    checker.validate_release_completeness(report, suite_paths=[manifest], root=tmp_path)
    target = tmp_path / changed_file
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprints"):
        checker.validate_release_completeness(report, suite_paths=[manifest], root=tmp_path)


def test_model_report_requires_actual_turn_observations(full_report):
    report = _as_model(full_report)
    report["cases"][0]["model_calls"] = []
    with pytest.raises(ValueError, match="every user turn"):
        checker.validate_release_completeness(report)


def test_multiturn_model_report_requires_all_turns(full_report):
    report = _as_model(full_report)
    item = next(item for item in report["cases"] if len(item.get("model_calls", [])) > 1)
    item["model_calls"].pop()
    with pytest.raises(ValueError, match="every user turn"):
        checker.validate_release_completeness(report)


def test_all_local_fallback_is_not_a_real_model_release(full_report):
    report = _as_model(full_report)
    for item in report["cases"]:
        for call in item.get("model_calls", []):
            call.update(attempted=False, fallback_reason="no_api_key")
    with pytest.raises(ValueError, match="successful real model call"):
        checker.validate_release_completeness(report)


def test_model_report_needs_observability_version(full_report):
    report = _as_model(full_report)
    report["manifest"]["parameters"] = {}
    with pytest.raises(ValueError, match="per-turn-v1"):
        checker.validate_release_completeness(report)


def test_packaging_rejects_offline_report_even_with_passing_baseline(full_report):
    with pytest.raises(ValueError, match="mode must be model"):
        checker.validate_release_completeness(full_report, required_mode="model")


def test_workflow_passes_required_mode_to_checker(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(workflow, "_run", lambda label, command: commands.append(command))
    workflow._verify_report(tmp_path / "report.json", tmp_path / "baseline.json", mode="model")
    assert commands[0][commands[0].index("--require-mode") + 1] == "model"


def test_package_checks_model_mode_before_build(monkeypatch, tmp_path):
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    args = workflow.parse_args(["package", "--model-report", str(report), "--model-baseline", str(report)])
    monkeypatch.setattr(workflow.platform, "system", lambda: "Windows")

    def reject_offline(report, baseline, *, mode):
        assert mode == "model"
        raise ValueError("model gate checked before build")

    monkeypatch.setattr(workflow, "_verify_report", reject_offline)
    monkeypatch.setattr(workflow, "_run", lambda *args, **kwargs: pytest.fail("build must not run"))
    with pytest.raises(ValueError, match="before build"):
        workflow._package(args, tmp_path)
