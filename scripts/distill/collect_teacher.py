"""Record teacher-model answers through the production /chat/stream pipeline.

The real app runs in-process (same retrieval, planner and prompts as the desktop
app) with the teacher model as its generator, and every generator call is
recorded.  One process loads the app once; worker threads keep several slow
teacher requests in flight.  Calls are matched to questions through the
"当前用户问题" line of the answer prompt.  Runs resume: answered ids are skipped.

    python scripts/distill/collect_teacher.py --key-file <path> --limit 200 --workers 16
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROVIDER_FAILURES = {"timeout", "rate_limit", "provider_error", "authentication"}
QUERY_MARKER = "当前用户问题："


def collect(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    key = Path(args.key_file).read_text(encoding="utf-8").strip()
    os.environ.update(
        {
            "AEGIS_LLM_PROVIDER": "teacher",
            "AEGIS_LLM_BASE_URL": args.base_url,
            "AEGIS_LLM_MODEL": args.model,
            "AEGIS_LLM_API_KEY": key,
            "AEGIS_LLM_TIMEOUT_SECONDS": str(args.timeout),
            "AEGIS_STORAGE_DIR": str(out_dir / "storage"),
            "AEGIS_EMBEDDING_LOCAL_ONLY": "1",
        }
    )
    sys.path.insert(0, str(ROOT / "backend"))
    from app import generation

    tasks = {
        generation.SYSTEM_PROMPT: "answer",
        generation.MEMORY_SUMMARY_PROMPT: "memory_summary",
        generation.USER_MEMORY_EXTRACTION_PROMPT: "memory_extraction",
    }
    questions = _load_questions(args)
    qid_by_query = {q["query"].strip(): q["id"] for q in questions}
    lock = threading.Lock()
    state = {"tokens": 0, "failures_in_a_row": 0, "stop": None, "rate_limited": 0}
    original = generation.OpenAICompatibleGenerator._completion

    from openai import RateLimitError

    def recording(self, payload: dict):
        # Quota-metered APIs reserve prompt + max_tokens up front; without a cap
        # they reserve the model's full output window and refuse the request.
        if args.max_tokens and "max_tokens" not in payload:
            payload = {**payload, "max_tokens": args.max_tokens}
        # The teacher API throttles bursts; wait and retry instead of letting the
        # app fall back, so concurrency settles at whatever the API allows.
        for attempt in range(7):
            try:
                response, latency_ms = original(self, payload)
                break
            except RateLimitError:
                if attempt == 6:
                    raise
                with lock:
                    state["rate_limited"] += 1
                time.sleep(min(15 * 2**attempt, 240))
        message = response.choices[0].message
        usage = response.usage.model_dump() if response.usage else {}
        messages = payload.get("messages") or []
        system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        asked = user.rsplit(QUERY_MARKER, 1)[1].strip() if QUERY_MARKER in user else None
        record = {
            "qid": qid_by_query.get(asked) if asked else None,
            "query_in_prompt": asked,
            "task": tasks.get(system, "other"),
            "request": {k: payload[k] for k in ("messages", "response_format", "temperature", "max_tokens") if k in payload},
            "content": message.content,
            "reasoning_chars": len(getattr(message, "reasoning_content", None) or ""),
            "finish_reason": response.choices[0].finish_reason,
            "model": getattr(response, "model", None),
            "usage": usage,
            "latency_ms": latency_ms,
        }
        with lock:
            state["tokens"] += int(usage.get("total_tokens") or 0)
            with (out_dir / "calls.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return response, latency_ms

    generation.OpenAICompatibleGenerator._completion = recording

    from fastapi.testclient import TestClient
    from app.main import app

    done = set()
    for directory in [out_dir, *map(Path, args.skip_done_in)]:
        done |= {r["qid"] for r in _latest_results(directory) if r["fallback_reason"] not in PROVIDER_FAILURES}
    pending = [q for q in questions if q["id"] not in done]
    if args.reverse:
        pending.reverse()
    print(f"{len(done)} already answered, {len(pending)} to go", flush=True)

    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None

    def ask(question: dict) -> None:
        with lock:
            if not state["stop"] and deadline and time.time() >= deadline:
                state["stop"] = f"time limit of {args.max_minutes} minutes reached"
            if state["stop"]:
                return
        started = time.perf_counter()
        response = client.post("/chat/stream", json={"query": question["query"]})
        answer = _answer_event(response.text)
        generation_info = answer.get("generation") or {}
        result = {
            "qid": question["id"],
            "topic_id": question["topic_id"],
            "status": response.status_code,
            "mode": answer.get("mode"),
            "fallback_reason": generation_info.get("fallback_reason"),
            "attempted": generation_info.get("attempted"),
            "seconds": round(time.perf_counter() - started, 2),
        }
        with lock:
            with (out_dir / "results.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")
            failed = result["fallback_reason"] in PROVIDER_FAILURES
            state["failures_in_a_row"] = state["failures_in_a_row"] + 1 if failed else 0
            if state["failures_in_a_row"] >= 10:
                state["stop"] = "10 provider failures in a row"
            elif args.token_budget and state["tokens"] >= args.token_budget:
                state["stop"] = f"token budget reached ({state['tokens']})"
        if failed:
            time.sleep(30)

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=args.workers) as pool:
        client.portal.call(_raise_thread_limit, args.workers * 4)
        for n, _ in enumerate(pool.map(ask, pending), 1):
            if n % 20 == 0:
                print(f"{n}/{len(pending)} done, {state['tokens']} tokens, {state['rate_limited']} rate-limit retries", flush=True)
    if state["stop"]:
        print(f"stopped early: {state['stop']}", flush=True)


def _raise_thread_limit(tokens: int) -> None:
    import anyio.to_thread

    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = max(limiter.total_tokens, tokens)


def _latest_results(out_dir: Path) -> list[dict]:
    """One result per question: the last attempt wins (retries append)."""
    return list({r["qid"]: r for r in _read_jsonl(out_dir, "results*.jsonl")}.values())


def _load_questions(args: argparse.Namespace) -> list[dict]:
    rows = [json.loads(line) for line in Path(args.questions).read_text("utf-8").splitlines() if line.strip()]
    return rows[: args.limit] if args.limit else rows


def _read_jsonl(out_dir: Path, pattern: str) -> list[dict]:
    return [json.loads(l) for p in out_dir.glob(pattern) for l in p.read_text("utf-8").splitlines() if l.strip()]


def _answer_event(stream: str) -> dict:
    for frame in stream.split("\n\n"):
        lines = frame.strip().splitlines()
        if lines and lines[0] == "event: answer":
            data = "".join(line[6:] for line in lines if line.startswith("data: "))
            return json.loads(data).get("answer") or {}
    return {}


def summarize(out_dir: Path) -> dict:
    results = _latest_results(out_dir)
    calls = _read_jsonl(out_dir, "calls*.jsonl")
    answer_calls = [c for c in calls if c["task"] == "answer"]

    def total(field: str, rows: list[dict]) -> int:
        return sum(int((c.get("usage") or {}).get(field) or 0) for c in rows)

    reasoning = sum(int(((c.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens") or 0) for c in calls)
    return {
        "questions": len(results),
        "modes": dict(Counter(r["mode"] for r in results)),
        "fallback_reasons": dict(Counter(r["fallback_reason"] for r in results if r["fallback_reason"])),
        "calls_by_task": dict(Counter(c["task"] for c in calls)),
        "answer_calls_unmatched": sum(1 for c in answer_calls if not c.get("qid")),
        "accepted_model_answers": sum(1 for r in results if r["mode"] == "model"),
        "prompt_tokens": total("prompt_tokens", calls),
        "completion_tokens": total("completion_tokens", calls),
        "reasoning_tokens": reasoning,
        "total_tokens": total("total_tokens", calls),
        "avg_answer_call_s": round(sum(c["latency_ms"] for c in answer_calls) / max(1, len(answer_calls)) / 1000, 1),
        "avg_tokens_per_answer_call": round(total("total_tokens", answer_calls) / max(1, len(answer_calls))),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", default=str(ROOT / ".runtime" / "distill" / "questions.jsonl"))
    parser.add_argument("--out-dir", default=str(ROOT / ".runtime" / "distill" / "teacher"))
    parser.add_argument("--key-file", required=True, help="file holding the teacher API key (never logged)")
    parser.add_argument("--base-url", default="https://discovery-api.intern-ai.org.cn/v1")
    parser.add_argument("--model", default="deepseek-v4.1-flash-1017")
    parser.add_argument("--limit", type=int, default=0, help="use only the first N questions (0 = all)")
    parser.add_argument("--workers", type=int, default=48, help="concurrent requests (threads)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds per teacher call; the API is slow")
    parser.add_argument("--token-budget", type=int, default=0, help="stop after this many teacher tokens (0 = none)")
    parser.add_argument("--max-minutes", type=float, default=0, help="stop submitting new questions after this long (0 = none)")
    parser.add_argument("--max-tokens", type=int, default=6144, help="output cap sent to the teacher (0 = none); answers peak near 5k tokens")
    parser.add_argument("--reverse", action="store_true", help="work from the last question backwards (to run two teachers in parallel)")
    parser.add_argument("--skip-done-in", action="append", default=[], help="also skip questions answered in this output dir")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    collect(args)
    summary = {"wall_minutes": round((time.time() - started) / 60, 1), **summarize(out_dir)}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()