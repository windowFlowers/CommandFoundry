"""Project memory whose experience is stored and found by Mem0.

The host still decides what is learned and writes the same sanitized records as
ProjectMemory; Mem0 only keeps them and finds them by embedding similarity. That
split won the comparison in docs/AGENT_EVOLUTION.md. No model reads a run to
extract memories here: extraction stored the contents of secret files.

Needs the ``memory`` extra: ``pip install -e ".[memory]"``.
"""
from __future__ import annotations

import logging
import os
import traceback
from pathlib import Path

from .memory import FACT_LABELS, MemoryStore, ProjectMemory, Recall, clean, executed_writes, fix_line, memory_block, tested, trusted

logger = logging.getLogger(__name__)
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
EMBEDDING_DIMS = 512
TOP_K = 5
NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}


def open_mem0(folder: Path, *, model: str, base_url: str, instructions: str | None = None):
    """A local Mem0: embedded Qdrant and FastEmbed. Telemetry stays off, nothing leaves the machine."""
    os.environ.setdefault("MEM0_TELEMETRY", "False")
    from mem0 import Memory

    folder.mkdir(parents=True, exist_ok=True)
    config = {
        "llm": {"provider": "vllm", "config": {"model": model, "vllm_base_url": base_url, "temperature": 0.1, "max_tokens": 1500}},
        "embedder": {"provider": "fastembed", "config": {"model": EMBEDDING_MODEL, "embedding_dims": EMBEDDING_DIMS}},
        "vector_store": {"provider": "qdrant", "config": {"path": str(folder / "qdrant"), "collection_name": "memory",
                                                          "embedding_model_dims": EMBEDDING_DIMS, "on_disk": True}},
        "history_db_path": str(folder / "history.db"),
    }
    if instructions:
        config["custom_instructions"] = instructions
    client = Memory.from_config(config)
    create = client.llm.client.chat.completions.create
    client.llm.client.chat.completions.create = lambda **arguments: create(**arguments, extra_body=NO_THINKING)
    return client


class ExternalMemory(ProjectMemory):
    """Declared facts stay in the host store; experience lives in the subclass's backend."""

    def __init__(self, store: MemoryStore, project: str, writes: set[str]):
        super().__init__(store, project, writes, learn=True)
        self.failures = 0
        self.errors: set[str] = set()

    def retrieve(self, query: str) -> list[str]:
        raise NotImplementedError

    def write(self, symptom: str, trace: list[dict], report: dict | None) -> None:
        raise NotImplementedError

    def _failed(self, action: str) -> None:
        """A memory fault must never end a run, so every backend error stops here."""
        logger.exception("memory %s failed", action)
        self.failures += 1
        self.errors.add(f"{action}: {traceback.format_exc().strip().splitlines()[-1][:300]}")

    def recall(self, task, backend, policy):
        base = super().recall(task, backend, policy)
        try:
            found = self.retrieve(base.symptom or task)
        except Exception:
            self._failed("recall")
            found = []
        if not found:
            return base
        block = memory_block([f"- {text}" for text in found])
        return Recall("\n".join(item for item in (base.text, block) if item), base.symptom, [str(index) for index in range(len(found))],
                      base.facts, base.stale)

    def hint(self, output, seen):
        return None

    def search(self, query):
        try:
            return "\n".join(self.retrieve(query)) or "没有相关记忆"
        except Exception:
            self._failed("search")
            return "没有相关记忆"

    def host_records(self, symptom: str, trace: list[dict], report: dict | None) -> list[str]:
        """The same rules and text as ProjectMemory.learn, flattened to one line per record."""
        verified = report.get("verified") if report else False
        if verified is None or not (verified or tested(trace, self.writes)):
            return []
        records = [f"{FACT_LABELS[key]}：{value}" for key, value in self._observed_facts(trace)] if verified else []
        writes = executed_writes(trace, self.writes)
        if writes and symptom:
            cause = clean((report or {}).get("root_cause"), 120) if verified else ""
            fixes = "；".join(fix_line(entry) for entry in writes[-6:])
            records.append(f"现象：{symptom}。根因：{cause}。{'当时有效的做法' if verified else '当时试过但没解决'}：{fixes}")
        return records

    def learn(self, run_id, *, symptom, trace, report, recalled):
        try:
            if self.store.claim_run(self.project, run_id):
                self.write(symptom, trace, report)
        except Exception:
            self._failed("write")


class Mem0Memory(ExternalMemory):
    def __init__(self, store: MemoryStore, project: str, writes: set[str], client):
        super().__init__(store, project, writes)
        self.client = client

    def retrieve(self, query):
        result = self.client.search(query[:500], top_k=TOP_K, filters={"user_id": self.project})
        return [item["memory"] for item in result["results"] if trusted(item["memory"])]

    def write(self, symptom, trace, report):
        known = set(self.dump())
        for record in self.host_records(symptom, trace, report):
            if record not in known:
                self.client.add([{"role": "user", "content": record}], user_id=self.project, infer=False)

    def dump(self) -> list[str]:
        return [item["memory"] for item in self.client.get_all(filters={"user_id": self.project}, top_k=1000)["results"]]

    def forget(self) -> None:
        self.client.delete_all(user_id=self.project)
