"""Third-party memory layers behind the agent's memory interface, for comparison.

Each backend keeps the same declared facts, progress note and policy as the
built-in memory and replaces only how experience is written and retrieved:

  mem0-llm   Mem0 extracts memories from the run transcript with the local model.
  mem0-host  The host writes the same verified records, Mem0 retrieves them.
  langmem    LangMem extracts memories; a LangGraph store retrieves them.
  mem0-safe  Mem0 extracts and retrieves, behind the host's learning rules and a
             transcript that never shows the extractor what a secret file holds.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.sanitize import redact
from app.troubleshoot.mem0_memory import EMBEDDING_DIMS, EMBEDDING_MODEL, TOP_K, ExternalMemory, Mem0Memory, open_mem0
from app.troubleshoot.memory import clean, tested, trusted

NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}
INSTRUCTIONS = """Extract durable knowledge about this software project from a troubleshooting run:
service names, health URLs, config file paths, maintenance commands, and which fix solved which error.
Say whether the fix worked. Keep each memory to one short sentence. Never store credentials.
If nothing durable was learned, return nothing."""


SECRET_PATH = re.compile(r"secret|credential|passw|token|\.key\b|\.pem\b", re.I)
CONTENT_TOOLS = {"read_file", "search_text", "write_file", "edit_file"}
HIDDEN = "[内容已隐藏]"
MIN_SECRET_LENGTH = 6


def touches_secret(entry):
    return bool(SECRET_PATH.search(json.dumps(entry.get("arguments"), ensure_ascii=False)))


def secret_values(trace):
    """What secret files hold. A bare value matches no pattern, so it is tracked by where it came from."""
    values = set()
    for entry in trace:
        if entry.get("tool") == "search_text":
            # A search over a whole directory prints "<file>:<line>:<text>" for secret files too.
            found = [line.rsplit(":", 1)[-1].strip() for line in str(entry.get("output") or "").splitlines() if SECRET_PATH.search(line)]
            values.update(value for value in found if len(value) >= MIN_SECRET_LENGTH)
        if entry.get("tool") in CONTENT_TOOLS and entry.get("ok") and touches_secret(entry):
            arguments = entry.get("arguments") or {}
            for text in (entry.get("output"), arguments.get("content"), arguments.get("old_text"), arguments.get("new_text")):
                values.update(line.strip() for line in str(text or "").splitlines() if len(line.strip()) >= MIN_SECRET_LENGTH)
    return values


def safe_lines(text, secrets, limit):
    for value in secrets:
        text = text.replace(value, HIDDEN)
    return " ".join(line for line in (clean(line) for line in text.splitlines()) if line)[:limit]


def safe_transcript(symptom, trace, report, secrets):
    lines = [f"现象：{safe_lines(symptom, secrets, 300)}"]
    for entry in trace:
        if "tool" in entry and entry["tool"] != "finish":
            secret = touches_secret(entry) and entry["tool"] in CONTENT_TOOLS
            arguments = {"path": entry["arguments"].get("path")} if secret else entry.get("arguments")
            output = HIDDEN if secret else safe_lines(entry.get("output", ""), secrets, 300)
            lines.append(f"{entry['tool']} {safe_lines(json.dumps(arguments, ensure_ascii=False), secrets, 200)} -> {output}")
    lines.append(f"结论：{safe_lines(json.dumps(report or {}, ensure_ascii=False), secrets, 500)}")
    return "\n".join(lines)


def transcript(symptom, trace, report):
    lines = [f"现象：{symptom}"]
    for entry in trace:
        if "tool" in entry and entry["tool"] != "finish":
            arguments = json.dumps(entry.get("arguments"), ensure_ascii=False)[:200]
            lines.append(f"{entry['tool']} {arguments} -> {entry.get('output', '')[:300]}")
    lines.append(f"结论：{json.dumps(report or {}, ensure_ascii=False)[:500]}")
    return "\n".join(lines)


class Mem0Extraction(Mem0Memory):
    """Mem0's own pipeline: its LLM reads the transcript and decides what to keep."""

    def __init__(self, store, project, writes, folder: Path, options):
        super().__init__(store, project, writes, open_mem0(folder, model=options.model, base_url=options.base_url, instructions=INSTRUCTIONS))

    def write(self, symptom, trace, report):
        self.client.add([{"role": "user", "content": transcript(symptom, trace, report)}], user_id=self.project)


class GuardedMem0(Mem0Extraction):
    def __init__(self, store, project, writes, folder, options):
        super().__init__(store, project, writes, folder, options)
        self.secrets = set()
        self.dropped = 0

    def _safe(self, text):
        return trusted(text) and redact(text) == text and not any(value in text for value in self.secrets)

    def retrieve(self, query):
        return [text for text in super().retrieve(query) if self._safe(text)]

    def write(self, symptom, trace, report):
        verified = report.get("verified") if report else False
        if verified is None or not (verified or tested(trace, self.writes)):
            return
        self.secrets |= secret_values(trace)
        content = safe_transcript(symptom, trace, report, self.secrets)
        added = self.client.add([{"role": "user", "content": content}], user_id=self.project)
        for item in added["results"]:
            if not self._safe(item["memory"]):
                self.client.delete(item["id"])
                self.dropped += 1


class LangMemBackend(ExternalMemory):
    def __init__(self, store, project, writes, folder: Path, options):
        super().__init__(store, project, writes)
        from fastembed import TextEmbedding
        from langchain_openai import ChatOpenAI
        from langgraph.store.memory import InMemoryStore
        from langmem import create_memory_manager
        embedder = TextEmbedding(EMBEDDING_MODEL)
        self.items = InMemoryStore(index={"dims": EMBEDDING_DIMS, "fields": ["content"],
                                          "embed": lambda texts: [vector.tolist() for vector in embedder.embed(list(texts))]})
        model = ChatOpenAI(model=options.model, base_url=options.base_url, api_key="local", temperature=0.1, max_tokens=1500,
                           extra_body=NO_THINKING)
        self.manager = create_memory_manager(model, instructions=INSTRUCTIONS, enable_inserts=True, enable_updates=True)
        self.namespace = (project,)

    def retrieve(self, query):
        return [item.value["content"] for item in self.items.search(self.namespace, query=query[:500], limit=TOP_K)]

    def write(self, symptom, trace, report):
        existing = [(item.key, "Memory", {"content": item.value["content"]}) for item in self.items.search(self.namespace, limit=50)]
        messages = [{"role": "user", "content": transcript(symptom, trace, report)}]
        # LangMem runs its own LangGraph; called from the agent's graph node it would inherit that run's context.
        with ThreadPoolExecutor(1) as pool:
            extracted = pool.submit(self.manager.invoke, {"messages": messages, "existing": existing}).result()
        for found in extracted:
            content = found.content["content"] if isinstance(found.content, dict) else found.content.content
            self.items.put(self.namespace, found.id, {"content": content})

    def dump(self):
        return [item.value["content"] for item in self.items.search(self.namespace, limit=1000)]


def create(name, store, project, writes, folder, options):
    if name == "mem0-llm":
        return Mem0Extraction(store, project, writes, folder, options)
    if name == "mem0-safe":
        return GuardedMem0(store, project, writes, folder, options)
    if name == "mem0-host":
        return Mem0Memory(store, project, writes, open_mem0(folder, model=options.model, base_url=options.base_url))
    return LangMemBackend(store, project, writes, folder, options)
