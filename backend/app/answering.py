from __future__ import annotations

import json
import re

import httpx

from .evidence import evidence_records
from .generation import DeepSeekGenerator
from .models import Answer, AnswerSegment, Citation, CommandBlock, ContextInfo, GenerationInfo, RetrievalHit
from .risk import review_commands


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


def _ground_segments(summary: str, segments: list[AnswerSegment], citations: list[Citation]) -> list[AnswerSegment]:
    ordered_valid = [item.citation_id for item in citations if item.citation_id]
    valid = set(ordered_valid)
    grounded: list[AnswerSegment] = []
    for segment in segments:
        ids = [value for value in dict.fromkeys(segment.citation_ids) if value in valid]
        if segment.text.strip() and ids:
            grounded.append(segment.model_copy(update={"citation_ids": ids}))
    if not grounded and summary.strip() and ordered_valid:
        grounded.append(AnswerSegment(text=summary.strip(), citation_ids=[ordered_valid[0]]))
    return grounded


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
    ) -> Answer:
        generation = GenerationInfo(
            attempted=attempted,
            provider="deepseek" if attempted else "local",
            model=getattr(self.generator, "model", None) if attempted else None,
            fallback_reason=reason,
        )
        if not hits:
            return Answer(
                mode="local_fallback",
                summary="内置知识库暂时没有找到足够相关的命令。",
                commands=[],
                notes=["请补充技术栈、目标动作或报错信息后重试，例如“Docker 查看最近 100 行日志”。"],
                citations=[],
                generation=generation,
                context=context or ContextInfo(),
            )
        primary = hits[0].topic
        commands = review_commands(primary.commands[:4])
        if commands:
            summary = f"可以使用下面的命令完成“{primary.title}”。请先替换占位参数，再复制执行。"
        else:
            summary = primary.summary[:600]
        citations = citations_from_hits(hits)
        commands = review_commands(grounded_commands(commands, hits))
        segments = _ground_segments(summary, [], citations)
        return Answer(
            mode="local_fallback",
            summary=summary,
            commands=commands,
            notes=list(dict.fromkeys([*primary.notes, "当前回答由本地知识确定性生成，未调用云端模型。"])),
            citations=citations,
            generation=generation,
            context=context or ContextInfo(),
            summary_segments=segments,
        )

    def answer(
        self,
        query: str,
        hits: list[RetrievalHit],
        *,
        context: ContextInfo | None = None,
        memory_context: str = "",
    ) -> tuple[Answer, str]:
        if not hits or not self.generator.available:
            reason = "no_hits" if not hits else "no_api_key"
            return self.local_fallback(hits, reason=reason, context=context), reason
        try:
            draft = (
                self.generator.generate(query, hits, memory_context=memory_context)
                if memory_context
                else self.generator.generate(query, hits)
            )
            citations = citations_from_hits(hits)
            commands = review_commands(grounded_commands(draft.commands, hits))
            segments = _ground_segments(draft.summary, draft.summary_segments, citations)
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
                    summary_segments=segments,
                ),
                "model",
            )
        except Exception as exc:
            reason = self._fallback_reason(exc)
            return self.local_fallback(hits, reason=reason, attempted=True, context=context), reason

    @staticmethod
    def _fallback_reason(exc: Exception) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            if exc.response.status_code in {401, 403}:
                return "authentication"
            if exc.response.status_code == 429:
                return "rate_limit"
            return "provider_error"
        if isinstance(exc, (ValueError, json.JSONDecodeError)):
            return "invalid_json"
        return "provider_error"
