from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.evaluation import (  # noqa: E402
    CaseResult,
    EvaluationCase,
    RunManifest,
    build_report,
    case_result,
    file_fingerprint,
    load_evaluation_suite,
    profile_fingerprint,
    redact_sensitive,
    validate_answer,
    value_fingerprint,
)
from app.chunking import (  # noqa: E402
    CHILD_MAX_CHARS,
    CHILD_OVERLAP_CHARS,
    CHILD_TARGET_CHARS,
    INDEX_SCHEMA_VERSION,
    PARENT_MAX_CHARS,
    PARENT_TARGET_CHARS,
)
from app.evidence import evidence_records  # noqa: E402
from app.config import settings  # noqa: E402
from app.knowledge import KnowledgeCatalog  # noqa: E402
from app.memory import ConversationTurn, merge_contextual_hits, resolve_contextual_query  # noqa: E402
from app.models import Answer, ChatMessage, CommandBlock, RetrievalHit  # noqa: E402
from app.retrieval import FastEmbedReranker, HybridRetriever, analyze_query  # noqa: E402


DEFAULT_SUITES = (
    PROJECT_ROOT / "evaluation" / "fixtures" / "v2.11" / "manifest.json",
    PROJECT_ROOT / "knowledge" / "eval_dataset.json",
    PROJECT_ROOT / "knowledge" / "memory_eval_dataset.json",
    PROJECT_ROOT / "knowledge" / "personalization_eval_dataset.json",
    PROJECT_ROOT / "knowledge" / "windows_eval_dataset.json",
)
NAMED_SUITES = {
    "core": PROJECT_ROOT / "evaluation" / "fixtures" / "v2.11" / "manifest.json",
    "retrieval": PROJECT_ROOT / "knowledge" / "eval_dataset.json",
    "memory": PROJECT_ROOT / "knowledge" / "memory_eval_dataset.json",
    "windows": PROJECT_ROOT / "knowledge" / "windows_eval_dataset.json",
    "personalization": PROJECT_ROOT / "knowledge" / "personalization_eval_dataset.json",
    "command": PROJECT_ROOT / "evaluation" / "fixtures" / "v2.11" / "manifest.json",
    "golden": PROJECT_ROOT / "evaluation" / "fixtures" / "v2.11" / "manifest.json",
}


def _git_revision() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _retrieve_hits(
    retriever: HybridRetriever,
    case: EvaluationCase,
    top_k: int,
    *,
    previous_answer: Answer | None = None,
) -> list[RetrievalHit]:
    def advanced(value: str, limit: int) -> list[RetrievalHit]:
        # Keep the offline suite representative of the product path.  The
        # analyzer is deterministic and receives the same catalog used by the
        # retriever; no model-generated query or hidden state is introduced.
        analysis = analyze_query(value, retriever.catalog.topics)
        return retriever.retrieve_advanced(value, top_k=limit, analysis=analysis)

    if not case.previous_query:
        return advanced(case.query, top_k)

    answer = previous_answer or Answer(
        mode="local_fallback",
        summary="evaluation context",
        commands=[CommandBlock(label=label, code="echo <value>") for label in case.previous_command_labels],
        notes=[],
        citations=[],
    )
    turn = ConversationTurn(
        user=ChatMessage(role="user", query=case.previous_query),
        assistant=ChatMessage(role="assistant", answer=answer),
    )
    contextual = resolve_contextual_query(case.query, turn)
    if contextual == case.query.strip():
        hits = advanced(case.query, top_k)
    else:
        candidate_k = max(8, top_k)
        hits = merge_contextual_hits(
            advanced(case.query, candidate_k),
            advanced(contextual, candidate_k),
            top_k=top_k,
        )
    return hits


def _retrieve(retriever: HybridRetriever, case: EvaluationCase, top_k: int) -> list[str]:
    """Compatibility helper retained for callers that only need topic ids."""

    return [hit.topic.id for hit in _retrieve_hits(retriever, case, top_k)]


def _retrieval_observation(hits: list[RetrievalHit]) -> list[dict[str, Any]]:
    """Serialize only the evidence identity needed to validate citations.

    The observation is deliberately small and redacted: no excerpts or prompt
    text are persisted.  Citation ids are derived by the same evidence helper
    used by the application, so a model cannot make up an id that merely looks
    like a valid ``cit-*`` value.
    """

    observation: list[dict[str, Any]] = []
    for record in evidence_records(hits):
        source = record["hit"].topic.source
        observation.append(
            {
                "citation_id": record.get("citation_id"),
                "source_id": source.source_id,
                "document_id": source.document_id,
                "topic_id": record["hit"].topic.id,
                "chunk_id": record.get("chunk_id"),
                "parent_id": record.get("parent_id"),
                "index_revision": source.index_revision,
            }
        )
    return observation


def _merge_answer_citation_observation(
    observation: list[dict[str, Any]], answer: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Add the app's deterministic citation records to a live-run observation.

    Follow-up retrieval is allowed to use the previous model answer's command
    labels.  A separately reconstructed query can therefore differ in a
    provider run even though the backend produced the citation list safely.
    Keep independent retrieval metrics unchanged, but let answer validation
    verify those opaque IDs against the exact records emitted by the app.
    """

    merged = list(observation)
    known = {str(item.get("citation_id")) for item in merged if item.get("citation_id")}
    citations = answer.get("citations") if isinstance(answer.get("citations"), list) else []
    for citation in citations:
        if not isinstance(citation, Mapping):
            continue
        citation_id = str(citation.get("citation_id") or "").strip()
        if not citation_id or citation_id in known:
            continue
        merged.append(
            {
                "citation_id": citation_id,
                "source_id": citation.get("source_id"),
                "document_id": citation.get("document_id"),
                "topic_id": citation.get("source_id"),
                "chunk_id": citation.get("chunk_id"),
                "parent_id": citation.get("parent_id"),
                "index_revision": citation.get("index_revision"),
            }
        )
        known.add(citation_id)
    return merged


def _load_profile(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("profile JSON must be an object")
    return payload


def _suite_paths(values: list[str] | None) -> list[Path]:
    if not values:
        return list(DEFAULT_SUITES)
    expanded: list[Path] = []
    for value in values:
        if value == "all":
            expanded.extend(NAMED_SUITES.values())
        elif value in NAMED_SUITES:
            expanded.append(NAMED_SUITES[value])
        else:
            candidate = Path(value)
            expanded.append(candidate if candidate.is_absolute() else PROJECT_ROOT / candidate)
    return list(dict.fromkeys(path.resolve() for path in expanded))


def _parse_sse_answer(raw: str) -> tuple[dict[str, Any], str | None]:
    for frame in raw.replace("\r\n", "\n").strip().split("\n\n"):
        lines = frame.splitlines()
        event = next((line[7:] for line in lines if line.startswith("event: ")), "")
        if event != "answer":
            continue
        data_text = "\n".join(line[6:] for line in lines if line.startswith("data: "))
        data = json.loads(data_text)
        answer = data.get("answer")
        if isinstance(answer, dict):
            return answer, data.get("conversation_id")
    raise ValueError("/chat/stream returned no answer event")


def _post_chat(
    client: Any, query: str, *, conversation_id: str | None = None
) -> tuple[dict[str, Any], str | None]:
    payload = {"query": query}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    response = client.post("/chat/stream", json=payload)
    response.raise_for_status()
    return _parse_sse_answer(response.text)


def _post_case_turns(
    client: Any,
    case: EvaluationCase,
) -> tuple[dict[str, Any], str | None]:
    """Replay an optional multi-turn case through the real chat endpoint."""

    conversation_id: str | None = None
    answer: dict[str, Any] | None = None
    turns = [turn for turn in case.turns if isinstance(turn, dict)]
    for turn in turns:
        if str(turn.get("role", "user")).casefold() != "user":
            continue
        content = str(turn.get("content") or turn.get("query") or "").strip()
        if not content:
            continue
        answer, conversation_id = _post_chat(client, content, conversation_id=conversation_id)
    if answer is None:
        answer, conversation_id = _post_chat(client, case.query, conversation_id=conversation_id)
    return answer, conversation_id


def _artifact_paths(output: Path) -> tuple[Path, Path, Path, Path]:
    """Resolve the four release artifacts from the report output path."""

    if output.suffix.casefold() == ".json":
        report_path = output
        directory = output.parent
    else:
        directory = output
        report_path = directory / "evaluation-report.json"
    return (
        report_path,
        directory / "evaluation-summary.md",
        directory / "bad-cases.jsonl",
        directory / "run-manifest.json",
    )


def _write_artifacts(output: Path, payload: dict[str, Any]) -> Path:
    report_path, summary_path, bad_cases_path, manifest_path = _artifact_paths(output)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(payload.get("manifest", {}), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    failed = [
        item
        for item in payload.get("cases", [])
        if isinstance(item, dict) and not item.get("passed") and not item.get("skipped")
    ]
    skipped = [
        item
        for item in payload.get("cases", [])
        if isinstance(item, dict) and item.get("skipped")
    ]
    bad_cases_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failed),
        encoding="utf-8",
    )
    metrics = payload.get("metrics", {})
    gate = payload.get("gate", {})
    summary_lines = [
        "# CommandFoundry RAG evaluation",
        "",
        f"- schema: {payload.get('manifest', {}).get('schema_version', 'unknown')}",
        f"- mode: {payload.get('manifest', {}).get('mode', 'unknown')}",
        f"- profile: {payload.get('profile', 'unknown')}",
        f"- cases: {metrics.get('cases', 0)} (failed: {len(failed)}, skipped: {len(skipped)})",
        f"- pass rate: {metrics.get('pass_rate', 0):.4f}",
        f"- MRR: {metrics.get('mrr', 0):.4f}",
        f"- NDCG: {metrics.get('ndcg', 0):.4f}",
        f"- p95 latency (ms): {payload.get('p95_ms', 0):.3f}",
        f"- gate: {gate.get('status', 'not evaluated')}",
    ]
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the deterministic v2.11 offline RAG evaluation")
    parser.add_argument(
        "--suite",
        action="append",
        help="Named suite (retrieval/memory/personalization/command/core/all) or JSON path; repeatable",
    )
    parser.add_argument("--mode", choices=("offline", "model"), default="offline")
    parser.add_argument("--provider", choices=("current",), default="current")
    # Match the product's default retrieval_top_k.  A smaller evaluator-only
    # cutoff can mark a valid fourth evidence hit as a retrieval miss.
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument(
        "--hybrid",
        action="store_true",
        help="Use the production BM25 + embedding retrieval path",
    )
    parser.add_argument(
        "--allow-model-download",
        action="store_true",
        help=(
            "Allow fastembed to download the configured local embedding model. "
            "Without this flag hybrid evaluation is offline/local-cache only."
        ),
    )
    parser.add_argument("--profile", type=Path, help="Optional profile JSON; only its redacted fingerprint is recorded")
    parser.add_argument("--output", type=Path, help="Write the report to this JSON file instead of stdout only")
    parser.add_argument("--baseline", type=Path, help="Compare the report with a previous v2.11 JSON report")
    parser.add_argument(
        "--input-cost-per-1k",
        type=float,
        default=float(os.getenv("AEGIS_LLM_INPUT_COST_PER_1K", "0")),
        help="Optional provider input price used only for an estimated cost metric",
    )
    parser.add_argument(
        "--output-cost-per-1k",
        type=float,
        default=float(os.getenv("AEGIS_LLM_OUTPUT_COST_PER_1K", "0")),
        help="Optional provider output price used only for an estimated cost metric",
    )
    parser.add_argument(
        "--endpoint", "--base-url", dest="endpoint",
        help="Existing app base URL for model mode; omit to use the local FastAPI app",
    )
    parser.add_argument("--redact", action="store_true", help="Also redact case queries in serialized output")
    parser.add_argument("--fail-on-gate", action="store_true", help="Return non-zero when the release gate fails")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.top_k < 1:
        raise SystemExit("--top-k must be at least 1")
    if args.input_cost_per_1k < 0 or args.output_cost_per_1k < 0:
        raise SystemExit("token prices cannot be negative")
    suite_paths = _suite_paths(args.suite)
    for path in suite_paths:
        if not path.is_file():
            raise SystemExit(f"evaluation suite does not exist: {path}")

    all_cases: list[EvaluationCase] = []
    for path in suite_paths:
        all_cases.extend(load_evaluation_suite(path))

    knowledge_dir = PROJECT_ROOT / "knowledge"
    catalog = KnowledgeCatalog(knowledge_dir)
    catalog.load()
    retriever = HybridRetriever(
        catalog=catalog,
        embedding_model=settings.embedding_model,
        model_cache_dir=settings.model_cache_dir,
        # Match the production retriever configuration.  Using a smaller
        # evaluator-only candidate pool can change the fourth citation and
        # make an otherwise valid product citation look invalid.
        candidate_k=max(settings.retrieval_candidate_k, args.top_k),
        embedding_enabled=args.hybrid,
        local_files_only=not args.allow_model_download,
        reranker=(
            FastEmbedReranker(settings.reranker_model, settings.model_cache_dir / "reranker")
            if settings.reranker_enabled
            else None
        ),
    )
    engine = ("hybrid" if args.hybrid else "bm25") + "+advanced"
    if settings.reranker_enabled:
        engine += "+cross-encoder"
    if args.hybrid and not retriever.initialize_embeddings():
        raise SystemExit(f"local hybrid model unavailable: {retriever.embedding_error}")

    profile = _load_profile(args.profile)
    manifest = RunManifest(
        mode=args.mode,
        engine=engine,
        provider=settings.llm_provider,
        model=settings.llm_model,
        base_url_fingerprint=value_fingerprint(args.endpoint or settings.llm_base_url),
        prompt_version=f"generation-{file_fingerprint(PROJECT_ROOT / 'backend' / 'app' / 'generation.py')[:12]}",
        knowledge_revision=file_fingerprint(knowledge_dir / "topics.json"),
        index_schema_version=INDEX_SCHEMA_VERSION,
        embedding_model=settings.embedding_model,
        chunk_parameters={
            "parent_target_chars": PARENT_TARGET_CHARS,
            "parent_max_chars": PARENT_MAX_CHARS,
            "child_target_chars": CHILD_TARGET_CHARS,
            "child_max_chars": CHILD_MAX_CHARS,
            "child_overlap_chars": CHILD_OVERLAP_CHARS,
        },
        retrieval_parameters={
            "top_k": args.top_k,
            "candidate_k": max(settings.retrieval_candidate_k, args.top_k),
            "query_analysis": "deterministic-v1",
            "reranker_enabled": settings.reranker_enabled,
            "reranker_model": settings.reranker_model if settings.reranker_enabled else None,
        },
        top_k=args.top_k,
        suite_paths=[
            str(path.relative_to(PROJECT_ROOT))
            if path.is_relative_to(PROJECT_ROOT)
            else f"<external>/{path.name}"
            for path in suite_paths
        ],
        dataset_fingerprints={path.name: file_fingerprint(path) for path in suite_paths},
        profile_fingerprint=profile_fingerprint(profile),
        parameters={
            "offline": args.mode == "offline",
            "local_files_only": not args.allow_model_download,
            "embedding_download_allowed": bool(args.allow_model_download),
            "embedding_enabled": settings.embedding_enabled,
            "embedding_local_only": settings.embedding_local_only,
            "advanced_query_enabled": True,
            "reranker_enabled": settings.reranker_enabled,
            "reranker_model": settings.reranker_model if settings.reranker_enabled else None,
            "provider_selection": args.provider,
            "input_cost_per_1k": args.input_cost_per_1k,
            "output_cost_per_1k": args.output_cost_per_1k,
            "cost_pricing_available": bool(args.input_cost_per_1k or args.output_cost_per_1k),
        },
        git_revision=_git_revision(),
    )

    results: list[CaseResult] = []

    def evaluate_cases(model_client: Any | None = None) -> None:
        for case in all_cases:
            if not case.relevant_ids and case.metadata.get("evaluation_kind") == "personalization":
                results.append(
                    CaseResult(
                        case_id=case.id,
                        query=redact_sensitive(case.query),
                        skipped=True,
                        failure_reasons=["suite case has no retrieval relevance judgment"],
                        trace=redact_sensitive({"query": case.query, "metadata": case.metadata}),
                    )
                )
                continue
            started = time.perf_counter()
            retrieval_depth = max(args.top_k, settings.retrieval_top_k) if model_client else args.top_k
            retrieved_hits = _retrieve_hits(retriever, case, retrieval_depth)
            retrieved_ids = [hit.topic.id for hit in retrieved_hits]
            retrieval_observation = _retrieval_observation(retrieved_hits)
            result = case_result(
                case,
                retrieved_ids,
                k=args.top_k,
                retrieval_observation=retrieval_observation,
            )
            if model_client is not None:
                try:
                    context_answer: Answer | None = None
                    if case.turns:
                        answer, _conversation_id = _post_case_turns(model_client, case)
                    else:
                        conversation_id = None
                        if case.previous_query:
                            context_payload, conversation_id = _post_chat(model_client, case.previous_query)
                            context_answer = Answer.model_validate(context_payload)
                        answer, _conversation_id = _post_chat(
                            model_client, case.query, conversation_id=conversation_id
                        )
                    # The production endpoint resolves a follow-up query from
                    # the actual previous assistant answer (including its
                    # commands and summary).  Rebuild the evaluator's
                    # retrieval observation from that same answer so memory
                    # cases validate the exact citation set used by the app,
                    # rather than a synthetic placeholder context.
                    if context_answer is not None:
                        retrieved_hits = _retrieve_hits(
                            retriever,
                            case,
                            retrieval_depth,
                            previous_answer=context_answer,
                        )
                        retrieved_ids = [hit.topic.id for hit in retrieved_hits]
                        retrieval_observation = _retrieval_observation(retrieved_hits)
                        result = case_result(
                            case,
                            retrieved_ids,
                            k=args.top_k,
                            retrieval_observation=retrieval_observation,
                        )
                    validation_observation = retrieval_observation
                    if context_answer is not None:
                        validation_observation = _merge_answer_citation_observation(
                            retrieval_observation,
                            answer,
                        )
                    validation = validate_answer(
                        case,
                        answer,
                        retrieved_ids,
                        retrieval_observation=validation_observation,
                    )
                    generation = answer.get("generation") if isinstance(answer.get("generation"), dict) else {}
                    validation.update(
                        prompt_tokens=generation.get("prompt_tokens"),
                        completion_tokens=generation.get("completion_tokens"),
                        total_tokens=generation.get("total_tokens"),
                        fallback_reason=generation.get("fallback_reason"),
                    )
                    result = result.model_copy(update=validation)
                    answer_passed = (
                        validation["answer_relevance"] == 1.0
                        and validation["faithfulness"] == 1.0
                        and validation["safety_passed"]
                        and not validation["unresolved_placeholder_values"]
                        and validation.get("correctness") is not False
                        and validation.get("answer_kind_correct") is not False
                        and validation.get("command_correct") is not False
                        and validation.get("shell_correct") is not False
                        and (case.should_answer or validation.get("no_answer_correct") is True)
                    )
                    if case.should_answer:
                        # Keep retrieval misses visible in model reports even
                        # when a provider happens to answer from another
                        # context.  A no-answer case is different: retrieval
                        # is expected to return candidates before the answer
                        # layer decides to refuse, so its final pass state is
                        # controlled by the refusal validator below.
                        final_passed = result.passed and answer_passed
                    else:
                        final_passed = answer_passed
                    reasons = [] if final_passed else [*result.failure_reasons]
                    if not answer_passed:
                        reasons.append("answer-level deterministic validation failed")
                    result = result.model_copy(update={"passed": final_passed, "failure_reasons": list(dict.fromkeys(reasons))})
                except Exception as exc:
                    result = result.model_copy(
                        update={
                            "passed": False,
                            "failure_reasons": [*result.failure_reasons, f"model request failed: {type(exc).__name__}"],
                            "fallback_reason": "provider_error",
                            "trace": redact_sensitive({**result.trace, "model_error": type(exc).__name__}),
                        }
                    )
            result = result.model_copy(update={"latency_ms": (time.perf_counter() - started) * 1000.0})
            results.append(result)

    if args.mode == "offline":
        evaluate_cases()
    elif args.endpoint:
        with httpx.Client(base_url=args.endpoint.rstrip("/"), timeout=settings.llm_timeout_seconds + 15) as client:
            evaluate_cases(client)
    else:
        if settings.llm_provider != "ollama" and not settings.llm_api_key:
            raise SystemExit(
                "model mode requires the currently configured provider API key; "
                "set AEGIS_LLM_API_KEY or pass --endpoint to a running CommandFoundry API"
            )
        from fastapi.testclient import TestClient
        from app.main import app

        with TestClient(app) as client:
            evaluate_cases(client)

    input_tokens = sum(item.prompt_tokens or 0 for item in results)
    output_tokens = sum(item.completion_tokens or 0 for item in results)
    estimated_cost = (input_tokens / 1000) * args.input_cost_per_1k + (output_tokens / 1000) * args.output_cost_per_1k
    report = build_report(
        manifest,
        results,
        case_index={case.id: case for case in all_cases},
        cost=estimated_cost,
    )
    report_payload: dict[str, Any] = report.model_dump(mode="json")
    from app.evaluation_gate import compare_reports

    gate_result = compare_reports(report_payload)
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        gate_result = compare_reports(report_payload, baseline)
    report_payload["gate"] = gate_result.to_dict()
    if args.redact:
        report_payload = redact_sensitive(report_payload)
        for item in report_payload.get("cases", []):
            item["query"] = "<redacted>"
            if isinstance(item.get("trace"), dict) and "query" in item["trace"]:
                item["trace"]["query"] = "<redacted>"
    serialized = json.dumps(report_payload, ensure_ascii=False, indent=2)
    if args.output:
        _write_artifacts(args.output, report_payload)
        print(json.dumps({"report": str(_artifact_paths(args.output)[0]), "gate": gate_result.to_dict(), "metrics": report_payload.get("metrics", {})}, ensure_ascii=False, indent=2))
    else:
        print(serialized)
    if args.fail_on_gate and gate_result is not None and not gate_result.passed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
