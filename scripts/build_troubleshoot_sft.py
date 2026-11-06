"""Turn recorded teacher runs into per-turn SFT samples.

Each sample is the exact context the agent sent plus the teacher's reply, so
progress notes and memory blocks stay where the student will see them. Only
runs the scorer accepted are used. Reasoning is dropped because the student
serves with thinking off. One app is kept out for validation.
"""
import argparse
import glob
import json
import random
from pathlib import Path

from transformers import AutoTokenizer

BANNED_SPLITS = {"holdout", "fresh", "final"}


def clean_message(message):
    result = {"role": message["role"], "content": message.get("content") or ""}
    if message.get("tool_call_id"):
        result["tool_call_id"] = message["tool_call_id"]
    calls = []
    for call in message.get("tool_calls") or []:
        arguments = call["function"]["arguments"]
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        calls.append({"type": "function", "function": {"name": call["function"]["name"], "arguments": arguments}})
    if calls:
        result["tool_calls"] = calls
    return result


def samples(run):
    for turn in run["turns"]:
        names = {tool["function"]["name"] for tool in turn["tools"]}
        reply = turn["reply"]
        calls = reply.get("tool_calls") or []
        if not calls or any(call["function"]["name"] not in names for call in calls):
            continue
        try:
            messages = [clean_message(message) for message in [*turn["messages"], reply]]
        except (json.JSONDecodeError, TypeError):
            continue
        yield {"messages": messages, "tools": turn["tools"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True, help="glob of teacher JSONL files")
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--val-app", default="arcade")
    parser.add_argument("--val-samples", type=int, default=120)
    parser.add_argument("--max-len", type=int, default=12288)
    parser.add_argument("--seed", type=int, default=13)
    options = parser.parse_args()
    tokenizer = AutoTokenizer.from_pretrained(options.model)
    train, val, stats = [], [], {"runs": 0, "skipped_runs": 0, "too_long": 0, "tokens": 0, "reply_tokens": 0}
    for path in sorted(glob.glob(options.runs)):
        for line in open(path, encoding="utf-8"):
            run = json.loads(line)
            if run["split"] in BANNED_SPLITS:
                raise SystemExit(f"evaluation split in training data: {run['id']}")
            if not run["success"] or run["false_success"] or run["violation"]:
                stats["skipped_runs"] += 1
                continue
            stats["runs"] += 1
            for sample in samples(run):
                prompt = tokenizer.apply_chat_template(sample["messages"][:-1], tools=sample["tools"], tokenize=False,
                                                       add_generation_prompt=True, enable_thinking=False)
                full = tokenizer.apply_chat_template(sample["messages"], tools=sample["tools"], tokenize=False, enable_thinking=False)
                if not full.startswith(prompt):
                    raise SystemExit("chat template renders the prompt differently with the reply appended")
                size = len(tokenizer(full, add_special_tokens=False)["input_ids"])
                if size > options.max_len:
                    stats["too_long"] += 1
                    continue
                reply = size - len(tokenizer(prompt, add_special_tokens=False)["input_ids"])
                sample.update(size=size, reply=reply, app=run["app"])
                (val if run["app"] == options.val_app else train).append(sample)
                if run["app"] != options.val_app:
                    stats["tokens"] += size
                    stats["reply_tokens"] += reply
    random.seed(options.seed)
    random.shuffle(train)
    random.shuffle(val)
    out = Path(options.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train", train), ("val", val[:options.val_samples])):
        with (out / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({**stats, "train": len(train), "val": min(len(val), options.val_samples), "apps": sorted({row["app"] for row in train}),
                      "p50": sorted(row["size"] for row in train)[len(train) // 2], "max": max(row["size"] for row in train)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
