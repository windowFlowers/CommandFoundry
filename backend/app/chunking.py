from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Iterable, Literal

from .extraction import BlockKind, ExtractedBlock, ExtractedDocument, SourceKind


PARENT_TARGET_CHARS = 1_800
PARENT_MAX_CHARS = 2_400
CHILD_TARGET_CHARS = 420
CHILD_MAX_CHARS = 800
CHILD_OVERLAP_CHARS = 80
INDEX_SCHEMA_VERSION = 2

CommandEvidenceKind = Literal["fenced_code", "inline_code", "standalone_command"]


@dataclass(frozen=True, slots=True)
class ChunkLocator:
    kind: SourceKind
    page_start: int | None = None
    page_end: int | None = None
    heading_path: tuple[str, ...] = ()
    line_start: int | None = None
    line_end: int | None = None
    paragraph_start: int | None = None
    paragraph_end: int | None = None
    char_start: int = 0
    char_end: int = 0
    table_start: int | None = None
    table_end: int | None = None
    table_row_start: int | None = None
    table_row_end: int | None = None
    element_start: int | None = None
    element_end: int | None = None

    def as_dict(self, *, include_internal: bool = True) -> dict:
        value = asdict(self)
        value["heading_path"] = list(self.heading_path)
        if not include_internal:
            for key in (
                "table_start",
                "table_end",
                "table_row_start",
                "table_row_end",
                "element_start",
                "element_end",
            ):
                value.pop(key, None)
        return value


@dataclass(frozen=True, slots=True)
class CommandEvidence:
    code: str
    kind: CommandEvidenceKind
    language: Literal["bash", "sql", "powershell", "text"] = "text"
    text_start: int = 0
    text_end: int = 0


@dataclass(frozen=True, slots=True)
class ParentChunk:
    id: str
    index: int
    text: str
    retrieval_text: str
    locator: ChunkLocator
    content_hash: str
    command_evidence: tuple[CommandEvidence, ...] = ()
    index_schema_version: int = INDEX_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ChildChunk:
    id: str
    parent_id: str
    index: int
    text: str
    retrieval_text: str
    locator: ChunkLocator
    content_hash: str
    command_evidence: tuple[CommandEvidence, ...] = ()
    index_schema_version: int = INDEX_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ChunkedDocument:
    document_sha256: str
    parents: tuple[ParentChunk, ...]
    children: tuple[ChildChunk, ...]
    index_schema_version: int = INDEX_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class _Piece:
    text: str
    kind: BlockKind
    heading_path: tuple[str, ...]
    char_start: int
    char_end: int
    page_start: int | None = None
    page_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    paragraph_start: int | None = None
    paragraph_end: int | None = None
    table_start: int | None = None
    table_end: int | None = None
    table_row_start: int | None = None
    table_row_end: int | None = None
    element_start: int | None = None
    element_end: int | None = None


def content_sha256(text: str) -> str:
    canonical = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stable_chunk_id(
    document_sha256: str,
    locator: ChunkLocator | dict,
    text: str,
    *,
    level: Literal["parent", "child"],
) -> str:
    """Create a deterministic ID independent of DB row/order and reindex time."""

    locator_value = locator.as_dict() if isinstance(locator, ChunkLocator) else dict(locator)
    canonical_locator = json.dumps(locator_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(
        "\x00".join(
            (
                "aegis-v2.3",
                level,
                document_sha256.casefold(),
                canonical_locator,
                content_sha256(text),
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"{'p' if level == 'parent' else 'c'}_{digest}"


def _piece_from_block(block: ExtractedBlock) -> _Piece:
    return _Piece(
        text=block.text,
        kind=block.kind,
        heading_path=block.heading_path,
        char_start=block.char_start,
        char_end=block.char_end,
        page_start=block.page_start,
        page_end=block.page_end,
        line_start=block.line_start,
        line_end=block.line_end,
        paragraph_start=block.paragraph_start,
        paragraph_end=block.paragraph_end,
        table_start=block.table_start,
        table_end=block.table_end,
        table_row_start=block.table_row_start,
        table_row_end=block.table_row_end,
        element_start=block.element_start,
        element_end=block.element_end,
    )


def _trim_range(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _preferred_ranges(text: str, target_size: int) -> list[tuple[int, int]]:
    if len(text) <= target_size:
        return [(0, len(text))]
    ranges: list[tuple[int, int]] = []
    start = 0
    length = len(text)
    while length - start > target_size:
        desired = start + target_size
        lower = start + max(1, target_size // 2)
        candidate = -1
        for pattern in (r"\n", r"(?<=[。！？.!?；;])\s*", r"\s+"):
            matches = list(re.finditer(pattern, text[lower:desired]))
            if matches:
                candidate = lower + matches[-1].end()
                break
        end = candidate if candidate > start else desired
        trimmed_start, trimmed_end = _trim_range(text, start, end)
        if trimmed_start < trimmed_end:
            ranges.append((trimmed_start, trimmed_end))
        start = end
        while start < length and text[start].isspace():
            start += 1
    if start < length:
        trimmed_start, trimmed_end = _trim_range(text, start, length)
        if trimmed_start < trimmed_end:
            ranges.append((trimmed_start, trimmed_end))
    return ranges


def _line_bounds(piece: _Piece, start: int, end: int) -> tuple[int | None, int | None]:
    if piece.line_start is None:
        return None, None
    line_start = piece.line_start + piece.text[:start].count("\n")
    line_end = piece.line_start + piece.text[:end].count("\n")
    if end > start and piece.text[end - 1 : end] == "\n":
        line_end -= 1
    return line_start, max(line_start, line_end)


def _slice_piece(piece: _Piece, start: int, end: int, *, text: str | None = None) -> _Piece:
    start, end = _trim_range(piece.text, start, end)
    line_start, line_end = _line_bounds(piece, start, end)
    return replace(
        piece,
        text=piece.text[start:end] if text is None else text,
        char_start=min(piece.char_end, piece.char_start + start),
        char_end=min(piece.char_end, piece.char_start + end),
        line_start=line_start,
        line_end=line_end,
    )


def _split_fenced_piece(piece: _Piece, target_size: int, max_size: int) -> list[_Piece]:
    lines = piece.text.splitlines()
    if not lines:
        return []
    match = re.match(r"^\s*(`{3,}|~{3,})([^\r\n]*)$", lines[0])
    if match is None:
        return [_slice_piece(piece, start, end) for start, end in _preferred_ranges(piece.text, target_size)]
    marker = match.group(1)
    closer_present = len(lines) > 1 and re.fullmatch(rf"\s*{re.escape(marker[0])}{{{len(marker)},}}\s*", lines[-1]) is not None
    body_lines = lines[1:-1] if closer_present else lines[1:]
    closed_text = piece.text if closer_present else f"{piece.text.rstrip()}\n{marker}"
    if len(closed_text) <= max_size:
        return [replace(piece, text=closed_text)]
    if not body_lines:
        closed = f"{lines[0]}\n{marker}"
        return [replace(piece, text=closed)]

    overhead = len(lines[0]) + len(marker) + 2
    target_body = max(1, target_size - overhead)
    max_body = max(1, max_size - overhead)
    groups: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for line in body_lines:
        extra = len(line) + (1 if current else 0)
        limit = target_body if current_size else max_body
        if current and current_size + extra > limit:
            groups.append(current)
            current = []
            current_size = 0
        # A single source line is intentionally never split; it may exceed the
        # configured maximum because preserving complete lines is the stronger invariant.
        current.append(line)
        current_size += len(line) + (1 if len(current) > 1 else 0)
    if current:
        groups.append(current)

    result: list[_Piece] = []
    source_cursor = len(lines[0]) + (1 if len(lines) > 1 else 0)
    for group in groups:
        body = "\n".join(group)
        source_start = source_cursor
        source_end = source_start + len(body)
        line_start, line_end = _line_bounds(piece, source_start, source_end)
        result.append(
            replace(
                piece,
                text=f"{lines[0]}\n{body}\n{marker}",
                char_start=min(piece.char_end, piece.char_start + source_start),
                char_end=min(piece.char_end, piece.char_start + source_end),
                line_start=line_start,
                line_end=line_end,
            )
        )
        source_cursor = source_end + 1
    return result


def _atomic_pieces(document: ExtractedDocument, target_size: int, max_size: int) -> list[_Piece]:
    return _split_pieces([_piece_from_block(block) for block in document.blocks], target_size, max_size)


def _split_pieces(source: list[_Piece], target_size: int, max_size: int) -> list[_Piece]:
    pieces: list[_Piece] = []
    for piece in source:
        if piece.kind == "code" or re.match(r"^\s*(`{3,}|~{3,})", piece.text):
            pieces.extend(_split_fenced_piece(piece, target_size, max_size))
        elif len(piece.text) > target_size:
            pieces.extend(
                _slice_piece(piece, start, end)
                for start, end in _preferred_ranges(piece.text, target_size)
            )
        else:
            pieces.append(piece)
    return [piece for piece in pieces if piece.text.strip()]


def _joined_size(pieces: Iterable[_Piece]) -> int:
    values = list(pieces)
    return sum(len(piece.text) for piece in values) + max(0, len(values) - 1) * 2


def _is_preferred_boundary(previous: _Piece, current: _Piece) -> bool:
    if current.kind == "heading" or previous.kind == "table_row" or current.kind == "table_row":
        return True
    if previous.page_end is not None and current.page_start is not None:
        return previous.page_end != current.page_start
    return previous.heading_path != current.heading_path


def _pack_parents(pieces: list[_Piece], target_size: int, max_size: int) -> list[list[_Piece]]:
    groups: list[list[_Piece]] = []
    current: list[_Piece] = []
    for piece in pieces:
        proposed = _joined_size([*current, piece])
        boundary = bool(current) and _is_preferred_boundary(current[-1], piece)
        should_flush = bool(current) and (
            proposed > max_size
            or proposed > target_size
            or (boundary and _joined_size(current) >= target_size // 2)
        )
        if should_flush:
            groups.append(current)
            current = []
        current.append(piece)
    if current:
        groups.append(current)
    return groups


def _overlap_tail(pieces: list[_Piece], overlap: int) -> list[_Piece]:
    if overlap <= 0:
        return []
    selected: list[_Piece] = []
    remaining = overlap
    for piece in reversed(pieces):
        if remaining <= 0:
            break
        # Never cut through a fenced block; a short complete fence may overlap.
        if piece.kind == "code" or re.match(r"^\s*(`{3,}|~{3,})", piece.text):
            if len(piece.text) <= remaining:
                selected.insert(0, piece)
                remaining -= len(piece.text) + 2
            break
        if len(piece.text) <= remaining:
            selected.insert(0, piece)
            remaining -= len(piece.text) + 2
        else:
            start = len(piece.text) - remaining
            while start < len(piece.text) and not piece.text[start].isspace() and start < len(piece.text) - 1:
                start += 1
            tail = _slice_piece(piece, start, len(piece.text))
            if tail.text:
                selected.insert(0, tail)
            remaining = 0
    return selected


def _pack_children(
    pieces: list[_Piece], target_size: int, max_size: int, overlap: int
) -> list[list[_Piece]]:
    groups: list[list[_Piece]] = []
    current: list[_Piece] = []
    for piece in pieces:
        proposed = _joined_size([*current, piece])
        if current and (proposed > target_size or proposed > max_size):
            groups.append(current)
            current = _overlap_tail(current, overlap)
            while current and _joined_size([*current, piece]) > max_size:
                current.pop(0)
        current.append(piece)
    if current:
        groups.append(current)
    return groups


def _common_heading_path(pieces: list[_Piece]) -> tuple[str, ...]:
    paths = [piece.heading_path for piece in pieces if piece.heading_path]
    if not paths:
        return ()
    prefix: list[str] = []
    for entries in zip(*paths):
        if len(set(entries)) != 1:
            break
        prefix.append(entries[0])
    return tuple(prefix)


def _optional_min(pieces: list[_Piece], name: str) -> int | None:
    values = [getattr(piece, name) for piece in pieces if getattr(piece, name) is not None]
    return min(values) if values else None


def _optional_max(pieces: list[_Piece], name: str) -> int | None:
    values = [getattr(piece, name) for piece in pieces if getattr(piece, name) is not None]
    return max(values) if values else None


def _locator(kind: SourceKind, pieces: list[_Piece]) -> ChunkLocator:
    return ChunkLocator(
        kind=kind,
        page_start=_optional_min(pieces, "page_start"),
        page_end=_optional_max(pieces, "page_end"),
        heading_path=_common_heading_path(pieces),
        line_start=_optional_min(pieces, "line_start"),
        line_end=_optional_max(pieces, "line_end"),
        paragraph_start=_optional_min(pieces, "paragraph_start"),
        paragraph_end=_optional_max(pieces, "paragraph_end"),
        char_start=min(piece.char_start for piece in pieces),
        char_end=max(piece.char_end for piece in pieces),
        table_start=_optional_min(pieces, "table_start"),
        table_end=_optional_max(pieces, "table_end"),
        table_row_start=_optional_min(pieces, "table_row_start"),
        table_row_end=_optional_max(pieces, "table_row_end"),
        element_start=_optional_min(pieces, "element_start"),
        element_end=_optional_max(pieces, "element_end"),
    )


def _language_from_hint(hint: str, code: str) -> Literal["bash", "sql", "powershell", "text"]:
    lowered = hint.casefold()
    if lowered in {"sh", "shell", "bash", "zsh"}:
        return "bash"
    if lowered in {"ps1", "pwsh", "powershell"}:
        return "powershell"
    if lowered in {"sql", "mysql", "postgres", "postgresql"}:
        return "sql"
    if re.match(r"^(?:Get|Set|New|Remove|Test|Invoke|Start|Stop)-[A-Za-z]", code):
        return "powershell"
    if re.match(r"^(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", code, flags=re.IGNORECASE):
        return "sql"
    return "bash" if code else "text"


_STANDALONE_COMMAND = re.compile(
    r"^(?:\$\s+|PS>\s+|>\s+)?(?:sudo\s+)?(?:"
    r"git|docker(?:\s+compose)?|kubectl|helm|npm|pnpm|yarn|node|python(?:3)?|pip(?:3)?|"
    r"poetry|uv|conda|java|mvn|gradle|dotnet|go|cargo|curl|wget|ssh|scp|rsync|"
    r"grep|rg|sed|awk|find|ls|cd|cp|mv|mkdir|rm|chmod|chown|tar|systemctl|journalctl|"
    r"(?:Get|Set|New|Remove|Test|Invoke|Start|Stop)-[A-Za-z][A-Za-z0-9]*|"
    r"(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b"
    r")(?:\s+.+)?$"
)


def extract_command_evidence(text: str) -> tuple[CommandEvidence, ...]:
    """Conservatively extract only explicitly code-shaped command evidence."""

    evidence: list[CommandEvidence] = []
    lines = text.splitlines(keepends=True)
    cursor = 0
    outside_parts: list[tuple[str, int]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.rstrip("\r\n")
        opening = re.match(r"^\s*(`{3,}|~{3,})([^\r\n]*)$", stripped)
        if opening is None:
            outside_parts.append((line, cursor))
            cursor += len(line)
            index += 1
            continue
        marker = opening.group(1)
        hint = opening.group(2).strip().split(maxsplit=1)[0] if opening.group(2).strip() else ""
        body_start = cursor + len(line)
        cursor += len(line)
        index += 1
        body: list[str] = []
        while index < len(lines):
            candidate = lines[index]
            candidate_stripped = candidate.rstrip("\r\n")
            if re.fullmatch(rf"\s*{re.escape(marker[0])}{{{len(marker)},}}\s*", candidate_stripped):
                cursor += len(candidate)
                index += 1
                break
            body.append(candidate)
            cursor += len(candidate)
            index += 1
        code = "".join(body).strip("\r\n")
        if code.strip():
            leading = len(code) - len(code.lstrip("\r\n"))
            evidence.append(
                CommandEvidence(
                    code=code,
                    kind="fenced_code",
                    language=_language_from_hint(hint, code),
                    text_start=body_start + leading,
                    text_end=body_start + leading + len(code),
                )
            )

    outside_text = "".join(value for value, _ in outside_parts)
    # Mapping is exact because every part records its original chunk-text start.
    outside_cursor = 0
    for value, original_start in outside_parts:
        for match in re.finditer(r"(?<!`)`([^`\r\n]+)`(?!`)", value):
            code = match.group(1).strip()
            if code:
                start = original_start + match.start(1) + len(match.group(1)) - len(match.group(1).lstrip())
                evidence.append(
                    CommandEvidence(
                        code=code,
                        kind="inline_code",
                        language=_language_from_hint("", code),
                        text_start=start,
                        text_end=start + len(code),
                    )
                )
        line_value = value.rstrip("\r\n").strip()
        promptless = re.sub(r"^(?:\$\s+|PS>\s+|>\s+)", "", line_value)
        if line_value and _STANDALONE_COMMAND.fullmatch(line_value):
            local = value.find(line_value) + len(line_value) - len(line_value.lstrip())
            evidence.append(
                CommandEvidence(
                    code=promptless,
                    kind="standalone_command",
                    language=_language_from_hint("", promptless),
                    text_start=original_start + local,
                    text_end=original_start + local + len(line_value),
                )
            )
        outside_cursor += len(value)

    unique: list[CommandEvidence] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        key = (item.kind, item.code)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return tuple(unique)


def _retrieval_text(text: str, heading_path: tuple[str, ...]) -> str:
    prefix = " > ".join(heading_path)
    return f"{prefix}\n{text}" if prefix and prefix not in text[: len(prefix) + 8] else text


def build_parent_child_chunks(
    document: ExtractedDocument,
    document_sha256: str,
    *,
    parent_target: int = PARENT_TARGET_CHARS,
    parent_max: int = PARENT_MAX_CHARS,
    child_target: int = CHILD_TARGET_CHARS,
    child_max: int = CHILD_MAX_CHARS,
    child_overlap: int = CHILD_OVERLAP_CHARS,
) -> ChunkedDocument:
    if parent_target <= 0 or child_target <= 0:
        raise ValueError("chunk targets must be positive")
    if parent_max < parent_target or child_max < child_target:
        raise ValueError("chunk maximums must be greater than or equal to targets")
    if child_overlap < 0 or child_overlap >= child_max:
        raise ValueError("child overlap must be non-negative and smaller than child maximum")
    if not document.blocks:
        return ChunkedDocument(document_sha256=document_sha256, parents=(), children=())

    # Build bounded section parents first. Child splitting happens only inside
    # each resulting parent, so changing child sizing cannot move parent boundaries.
    parent_pieces = _atomic_pieces(document, parent_target, parent_max)
    parent_groups = _pack_parents(parent_pieces, parent_target, parent_max)
    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []
    child_index = 0
    for parent_index, parent_group in enumerate(parent_groups):
        parent_text = "\n\n".join(piece.text for piece in parent_group)
        parent_locator = _locator(document.kind, parent_group)
        parent_id = stable_chunk_id(document_sha256, parent_locator, parent_text, level="parent")
        parents.append(
            ParentChunk(
                id=parent_id,
                index=parent_index,
                text=parent_text,
                retrieval_text=_retrieval_text(parent_text, parent_locator.heading_path),
                locator=parent_locator,
                content_hash=content_sha256(parent_text),
                command_evidence=extract_command_evidence(parent_text),
            )
        )
        child_pieces = _split_pieces(parent_group, child_target, child_max)
        for child_group in _pack_children(child_pieces, child_target, child_max, child_overlap):
            child_text = "\n\n".join(piece.text for piece in child_group)
            child_locator = _locator(document.kind, child_group)
            child_id = stable_chunk_id(document_sha256, child_locator, child_text, level="child")
            children.append(
                ChildChunk(
                    id=child_id,
                    parent_id=parent_id,
                    index=child_index,
                    text=child_text,
                    retrieval_text=_retrieval_text(child_text, child_locator.heading_path),
                    locator=child_locator,
                    content_hash=content_sha256(child_text),
                    command_evidence=extract_command_evidence(child_text),
                )
            )
            child_index += 1
    return ChunkedDocument(
        document_sha256=document_sha256,
        parents=tuple(parents),
        children=tuple(children),
    )


# Concise alias for callers that treat chunking as a service function.
chunk_document = build_parent_child_chunks
