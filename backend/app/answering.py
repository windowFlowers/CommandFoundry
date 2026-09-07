from __future__ import annotations

from .generation import DeepSeekGenerator
from .models import Answer, Citation, RetrievalHit
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
        )
        for hit in hits
    ]


class AnswerService:
    def __init__(self, generator: DeepSeekGenerator) -> None:
        self.generator = generator

    def local_fallback(self, hits: list[RetrievalHit]) -> Answer:
        if not hits:
            return Answer(
                mode="local_fallback",
                summary="内置知识库暂时没有找到足够相关的命令。",
                commands=[],
                notes=["请补充技术栈、目标动作或报错信息后重试，例如“Docker 查看最近 100 行日志”。"],
                citations=[],
            )
        primary = hits[0].topic
        return Answer(
            mode="local_fallback",
            summary=f"可以使用下面的命令完成“{primary.title}”。请先替换占位参数，再复制执行。",
            commands=review_commands(primary.commands[:4]),
            notes=list(dict.fromkeys([*primary.notes, "当前回答由本地知识确定性生成，未调用云端模型。"])),
            citations=citations_from_hits(hits),
        )

    def answer(self, query: str, hits: list[RetrievalHit]) -> tuple[Answer, str]:
        if not hits or not self.generator.available:
            return self.local_fallback(hits), "no_hits" if not hits else "no_api_key"
        try:
            draft = self.generator.generate(query, hits)
            return (
                Answer(
                    mode="model",
                    summary=draft.summary,
                    commands=review_commands(draft.commands),
                    notes=draft.notes,
                    citations=citations_from_hits(hits),
                ),
                "model",
            )
        except Exception as exc:
            return self.local_fallback(hits), type(exc).__name__
