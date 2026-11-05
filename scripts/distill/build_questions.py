"""Build distillation questions for the answer generator.

1. A self-hosted OpenAI-compatible model (free to run) writes paraphrased user
   questions for every knowledge topic.
2. Questions close to any evaluation query, topic title or alias are dropped.
   The v2.11 suites are written from those aliases, so this keeps training data
   out of the 662-case benchmark.

    python scripts/distill/build_questions.py --base-url http://127.0.0.1:18004/v1
"""

from __future__ import annotations

import argparse
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[2]
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"

PROMPT = """你在为一个开发者命令助手构造用户提问。根据下面的命令主题，写出 {n} 个真实用户可能提出的、彼此不同的问题。
要求：
- 约 70% 用中文、30% 用英文；口语化的、书面的、带具体场景（文件名、端口、分支名、服务名等）的问题都要有；
- 有的问题针对某条具体命令，有的只描述想达成的目标而不提命令名；
- 其中 1 个问题询问该主题资料里没有覆盖的相关操作；
- 不要编号，不要解释，不要照抄下面的标题或已有别名。
只输出 JSON：{{"questions": ["...", "..."]}}

主题：{title}
说明：{summary}
资料中的命令：{labels}
已有别名（不要重复）：{aliases}"""


def _rows(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    data = json.loads(path.read_text("utf-8"))
    return data if isinstance(data, list) else next(v for v in data.values() if isinstance(v, list))


def protected_texts(topics: list[dict]) -> list[str]:
    texts: list[str] = []
    for topic in topics:
        texts.append(topic["title"])
        texts.extend(topic.get("aliases") or [])
    for name in ("eval_dataset.json", "windows_eval_dataset.json"):
        texts.extend(row["query"] for row in _rows(ROOT / "knowledge" / name))
    for row in _rows(ROOT / "knowledge" / "memory_eval_dataset.json"):
        texts.extend([row["previous_query"], row["follow_up"]])
    texts.extend(row["message"] for row in _rows(ROOT / "knowledge" / "personalization_eval_dataset.json"))
    for path in (ROOT / "evaluation" / "fixtures").rglob("*.jsonl"):
        texts.extend(row["query"] for row in _rows(path))
    return sorted({text.strip() for text in texts if text and text.strip()})


def generate(client: OpenAI, model: str, topic: dict, n: int) -> list[str]:
    prompt = PROMPT.format(
        n=n,
        title=topic["title"],
        summary=topic.get("summary") or "",
        labels="；".join(command["label"] for command in topic.get("commands") or [])[:600],
        aliases="；".join(topic.get("aliases") or []),
    )
    for _ in range(3):
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0.9,
                max_tokens=1500,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            questions = json.loads(response.choices[0].message.content)["questions"]
            return [q.strip() for q in questions if isinstance(q, str) and 4 <= len(q.strip()) <= 200]
        except Exception:  # noqa: BLE001 - retry malformed generations
            continue
    return []


def _normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text.casefold())


def _embed(model: TextEmbedding, texts: list[str]) -> np.ndarray:
    vectors = np.array(list(model.embed(texts)), dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:18004/v1")
    parser.add_argument("--model", default="qwen3.5-4b")
    parser.add_argument("--per-topic", type=int, default=12)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--eval-threshold", type=float, default=0.85, help="drop if cosine >= this to any protected text")
    parser.add_argument("--dup-threshold", type=float, default=0.93, help="drop near-duplicates among generated questions")
    parser.add_argument("--output", default=str(ROOT / ".runtime" / "distill" / "questions.jsonl"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    topics = json.loads((ROOT / "knowledge" / "topics.json").read_text("utf-8"))
    client = OpenAI(base_url=args.base_url, api_key="local", timeout=180, max_retries=0)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        batches = list(pool.map(lambda topic: generate(client, args.model, topic, args.per_topic), topics))
    raw = [
        {"topic_id": topic["id"], "domain": topic["domain"], "query": query}
        for topic, questions in zip(topics, batches)
        for query in questions
    ]
    print(f"generated {len(raw)} questions for {sum(1 for b in batches if b)}/{len(topics)} topics")

    protected = protected_texts(topics)
    protected_norm = {_normalize(text) for text in protected}
    embedder = TextEmbedding(model_name=EMBEDDING_MODEL, cache_dir=str(ROOT / "models" / "cache"), local_files_only=True)
    generated_vectors = _embed(embedder, [row["query"] for row in raw])
    max_sim = (generated_vectors @ _embed(embedder, protected).T).max(axis=1)

    rng = random.Random(args.seed)
    order = list(range(len(raw)))
    rng.shuffle(order)
    kept: list[int] = []
    dropped = {"exact_eval": 0, "near_eval": 0, "near_duplicate": 0}
    for i in order:
        if _normalize(raw[i]["query"]) in protected_norm:
            dropped["exact_eval"] += 1
        elif max_sim[i] >= args.eval_threshold:
            dropped["near_eval"] += 1
        elif kept and float((generated_vectors[kept] @ generated_vectors[i]).max()) >= args.dup_threshold:
            dropped["near_duplicate"] += 1
        else:
            kept.append(i)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix(".raw.jsonl").open("w", encoding="utf-8") as fh:
        for row, sim in zip(raw, max_sim):
            fh.write(json.dumps({**row, "max_eval_sim": round(float(sim), 4)}, ensure_ascii=False) + "\n")
    with output.open("w", encoding="utf-8") as fh:
        for n, i in enumerate(kept, 1):
            row = {"id": f"q-{n:05d}", **raw[i], "max_eval_sim": round(float(max_sim[i]), 4)}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"protected texts: {len(protected)}; dropped: {dropped}; kept {len(kept)} -> {output}")


if __name__ == "__main__":
    main()