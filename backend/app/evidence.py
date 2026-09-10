from __future__ import annotations

import hashlib

from .models import RetrievalHit


def citation_id(source_id: str, chunk_id: str) -> str:
    digest = hashlib.sha256(f"{source_id}\x1f{chunk_id}".encode("utf-8")).hexdigest()[:12]
    return f"cit-{digest}"


def evidence_records(hits: list[RetrievalHit]) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    for hit in hits:
        source = hit.topic.source
        if hit.matched_chunks:
            for matched in hit.matched_chunks:
                value = citation_id(source.source_id, matched.chunk_id)
                if value in seen:
                    continue
                seen.add(value)
                records.append(
                    {
                        "citation_id": value,
                        "chunk_id": matched.chunk_id,
                        "parent_id": matched.parent_id,
                        "text": matched.text,
                        "parent_text": hit.parent_text or hit.topic.summary,
                        "locator": matched.locator,
                        "command_evidence": matched.command_evidence,
                        "hit": hit,
                    }
                )
            continue
        value = citation_id(source.source_id, hit.topic.id)
        if value in seen:
            continue
        seen.add(value)
        records.append(
            {
                "citation_id": value,
                "chunk_id": None,
                "parent_id": None,
                "text": hit.topic.summary,
                "parent_text": hit.topic.summary,
                "locator": None,
                "command_evidence": [item.code for item in hit.topic.commands],
                "hit": hit,
            }
        )
    return records
