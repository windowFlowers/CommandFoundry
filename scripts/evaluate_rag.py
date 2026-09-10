from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.knowledge import KnowledgeCatalog  # noqa: E402
from app.memory import ConversationTurn, merge_contextual_hits, resolve_contextual_query  # noqa: E402
from app.models import Answer, ChatMessage, CommandBlock  # noqa: E402
from app.retrieval import HybridRetriever  # noqa: E402


def evaluate(retriever: HybridRetriever, cases: list[dict]) -> dict[str, float | int]:
    top1 = 0
    top3 = 0
    for case in cases:
        result_ids = [item.topic.id for item in retriever.retrieve(case["query"], top_k=3)]
        top1 += bool(result_ids and result_ids[0] == case["expected_topic_id"])
        top3 += case["expected_topic_id"] in result_ids
    total = len(cases)
    return {
        "cases": total,
        "top_1_hits": top1,
        "top_3_hits": top3,
        "top_1_rate": round(top1 / total, 4),
        "top_3_rate": round(top3 / total, 4),
    }


def evaluate_memory(retriever: HybridRetriever, cases: list[dict]) -> dict[str, object]:
    top1 = 0
    top3 = 0
    failed_case_ids: list[str] = []
    for case in cases:
        answer = Answer(
            mode="local_fallback",
            summary="历史回答摘要",
            commands=[
                CommandBlock(label=label, code="echo <value>")
                for label in case["previous_command_labels"]
            ],
            notes=[],
            citations=[],
        )
        turn = ConversationTurn(
            user=ChatMessage(role="user", query=case["previous_query"]),
            assistant=ChatMessage(role="assistant", answer=answer),
        )
        follow_up = case["follow_up"]
        contextual = resolve_contextual_query(follow_up, turn)
        if contextual == follow_up.strip():
            results = retriever.retrieve(follow_up, top_k=3)
        else:
            results = merge_contextual_hits(
                retriever.retrieve(follow_up, top_k=8),
                retriever.retrieve(contextual, top_k=8),
                top_k=3,
            )
        result_ids = [item.topic.id for item in results]
        expected = case["expected_topic_id"]
        if expected is None:
            matched = not result_ids
            top1 += matched
            top3 += matched
        else:
            top1 += bool(result_ids and result_ids[0] == expected)
            top3 += expected in result_ids
            matched = expected in result_ids
        if not matched:
            failed_case_ids.append(case["id"])
    total = len(cases)
    return {
        "cases": total,
        "top_1_hits": top1,
        "top_3_hits": top3,
        "top_1_rate": round(top1 / total, 4),
        "top_3_rate": round(top3 / total, 4),
        "failed_case_ids": failed_case_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate AegisCopilot BM25 and hybrid retrieval")
    parser.add_argument("--skip-hybrid", action="store_true", help="Only run the deterministic BM25 baseline")
    args = parser.parse_args()

    knowledge_dir = PROJECT_ROOT / "knowledge"
    catalog = KnowledgeCatalog(knowledge_dir)
    catalog.load()
    cases = json.loads((knowledge_dir / "eval_dataset.json").read_text(encoding="utf-8"))
    memory_cases = json.loads(
        (knowledge_dir / "memory_eval_dataset.json").read_text(encoding="utf-8")
    )
    common = {
        "catalog": catalog,
        "embedding_model": "BAAI/bge-small-zh-v1.5",
        "model_cache_dir": PROJECT_ROOT / "models" / "cache",
        "candidate_k": 12,
    }

    result: dict[str, object] = {
        "dataset": str(knowledge_dir / "eval_dataset.json"),
        "bm25": {},
    }
    bm25 = HybridRetriever(**common, embedding_enabled=False)
    result["bm25"] = {
        "single_turn": evaluate(bm25, cases),
        "multi_turn": evaluate_memory(bm25, memory_cases),
    }
    if not args.skip_hybrid:
        hybrid = HybridRetriever(**common, embedding_enabled=True, local_files_only=True)
        if not hybrid.initialize_embeddings():
            result["hybrid_error"] = hybrid.embedding_error
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        result["hybrid"] = {
            "single_turn": evaluate(hybrid, cases),
            "multi_turn": evaluate_memory(hybrid, memory_cases),
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
