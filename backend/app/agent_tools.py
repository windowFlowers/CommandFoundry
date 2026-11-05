"""Agent adapters around the existing knowledge, evidence and planner services.

The host binds the original user turn and its conversation/knowledge scope.
Model tool arguments can search or select evidence, never supply planner slots
or an executable command. Final output selects a server-produced Answer.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from pydantic import Field

from .agent_runtime import (
    AgentRun, AgentRuntime, AgentView, Contract, DecisionError, RuntimeErrorBase, ToolSpec, _safe,
)
from .answering import AnswerService
from .command_planner import CommandPlanner, contains_sensitive_input, refusal_reason_for
from .evidence import evidence_records
from .models import Answer, RetrievalHit
from .repository import ConversationRepository
from .retrieval import KnowledgeIndexManager, has_sufficient_evidence
from .recipes import has_unresolved_placeholders
from .risk import review_commands


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class SearchArgs(Contract):
    """Search the selected knowledge base; no other scope can be specified."""
    query: str = Field(min_length=1, max_length=1200)
    top_k: int = Field(default=4, ge=1, le=6)


class ReadArgs(Contract):
    """Read a citation returned by knowledge_search in this run."""
    citation_id: str = Field(pattern=r"^cit-[a-f0-9]{12}$")


class NoArgs(Contract):
    """Use the original user message; parameters are not supplied by the model."""


class AgentToolset:
    def __init__(self, *, query: str, conversation_id: str, knowledge_base_id: str,
                 repository: ConversationRepository, indexes: KnowledgeIndexManager,
                 planner: CommandPlanner, answers: AnswerService):
        self.query = query
        self.conversation_id = conversation_id
        self.knowledge_base_id = knowledge_base_id
        self.repository, self.indexes = repository, indexes
        self.planner, self.answers = planner, answers
        self.scope_fingerprint = _hash({"query": query, "conversation": conversation_id,
                                       "knowledge_base": knowledge_base_id})
        self.view: AgentView | None = None
        self.hits: list[RetrievalHit] = []
        self.records: dict[str, dict] = {}
        self.read_ids: set[str] = set()
        self.generation_calls: list[dict] = []
        self._check_scope()
        pending = self.repository.get_pending_command_plan(
            conversation_id, knowledge_base_id=knowledge_base_id)
        self.pending_topic_id = ((pending or {}).get("slots") or {}).get("__meta__", {}).get("topic_id")
        if not self.pending_topic_id:
            # Shell filters can rank generic Windows pages above a POSIX-
            # labelled recipe that also has trusted Windows variants. Resolve
            # an explicit current-turn recipe inside this KB, then grant its
            # real evidence alongside ordinary hybrid search results.
            allowed = self.indexes.get(knowledge_base_id).catalog.topics
            allowed_ids = {topic.id.removeprefix("parent.") for topic in allowed}
            scoped = SimpleNamespace(topics=allowed,
                recipes=[recipe for recipe in getattr(planner.catalog, "recipes", [])
                         if recipe.topic_id in allowed_ids])
            scoped_planner = CommandPlanner(catalog=scoped, repository=repository)
            recipe = scoped_planner._recipe_for(query, [RetrievalHit(topic=topic, score=0.0) for topic in allowed])
            if recipe:
                hit = scoped_planner._topic_hit(recipe, [], None)
                if hit:
                    self.pending_topic_id = hit.topic.id

    def _check_scope(self):
        conversation = self.repository.get(self.conversation_id)
        if (conversation is None or conversation.knowledge_base_id != self.knowledge_base_id
                or self.repository.get_knowledge_base(self.knowledge_base_id) is None):
            raise RuntimeErrorBase("knowledge_scope_mismatch")

    @staticmethod
    def _fingerprint(record):
        return _hash({"topic": record["hit"].topic.model_dump(mode="json"),
                      "text": record["text"], "parent_text": record["parent_text"],
                      "locator": record["locator"].model_dump(mode="json") if record["locator"] else None,
                      "command_evidence": record["command_evidence"]})

    def _retrieve(self, args: SearchArgs, *, plan_topic_id=None):
        self._check_scope()
        if contains_sensitive_input(args.query):
            raise RuntimeErrorBase("sensitive_content")
        hits = self.indexes.retrieve_advanced(self.knowledge_base_id, args.query, top_k=args.top_k)
        topic_id = plan_topic_id
        if topic_id and not any(hit.topic.id == topic_id for hit in hits):
            # A short clarification reply can omit the task name. Resolve the
            # persisted plan's topic only inside the host-bound knowledge base.
            topic = next((topic for topic in self.indexes.get(self.knowledge_base_id).catalog.topics
                          if topic.id == topic_id), None)
            if topic:
                hits.append(RetrievalHit(topic=topic, score=0.0))
        records = evidence_records(hits)
        # Check full model-visible data, including content beyond the excerpts
        # shown to the decision model. Truncation cannot hide credentials.
        _safe({"hits": [hit.model_dump(mode="json") for hit in hits],
               "parents": [record["parent_text"] for record in records]}, limit=200000)
        return hits, records

    def _add_records(self, hits, records):
        by_topic = {hit.topic.id: hit for hit in self.hits}
        for hit in hits:
            by_topic[hit.topic.id] = hit
        self.hits = list(by_topic.values())
        for record in records:
            previous = self.records.get(record["citation_id"])
            if previous and self._fingerprint(previous) != self._fingerprint(record):
                raise RuntimeErrorBase("evidence_revision_changed")
            # Citation models created by the legacy service may share locator
            # objects with retrieval hits. Keep an independent evidence copy
            # so accidental answer mutation cannot move both sides together.
            self.records[record["citation_id"]] = {
                **record, "hit": record["hit"].model_copy(deep=True),
                "locator": record["locator"].model_copy(deep=True) if record["locator"] else None,
                "command_evidence": list(record["command_evidence"]),
            }
        if len(self.records) > 48:
            raise RuntimeErrorBase("evidence_budget_exhausted")

    def bind(self, view: AgentView):
        """Rebuild evidence grants from durable observations after a restart."""
        self._check_scope()
        if view.query != self.query:
            raise RuntimeErrorBase("query_scope_mismatch")
        self.view = view
        self.hits, self.records, self.read_ids = [], {}, set()
        for observation in view.observations:
            if observation.name == "knowledge_search":
                args = SearchArgs.model_validate(observation.arguments)
                output = observation.output
                if not isinstance(output, dict) or output.get("scope") != self.scope_fingerprint:
                    raise RuntimeErrorBase("evidence_scope_mismatch")
                hits, records = self._retrieve(args, plan_topic_id=output.get("plan_topic_id"))
                current = {record["citation_id"]: self._fingerprint(record) for record in records}
                saved = {item["citation_id"]: item["fingerprint"] for item in output["evidence"]}
                if current != saved:
                    raise RuntimeErrorBase("evidence_revision_changed")
                self._add_records(hits, records)
            elif observation.name == "knowledge_read":
                args = ReadArgs.model_validate(observation.arguments)
                record = self.records.get(args.citation_id)
                if (record is None or not isinstance(observation.output, dict)
                        or observation.output.get("fingerprint") != self._fingerprint(record)):
                    raise RuntimeErrorBase("evidence_revision_changed")
                self.read_ids.add(args.citation_id)

    def _search(self, args: SearchArgs):
        hits, records = self._retrieve(args, plan_topic_id=self.pending_topic_id)
        self._add_records(hits, records)
        return {"scope": self.scope_fingerprint, "plan_topic_id": self.pending_topic_id, "evidence": [
            {"citation_id": record["citation_id"], "fingerprint": self._fingerprint(record),
             "topic_id": record["hit"].topic.id, "title": record["hit"].topic.title,
             "excerpt": record["text"][:500], "source_id": record["hit"].topic.source.source_id,
             "revision": record["hit"].topic.source.revision,
             "chunk_id": record["chunk_id"], "parent_id": record["parent_id"]}
            for record in records]}

    def _read(self, args: ReadArgs):
        self._check_scope()
        record = self.records.get(args.citation_id)
        if record is None:
            raise RuntimeErrorBase("citation_not_granted")
        self.read_ids.add(args.citation_id)
        return {"citation_id": args.citation_id, "fingerprint": self._fingerprint(record),
                "excerpt": record["text"][:2000], "parent_context": record["parent_text"][:4000],
                "locator": record["locator"].model_dump(mode="json") if record["locator"] else None,
                "commands": record["command_evidence"][:4]}

    def _require_evidence(self):
        self._check_scope()
        if self.view is None or not any(o.name == "knowledge_search" for o in self.view.observations):
            raise RuntimeErrorBase("knowledge_search_required")
        if self.hits and not self.read_ids:
            raise RuntimeErrorBase("knowledge_read_required")

    def _cached_plan(self):
        if self.view:
            return next((o.output for o in reversed(self.view.observations)
                         if o.name in {"command_plan", "answer_prepare"} and isinstance(o.output, dict)), None)
        return None

    def _validate_answer(self, value):
        answer = Answer.model_validate(value)
        valid_ids = set(self.records)
        citations = {item.citation_id for item in answer.citations}
        if not citations <= valid_ids:
            raise RuntimeErrorBase("invalid_citation")
        for citation in answer.citations:
            record = self.records[citation.citation_id]
            source = record["hit"].topic.source
            if (citation.source_id != source.source_id or citation.revision != source.revision
                    or citation.index_revision != source.index_revision or citation.document_sha256 != source.sha256
                    or citation.chunk_id != record["chunk_id"] or citation.parent_id != record["parent_id"]
                    or citation.locator != record["locator"] or citation.document_id != source.document_id
                    or citation.knowledge_base_id != source.knowledge_base_id
                    or citation.source_url != source.source_url or citation.license != source.license):
                raise RuntimeErrorBase("invalid_citation")
        for item in [*answer.commands, *answer.summary_segments]:
            if not set(item.citation_ids) <= citations:
                raise RuntimeErrorBase("invalid_citation")
        if answer.command_plan:
            recipe = self.planner._recipe_for(self.query, self.hits,
                {"template_id": answer.command_plan.template_id})
            hit = self.planner._topic_hit(recipe, self.hits, None) if recipe else None
            if (recipe is None or hit is None or answer.command_plan.source_revision !=
                    self.planner._revision_for_recipe(recipe, hit, self.hits)):
                raise RuntimeErrorBase("recipe_revision_changed")
        if answer.answer_kind == "direct":
            if not answer.commands or any(not command.citation_ids or has_unresolved_placeholders(command.code)
                                          for command in answer.commands):
                raise RuntimeErrorBase("invalid_direct_answer")
        reviewed = review_commands(answer.commands)
        if _json([c.model_dump(mode="json") for c in reviewed]) != _json(
                [c.model_dump(mode="json") for c in answer.commands]):
            raise RuntimeErrorBase("risk_review_mismatch")
        return answer

    def _recipe_fingerprint(self, answer: Answer | None):
        if answer is None or answer.command_plan is None:
            return None
        recipe = self.planner._recipe_for(self.query, self.hits,
            {"template_id": answer.command_plan.template_id})
        if recipe is None:
            raise RuntimeErrorBase("recipe_revision_changed")
        return _hash(asdict(recipe))

    def _envelope_answer(self, envelope):
        if envelope.get("scope") != self.scope_fingerprint:
            raise RuntimeErrorBase("answer_scope_mismatch")
        answer = self._validate_answer(envelope["answer"])
        if envelope.get("recipe_fingerprint") != self._recipe_fingerprint(answer):
            raise RuntimeErrorBase("recipe_revision_changed")
        return answer

    def _envelope(self, answer: Answer | None, *, planner_owned: bool):
        return {"scope": self.scope_fingerprint, "planner_checked": True,
                "planner_owned": planner_owned,
                "recipe_fingerprint": self._recipe_fingerprint(answer),
                "answer": self._validate_answer(answer).model_dump(mode="json") if answer else None}

    def _plan(self, _: NoArgs):
        self._require_evidence()
        cached = self._cached_plan()
        if cached is not None:
            return cached
        pending = self.repository.get_pending_command_plan(
            self.conversation_id, knowledge_base_id=self.knowledge_base_id)
        # Reuse the planner with a scope-limited catalog so its compatibility
        # fallback cannot resolve unrelated builtin topics for a private KB.
        ids = {hit.topic.id.removeprefix("parent.") for hit in self.hits}
        scoped = SimpleNamespace(topics=[hit.topic for hit in self.hits],
            recipes=[recipe for recipe in getattr(self.planner.catalog, "recipes", []) if recipe.topic_id in ids])
        planner = CommandPlanner(catalog=scoped, repository=self.repository)
        owned = bool(pending or planner._recipe_for(self.query, self.hits, pending)
                     or planner.is_template_request(self.query))
        answer = planner.handle(self.query, self.hits, conversation_id=self.conversation_id,
                                knowledge_base_id=self.knowledge_base_id)
        return self._envelope(answer, planner_owned=owned)

    def _prepare(self, _: NoArgs):
        self._require_evidence()
        planned = self._cached_plan() or self._plan(NoArgs())
        if planned.get("answer") is not None:
            return planned
        if self.planner.is_template_request(self.query) or planned["planner_owned"]:
            answer = self.answers.local_fallback(self.hits, reason="no_api_key", query=self.query,
                                                 single_template=True)
        elif not has_sufficient_evidence(self.hits):
            answer = self.answers.refusal("no_evidence")
        else:
            started = perf_counter()
            answer, _ = self.answers.answer(self.query, self.hits)
            failures = {"timeout": "provider_timeout", "rate_limit": "provider_rate_limit",
                        "authentication": "provider_authentication", "provider_error": "provider_error",
                        "invalid_json": "invalid_model_decision"}
            generation = answer.generation
            self.generation_calls.append({"attempted": generation.attempted,
                "error_code": failures.get(generation.fallback_reason),
                "latency_ms": round((perf_counter() - started) * 1000),
                "prompt_tokens": generation.prompt_tokens or 0,
                "completion_tokens": generation.completion_tokens or 0,
                "total_tokens": generation.total_tokens or 0})
            if answer.generation.attempted and answer.generation.fallback_reason in failures:
                raise DecisionError(failures[answer.generation.fallback_reason])
        return self._envelope(answer, planner_owned=planned["planner_owned"])

    def tools(self):
        revision = "m2-v1:" + self.scope_fingerprint
        return [ToolSpec(name=name, arguments_model=model, executor=execute, revision=revision)
                for name, model, execute in [
                    ("knowledge_search", SearchArgs, self._search),
                    ("knowledge_read", ReadArgs, self._read),
                    ("command_plan", NoArgs, self._plan),
                    ("answer_prepare", NoArgs, self._prepare)]]

    def finish(self, view: AgentView, answer_call_id: str) -> str:
        self.bind(view)
        observation = next((o for o in view.observations if o.call_id == answer_call_id), None)
        if (observation is None or observation.name not in {"command_plan", "answer_prepare"}
                or not isinstance(observation.output, dict)
                or observation.output.get("scope") != self.scope_fingerprint
                or observation.output.get("answer") is None):
            raise RuntimeErrorBase("unverified_final_answer")
        return _json(self._envelope_answer(observation.output).model_dump(mode="json"))


class AgentSession:
    """Host-scoped entry point for the real model/tool loop (UI joins in M5)."""
    def __init__(self, database_path: Path, *, toolset: AgentToolset, generator, max_steps=12):
        from .agent_model import AgentModelDecider
        self.toolset = toolset
        self.model = AgentModelDecider(generator, tools=toolset.tools(), finish_resolver=toolset.finish,
            descriptions={
                "knowledge_search": "Search the selected KB for this task. Start here. Returns exact citation IDs.",
                "knowledge_read": "Read an exact citation from knowledge_search, including parent context and command evidence. Read relevant evidence before planning or answering.",
                "command_plan": "Plan the original user task using trusted recipes and only original user facts. No arguments. Returns a direct command, one clarification question, a template, or no recipe. Finish an available answer with agent_finish.",
                "answer_prepare": "Prepare a validated answer for the original user task after searching and reading evidence. Reuses command planning first; otherwise uses existing grounded RAG. No model-supplied command or parameters. Finish this answer with agent_finish.",
            })
        def decide(view):
            toolset.bind(view)
            return self.model(view)
        self.runtime = AgentRuntime(database_path, decider=decide, tools=toolset.tools(),
                                    max_steps=max_steps, scope_fingerprint=toolset.scope_fingerprint,
                                    before_tool=toolset.bind)

    def create(self) -> AgentRun:
        if contains_sensitive_input(self.toolset.query) or refusal_reason_for(self.toolset.query):
            raise RuntimeErrorBase("query_refused")
        return self.runtime.create(self.toolset.query)

    def run(self, run_id: str) -> AgentRun:
        return self.runtime.run(run_id)

    def answer(self, run_id: str) -> Answer:
        run = self.runtime.get(run_id)
        if run.status != "completed" or not run.answer:
            raise RuntimeErrorBase("run_not_completed")
        view = AgentView.model_validate({key: getattr(run, key) for key in AgentView.model_fields})
        self.toolset.bind(view)
        answer = self.toolset._validate_answer(json.loads(run.answer))
        envelope = next((o.output for o in run.observations
            if o.name in {"command_plan", "answer_prepare"} and isinstance(o.output, dict)
            and o.output.get("scope") == self.toolset.scope_fingerprint
            and o.output.get("answer") == answer.model_dump(mode="json")), None)
        if envelope is None:
            raise RuntimeErrorBase("unverified_final_answer")
        self.toolset._envelope_answer(envelope)
        return answer

    def close(self):
        self.runtime.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
