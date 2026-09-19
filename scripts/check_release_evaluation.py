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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.evaluation_gate import compare_reports  # noqa: E402


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
    args = parser.parse_args(argv)

    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise SystemExit("evaluation report must be a JSON object")
    manifest = report.get("manifest")
    if not isinstance(manifest, dict):
        raise SystemExit("evaluation report is missing manifest")

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
