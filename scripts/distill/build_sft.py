"""Turn recorded teacher calls into a chat SFT dataset for the local student model.

Only questions the app accepted as model answers (mode == "model") are used, and
from each of those only the answer-generation call: the student sees exactly the
system/user prompt the app sends and learns the teacher's final JSON reply (the
teacher's reasoning is not part of the target; the student runs without thinking).
Validation is split by topic so near-duplicate questions do not leak across.

    python scripts/distill/build_sft.py --teacher-dir .runtime/distill/deepseek --teacher-dir .runtime/distill/pilot
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _accepted(teacher_dir: Path) -> dict[str, dict]:
    latest = {r["qid"]: r for r in _read_jsonl(teacher_dir / "results.jsonl")}
    return {qid: r for qid, r in latest.items() if r["mode"] == "model"}


def _answer_calls(teacher_dir: Path) -> dict[str, dict]:
    """Last well-formed answer call per question."""
    calls = {}
    for call in _read_jsonl(teacher_dir / "calls.jsonl"):
        if call["task"] != "answer" or not call.get("qid") or call.get("finish_reason") != "stop":
            continue
        try:
            reply = json.loads(call["content"] or "")
        except json.JSONDecodeError:
            continue
        if isinstance(reply, dict) and reply.get("summary"):
            calls[call["qid"]] = call
    return calls


def _is_val(topic_id: str, val_percent: int) -> bool:
    return int(hashlib.sha1(topic_id.encode("utf-8")).hexdigest(), 16) % 100 < val_percent


def build(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    questions = {q["id"]: q for q in _read_jsonl(Path(args.questions))}
    records, stats = {}, Counter()
    for teacher_dir in map(Path, args.teacher_dir):
        calls = _answer_calls(teacher_dir)
        for qid, result in _accepted(teacher_dir).items():
            if qid in records:
                stats["duplicate_skipped"] += 1
                continue
            call = calls.get(qid)
            if call is None:
                stats[f"no_answer_call:{teacher_dir.name}"] += 1
                continue
            records[qid] = {
                "id": qid,
                "topic_id": result["topic_id"],
                "domain": questions[qid]["domain"],
                "teacher": f"{teacher_dir.name}:{call.get('model')}",
                "messages": [*call["request"]["messages"], {"role": "assistant", "content": call["content"]}],
            }
            stats[f"teacher:{teacher_dir.name}"] += 1

    splits = {"train": [], "val": []}
    for qid in sorted(records):
        record = records[qid]
        splits["val" if _is_val(record["topic_id"], args.val_percent) else "train"].append(record)
    for name, rows in splits.items():
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        stats[name] = len(rows)
    stats["val_topics"] = len({r["topic_id"] for r in splits["val"]})
    stats["domains"] = dict(Counter(r["domain"] for r in records.values()))
    (out_dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--teacher-dir", action="append", required=True, help="collector output dir; earlier dirs win on overlap")
    parser.add_argument("--questions", default=".runtime/distill/questions.jsonl")
    parser.add_argument("--out-dir", default=".runtime/distill/sft")
    parser.add_argument("--val-percent", type=int, default=4, help="share of topics held out for validation loss")
    build(parser.parse_args())


if __name__ == "__main__":
    main()