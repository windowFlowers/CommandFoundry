"""Concurrent load test for OpenAI-compatible chat endpoints (llama.cpp, vLLM, ...).

Sends RAG-shaped requests (system prompt + ~1.5k tokens of evidence + question)
at fixed concurrency levels and reports latency, time to first token and
throughput.  Standard library only, so it runs on an offline inference server:

    python3 benchmark_serving.py --base-url http://127.0.0.1:18000/v1 \
        --model qwen3.5-4b --label vllm --output vllm.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SYSTEM = "你是一个谨慎的开发者命令助手。只能根据给出的参考资料回答，并在回答中说明依据。"
EVIDENCE = (
    "git revert 会创建一个新的提交来撤销指定提交引入的更改，不会改写历史，适合已经推送到共享分支的提交。"
    "git reset --hard 会移动分支指针并丢弃工作区修改，只应在本地未推送的提交上使用。"
    "docker container prune 会删除所有已停止的容器，执行前会要求确认，可以加 -f 跳过确认。"
    "docker system df 用于查看镜像、容器和数据卷占用的磁盘空间。"
    "在 Windows PowerShell 中，Get-NetTCPConnection -LocalPort 8080 可以找到占用端口的连接，"
    "再用 Get-Process -Id <pid> 查看进程名称。python -m venv .venv 会在当前目录创建虚拟环境，"
    "Windows 上通过 .venv\\Scripts\\Activate.ps1 激活，Linux 上通过 source .venv/bin/activate 激活。"
) * 6
QUESTIONS = [
    "如何安全地撤销一个已经推送的 Git 提交？",
    "怎样清理已经停止的 Docker 容器？",
    "Windows 上如何查看 8080 端口被哪个进程占用？",
    "如何创建并激活 Python 虚拟环境？",
]


def one_request(base_url: str, model: str, idx: int, max_tokens: int) -> dict:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            # The request id leads the user turn so requests do not share a cached prefix
            # beyond the system prompt, as with real retrieval results.
            {"role": "user", "content": f"[请求 {idx}] 参考资料：{EVIDENCE}\n问题：{QUESTIONS[idx % len(QUESTIONS)]}"},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    ttft = None
    chunks = 0
    usage: dict = {}
    with urllib.request.urlopen(request, timeout=600) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            usage = event.get("usage") or usage
            for choice in event.get("choices") or []:
                if (choice.get("delta") or {}).get("content"):
                    if ttft is None:
                        ttft = time.perf_counter() - started
                    chunks += 1
    return {
        "latency_s": time.perf_counter() - started,
        "ttft_s": ttft or 0.0,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens") or chunks,
    }


def p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=20)[18] if len(values) > 1 else values[0]


def run_level(args: argparse.Namespace, concurrency: int, offset: int) -> dict:
    total = args.requests or max(8, 4 * concurrency)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda i: one_request(args.base_url, args.model, offset + i, args.max_tokens), range(total)))
    wall = time.perf_counter() - started
    latencies = [r["latency_s"] for r in results]
    ttfts = [r["ttft_s"] for r in results]
    output_tokens = sum(r["completion_tokens"] for r in results)
    return {
        "concurrency": concurrency,
        "requests": total,
        "wall_s": round(wall, 2),
        "requests_per_s": round(total / wall, 3),
        "output_tokens_per_s": round(output_tokens / wall, 1),
        "latency_mean_s": round(statistics.mean(latencies), 3),
        "latency_p95_s": round(p95(latencies), 3),
        "ttft_mean_s": round(statistics.mean(ttfts), 3),
        "ttft_p95_s": round(p95(ttfts), 3),
        "prompt_tokens_mean": round(statistics.mean(r["prompt_tokens"] for r in results)),
        "completion_tokens_mean": round(output_tokens / total),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:18000/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8, 16])
    parser.add_argument("--requests", type=int, default=0, help="requests per level (default: max(8, 4 x concurrency))")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--output", help="write results as JSON to this path")
    args = parser.parse_args()

    for i in range(2):  # warm-up, excluded from the results
        one_request(args.base_url, args.model, -1 - i, 16)
    rows = []
    for n, concurrency in enumerate(args.concurrency):
        row = run_level(args, concurrency, offset=n * 1000)
        rows.append(row)
        print(json.dumps({"label": args.label, **row}, ensure_ascii=False), flush=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump({"label": args.label, "model": args.model, "base_url": args.base_url, "results": rows}, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()