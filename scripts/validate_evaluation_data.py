"""Validate versioned, offline evaluation fixtures.

The validator deliberately uses only the Python standard library so it can run
before backend dependencies are installed and without accessing the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = PROJECT_ROOT / "evaluation" / "fixtures" / "v2.11"
DEFAULT_TOPICS = PROJECT_ROOT / "knowledge" / "topics.json"

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|pk)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|password|token|secret)\s*[:=]\s*"
        r"(?!<[^>]+>)[^\s\"']{8,}"
    ),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~-]{8,}"),
)


class ValidationErrors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, message: str) -> None:
        self.items.append(message)

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.add(message)


def load_json(path: Path, errors: ValidationErrors) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.add(f"missing file: {path}")
    except json.JSONDecodeError as exc:
        errors.add(f"invalid JSON in {path}: {exc}")
    return None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_safe_fixture_path(value: str) -> bool:
    path = Path(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not re.match(r"^[A-Za-z]:[\\/]", value)
    )


def scan_for_secrets(fixtures: Path, errors: ValidationErrors) -> None:
    for path in sorted(p for p in fixtures.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.add(f"fixture is not UTF-8 text: {path.relative_to(fixtures)}")
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in SECRET_PATTERNS):
                errors.add(
                    f"possible secret in {path.relative_to(fixtures)}:{line_number}"
                )


def load_jsonl(path: Path, errors: ValidationErrors) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        errors.add(f"missing dataset: {path}")
        return records
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            errors.add(f"blank JSONL line in {path.name}:{number}")
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.add(f"invalid JSONL in {path.name}:{number}: {exc.msg}")
            continue
        if not isinstance(record, dict):
            errors.add(f"record is not an object in {path.name}:{number}")
            continue
        records.append(record)
    return records


def load_qrels(path: Path, errors: ValidationErrors) -> dict[str, dict[str, int]]:
    """Load TREC qrels while allowing several relevant documents per query."""

    qrels: dict[str, dict[str, int]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        errors.add(f"missing qrels: {path}")
        return qrels
    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 4:
            errors.add(f"qrels must have four columns in {path.name}:{number}")
            continue
        query_id, iteration, document_id, relevance_text = parts
        if iteration != "0":
            errors.add(f"qrels iteration must be 0 in {path.name}:{number}")
        try:
            relevance = int(relevance_text)
        except ValueError:
            errors.add(f"qrels relevance is not an integer in {path.name}:{number}")
            continue
        if relevance < 0:
            errors.add(f"qrels relevance must be non-negative in {path.name}:{number}")
        judgments = qrels.setdefault(query_id, {})
        if document_id in judgments:
            errors.add(
                f"duplicate qrels query/document pair {query_id!r}/{document_id!r} "
                f"in {path.name}"
            )
            continue
        judgments[document_id] = relevance
    return qrels


def answer_scenarios(record: dict[str, Any]) -> set[str]:
    answerability = record.get("answerability")
    tags = set(record.get("adversarial_tags", []))
    if "prompt_injection" in tags:
        return {"prompt_injection"}
    if "placeholder_safety" in tags:
        return {"placeholder_safety"}
    if answerability in {"direct", "clarification", "template"}:
        return {answerability}
    if answerability == "unanswerable" and "refusal" in tags:
        return {"unanswerable_refusal"}
    return set()


def validate_answer_record(
    record: dict[str, Any],
    prefix: str,
    answer_required_fields: set[str],
    errors: ValidationErrors,
) -> None:
    for field in answer_required_fields:
        errors.require(field in record, f"{prefix}: missing answer field {field!r}")
    errors.require(
        record.get("answerability") in {"direct", "clarification", "template", "answerable", "unanswerable"},
        f"{prefix}: invalid answerability",
    )
    errors.require(
        isinstance(record.get("reference_answer"), str) and bool(record["reference_answer"].strip()),
        f"{prefix}: reference_answer must be a non-empty string",
    )
    for field in ("reference_commands", "adversarial_tags"):
        value = record.get(field)
        errors.require(
            isinstance(value, list) and all(isinstance(item, str) for item in value),
            f"{prefix}: {field} must be an array of strings",
        )
    errors.require(record.get("risk_level") in {"low", "medium", "high"}, f"{prefix}: invalid risk_level")


def validate_dataset(
    dataset: dict[str, Any],
    fixtures: Path,
    required_fields: dict[str, str],
    allowed_categories: set[str],
    allowed_difficulties: set[str],
    allowed_fields: set[str],
    known_topic_ids: set[str],
    answer_required_fields: set[str],
    errors: ValidationErrors,
) -> tuple[int, Counter[str], Counter[str]]:
    name = dataset.get("name", "<unnamed>")
    path_value = dataset.get("path")
    qrels_value = dataset.get("qrels")
    errors.require(isinstance(path_value, str) and path_value, f"{name}: missing dataset path")
    errors.require(isinstance(qrels_value, str) and qrels_value, f"{name}: missing qrels path")
    if not isinstance(path_value, str) or not isinstance(qrels_value, str):
        return 0, Counter(), Counter()
    errors.require(is_safe_fixture_path(path_value), f"{name}: dataset path must stay within fixtures")
    errors.require(is_safe_fixture_path(qrels_value), f"{name}: qrels path must stay within fixtures")
    if not is_safe_fixture_path(path_value) or not is_safe_fixture_path(qrels_value):
        return 0, Counter(), Counter()
    path = fixtures / path_value
    qrels_path = fixtures / qrels_value
    records = load_jsonl(path, errors)
    qrels = load_qrels(qrels_path, errors)
    record_ids: set[str] = set()
    categories: Counter[str] = Counter()
    scenarios: Counter[str] = Counter()
    is_answer_dataset = dataset.get("kind") == "answer"
    for line_number, record in enumerate(records, start=1):
        prefix = f"{name}:{line_number}"
        for field in record:
            errors.require(field in allowed_fields, f"{prefix}: unknown field {field!r}")
        for field, expected_type in required_fields.items():
            value = record.get(field)
            type_matches = (
                (expected_type == "string" and isinstance(value, str))
                or (expected_type == "integer" and isinstance(value, int) and not isinstance(value, bool))
            )
            errors.require(type_matches, f"{prefix}: {field!r} must be {expected_type}")
        identifier = record.get("id")
        if isinstance(identifier, str):
            errors.require(bool(identifier.strip()), f"{prefix}: id cannot be blank")
            if identifier in record_ids:
                errors.add(f"{prefix}: duplicate id {identifier!r}")
            record_ids.add(identifier)
        query = record.get("query")
        if isinstance(query, str):
            errors.require(bool(query.strip()), f"{prefix}: query cannot be blank")
        topic_id = record.get("expected_topic_id")
        if isinstance(topic_id, str):
            errors.require(topic_id in known_topic_ids, f"{prefix}: unknown topic id {topic_id!r}")
        category = record.get("category")
        if isinstance(category, str):
            errors.require(category in allowed_categories, f"{prefix}: invalid category {category!r}")
            categories[category] += 1
        difficulty = record.get("difficulty")
        if isinstance(difficulty, str):
            errors.require(
                difficulty in allowed_difficulties,
                f"{prefix}: invalid difficulty {difficulty!r}",
            )
        relevance = record.get("expected_relevance")
        if isinstance(relevance, int) and not isinstance(relevance, bool):
            errors.require(relevance >= 0, f"{prefix}: expected_relevance must be non-negative")
        if is_answer_dataset:
            validate_answer_record(record, prefix, answer_required_fields, errors)
            scenarios.update(answer_scenarios(record))
        if isinstance(identifier, str) and isinstance(topic_id, str) and isinstance(relevance, int):
            qrel = qrels.get(identifier)
            errors.require(qrel is not None, f"{prefix}: missing qrels row")
            if qrel is not None:
                errors.require(topic_id in qrel, f"{prefix}: qrels document id does not match expected_topic_id")
                if topic_id in qrel:
                    errors.require(qrel[topic_id] == relevance, f"{prefix}: qrels relevance does not match expected_relevance")
    errors.require(
        set(qrels) == record_ids,
        f"{name}: qrels query ids must exactly match dataset ids",
    )
    declared_count = dataset.get("case_count")
    errors.require(declared_count == len(records), f"{name}: manifest case_count does not match records")
    declared_hash = dataset.get("sha256")
    if isinstance(declared_hash, str) and path.is_file():
        errors.require(declared_hash == sha256(path), f"{name}: manifest sha256 does not match dataset")
    else:
        errors.add(f"{name}: manifest must contain a sha256 for the dataset")
    declared_qrels_hash = dataset.get("qrels_sha256")
    if isinstance(declared_qrels_hash, str) and qrels_path.is_file():
        errors.require(
            declared_qrels_hash == sha256(qrels_path),
            f"{name}: manifest qrels_sha256 does not match qrels",
        )
    else:
        errors.add(f"{name}: manifest must contain a qrels_sha256")
    return len(records), categories, scenarios


def validate(fixtures: Path, topics_path: Path) -> ValidationErrors:
    errors = ValidationErrors()
    errors.require(fixtures.is_dir(), f"fixture directory does not exist: {fixtures}")
    if not fixtures.is_dir():
        return errors
    scan_for_secrets(fixtures, errors)
    manifest = load_json(fixtures / "manifest.json", errors)
    schema = load_json(fixtures / "schema.json", errors)
    topics = load_json(topics_path, errors)
    if not isinstance(manifest, dict) or not isinstance(schema, dict) or not isinstance(topics, list):
        return errors
    errors.require(manifest.get("fixture_version") == "2.11.0", "manifest: fixture_version must be 2.11.0")
    errors.require(manifest.get("schema") == "schema.json", "manifest: schema must be schema.json")
    required_fields = schema.get("required_fields")
    errors.require(isinstance(required_fields, dict), "schema: required_fields must be an object")
    if not isinstance(required_fields, dict):
        return errors
    expected_required = {"id", "query", "expected_topic_id", "category", "difficulty", "expected_relevance", "source"}
    errors.require(expected_required <= set(required_fields), "schema: required_fields is incomplete")
    allowed_categories = schema.get("allowed_categories")
    allowed_difficulties = schema.get("allowed_difficulties")
    answer_required_fields = schema.get("answer_required_fields")
    answer_scenario_names = schema.get("answer_scenarios")
    errors.require(isinstance(allowed_categories, list) and allowed_categories, "schema: allowed_categories is required")
    errors.require(isinstance(allowed_difficulties, list) and allowed_difficulties, "schema: allowed_difficulties is required")
    errors.require(
        isinstance(answer_required_fields, list) and all(isinstance(item, str) for item in answer_required_fields),
        "schema: answer_required_fields must be an array of strings",
    )
    errors.require(
        isinstance(answer_scenario_names, list) and all(isinstance(item, str) for item in answer_scenario_names),
        "schema: answer_scenarios must be an array of strings",
    )
    properties = schema.get("properties")
    errors.require(isinstance(properties, dict) and properties, "schema: properties is required")
    if (
        not isinstance(allowed_categories, list)
        or not isinstance(allowed_difficulties, list)
        or not isinstance(answer_required_fields, list)
        or not isinstance(answer_scenario_names, list)
        or not isinstance(properties, dict)
    ):
        return errors
    known_topic_ids = {topic.get("id") for topic in topics if isinstance(topic, dict) and isinstance(topic.get("id"), str)}
    datasets = manifest.get("datasets")
    errors.require(isinstance(datasets, list) and datasets, "manifest: datasets must be a non-empty list")
    if not isinstance(datasets, list):
        return errors
    total = 0
    category_counts: Counter[str] = Counter()
    scenario_counts: Counter[str] = Counter()
    dataset_paths: set[str] = set()
    qrels_paths: set[str] = set()
    for dataset in datasets:
        if not isinstance(dataset, dict):
            errors.add("manifest: dataset entry must be an object")
            continue
        errors.require(
            dataset.get("kind") in {"retrieval", "answer"},
            "manifest: dataset kind must be retrieval or answer",
        )
        if isinstance(dataset.get("path"), str):
            dataset_paths.add(dataset["path"])
        if isinstance(dataset.get("qrels"), str):
            qrels_paths.add(dataset["qrels"])
        count, counts, scenarios = validate_dataset(
            dataset,
            fixtures,
            required_fields,
            set(allowed_categories),
            set(allowed_difficulties),
            set(properties),
            known_topic_ids,
            set(answer_required_fields),
            errors,
        )
        total += count
        category_counts.update(counts)
        scenario_counts.update(scenarios)
    minimum_cases = schema.get("minimum_cases")
    errors.require(isinstance(minimum_cases, int) and total >= minimum_cases, "fixture set does not meet minimum_cases")
    declared_categories = manifest.get("category_counts")
    errors.require(declared_categories == dict(sorted(category_counts.items())), "manifest: category_counts does not match records")
    errors.require(set(category_counts) == set(allowed_categories), "fixture set must cover every allowed category")
    errors.require(
        manifest.get("answer_scenario_counts") == dict(sorted(scenario_counts.items())),
        "manifest: answer_scenario_counts does not match answer records",
    )
    errors.require(
        set(scenario_counts) == set(answer_scenario_names),
        "answer fixture set must cover every required answer scenario",
    )
    listed_jsonl = {path.relative_to(fixtures).as_posix() for path in fixtures.rglob("*.jsonl")}
    listed_qrels = {path.relative_to(fixtures).as_posix() for path in fixtures.rglob("*.qrels")}
    errors.require(listed_jsonl == dataset_paths, "manifest must list every JSONL dataset exactly once")
    errors.require(listed_qrels == qrels_paths, "manifest must list every qrels file exactly once")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate offline evaluation fixtures")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--topics", type=Path, default=DEFAULT_TOPICS)
    args = parser.parse_args()
    errors = validate(args.fixtures.resolve(), args.topics.resolve())
    if errors.items:
        print("Evaluation fixture validation failed:", file=sys.stderr)
        for error in errors.items:
            print(f"- {error}", file=sys.stderr)
        return 1
    manifest = json.loads((args.fixtures / "manifest.json").read_text(encoding="utf-8"))
    total = sum(dataset["case_count"] for dataset in manifest["datasets"])
    print(f"Evaluation fixtures valid: {total} cases, version {manifest['fixture_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
