"""Score retrieval on hand-written everyday questions that are not topic aliases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.knowledge import KnowledgeCatalog  # noqa: E402
from app.retrieval import HybridRetriever, analyze_query  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-dir", type=Path, default=ROOT / "knowledge")
    parser.add_argument("--cases", type=Path, default=ROOT / "evaluation" / "fixtures" / "coverage_holdout.json")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--show-misses", action="store_true")
    args = parser.parse_args()

    catalog = KnowledgeCatalog(args.knowledge_dir)
    catalog.load()
    retriever = HybridRetriever(
        catalog=catalog,
        embedding_model=settings.embedding_model,
        model_cache_dir=settings.model_cache_dir,
        candidate_k=max(settings.retrieval_candidate_k, args.top_k),
        embedding_enabled=True,
        local_files_only=True,
    )
    if not retriever.initialize_embeddings():
        raise SystemExit(f"local embedding model unavailable: {retriever.embedding_error}")

    groups: dict[str, list[int]] = {}
    for case in json.loads(args.cases.read_text(encoding="utf-8")):
        query = case["query"]
        hits = retriever.retrieve_advanced(query, top_k=args.top_k, analysis=analyze_query(query, catalog.topics))
        ids = [hit.topic.id for hit in hits]
        expected = set(case["expected_topic_ids"])
        top1, topk = int(ids[:1] != [] and ids[0] in expected), int(bool(expected & set(ids)))
        for name in (case["group"], "all"):
            total = groups.setdefault(name, [0, 0, 0])
            total[0] += 1
            total[1] += top1
            total[2] += topk
        if args.show_misses and not topk:
            print(f"MISS {query} -> {ids}")
    for name, (count, top1, topk) in groups.items():
        print(f"{name}: n={count} top1={top1 / count:.1%} top{args.top_k}={topk / count:.1%}")


if __name__ == "__main__":
    main()
