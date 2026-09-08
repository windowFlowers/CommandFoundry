from __future__ import annotations

import json

import httpx

from .generation import DeepSeekGenerator
from .models import Answer, Citation, GenerationInfo, RetrievalHit
from .risk import review_commands


def citations_from_hits(hits: list[RetrievalHit]) -> list[Citation]:
    return [
        Citation(
            source_id=hit.topic.source.source_id,
            title=hit.topic.source.title,
            source_url=hit.topic.source.source_url,
            license=hit.topic.source.license,
            revision=hit.topic.source.revision,
            excerpt=f"{hit.topic.title}：{hit.topic.summary}"[:360],
            score=hit.score,
            domain=hit.topic.domain,
            document_id=hit.topic.source.document_id,
            knowledge_base_id=hit.topic.source.knowledge_base_id,
            source_kind=hit.topic.source.kind,
        )
        for hit in hits
    ]


class AnswerService:
    def __init__(self, generator: DeepSeekGenerator) -> None:
        self.generator = generator

    def local_fallback(
        self,
        hits: list[RetrievalHit],
        *,
        reason: str,
        attempted: bool = False,
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
            )
        primary = hits[0].topic
        commands = review_commands(primary.commands[:4])
        if commands:
            summary = f"可以使用下面的命令完成“{primary.title}”。请先替换占位参数，再复制执行。"
        else:
            summary = primary.summary[:600]
        return Answer(
            mode="local_fallback",
            summary=summary,
            commands=commands,
            notes=list(dict.fromkeys([*primary.notes, "当前回答由本地知识确定性生成，未调用云端模型。"])),
            citations=citations_from_hits(hits),
            generation=generation,
        )

    def answer(self, query: str, hits: list[RetrievalHit]) -> tuple[Answer, str]:
        if not hits or not self.generator.available:
            reason = "no_hits" if not hits else "no_api_key"
            return self.local_fallback(hits, reason=reason), reason
        try:
            draft = self.generator.generate(query, hits)
            return (
                Answer(
                    mode="model",
                    summary=draft.summary,
                    commands=review_commands(draft.commands),
                    notes=draft.notes,
                    citations=citations_from_hits(hits),
                    generation=draft.generation,
                ),
                "model",
            )
        except Exception as exc:
            reason = self._fallback_reason(exc)
            return self.local_fallback(hits, reason=reason, attempted=True), reason

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
