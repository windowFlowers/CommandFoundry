from __future__ import annotations

from app.evaluation_gate import compare_reports, redact_report, validate_redacted_report


def report(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "profile": "default",
        "quality": 95.0,
        "p95_ms": 100.0,
        "cost": 2.0,
        "invalid_citations": 0,
        "unresolved_placeholders": 0,
        "credential_leakage": 0,
        "injection_safety_flags": 0,
        "schema_errors": [],
        "revision": "source-v1",
        "expected_revision": "source-v1",
        "commit": "abc123",
        "expected_commit": "abc123",
        "metrics": {
            "cases": 1,
            "evaluated_cases": 1,
            "skipped_cases": 0,
            "passed_cases": 1,
            "error_cases": 0,
            "pass_rate": 1.0,
        },
        "cases": [{"case_id": "ok", "passed": True}],
    }
    value.update(overrides)
    return value


def test_passing_reports_allow_boundary_tolerances() -> None:
    baseline = report()
    candidate = report(quality=94.0, p95_ms=120.0, cost=2.4)

    result = compare_reports(candidate, baseline)

    assert result.passed
    assert result.comparable
    assert result.to_dict()["status"] == "pass"


def test_tolerance_regressions_fail_release() -> None:
    result = compare_reports(report(quality=93.9, p95_ms=121.0, cost=2.41), report())

    assert result.status == "fail"
    assert {issue.gate for issue in result.tolerance_failures} == {"quality", "p95", "cost"}


def test_fractional_quality_uses_a_one_percentage_point_tolerance() -> None:
    baseline = report(quality_score=0.95)
    baseline.pop("quality")
    candidate = report(quality_score=0.94)
    candidate.pop("quality")

    assert compare_reports(candidate, baseline).passed
    candidate["quality_score"] = 0.939
    assert compare_reports(candidate, baseline).tolerance_failures[0].gate == "quality"


def test_hard_gates_cover_reported_safety_findings() -> None:
    result = compare_reports(
        report(
            invalid_citations=["cit-9"],
            unresolved_placeholders=1,
            credential_leakage=True,
            injection_safety_flags=["prompt-injection"],
            revision_mismatch=True,
            expected_commit="def456",
        )
    )

    assert result.status == "fail"
    assert {issue.gate for issue in result.hard_failures} == {
        "invalid_citations",
        "unresolved_placeholders",
        "credential_leakage",
        "injection_safety_flags",
        "revision_mismatch",
        "commit_mismatch",
    }


def test_schema_errors_are_hard_release_failures() -> None:
    result = compare_reports(report(schema_errors=["missing citation_id"]))

    assert result.status == "fail"
    assert result.schema_errors[0].gate == "schema_errors"


def test_non_object_report_is_a_schema_failure() -> None:
    result = compare_reports(["not", "a", "report"])

    assert result.status == "fail"
    assert result.schema_errors[0].gate == "schema"


def test_profile_mismatch_is_not_comparable() -> None:
    result = compare_reports(report(profile="team-a"), report(profile="team-b"))

    assert result.status == "not_comparable"
    assert not result.comparable


def test_hard_failure_takes_precedence_over_profile_mismatch() -> None:
    result = compare_reports(report(profile="team-a", invalid_citations=1), report(profile="team-b"))

    assert result.status == "fail"
    assert result.hard_failures[0].gate == "invalid_citations"


def test_redaction_and_validation_do_not_expose_credentials() -> None:
    unsafe = report(log="Authorization: Bearer this-is-a-long-real-token", api_key="sk-proj-abcdefghijklmnopqrstuvwxyz")

    assert validate_redacted_report(unsafe) == ["$.log", "$.api_key"]
    safe = redact_report(unsafe)
    assert validate_redacted_report(safe) == []
    assert safe["api_key"] == "[REDACTED]"
    assert "this-is-a-long-real-token" not in safe["log"]
    token_report = redact_report({"prompt_tokens": 12, "completion_tokens": 8})
    assert token_report == {"prompt_tokens": 12, "completion_tokens": 8}


def test_unredacted_credential_pattern_blocks_release() -> None:
    result = compare_reports(report(details={"note": "token=abcdefghijklmnop"}))

    assert result.status == "fail"
    assert result.hard_failures[-1].gate == "credential_leakage"


def test_incorrect_refusal_is_a_hard_gate() -> None:
    result = compare_reports(report(refusal_errors=["unknown-1"]))

    assert result.status == "fail"
    assert result.hard_failures[0].gate == "refusal_errors"


def test_fractional_quality_uses_one_percentage_point_not_one_whole_point() -> None:
    baseline = report(quality=0.95)
    candidate = report(quality=0.939)

    result = compare_reports(candidate, baseline)

    assert result.status == "fail"
    assert result.tolerance_failures[0].gate == "quality"


def test_first_baseline_rejects_failed_or_empty_evaluation() -> None:
    failed = report(
        quality=0.0,
        metrics={
            "cases": 1,
            "evaluated_cases": 1,
            "skipped_cases": 0,
            "passed_cases": 0,
            "error_cases": 0,
            "pass_rate": 0.0,
        },
        cases=[{"case_id": "broken", "passed": False}],
        failed_case_ids=["broken"],
    )
    result = compare_reports(failed)
    assert result.status == "fail"
    assert result.schema_errors

    empty = report(
        quality=0.0,
        metrics={
            "cases": 0,
            "evaluated_cases": 0,
            "skipped_cases": 0,
            "passed_cases": 0,
            "error_cases": 0,
            "pass_rate": 0.0,
        },
        cases=[],
    )
    assert compare_reports(empty).status == "fail"

    partial = report(
        quality=0.9,
        metrics={
            "cases": 10,
            "evaluated_cases": 10,
            "skipped_cases": 0,
            "passed_cases": 9,
            "error_cases": 0,
            "pass_rate": 0.9,
        },
        cases=[{"case_id": f"case-{index}", "passed": index < 9} for index in range(10)],
        failed_case_ids=["case-9"],
    )
    partial_result = compare_reports(partial)
    assert partial_result.status == "fail"
    assert any(issue.gate == "failed_cases_without_baseline" for issue in partial_result.hard_failures)


def test_execution_errors_are_hard_failures_even_with_a_baseline() -> None:
    candidate = report(
        metrics={
            "cases": 1,
            "evaluated_cases": 1,
            "skipped_cases": 0,
            "passed_cases": 1,
            "error_cases": 1,
            "pass_rate": 1.0,
        },
        cases=[{"case_id": "ok", "passed": True}],
    )
    result = compare_reports(candidate, report())
    assert result.status == "fail"
    assert any(issue.gate == "execution_errors" for issue in result.hard_failures)
