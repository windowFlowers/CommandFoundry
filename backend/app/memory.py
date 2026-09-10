from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from queue import Queue
from threading import RLock, Thread

from .generation import DeepSeekGenerator
from .models import (
    ChatMessage,
    ContextInfo,
    Conversation,
    ConversationMemory,
    MemorySummary,
    RetrievalHit,
)
from .repository import ConversationRepository


CJK_CHARACTER = re.compile(r"[\u3400-\u9fff]")
REFERENCE_PATTERN = re.compile(
    r"(^\s*那|它|这个|那个|上述|上面|前面|前一个|后一种|第一种|第二种|第三种|"
    r"这种|该方法|该命令|换成|改成|怎么写\s*[？?]?$|安全吗|会丢|还有别的|呢\s*[？?]?$)",
    re.IGNORECASE,
)
ORDINAL_PATTERN = re.compile(r"第\s*([一二三123])\s*(?:种|个|条|项|方案|方法)")
CONSTRAINT_PATTERN = re.compile(r"必须|不要|不能|只(?:要|用|允许)|安全|不丢|避免|限制|不自动")
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"),
    re.compile(r"(?i)\b(password|passwd|token|api[_ -]?key)\s*[:=]\s*\S+"),
)
TECHNOLOGIES = (
    "PostgreSQL",
    "PowerShell",
    "Kubernetes",
    "Windows",
    "macOS",
    "Linux",
    "MySQL",
    "Docker",
    "Node.js",
    "Node",
    "Python",
    "Redis",
    "Nginx",
    "GitHub",
    "Git",
    "pnpm",
    "npm",
    "Shell",
)


def estimate_tokens(text: str) -> int:
    """Cheap, conservative token estimate for mixed Chinese, prose and command text."""
    if not text:
        return 0
    cjk = len(CJK_CHARACTER.findall(text))
    remainder = CJK_CHARACTER.sub("", text)
    return max(1, cjk + math.ceil(len(remainder.encode("utf-8")) / 4))


@dataclass(frozen=True)
class ConversationTurn:
    user: ChatMessage
    assistant: ChatMessage


@dataclass(frozen=True)
class BuiltMemoryContext:
    info: ContextInfo
    prompt_payload: str


def _complete_turns(messages: list[ChatMessage]) -> list[ConversationTurn]:
    turns: list[ConversationTurn] = []
    pending_user: ChatMessage | None = None
    for message in messages:
        if message.role == "user":
            pending_user = message
        elif pending_user is not None and message.answer is not None:
            turns.append(ConversationTurn(user=pending_user, assistant=message))
            pending_user = None
    return turns


def _after_boundary(messages: list[ChatMessage], *boundary_ids: str | None) -> list[ChatMessage]:
    positions = {message.id: index for index, message in enumerate(messages)}
    boundary = max((positions.get(value, -1) for value in boundary_ids if value), default=-1)
    return messages[boundary + 1 :]


def _summary_has_content(summary: MemorySummary) -> bool:
    return bool(
        summary.current_goal
        or summary.environments
        or summary.constraints
        or summary.decisions
        or summary.open_questions
        or summary.history_topics
    )


def _sanitize_memory_text(value: str, *, limit: int) -> str:
    """Keep conversational intent while removing common secrets and pasted code blocks."""
    text = re.sub(r"```[\s\S]*?```", "[代码块已省略]", value)
    text = re.sub(r"`[^`\r\n]+`", "[命令已省略]", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def _answer_memory_payload(
    message: ChatMessage,
    *,
    summary_limit: int = 320,
    command_limit: int = 4,
    label_limit: int = 64,
    note_limit: int = 2,
) -> dict:
    answer = message.answer
    if answer is None:
        return {}
    return {
        "summary": _sanitize_memory_text(answer.summary, limit=summary_limit),
        "commands": [
            {
                "position": index + 1,
                "label": _sanitize_memory_text(command.label, limit=label_limit),
                "risk": command.risk.value,
                "warning": _sanitize_memory_text(command.warning or "", limit=96),
            }
            for index, command in enumerate(answer.commands[:command_limit])
        ],
        "notes": [_sanitize_memory_text(note, limit=96) for note in answer.notes[:note_limit]],
    }


def _turn_payload(turn: ConversationTurn, *, compact: bool = False) -> dict:
    return {
        "user_message_id": turn.user.id,
        "assistant_message_id": turn.assistant.id,
        "user": _sanitize_memory_text(turn.user.query or "", limit=240 if compact else 520),
        "assistant": _answer_memory_payload(
            turn.assistant,
            summary_limit=160 if compact else 320,
            command_limit=2 if compact else 4,
            label_limit=48 if compact else 64,
            note_limit=0 if compact else 2,
        ),
    }


def _summary_context_payload(summary: MemorySummary) -> dict:
    """Bound stored summaries before putting them back into a generation prompt."""
    return {
        "current_goal": _sanitize_memory_text(summary.current_goal, limit=160),
        "environments": [_sanitize_memory_text(item, limit=64) for item in summary.environments[-2:]],
        "constraints": [_sanitize_memory_text(item, limit=64) for item in summary.constraints[-2:]],
        "decisions": [_sanitize_memory_text(item, limit=64) for item in summary.decisions[-2:]],
        "open_questions": [_sanitize_memory_text(item, limit=64) for item in summary.open_questions[-2:]],
        "history_topics": [_sanitize_memory_text(item, limit=64) for item in summary.history_topics[-2:]],
    }


def _unique_recent(values: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    for value in reversed(values):
        normalized = value.strip()
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return list(reversed(result))


def _detect_environments(texts: list[str]) -> list[str]:
    joined = "\n".join(texts)
    return [name for name in TECHNOLOGIES if re.search(re.escape(name), joined, re.IGNORECASE)]


def build_local_summary(previous: MemorySummary, turns: list[ConversationTurn]) -> MemorySummary:
    queries = [
        _sanitize_memory_text(turn.user.query or "", limit=180)
        for turn in turns
        if (turn.user.query or "").strip()
    ]
    environments = _unique_recent(
        [*previous.environments, *_detect_environments(queries)],
        limit=12,
    )
    constraints = _unique_recent(
        [
            *previous.constraints,
            *(query for query in queries if CONSTRAINT_PATTERN.search(query)),
        ],
        limit=12,
    )
    topics = _unique_recent([*previous.history_topics, *queries], limit=12)
    return MemorySummary(
        current_goal=queries[-1] if queries else previous.current_goal,
        environments=environments,
        constraints=constraints,
        decisions=previous.decisions,
        open_questions=previous.open_questions,
        history_topics=topics,
    )


def _technology_in(text: str) -> str | None:
    return next((name for name in TECHNOLOGIES if re.search(re.escape(name), text, re.IGNORECASE)), None)


def resolve_contextual_query(query: str, turn: ConversationTurn | None) -> str:
    current = query.strip()
    if turn is None or not REFERENCE_PATTERN.search(current):
        return current
    previous = (turn.user.query or "").strip()
    if not previous:
        return current
    target = _technology_in(current)
    previous_technology = _technology_in(previous)
    if target and previous_technology and target.casefold() != previous_technology.casefold():
        rewritten = re.sub(
            re.escape(previous_technology),
            target,
            previous,
            flags=re.IGNORECASE,
        )
        return f"{rewritten}；补充要求：{current}"
    commands = (turn.assistant.answer.commands if turn.assistant.answer else [])[:6]
    ordinal = ORDINAL_PATTERN.search(current)
    selected_label = ""
    if ordinal:
        ordinal_index = {"一": 0, "1": 0, "二": 1, "2": 1, "三": 2, "3": 2}[ordinal.group(1)]
        if ordinal_index < len(commands):
            selected_label = commands[ordinal_index].label[:80]
    if selected_label:
        technology_context = previous_technology or previous
        return f"{technology_context}；上一回答指向方案：{selected_label}；追问：{current}"
    labels = "、".join(command.label[:80] for command in commands)
    label_context = f"；上一回答方案：{labels}" if labels else ""
    return f"{previous}{label_context}；追问：{current}"


def merge_contextual_hits(
    raw_hits: list[RetrievalHit],
    contextual_hits: list[RetrievalHit],
    *,
    top_k: int,
    raw_weight: float = 0.35,
    contextual_weight: float = 1.0,
    constant: int = 60,
) -> list[RetrievalHit]:
    scores: dict[str, float] = {}
    representatives: dict[str, RetrievalHit] = {}
    for hits, weight in ((raw_hits, raw_weight), (contextual_hits, contextual_weight)):
        for rank, hit in enumerate(hits, start=1):
            topic_id = hit.topic.id
            scores[topic_id] = scores.get(topic_id, 0.0) + weight / (constant + rank)
            existing = representatives.get(topic_id)
            if existing is None or weight == contextual_weight:
                if existing is not None:
                    matched = {item.chunk_id: item for item in existing.matched_chunks}
                    matched.update({item.chunk_id: item for item in hit.matched_chunks})
                    hit = hit.model_copy(update={"matched_chunks": list(matched.values())})
                representatives[topic_id] = hit
            elif existing:
                matched = {item.chunk_id: item for item in existing.matched_chunks}
                matched.update({item.chunk_id: item for item in hit.matched_chunks})
                representatives[topic_id] = existing.model_copy(
                    update={
                        "bm25_score": max(existing.bm25_score, hit.bm25_score),
                        "vector_score": max(existing.vector_score, hit.vector_score),
                        "matched_chunks": list(matched.values()),
                    }
                )
    # A short raw follow-up (for example “第二种安全吗”) often has only generic
    # lexical overlap. Preserve it as a weak signal, while anchoring the result on
    # the best match from the explicitly contextualized query.
    if contextual_hits:
        anchor_id = contextual_hits[0].topic.id
        scores[anchor_id] = scores.get(anchor_id, 0.0) + contextual_weight / (constant + 1)
    ordered = sorted(scores, key=scores.get, reverse=True)
    return [
        representatives[topic_id].model_copy(update={"score": round(scores[topic_id], 6)})
        for topic_id in ordered[:top_k]
    ]


class ConversationMemoryService:
    def __init__(
        self,
        *,
        repository: ConversationRepository,
        generator: DeepSeekGenerator,
        context_budget: int = 2400,
        summary_trigger_tokens: int = 1800,
        summary_trigger_turns: int = 6,
    ) -> None:
        self.repository = repository
        self.generator = generator
        self.context_budget = context_budget
        self.summary_trigger_tokens = summary_trigger_tokens
        self.summary_trigger_turns = summary_trigger_turns
        self._queue: Queue[str] = Queue()
        self._queued: set[str] = set()
        self._queue_lock = RLock()
        self._worker = Thread(target=self._worker_loop, name="aegis-memory-summarizer", daemon=True)
        self._worker.start()
        for conversation_id in self.repository.list_pending_memory_ids():
            self.enqueue(conversation_id)

    @staticmethod
    def _parse_summary(record: dict) -> MemorySummary:
        try:
            return MemorySummary.model_validate_json(record.get("summary_json") or "{}")
        except (ValueError, TypeError):
            return MemorySummary()

    def build_context(self, conversation: Conversation, query: str) -> BuiltMemoryContext:
        record = self.repository.get_memory_record(conversation.id)
        summary = self._parse_summary(record)
        summary_used = _summary_has_content(summary)
        messages = _after_boundary(
            conversation.messages,
            record.get("reset_after_message_id"),
            record.get("compacted_through_message_id"),
        )
        turns = _complete_turns(messages)
        summary_payload = _summary_context_payload(summary) if summary_used else None
        base_payload: dict = {"summary": summary_payload}
        base_tokens = estimate_tokens(json.dumps(base_payload, ensure_ascii=False)) if summary_used else 0
        # Reserve a small amount for JSON keys and separators that are not present in
        # the per-turn estimate. The newest two turns are admitted in compact form if
        # the full payload would consume their shared budget.
        remaining = max(0, self.context_budget - base_tokens - 48)
        selected_reversed: list[tuple[ConversationTurn, dict]] = []
        reversed_turns = list(reversed(turns))
        for index, turn in enumerate(reversed_turns):
            payload = _turn_payload(turn)
            serialized = json.dumps(payload, ensure_ascii=False)
            cost = estimate_tokens(serialized)
            priority_turn = index < 2
            reserve_for_next = 360 if index == 0 and len(reversed_turns) > 1 else 0
            allowed = max(0, remaining - reserve_for_next)
            if priority_turn and cost > allowed:
                payload = _turn_payload(turn, compact=True)
                serialized = json.dumps(payload, ensure_ascii=False)
                cost = estimate_tokens(serialized)
            if cost > allowed:
                if priority_turn:
                    continue
                break
            selected_reversed.append((turn, payload))
            remaining = max(0, remaining - cost)
        selected_pairs = list(reversed(selected_reversed))
        selected = [turn for turn, _ in selected_pairs]
        retrieval_query = resolve_contextual_query(query, selected[-1] if selected else None)
        used_ids = [message.id for turn in selected for message in (turn.user, turn.assistant)]
        prompt = {
            "summary": summary_payload,
            "recent_turns": [payload for _, payload in selected_pairs],
        }
        prompt_payload = json.dumps(prompt, ensure_ascii=False) if summary_used or selected else ""
        tokens = estimate_tokens(prompt_payload)
        # The caps above make this exceptional, but never violate the public budget
        # even if a future schema adds larger fields.
        while tokens > self.context_budget and prompt["recent_turns"]:
            prompt["recent_turns"].pop(0)
            if selected:
                selected.pop(0)
            prompt_payload = json.dumps(prompt, ensure_ascii=False)
            tokens = estimate_tokens(prompt_payload)
        used_ids = [message.id for turn in selected for message in (turn.user, turn.assistant)]
        used = bool(summary_used or selected)
        return BuiltMemoryContext(
            info=ContextInfo(
                used=used,
                strategy="summary_recent" if summary_used else "recent" if selected else "none",
                recent_turn_count=len(selected),
                used_message_ids=used_ids,
                summary_used=summary_used,
                retrieval_query=retrieval_query,
                estimated_tokens=tokens,
            ),
            prompt_payload=prompt_payload,
        )

    def compact_if_needed(self, conversation_id: str) -> None:
        conversation = self.repository.get(conversation_id)
        if conversation is None:
            return
        record = self.repository.get_memory_record(conversation_id)
        messages = _after_boundary(
            conversation.messages,
            record.get("reset_after_message_id"),
            record.get("compacted_through_message_id"),
        )
        turns = _complete_turns(messages)
        token_count = sum(
            estimate_tokens(json.dumps(_turn_payload(turn), ensure_ascii=False))
            for turn in turns
        )
        if len(turns) <= self.summary_trigger_turns and token_count <= self.summary_trigger_tokens:
            return
        compacted = turns[:-2]
        if not compacted:
            return
        previous = self._parse_summary(record)
        local_summary = build_local_summary(previous, compacted)
        pending_input = None
        if self.generator.available:
            pending_input = {
                "previous_summary": previous.model_dump(mode="json"),
                "turns": [_turn_payload(turn) for turn in compacted],
            }
        self.repository.save_local_memory(
            conversation_id,
            summary=local_summary,
            compacted_through_message_id=compacted[-1].assistant.id,
            estimated_tokens=estimate_tokens(local_summary.model_dump_json()),
            pending_input=pending_input,
        )
        if pending_input:
            self.enqueue(conversation_id)

    def enqueue(self, conversation_id: str) -> None:
        with self._queue_lock:
            if conversation_id in self._queued:
                return
            self._queued.add(conversation_id)
            self._queue.put(conversation_id)

    def _worker_loop(self) -> None:
        while True:
            conversation_id = self._queue.get()
            try:
                while True:
                    record = self.repository.claim_pending_memory(conversation_id)
                    if record is None:
                        break
                    revision = int(record["revision"])
                    try:
                        payload = json.loads(record["pending_input_json"])
                        summary = self.generator.summarize_memory(payload)
                        self.repository.complete_model_memory(
                            conversation_id,
                            expected_revision=revision,
                            summary=summary,
                        )
                    except Exception as exc:
                        self.repository.complete_model_memory(
                            conversation_id,
                            expected_revision=revision,
                            summary=None,
                            error=type(exc).__name__,
                        )
            finally:
                with self._queue_lock:
                    self._queued.discard(conversation_id)
                self._queue.task_done()
                try:
                    if self.repository.get_memory_record(conversation_id)["status"] == "pending":
                        self.enqueue(conversation_id)
                except KeyError:
                    pass

    def get_state(self, conversation_id: str) -> ConversationMemory:
        return self.repository.memory_state(conversation_id)

    def reset(self, conversation_id: str) -> bool:
        return self.repository.reset_memory(conversation_id)
