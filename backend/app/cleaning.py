from __future__ import annotations

"""Deterministic document cleaning between extraction and chunking.

This module intentionally does not call a model and never changes the source
file.  It only removes high-confidence structural noise from the extracted
view that is persisted in the search index.  Every retained block keeps its
source locator (page/line/paragraph/table metadata); character offsets are
rebuilt against the cleaned canonical content so citation highlighting stays
exact.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from math import ceil
from typing import Iterable

from .extraction import ExtractedBlock, ExtractedDocument
from .models import DocumentQualityReport


CLEANING_REVISION = "v2.12.0"

# C0 controls are allowed only where they are structural text separators.
_DISALLOWED_CONTROL_RE = re.compile(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_TABLE_RE = re.compile(r"(^\s*\|)|(^\s*[-+|: ]{3,}\s*$)|(\s\|\s)")


@dataclass(frozen=True, slots=True)
class CleanedDocument:
    document: ExtractedDocument
    report: DocumentQualityReport


def _line_normalize(value: str, *, preserve_layout: bool) -> str:
    """Normalize line endings and safe noise without touching command layout."""

    value = value.replace("\ufeff", "").replace("\x00", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if preserve_layout:
        # A fenced command/table must retain indentation and separators.  Only
        # remove controls that can never be meaningful in those formats.  The
        # leading indentation is retained, while line-end whitespace is safe
        # to remove from both commands and table separators.
        value = _DISALLOWED_CONTROL_RE.sub("", value)
        return "\n".join(line.rstrip(" \t") for line in value.split("\n"))
    value = unicodedata.normalize("NFKC", value)
    value = _DISALLOWED_CONTROL_RE.sub("", value)
    lines = [line.rstrip() for line in value.split("\n")]
    # Extraction already separates structural blocks.  This guard is useful
    # for callers that construct an ExtractedDocument directly in tests and
    # keeps accidental long blank runs from entering the index.
    compact: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run > 2:
                continue
        else:
            blank_run = 0
        compact.append(line)
    return "\n".join(compact).strip("\n")


def _normalize_path(path: Iterable[str]) -> tuple[str, ...]:
    return tuple(_line_normalize(value, preserve_layout=False).strip() for value in path)


def _is_table_or_code(block: ExtractedBlock) -> bool:
    return block.kind in {"code", "table_row"} or bool(
        _FENCE_RE.match(block.text) or _TABLE_RE.search(block.text)
    )


def _normalized_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _near_duplicate(left: str, right: str) -> bool:
    """Return true only for extremely high-confidence adjacent duplicates."""

    left_key = _normalized_key(left)
    right_key = _normalized_key(right)
    if not left_key or not right_key or left_key == right_key:
        return left_key == right_key and bool(left_key)
    # Very short paragraphs are often legitimate repeated labels.  Require a
    # meaningful body before applying token/similarity removal.
    left_tokens = _TOKEN_RE.findall(left_key)
    right_tokens = _TOKEN_RE.findall(right_key)
    if min(len(left_tokens), len(right_tokens)) < 8:
        return False
    union = set(left_tokens) | set(right_tokens)
    intersection = set(left_tokens) & set(right_tokens)
    jaccard = len(intersection) / len(union) if union else 0.0
    return jaccard >= 0.98 and SequenceMatcher(None, left_key, right_key).ratio() >= 0.985


def _pdf_header_footer_indexes(blocks: list[ExtractedBlock]) -> set[int]:
    """Find repeated page-edge lines conservatively.

    A line must be at the first or last extracted paragraph of at least three
    pages and occur on at least half of all pages.  This avoids deleting a
    legitimate paragraph that happens to repeat on a short PDF.
    """

    pages: dict[int, list[int]] = {}
    for index, block in enumerate(blocks):
        if block.page_start is None:
            continue
        pages.setdefault(block.page_start, []).append(index)
    if len(pages) < 3:
        return set()

    candidates: dict[str, set[int]] = {}
    edge_candidates: set[int] = set()
    for page, indexes in pages.items():
        edge_indexes = {indexes[0], indexes[-1]}
        for index in edge_indexes:
            edge_candidates.add(index)
            block = blocks[index]
            if block.kind != "paragraph" or _is_table_or_code(block):
                continue
            key = _normalized_key(block.text)
            # A page title or footer should be short; long repeated paragraphs
            # are more likely to be actual content.
            if key and len(key) <= 160:
                candidates.setdefault(key, set()).add(page)

    threshold = ceil(len(pages) * 0.5)
    repeated = {key for key, seen_pages in candidates.items() if len(seen_pages) >= 3 and len(seen_pages) >= threshold}
    return {
        index
        for index in edge_candidates
        if _normalized_key(blocks[index].text) in repeated
        and blocks[index].kind == "paragraph"
    }


def _quality_scan(
    text: str,
    raw_bytes: bytes | None,
    source_kind: str,
    *,
    raw_text: str | None = None,
) -> tuple[list[str], list[str], bool]:
    markers: list[str] = []
    warnings: list[str] = []
    rejected = False
    observed_text = raw_text if raw_text is not None else text
    replacement_count = observed_text.count("\ufffd")
    if replacement_count:
        markers.append("replacement_character")
        warnings.append("检测到 Unicode 替换字符，部分原文可能无法可靠解码")
    control_count = len(_DISALLOWED_CONTROL_RE.findall(observed_text))
    if control_count:
        markers.append("control_character")
        warnings.append("检测到异常控制字符")

    if raw_bytes is not None and source_kind in {"text", "markdown"}:
        nul_count = raw_bytes.count(b"\x00")
        if nul_count:
            markers.append("nul_byte")
            warnings.append("文本中包含 NUL 字节，已从索引内容中移除")
        # A text upload with a large amount of control bytes is usually a
        # mislabeled binary file.  Keep the threshold high to avoid rejecting
        # legitimate terminal transcripts containing a few control markers.
        if len(raw_bytes) and (
            control_count / max(len(observed_text), 1) > 0.08 or nul_count / len(raw_bytes) > 0.08
        ):
            markers.append("binary_like_content")
            warnings.append("有效文本比例过低，文件可能是二进制内容")
            rejected = True

    visible = sum(1 for char in text if not char.isspace() and unicodedata.category(char)[0] != "C")
    if visible == 0:
        markers.append("empty_text")
        warnings.append("清洗后没有可索引的文本")
        rejected = True
    if replacement_count > max(3, len(observed_text) // 100):
        markers.append("too_many_replacement_characters")
        rejected = True
    return markers, warnings, rejected


def _removed_blank_line_count(raw_bytes: bytes | None, source_kind: str) -> int:
    """Count excess blank source lines removed before canonical assembly."""

    if raw_bytes is None or source_kind not in {"text", "markdown"}:
        return 0
    decoded: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            decoded = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        return 0
    lines = decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    removed = 0
    run = 0
    for line in lines + ["__end__"]:
        if not line.strip():
            run += 1
            continue
        # A canonical block separator contains one blank line.  Extra blank
        # lines are pure layout noise and can be counted safely.
        removed += max(0, run - 1)
        run = 0
    return removed


def _rebuild_document(kind: str, blocks: list[ExtractedBlock]) -> ExtractedDocument:
    content_parts: list[str] = []
    rebuilt: list[ExtractedBlock] = []
    cursor = 0
    for block in blocks:
        text = block.text.strip("\n")
        if not text.strip():
            continue
        if content_parts:
            content_parts.append("\n\n")
            cursor += 2
        start = cursor
        content_parts.append(text)
        cursor += len(text)
        rebuilt.append(replace(block, text=text, char_start=start, char_end=cursor))
    content = "".join(content_parts)
    return ExtractedDocument(content, rebuilt, kind)  # type: ignore[arg-type]


def clean_extracted_document(
    document: ExtractedDocument,
    *,
    raw_bytes: bytes | None = None,
) -> CleanedDocument:
    """Clean one extracted document using only deterministic rules."""

    raw_chars = len(document.content)
    raw_blocks = len(document.blocks)
    normalized: list[ExtractedBlock] = []
    removed_blank_lines = _removed_blank_line_count(raw_bytes, document.kind)
    for block in document.blocks:
        preserve_layout = _is_table_or_code(block)
        value = _line_normalize(block.text, preserve_layout=preserve_layout)
        if not value.strip():
            removed_blank_lines += max(1, block.text.count("\n"))
            continue
        path = _normalize_path(block.heading_path) if not preserve_layout else block.heading_path
        normalized.append(replace(block, text=value, heading_path=path))

    header_footer_indexes = _pdf_header_footer_indexes(normalized) if document.kind == "pdf" else set()
    kept: list[ExtractedBlock] = []
    removed_exact = 0
    removed_near = 0
    for index, block in enumerate(normalized):
        if index in header_footer_indexes:
            continue
        if kept and block.kind == "paragraph" and kept[-1].kind == "paragraph":
            if _normalized_key(block.text) == _normalized_key(kept[-1].text):
                removed_exact += 1
                continue
            if (
                not _is_table_or_code(block)
                and not _is_table_or_code(kept[-1])
                and _near_duplicate(kept[-1].text, block.text)
            ):
                removed_near += 1
                continue
        kept.append(block)

    cleaned = _rebuild_document(document.kind, kept)
    markers, warnings, rejected = _quality_scan(
        cleaned.content,
        raw_bytes,
        document.kind,
        raw_text=document.content,
    )
    if not cleaned.content.strip():
        rejected = True
    if removed_blank_lines:
        warnings.append("已移除多余空行")
    if header_footer_indexes:
        warnings.append("已移除跨页面重复页眉或页脚")
    if removed_exact:
        warnings.append("已移除相邻重复段落")
    if removed_near:
        warnings.append("已移除高置信度近重复段落")
    # Keep messages deterministic and non-sensitive for the API/UI.
    warnings = list(dict.fromkeys(warnings))
    status = "rejected" if rejected else ("warning" if warnings or markers else "clean")
    clean_hash = hashlib.sha256(cleaned.content.encode("utf-8")).hexdigest() if cleaned.content else ""
    report = DocumentQualityReport(
        cleaning_revision=CLEANING_REVISION,
        status=status,
        raw_chars=raw_chars,
        clean_chars=len(cleaned.content),
        raw_blocks=raw_blocks,
        clean_blocks=len(cleaned.blocks),
        removed_blank_lines=removed_blank_lines,
        removed_headers_footers=len(header_footer_indexes),
        removed_exact_duplicates=removed_exact,
        removed_near_duplicates=removed_near,
        suspicious_markers=list(dict.fromkeys(markers)),
        warnings=warnings,
        cleaned_content_sha256=clean_hash,
    )
    return CleanedDocument(cleaned, report)


# Short public alias for integrations that treat extraction and cleaning as a
# pipeline stage.
clean_document = clean_extracted_document
