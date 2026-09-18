from __future__ import annotations

import json
import inspect
import re

import httpx
from openai import APIStatusError, APITimeoutError

from .evidence import evidence_records
from .generation import DeepSeekGenerator
from .models import (
    Answer,
    AnswerSegment,
    Citation,
    CommandBlock,
    ContextInfo,
    GenerationInfo,
    PersonalizationInfo,
    RetrievalHit,
)
from .risk import review_commands
from .recipes import has_unresolved_placeholders


def _normalize_command_code(value: str) -> str:
    normalized = "\n".join(
        line.rstrip() for line in value.replace("\r\n", "\n").strip().split("\n")
    )
    return re.sub(r"<[^<>\r\n]+>", "<placeholder>", normalized)


def _strict_command_code(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").strip().split("\n"))


def grounded_commands(commands: list[CommandBlock], hits: list[RetrievalHit]) -> list[CommandBlock]:
    """Return only canonical commands that occur in the current retrieval evidence."""
    evidence: dict[str, tuple[CommandBlock, list[str]]] = {}
    strict_upload: dict[str, list[str]] = {}
    records = evidence_records(hits)
    record_ids_by_hit: dict[str, list[str]] = {}
    for record in records:
        record_ids_by_hit.setdefault(record["hit"].topic.id, []).append(record["citation_id"])
        if record["hit"].topic.source.kind == "upload":
            for code in record["command_evidence"]:
                strict_upload.setdefault(_strict_command_code(code), []).append(record["citation_id"])
    for hit in hits:
        if hit.topic.source.kind == "upload":
            continue
        ids = record_ids_by_hit.get(hit.topic.id, [])
        for canonical in hit.topic.commands:
            evidence[_normalize_command_code(canonical.code)] = (canonical, ids)
    grounded = []
    seen: set[str] = set()
    for command in commands:
        normalized = _normalize_command_code(command.code)
        strict = _strict_command_code(command.code)
        upload_ids = list(dict.fromkeys(strict_upload.get(strict, [])))
        canonical_entry = evidence.get(normalized)
        if not upload_ids and canonical_entry is None:
            continue
        seen_key = f"upload:{strict}" if upload_ids else f"builtin:{normalized}"
        if seen_key in seen:
            continue
        seen.add(seen_key)
        if upload_ids:
            grounded.append(command.model_copy(update={"citation_ids": upload_ids}))
        else:
            canonical, ids = canonical_entry  # type: ignore[misc]
            grounded.append(canonical.model_copy(update={"citation_ids": list(dict.fromkeys(ids))}))
    return grounded


def citations_from_hits(hits: list[RetrievalHit]) -> list[Citation]:
    citations: list[Citation] = []
    for record in evidence_records(hits):
        hit = record["hit"]
        source = hit.topic.source
        citations.append(
            Citation(
                source_id=source.source_id,
                title=source.title,
                source_url=source.source_url,
                license=source.license,
                revision=source.revision,
                excerpt=str(record["text"])[:360],
                score=record.get("score", hit.score),
                domain=hit.topic.domain,
                document_id=source.document_id,
                knowledge_base_id=source.knowledge_base_id,
                source_kind=source.kind,
                citation_id=record["citation_id"],
                chunk_id=record["chunk_id"],
                parent_id=record["parent_id"],
                document_sha256=source.sha256,
                index_revision=source.index_revision,
                locator=record["locator"],
            )
        )
    return citations


def _ground_segments(
    summary: str,
    segments: list[AnswerSegment],
    citations: list[Citation],
    *,
    allow_deterministic_summary_fallback: bool = False,
) -> list[AnswerSegment]:
    """Keep only model segments that name current evidence citations.

    A deterministic local summary is constructed directly from the selected
    local topic, so it can reasonably inherit that topic's first citation when
    the UI needs a segment.  A model-written summary is different: assigning
    its entire text to an arbitrary first citation would falsely imply precise
    support.  Callers must opt in only for the deterministic case.
    """
    ordered_valid = [item.citation_id for item in citations if item.citation_id]
    valid = set(ordered_valid)
    grounded: list[AnswerSegment] = []
    for segment in segments:
        ids = [value for value in dict.fromkeys(segment.citation_ids) if value in valid]
        if segment.text.strip() and ids:
            grounded.append(segment.model_copy(update={"citation_ids": ids}))
    if allow_deterministic_summary_fallback and not grounded and summary.strip() and ordered_valid:
        grounded.append(AnswerSegment(text=summary.strip(), citation_ids=[ordered_valid[0]]))
    return grounded


def _fallback_preference_texts(personalization_context: str) -> list[str]:
    """Read the already-sanitized preference display values defensively.

    The model context is a JSON array produced by ``PersonalizationService``.
    A fallback must remain available for old callers and malformed stored data,
    so this intentionally treats it as optional, bounded presentation input
    rather than trusting its schema or using it as technical evidence.
    """

    if not isinstance(personalization_context, str) or not personalization_context.strip():
        return []
    try:
        payload = json.loads(personalization_context)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    values: list[str] = []
    for item in payload[:8]:
        if not isinstance(item, dict):
            continue
        for field in ("preference", "display_text", "value"):
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                values.append(value.strip()[:240])
    return values


def _last_match_position(text: str, phrases: tuple[str, ...]) -> int:
    lowered = text.casefold()
    return max((lowered.rfind(phrase.casefold()) for phrase in phrases), default=-1)


def _last_exact_token_position(text: str, token: str) -> int:
    matches = list(re.finditer(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])", text, re.IGNORECASE))
    return matches[-1].start() if matches else -1


def _fallback_presentation(query: str, personalization_context: str) -> tuple[str, str, str | None]:
    """Return language, detail level, and expertise for deterministic prose.

    Stored preferences are applied first.  The current query is inspected last,
    so an explicit request such as “这次请用英文回答并保持简洁” wins even
    when the saved profile says otherwise.  This controls only locally-written
    glue text; commands and citations continue to come exclusively from the
    retrieved evidence.
    """

    language = "zh-CN"
    detail = "concise"
    expertise: str | None = None

    def apply(text: str) -> tuple[str | None, str | None, str | None]:
        english = max(
            _last_match_position(text, ("英文", "英语", "english", "en-us")),
            _last_exact_token_position(text, "en"),
        )
        chinese = max(
            _last_match_position(text, ("中文", "汉语", "chinese", "zh-cn")),
            _last_exact_token_position(text, "zh"),
        )
        detected_language = (
            "en" if english > chinese and english >= 0 else "zh-CN" if chinese >= 0 else None
        )

        concise = _last_match_position(text, ("简短", "简洁", "精炼", "少废话", "concise", "brief"))
        detailed = _last_match_position(text, ("详细", "展开", "多解释", "讲透", "detailed", "verbose"))
        detected_detail = (
            "detailed"
            if detailed > concise and detailed >= 0
            else "concise"
            if concise >= 0
            else None
        )

        beginner = _last_match_position(text, ("新手", "初学者", "入门", "beginner", "novice", "junior"))
        intermediate = _last_match_position(text, ("中级", "intermediate", "mid"))
        advanced = _last_match_position(text, ("高级", "资深", "专家", "advanced", "senior", "expert"))
        positions = {
            "beginner": beginner,
            "intermediate": intermediate,
            "advanced": advanced,
        }
        detected_expertise, position = max(positions.items(), key=lambda item: item[1])
        return detected_language, detected_detail, detected_expertise if position >= 0 else None

    for text in _fallback_preference_texts(personalization_context):
        detected_language, detected_detail, detected_expertise = apply(text)
        if detected_language:
            language = detected_language
        if detected_detail:
            detail = detected_detail
        if detected_expertise:
            expertise = detected_expertise

    query_language, query_detail, query_expertise = apply(query)
    if query_language:
        language = query_language
    if query_detail:
        detail = query_detail
    if query_expertise:
        expertise = query_expertise
    return language, detail, expertise


class AnswerService:
    def __init__(self, generator: DeepSeekGenerator) -> None:
        self.generator = generator

    def local_fallback(
        self,
        hits: list[RetrievalHit],
        *,
        reason: str,
        attempted: bool = False,
        context: ContextInfo | None = None,
        personalization: PersonalizationInfo | None = None,
        query: str = "",
        personalization_context: str = "",
        single_template: bool = False,
    ) -> Answer:
        language, detail, expertise = _fallback_presentation(query, personalization_context)
        generation = GenerationInfo(
            attempted=attempted,
            provider=getattr(self.generator, "provider", "deepseek") if attempted else "local",
            model=getattr(self.generator, "model", None) if attempted else None,
            fallback_reason=reason,
        )
        if not hits:
            if language == "en":
                summary = "The local knowledge base does not yet contain enough relevant command guidance."
                notes = [
                    "Add the technology stack, target action, or error message and try again, for example: “Docker show the latest 100 log lines.”"
                ]
                if expertise == "beginner":
                    notes.append("Beginner note: include the tool and goal so the next answer can cite a safe local procedure.")
            else:
                summary = "内置知识库暂时没有找到足够相关的命令。"
                notes = ["请补充技术栈、目标动作或报错信息后重试，例如“Docker 查看最近 100 行日志”。"]
                if expertise == "beginner":
                    notes.append("新手提示：补充工具名称和目标动作后，系统才能给出可追溯的本地方案。")
            return Answer(
                mode="local_fallback",
                summary=summary,
                commands=[],
                notes=notes,
                citations=[],
                generation=generation,
                context=context or ContextInfo(),
                personalization=personalization or PersonalizationInfo(),
            )
        primary = hits[0].topic
        candidate_commands = primary.commands[:4]
        # A generic template answer should remain useful but unambiguous.  If
        # the selected evidence contains parameters, expose one representative
        # source command instead of four mutually incompatible variants.  The
        # command planner takes over when a curated recipe can fill it.
        if single_template and candidate_commands:
            candidate_commands = [candidate_commands[0]]
        elif any(has_unresolved_placeholders(item.code) for item in candidate_commands):
            candidate_commands = [
                next(item for item in candidate_commands if has_unresolved_placeholders(item.code))
            ]
        commands = review_commands(candidate_commands)
        if language == "en":
            if commands:
                summary = (
                    f"You can use the evidence-backed command(s) below for “{primary.title}”. "
                    "Replace placeholder values before running them."
                )
            else:
                summary = f"The cited local evidence contains the relevant guidance for “{primary.title}”."
            if detail == "detailed" and primary.summary:
                summary = f"{summary} Evidence detail: {primary.summary[:600]}"
        else:
            if commands:
                summary = f"可以使用下面的命令完成“{primary.title}”。请先替换占位参数，再复制执行。"
            else:
                summary = primary.summary[:600 if detail == "detailed" else 240]
            if detail == "detailed" and commands and primary.summary:
                summary = f"{summary} 相关本地证据：{primary.summary[:600]}"
        citations = citations_from_hits(hits)
        commands = review_commands(grounded_commands(commands, hits))
        segments = _ground_segments(
            summary,
            [],
            citations,
            allow_deterministic_summary_fallback=True,
        )
        local_note = (
            "This answer was generated deterministically from local knowledge; no cloud model was called."
            if language == "en"
            else "当前回答由本地知识确定性生成，未调用云端模型。"
        )
        beginner_note = (
            "Beginner note: review the prerequisites and confirm the target environment before running a command."
            if language == "en"
            else "新手提示：执行命令前先核对前置条件，并确认目标环境。"
        )
        notes = [*primary.notes, local_note]
        if expertise == "beginner":
            notes.append(beginner_note)
        return Answer(
            mode="local_fallback",
            summary=summary,
            commands=commands,
            notes=list(dict.fromkeys(notes)),
            citations=citations,
            generation=generation,
            context=context or ContextInfo(),
            personalization=personalization or PersonalizationInfo(),
            summary_segments=segments,
        )

    def answer(
        self,
        query: str,
        hits: list[RetrievalHit],
        *,
        context: ContextInfo | None = None,
        memory_context: str = "",
        personalization: PersonalizationInfo | None = None,
        personalization_context: str = "",
    ) -> tuple[Answer, str]:
        personalization_info = personalization or PersonalizationInfo()
        if not hits or not self.generator.available:
            reason = "no_hits" if not hits else "no_api_key"
            return self.local_fallback(
                hits,
                reason=reason,
                context=context,
                personalization=personalization_info,
                query=query,
                personalization_context=personalization_context,
            ), reason
        attempted_personalization = personalization_info.model_copy(
            update={"sent_to_model": bool(personalization_context)}
        )
        try:
            parameters = inspect.signature(self.generator.generate).parameters
            generation_kwargs = {}
            if "memory_context" in parameters:
                generation_kwargs["memory_context"] = memory_context
            if "personalization_context" in parameters:
                generation_kwargs["personalization_context"] = personalization_context
            draft = self.generator.generate(query, hits, **generation_kwargs)
            citations = citations_from_hits(hits)
            commands = review_commands(grounded_commands(draft.commands, hits))
            segments = _ground_segments(draft.summary, draft.summary_segments, citations)
            if not segments:
                # ``summary_segments`` is the contract that binds model prose to
                # exact evidence.  Do not attach the whole model summary to the
                # first available citation just to render a marker; return the
                # deterministic, evidence-derived fallback instead.
                return self.local_fallback(
                    hits,
                    reason="ungrounded",
                    attempted=True,
                    context=context,
                    personalization=attempted_personalization,
                    query=query,
                    personalization_context=personalization_context,
                ), "ungrounded"
            summary = "".join(segment.text for segment in segments) or draft.summary
            return (
                Answer(
                    mode="model",
                    summary=summary,
                    commands=commands,
                    notes=draft.notes,
                    citations=citations,
                    generation=draft.generation,
                    context=context or ContextInfo(),
                    personalization=attempted_personalization,
                    summary_segments=segments,
                ),
                "model",
            )
        except Exception as exc:
            reason = self._fallback_reason(exc)
            return self.local_fallback(
                hits,
                reason=reason,
                attempted=True,
                context=context,
                personalization=attempted_personalization,
                query=query,
                personalization_context=personalization_context,
            ), reason

    @staticmethod
    def _fallback_reason(exc: Exception) -> str:
        if isinstance(exc, (httpx.TimeoutException, APITimeoutError)):
            return "timeout"
        status_code = None
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
        elif isinstance(exc, APIStatusError):
            status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            if status_code in {401, 403}:
                return "authentication"
            if status_code == 429:
                return "rate_limit"
            return "provider_error"
        if isinstance(exc, (ValueError, json.JSONDecodeError)):
            return "invalid_json"
        return "provider_error"
