"""Small paid Agent tool-loop acceptance run; never a release evaluation.

Reads an explicitly supplied key file into this process only. Reports contain
safe aggregate evidence, never prompts, keys, generated commands or paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent_tools import AgentSession, AgentToolset
from app.agent_model import PROMPT_VERSION
from app.answering import AnswerService
from app.command_planner import CommandPlanner
from app.evaluation_gate import find_credential_leaks, redact_report
from app.generation import OpenAICompatibleGenerator
from app.knowledge import KnowledgeCatalog
from app.models import DEFAULT_KNOWLEDGE_BASE_ID
from app.repository import ConversationRepository
from app.retrieval import EmbeddingEngine, KnowledgeIndexManager


def read_authorized_key(path: Path) -> str:
    value = path.read_text(encoding="utf-8-sig").strip()
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        value = next((str(parsed[key]).strip() for key in ("api_key", "API_KEY", "apikey", "key")
                      if parsed.get(key)), "")
    else:
        match = re.search(r"\bsk-[A-Za-z0-9_-]{16,}\b", value)
        if match:
            value = match.group(0)
        elif re.fullmatch(r"[A-Za-z0-9._-]{16,512}", value) is None:
            raise ValueError("invalid_key_file")
    if not value or len(value) > 512 or any(character.isspace() for character in value):
        raise ValueError("invalid_key_file")
    return value


def run(args) -> int:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    key = read_authorized_key(args.key_file)
    generator = OpenAICompatibleGenerator(api_key=key, base_url=args.base_url, model=args.model,
        provider=args.provider, timeout_seconds=args.timeout)
    repository = ConversationRepository(output / "local-business.db")
    catalog = KnowledgeCatalog(ROOT / "knowledge")
    catalog.load()
    engine = EmbeddingEngine("none", output / "cache", enabled=False, local_files_only=True)
    indexes = KnowledgeIndexManager(catalog=catalog, repository=repository,
                                   embedding_engine=engine, candidate_k=20)
    manifest = {
        "timestamp": datetime.now(UTC).isoformat(), "mode": "model", "development_only": True,
        "release_allowed": False, "provider": args.provider, "model": args.model,
        "prompt_version": PROMPT_VERSION, "timeout_seconds": args.timeout,
        "decision_reply_max_tokens": 512,
        "base_url_fingerprint": hashlib.sha256(args.base_url.encode()).hexdigest(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty_worktree": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "knowledge_fingerprint": hashlib.sha256((ROOT / "knowledge" / "manifest.json").read_bytes()).hexdigest(),
    }
    cases = [
        ("agent-python-clarification", "Python 怎么创建并激活虚拟环境？", "clarification", "target_path"),
        ("agent-python-direct", r"Python 创建并激活虚拟环境，目标目录 F:\Agent 示例，PowerShell，环境名 .venv", "direct", None),
        ("agent-git-direct", "git revert HEAD", "direct", None),
    ]
    results = []
    for case_id, query, expected_kind, expected_slot in cases:
        conversation = repository.create(knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID)
        toolset = AgentToolset(query=query, conversation_id=conversation.id,
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID, repository=repository, indexes=indexes,
            planner=CommandPlanner(catalog=catalog, repository=repository), answers=AnswerService(generator))
        started = perf_counter()
        result = {"case_id": case_id, "passed": False, "status": "failed", "error_code": None}
        with AgentSession(output / "local-agent.db", toolset=toolset, generator=generator) as session:
            try:
                state = session.run(session.create().run_id)
                result.update(status=state.status, error_code=state.error_code, steps=state.steps,
                              tools=[observation.name for observation in state.observations])
                if state.status == "completed":
                    answer = session.answer(state.run_id)
                    result.update(answer_kind=answer.answer_kind,
                        command_count=len(answer.commands), citation_count=len(answer.citations),
                        shell=answer.commands[0].shell if answer.commands else None,
                        passed=(answer.answer_kind == expected_kind and
                                (expected_slot is None or answer.clarification and answer.clarification.slot == expected_slot)))
            except Exception as error:
                result["error_code"] = type(error).__name__
            result["model_calls"] = session.model.calls
            result["answer_generation_calls"] = toolset.generation_calls
        result["latency_ms"] = round((perf_counter() - started) * 1000)
        results.append(result)
        print(json.dumps({"case_id": case_id, "passed": result["passed"],
                          "status": result["status"], "error_code": result["error_code"]}), flush=True)
        # Auth, quotas and unsupported native tool calls cannot be fixed by
        # repeating the same paid request in this small acceptance run.
        if result["error_code"] in {"provider_authentication", "provider_rate_limit", "provider_error"}:
            break
    report = {"manifest": manifest, "cases": results,
              "passed": len(results) == len(cases) and all(result["passed"] for result in results)}
    report = redact_report(report)
    if find_credential_leaks(report) or key in json.dumps(report):
        raise ValueError("report_redaction_failed")
    for database in output.glob("*.db*"):
        if database.is_file() and key.encode() in database.read_bytes():
            raise ValueError("checkpoint_credential_leak")
    (output / "agent-smoke-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "release_allowed": False}), flush=True)
    return 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default="custom")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__, "release_allowed": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
