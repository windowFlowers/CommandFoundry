from __future__ import annotations

import re


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _semantic_blocks(text: str) -> list[str]:
    lines = normalize_text(text).splitlines()
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False

    def flush() -> None:
        if current:
            value = "\n".join(current).strip()
            if value:
                blocks.append(value)
            current.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_fence:
                flush()
                in_fence = True
            current.append(line)
            if in_fence and len(current) > 1 and stripped == "```":
                in_fence = False
                flush()
            continue
        if in_fence:
            current.append(line)
            continue
        if re.match(r"^#{1,6}\s+", stripped):
            flush()
            current.append(line)
            flush()
        elif not stripped:
            flush()
        else:
            current.append(line)
    flush()
    return blocks


def _split_large_prose(block: str, size: int) -> list[str]:
    if len(block) <= size or block.lstrip().startswith("```"):
        return [block]
    pieces = re.split(r"(?<=[。！？.!?；;])\s*|\n", block)
    result: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        if len(piece) > size:
            if current:
                result.append(current)
                current = ""
            result.extend(piece[index : index + size] for index in range(0, len(piece), size))
        elif not current:
            current = piece
        elif len(current) + len(piece) + 1 <= size:
            current += "\n" + piece
        else:
            result.append(current)
            current = piece
    if current:
        result.append(current)
    return result


def split_into_chunks(text: str, chunk_size: int = 800, overlap: int = 120) -> list[str]:
    """Split on headings/paragraphs while keeping fenced code blocks indivisible."""
    blocks: list[str] = []
    for block in _semantic_blocks(text):
        blocks.extend(_split_large_prose(block, chunk_size))
    if not blocks:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for block in blocks:
        extra = len(block) + (2 if current else 0)
        if current and current_size + extra > chunk_size:
            chunks.append("\n\n".join(current))
            overlap_blocks: list[str] = []
            overlap_size = 0
            for previous in reversed(current):
                if overlap_size + len(previous) > overlap:
                    break
                overlap_blocks.insert(0, previous)
                overlap_size += len(previous) + 2
            current = overlap_blocks
            current_size = len("\n\n".join(current))
        current.append(block)
        current_size += len(block) + (2 if len(current) > 1 else 0)
    if current:
        chunks.append("\n\n".join(current))
    return list(dict.fromkeys(chunk.strip() for chunk in chunks if chunk.strip()))
