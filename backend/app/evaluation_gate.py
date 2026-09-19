"""Release gates for machine-readable evaluation reports.

The evaluator deliberately stays independent from the runner so a report can
be checked in CI, a release script, or a local review without importing any of
the application stack.  Reports are plain mappings; accepting a few common
field aliases keeps the gate usable while the evaluator evolves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import math
import re
from typing import Any, Literal


QUALITY_DROP_TOLERANCE_PP = 1.0
P95_INCREASE_TOLERANCE = 0.20
COST_INCREASE_TOLERANCE = 0.20

_MISSING = object()
_REDACTED = "[REDACTED]"

_METRIC_ALIASES = {
    "quality": ("quality", "quality_score", "quality_percent"),
    "p95": ("p95", "p95_ms", "latency_p95", "latency_p95_ms"),
    "cost": ("cost", "cost_usd", "cost_per_case"),
}
_HARD_GATE_ALIASES = {
    "invalid_citations": ("invalid_citations", "citation_errors"),
    "unresolved_placeholders": ("unresolved_placeholders", "placeholder_errors"),
    "credential_leakage": ("credential_leakage", "credential_leaks", "secret_leaks"),
    "injection_safety_flags": (
        "injection_safety_flags",
        "injection_flags",
        "prompt_injection_flags",
        "high_risk_safety_flags",
        "high_risk_flags",
    ),
    "schema_errors": ("schema_errors", "report_schema_errors"),
    "refusal_errors": ("refusal_errors", "no_answer_failures", "incorrect_refusals"),
    "revision_mismatch": ("revision_mismatch", "source_revision_mismatch"),
    "commit_mismatch": ("commit_mismatch", "release_commit_mismatch"),
}
_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret|private[_-]?key)", re.I)
_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r"\b(?:sk-(?:proj-)?|[rps]k_)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[=:]\s*['\"]?[A-Za-z0-9._~+/=-]{12,}"),
)


@dataclass(frozen=True)
class GateIssue:
    """One release-gate finding, suitable for JSON serialization."""

    gate: str
    message: str
    kind: Literal["hard", "tolerance", "schema"]


@dataclass(frozen=True)
class ReleaseGateResult:
    """The result of comparing a candidate report to an optional baseline."""

    status: Literal["pass", "fail", "not_comparable"]
    comparable: bool
    hard_failures: tuple[GateIssue, ...] = ()
    tolerance_failures: tuple[GateIssue, ...] = ()
    schema_errors: tuple[GateIssue, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "comparable": self.comparable,
            "passed": self.passed,
            "hard_failures": [asdict(issue) for issue in self.hard_failures],
            "tolerance_failures": [asdict(issue) for issue in self.tolerance_failures],
            "schema_errors": [asdict(issue) for issue in self.schema_errors],
        }


def _value(report: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in report:
            return report[name]
    return _MISSING


def _truthy_finding(value: Any) -> bool:
    """Return whether a report field represents at least one finding."""

    if value is _MISSING or value is None or value is False:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (str, bytes, Mapping, Sequence)):
        return len(value) > 0
    return bool(value)


def _numeric_metric(report: Mapping[str, Any], metric: str) -> tuple[float | None, GateIssue | None]:
    value = _value(report, _METRIC_ALIASES[metric])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None, GateIssue(metric, f"missing or invalid numeric {metric} metric", "schema")
    if float(value) < 0:
        return None, GateIssue(metric, f"{metric} metric cannot be negative", "schema")
    return float(value), None


def _report_profile(report: Mapping[str, Any]) -> str | None:
    profile = _value(report, ("profile", "profile_id", "evaluation_profile"))
    return profile.strip() if isinstance(profile, str) and profile.strip() else None


def _identity_mismatch(report: Mapping[str, Any], kind: str) -> bool:
    """Recognize explicit mismatch flags and expected/actual identity pairs."""

    if _truthy_finding(_value(report, _HARD_GATE_ALIASES[f"{kind}_mismatch"])):
        return True
    actual = _value(report, (kind, f"evaluated_{kind}", f"actual_{kind}"))
    expected = _value(report, (f"expected_{kind}", f"release_{kind}", f"target_{kind}"))
    return actual is not _MISSING and expected is not _MISSING and str(actual) != str(expected)


def _quality_tolerance(baseline: float, candidate: float) -> float:
    """Return one percentage point in the report's quality score unit.

    Evaluators commonly express quality either as 95.3 (percent) or 0.953
    (fraction).  Supporting both prevents a nominal one-point gate from
    silently becoming a 100-point allowance for fractional scores.
    """

    return 0.01 if 0 <= baseline <= 1 and 0 <= candidate <= 1 else QUALITY_DROP_TOLERANCE_PP


def _walk_strings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, Mapping):
        return [item for key, child in value.items() for item in _walk_strings(child, f"{path}.{key}")]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for index, child in enumerate(value) for item in _walk_strings(child, f"{path}[{index}]")]
    return []


def find_credential_leaks(report: Mapping[str, Any]) -> list[str]:
    """Return report paths containing likely unredacted credentials.

    This is intentionally pattern-based.  A secret-shaped value in a report is
    a release blocker even if the evaluator forgot to increment its own leak
    counter.
    """

    leaks: list[str] = []
    for path, text in _walk_strings(report):
        if any(pattern.search(text) for pattern in _CREDENTIAL_PATTERNS):
            leaks.append(path)
    return leaks


def redact_report(value: Any) -> Any:
    """Return a deep, display-safe copy of an evaluation report.

    Sensitive-named fields are replaced wholesale; known credential shapes in
    otherwise useful text are replaced in place.
    """

    if isinstance(value, Mapping):
        return {
            str(key): (
                _REDACTED
                if _SENSITIVE_KEY.search(str(key))
                and str(key).casefold() not in {"prompt_tokens", "completion_tokens", "total_tokens"}
                else redact_report(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_report(child) for child in value]
    if isinstance(value, tuple):
        return tuple(redact_report(child) for child in value)
    if isinstance(value, str):
        redacted = value
        for pattern in _CREDENTIAL_PATTERNS:
            redacted = pattern.sub(_REDACTED, redacted)
        return redacted
    return value


def validate_redacted_report(report: Mapping[str, Any]) -> list[str]:
    """Return credential-bearing paths; an empty result is safe to display."""

    return find_credential_leaks(report)


def validate_report(report: Any, *, require_metrics: bool = True) -> list[GateIssue]:
    """Validate the report shape before it participates in a release gate."""

    if not isinstance(report, Mapping):
        return [GateIssue("schema", "report must be an object", "schema")]

    errors: list[GateIssue] = []
    if not _report_profile(report):
        errors.append(GateIssue("profile", "missing non-empty evaluation profile", "schema"))
    if require_metrics:
        for metric in _METRIC_ALIASES:
            _, error = _numeric_metric(report, metric)
            if error:
                errors.append(error)
        metrics = report.get("metrics")
        if not isinstance(metrics, Mapping):
            errors.append(GateIssue("metrics", "report is missing its metrics object", "schema"))
        else:
            integer_fields = ("cases", "evaluated_cases", "skipped_cases", "passed_cases", "error_cases")
            values: dict[str, int] = {}
            for field in integer_fields:
                value = metrics.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    errors.append(GateIssue("metrics", f"metrics.{field} must be a non-negative integer", "schema"))
                else:
                    values[field] = value
            if len(values) == len(integer_fields):
                cases = values["cases"]
                evaluated = values["evaluated_cases"]
                skipped = values["skipped_cases"]
                passed = values["passed_cases"]
                if cases < 1:
                    errors.append(GateIssue("metrics", "metrics.cases must be at least one", "schema"))
                if evaluated < 1:
                    errors.append(GateIssue("metrics", "metrics.evaluated_cases must be at least one", "schema"))
                if evaluated > cases:
                    errors.append(GateIssue("metrics", "evaluated_cases cannot exceed cases", "schema"))
                if passed > evaluated:
                    errors.append(GateIssue("metrics", "passed_cases cannot exceed evaluated_cases", "schema"))
                if skipped != cases - evaluated:
                    errors.append(GateIssue("metrics", "skipped_cases must equal cases - evaluated_cases", "schema"))
                if values["error_cases"] > evaluated:
                    errors.append(GateIssue("metrics", "error_cases cannot exceed evaluated_cases", "schema"))
                if passed == 0:
                    errors.append(GateIssue("metrics", "evaluation has no passing cases", "schema"))
                cases_list = report.get("cases")
                if not isinstance(cases_list, list):
                    errors.append(GateIssue("metrics", "report is missing its serialized cases list", "schema"))
                elif len(cases_list) != cases:
                    errors.append(GateIssue("metrics", "metrics.cases does not match the serialized cases list", "schema"))
                failed_case_ids = report.get("failed_case_ids")
                if failed_case_ids is not None and (
                    not isinstance(failed_case_ids, list)
                    or not all(isinstance(item, str) for item in failed_case_ids)
                ):
                    errors.append(GateIssue("metrics", "failed_case_ids must be an array of strings", "schema"))
                pass_rate = metrics.get("pass_rate")
                if isinstance(pass_rate, (int, float)) and not isinstance(pass_rate, bool):
                    if not math.isclose(float(pass_rate), passed / evaluated if evaluated else 0.0, rel_tol=1e-6, abs_tol=1e-9):
                        errors.append(GateIssue("metrics", "metrics.pass_rate is inconsistent with case counts", "schema"))
    if _truthy_finding(_value(report, _HARD_GATE_ALIASES["schema_errors"])):
        errors.append(GateIssue("schema_errors", "evaluator reported schema errors", "schema"))
    return errors


def _execution_failures(report: Mapping[str, Any]) -> list[GateIssue]:
    """Return failures that mean the evaluator did not complete cleanly.

    A lower quality score is a comparable regression and is handled by the
    baseline tolerance.  A request/runner error, an empty run, or an omitted
    first-baseline case list is different: it means there is no trustworthy
    result to compare and must never be released silently.
    """

    failures: list[GateIssue] = []
    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        return failures
    error_cases = metrics.get("error_cases")
    error_rate = metrics.get("error_rate")
    if isinstance(error_cases, (int, float)) and not isinstance(error_cases, bool) and error_cases > 0:
        failures.append(GateIssue("execution_errors", f"evaluation reported {error_cases:g} error cases", "hard"))
    elif isinstance(error_rate, (int, float)) and not isinstance(error_rate, bool) and error_rate > 0:
        failures.append(GateIssue("execution_errors", f"evaluation reported an error rate of {error_rate:g}", "hard"))
    evaluated = metrics.get("evaluated_cases")
    if isinstance(evaluated, (int, float)) and not isinstance(evaluated, bool) and evaluated <= 0:
        failures.append(GateIssue("empty_evaluation", "evaluation did not execute any cases", "hard"))
    return failures


def compare_reports(candidate: Any, baseline: Any | None = None) -> ReleaseGateResult:
    """Apply hard safety gates and baseline tolerances to an evaluation report.

    Quality may fall by at most one percentage point.  p95 latency and cost may
    each increase by at most 20 percent.  A different profile is deliberately
    ``not_comparable`` rather than a failed regression; hard failures in the
    candidate still take precedence because they must never be released.
    """

    candidate_errors = validate_report(candidate)
    hard: list[GateIssue] = []
    schema = list(candidate_errors)
    if not isinstance(candidate, Mapping):
        return ReleaseGateResult("fail", False, (), (), tuple(schema))

    hard.extend(_execution_failures(candidate))
    # A first baseline is an explicit approval point, but it still must be a
    # clean run. Once a baseline exists, ordinary failed cases are evaluated by
    # the configured quality tolerance instead of becoming an unconditional
    # hard failure.
    if baseline is None:
        failed_case_ids = candidate.get("failed_case_ids")
        if isinstance(failed_case_ids, Sequence) and not isinstance(failed_case_ids, (str, bytes)) and failed_case_ids:
            hard.append(
                GateIssue(
                    "failed_cases_without_baseline",
                    f"candidate has {len(failed_case_ids)} failed cases and no baseline",
                    "hard",
                )
            )
        quality_value, _ = _numeric_metric(candidate, "quality")
        metrics = candidate.get("metrics")
        if quality_value == 0 and isinstance(metrics, Mapping) and metrics.get("evaluated_cases", 0):
            hard.append(GateIssue("zero_quality", "candidate quality is zero", "hard"))

    for gate, aliases in _HARD_GATE_ALIASES.items():
        if gate == "schema_errors":
            continue
        if _truthy_finding(_value(candidate, aliases)):
            hard.append(GateIssue(gate, f"candidate reported {gate.replace('_', ' ')}", "hard"))
    for kind in ("revision", "commit"):
        if _identity_mismatch(candidate, kind):
            hard.append(GateIssue(f"{kind}_mismatch", f"candidate {kind} does not match its expected {kind}", "hard"))
    leaks = find_credential_leaks(candidate)
    if leaks:
        hard.append(GateIssue("credential_leakage", f"unredacted credential-like data at {', '.join(leaks)}", "hard"))

    if schema or hard:
        return ReleaseGateResult("fail", False, tuple(hard), (), tuple(schema))
    if baseline is None:
        return ReleaseGateResult("pass", True)

    baseline_errors = validate_report(baseline)
    if baseline_errors:
        return ReleaseGateResult("fail", False, (), (), tuple(baseline_errors))
    candidate_profile = _report_profile(candidate)
    baseline_profile = _report_profile(baseline)
    if candidate_profile != baseline_profile:
        return ReleaseGateResult("not_comparable", False)

    tolerance: list[GateIssue] = []
    candidate_quality, _ = _numeric_metric(candidate, "quality")
    baseline_quality, _ = _numeric_metric(baseline, "quality")
    assert candidate_quality is not None and baseline_quality is not None
    quality_tolerance = _quality_tolerance(baseline_quality, candidate_quality)
    if candidate_quality < baseline_quality - quality_tolerance:
        tolerance.append(
            GateIssue("quality", f"quality fell {baseline_quality - candidate_quality:.3g}pp (limit {quality_tolerance:.3g}pp)", "tolerance")
        )
    for metric in ("p95", "cost"):
        candidate_value, _ = _numeric_metric(candidate, metric)
        baseline_value, _ = _numeric_metric(baseline, metric)
        assert candidate_value is not None and baseline_value is not None
        allowed_increase = P95_INCREASE_TOLERANCE if metric == "p95" else COST_INCREASE_TOLERANCE
        if baseline_value == 0:
            exceeded = candidate_value > 0
        else:
            exceeded = candidate_value > baseline_value * (1 + allowed_increase)
        if exceeded:
            increase = "new non-zero value" if baseline_value == 0 else f"{(candidate_value / baseline_value - 1) * 100:.3g}%"
            tolerance.append(GateIssue(metric, f"{metric} increased {increase} (limit {allowed_increase * 100:g}%)", "tolerance"))
    if tolerance:
        return ReleaseGateResult("fail", True, (), tuple(tolerance))
    return ReleaseGateResult("pass", True)


def evaluate_release_gate(candidate: Any, baseline: Any | None = None) -> ReleaseGateResult:
    """Named entry point for release scripts; equivalent to :func:`compare_reports`."""

    return compare_reports(candidate, baseline)


__all__ = [
    "COST_INCREASE_TOLERANCE",
    "P95_INCREASE_TOLERANCE",
    "QUALITY_DROP_TOLERANCE_PP",
    "GateIssue",
    "ReleaseGateResult",
    "compare_reports",
    "evaluate_release_gate",
    "find_credential_leaks",
    "redact_report",
    "validate_redacted_report",
    "validate_report",
]
