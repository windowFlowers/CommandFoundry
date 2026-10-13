"""Regression tests for release evidence before introducing tool loops."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest

from app.evaluation import CaseResult, EvaluationCase, ModelCallObservation, RunManifest, build_report
from app.evaluation_gate import compare_reports


@pytest.mark.parametrize("reason", ["timeout", "rate_limit", "authentication", "provider_error", "invalid_json"])
def test_correct_fallback_does_not_hide_provider_failure(reason):
    result = CaseResult(case_id="case", passed=True, answer_relevance=1, fallback_reason=reason)
    report = build_report(RunManifest(mode="model"), [result]).model_dump(mode="json")
    assert report["metrics"]["passed_cases"] == 1
    assert report["metrics"]["provider_failure_cases"] == 1
    assert not compare_reports(report).passed


def test_earlier_turn_timeout_survives_successful_final_answer():
    result = CaseResult(case_id="multi", passed=True, model_calls=[
        ModelCallObservation(attempted=True, fallback_reason="timeout"),
        ModelCallObservation(attempted=True, total_tokens=100),
        ModelCallObservation(attempted=False, fallback_reason="ungrounded"),
    ])
    report = build_report(RunManifest(mode="model"), [result]).model_dump(mode="json")
    assert report["metrics"]["model_attempts"] == 2
    assert report["metrics"]["model_successes"] == 1
    assert report["metrics"]["timeout_turns"] == 1
    assert report["metrics"]["local_turns"] == 1
    # Even forged summary counts cannot override per-turn evidence.
    report["metrics"]["provider_failure_cases"] = 0
    assert not compare_reports(report).passed


def test_expected_local_refusal_is_not_a_provider_error():
    result = CaseResult(case_id="refusal", passed=True, model_calls=[
        ModelCallObservation(attempted=False, fallback_reason="ungrounded"),
    ])
    report = build_report(RunManifest(mode="model"), [result]).model_dump(mode="json")
    assert report["metrics"]["model_successes"] == 0
    assert report["metrics"]["provider_failure_cases"] == 0
    assert compare_reports(report).passed  # smoke gate; full release requires cloud evidence


def test_sse_runner_records_all_turns_without_response_text():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_evaluation.py"
    spec = spec_from_file_location("agent_eval_runner", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        reason = '"timeout"' if count == 1 else "null"
        return httpx.Response(200, text=(
            'event: answer\ndata: {"conversation_id":"c","answer":'
            '{"summary":"sensitive output excluded","generation":'
            '{"attempted":true,"fallback_reason":' + reason + '}}}\n\n'
        ))

    case = EvaluationCase(id="multi", query="second", turns=[
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ignored"},
        {"role": "user", "content": "second"},
    ])
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        module._post_case_turns(client, case, calls)
    assert [call.fallback_reason for call in calls] == ["timeout", None]
    assert "sensitive output" not in str(calls)


def _runner():
    spec = spec_from_file_location("agent_eval_runner", Path(__file__).resolve().parents[2] / "scripts/run_evaluation.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_external_smoke_suite_fingerprints_still_supported(tmp_path):
    suite = tmp_path / "external.json"
    suite.write_text('[]', encoding="utf-8")
    fingerprints = _runner()._dataset_fingerprints([suite])
    assert list(fingerprints) == ["<external-0>/external.json"]
    assert str(tmp_path) not in str(fingerprints)


def test_failed_later_turn_keeps_earlier_tokens_and_timeout_becomes_bad_case():
    runner = _runner()
    earlier = ModelCallObservation(attempted=True, prompt_tokens=20, completion_tokens=10, total_tokens=30)
    failed = CaseResult(case_id="multi", passed=False, fallback_reason="provider_error")
    assert runner._with_model_observations(failed, [earlier]).total_tokens == 30
    answer_passed = CaseResult(case_id="timeout", passed=True)
    result = runner._with_model_observations(answer_passed, [ModelCallObservation(attempted=True, fallback_reason="timeout")])
    assert not result.passed
    assert result.failure_reasons == ["provider failure caused local fallback"]
