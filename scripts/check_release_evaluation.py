"""Check that a release candidate has a matching, passing evaluation report.

This is intentionally a small, dependency-light release guard.  It does not
run a model or build an installer; it verifies that those steps already
produced a report for the exact source and knowledge revision being released.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.evaluation_gate import compare_reports  # noqa: E402
from app.evaluation import load_evaluation_suite  # noqa: E402


# A single release-suite inventory shared by the runner and release checker.
RELEASE_SUITE_PATHS = (
    PROJECT_ROOT / "evaluation" / "fixtures" / "v2.11" / "manifest.json",
    PROJECT_ROOT / "knowledge" / "eval_dataset.json",
    PROJECT_ROOT / "knowledge" / "memory_eval_dataset.json",
    PROJECT_ROOT / "knowledge" / "personalization_eval_dataset.json",
    PROJECT_ROOT / "knowledge" / "windows_eval_dataset.json",
)


def dataset_fingerprints_for_suites(
    paths: Iterable[Path], *, root: Path = PROJECT_ROOT
) -> dict[str, str]:
    """Hash suite inputs including every manifest dataset, qrel and schema.

    Hashing only a manifest misses edits to a dataset whose manifest was not
    regenerated. Relative path keys avoid collisions between equal basenames.
    """
    root = root.resolve()
    fingerprints: dict[str, str] = {}
    visiting: set[Path] = set()

    def visit(path: Path) -> None:
        path = path.resolve()
        if not path.is_relative_to(root):
            raise ValueError("evaluation suite input must remain inside the project")
        if path in visiting:
            raise ValueError("cyclic evaluation suite manifest")
        key = path.relative_to(root).as_posix()
        if key in fingerprints:
            return
        content = path.read_bytes()
        visiting.add(path)
        if path.suffix.casefold() == ".json":
            payload = json.loads(content)
            if isinstance(payload, dict) and "datasets" in payload:
                for dataset in payload["datasets"]:
                    visit(path.parent / dataset["path"])
                    if dataset.get("qrels"):
                        visit(path.parent / dataset["qrels"])
                if payload.get("schema"):
                    visit(path.parent / payload["schema"])
        visiting.remove(path)
        fingerprints[key] = hashlib.sha256(content).hexdigest()

    for path in paths:
        visit(Path(path))
    return dict(sorted(fingerprints.items()))


def _turn_count(case: Any) -> int:
    if case.turns:
        count = sum(
            1 for turn in case.turns
            if isinstance(turn, dict)
            and str(turn.get("role", "user")).casefold() == "user"
            and str(turn.get("content") or turn.get("query") or "").strip()
        )
        return max(1, count)
    return 2 if case.previous_query else 1


def validate_release_completeness(
    report: dict[str, Any], *, suite_paths: Iterable[Path] | None = None,
    root: Path = PROJECT_ROOT, required_mode: str | None = None,
) -> None:
    """Fail closed on partial suites, stale inputs and missing model evidence."""
    paths = tuple(suite_paths if suite_paths is not None else RELEASE_SUITE_PATHS)
    manifest = report.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("evaluation report is missing manifest")
    mode = manifest.get("mode")
    if mode not in {"offline", "model"} or (required_mode and mode != required_mode):
        raise ValueError(f"evaluation report mode must be {required_mode or 'offline or model'}")
    expected_paths = {path.resolve().relative_to(root.resolve()).as_posix() for path in paths}
    actual_paths = manifest.get("suite_paths")
    if not isinstance(actual_paths, list) or len(actual_paths) != len(expected_paths):
        raise ValueError("release report must use the complete suite all inventory")
    if set(str(path).replace("\\", "/") for path in actual_paths) != expected_paths:
        raise ValueError("release report must use the complete suite all inventory")
    if manifest.get("dataset_fingerprints") != dataset_fingerprints_for_suites(paths, root=root):
        raise ValueError("evaluation dataset fingerprints do not match current inputs")

    expected_cases = [case for path in paths for case in load_evaluation_suite(path)]
    expected_ids = [case.id for case in expected_cases]
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("release suite contains duplicate case IDs")
    results = report.get("cases")
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise ValueError("release report must contain case results")
    actual_ids = [item.get("case_id") for item in results]
    if any(not isinstance(item, str) for item in actual_ids) or Counter(actual_ids) != Counter(expected_ids):
        raise ValueError("release report case IDs must match suite all exactly (no missing or duplicate cases)")
    by_id = {item["case_id"]: item for item in results}
    model_successes = 0
    if mode == "model" and manifest.get("parameters", {}).get("model_observability") != "per-turn-v1":
        raise ValueError("model report is missing per-turn-v1 model observability")
    for case in expected_cases:
        item = by_id[case.id]
        permitted_skip = not case.relevant_ids and case.metadata.get("evaluation_kind") == "personalization"
        if item.get("skipped"):
            if not permitted_skip:
                raise ValueError(f"release case may not be skipped: {case.id}")
            continue
        if mode != "model":
            continue
        calls = item.get("model_calls")
        if not isinstance(calls, list) or len(calls) != _turn_count(case):
            raise ValueError(f"model report must record every user turn: {case.id}")
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("attempted"), bool):
                raise ValueError(f"invalid model call observation: {case.id}")
            if call["attempted"] and not call.get("fallback_reason"):
                model_successes += 1
    if mode == "model" and not model_successes:
        raise ValueError("model release report must contain a successful real model call")


def _git_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _knowledge_revision() -> str:
    return hashlib.sha256(
        (PROJECT_ROOT / "knowledge" / "topics.json").read_bytes()
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a release evaluation report")
    parser.add_argument("report", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--require-baseline", action="store_true")
    parser.add_argument("--require-mode", choices=("offline", "model"))
    args = parser.parse_args(argv)

    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise SystemExit("evaluation report must be a JSON object")
    manifest = report.get("manifest")
    if not isinstance(manifest, dict):
        raise SystemExit("evaluation report is missing manifest")
    try:
        validate_release_completeness(report, required_mode=args.require_mode)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise SystemExit(f"release evaluation is incomplete: {exc}") from exc

    expected_commit = _git_revision()
    expected_knowledge = _knowledge_revision()
    if manifest.get("git_revision") != expected_commit:
        raise SystemExit(
            "evaluation report git revision does not match HEAD: "
            f"{manifest.get('git_revision')} != {expected_commit}"
        )
    if manifest.get("knowledge_revision") != expected_knowledge:
        raise SystemExit("evaluation report knowledge revision does not match topics.json")

    if args.require_baseline and not args.baseline:
        raise SystemExit("--require-baseline requires --baseline")
    baseline = None
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if not isinstance(baseline, dict):
            raise SystemExit("baseline report must be a JSON object")

    result = compare_reports(report, baseline)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if not result.passed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
