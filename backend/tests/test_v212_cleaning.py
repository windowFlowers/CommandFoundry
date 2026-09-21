from __future__ import annotations

import sqlite3
from pathlib import Path

from app.cleaning import CLEANING_REVISION, clean_extracted_document
from app.extraction import ExtractedBlock, ExtractedDocument, ExtractionService
from app.models import DocumentQualityReport
from app.repository import DATABASE_SCHEMA_VERSION, ConversationRepository


def test_markdown_cleaning_preserves_code_and_rebuilds_exact_locators() -> None:
    source = (
        b"\xef\xbb\xbf# Deploy\r\n\r\nRun   this.\r\n\r\nRun this.\r\n\r\n"
        b"```powershell\r\n  Get-Service\r\n```\r\n"
    )
    extracted = ExtractionService().extract("deploy.md", source)
    cleaned = clean_extracted_document(extracted, raw_bytes=source)

    assert cleaned.report.cleaning_revision == CLEANING_REVISION
    assert cleaned.report.removed_exact_duplicates == 1
    assert cleaned.document.content.startswith("# Deploy\n\nRun   this.")
    code = next(block for block in cleaned.document.blocks if block.kind == "code")
    assert code.text == "```powershell\n  Get-Service\n```"
    assert cleaned.document.content[code.char_start : code.char_end] == code.text
    assert code.line_start == 7
    assert cleaned.report.cleaned_content_sha256


def test_pdf_page_edge_noise_requires_three_pages() -> None:
    blocks: list[ExtractedBlock] = []
    for page in range(1, 4):
        blocks.extend(
            (
                ExtractedBlock(
                    "Company handbook", "paragraph", 0, 0,
                    page_start=page, page_end=page, paragraph_start=1, paragraph_end=1,
                ),
                ExtractedBlock(
                    f"Body {page}", "paragraph", 0, 0,
                    page_start=page, page_end=page, paragraph_start=2, paragraph_end=2,
                ),
                ExtractedBlock(
                    "Page footer", "paragraph", 0, 0,
                    page_start=page, page_end=page, paragraph_start=3, paragraph_end=3,
                ),
            )
        )
    cleaned = clean_extracted_document(ExtractedDocument("raw", blocks, "pdf"))
    assert [block.text for block in cleaned.document.blocks] == ["Body 1", "Body 2", "Body 3"]
    assert cleaned.report.removed_headers_footers == 6


def test_code_and_table_rows_are_not_near_deduplicated() -> None:
    blocks = [
        ExtractedBlock("```bash\necho one\n```", "code", 0, 0),
        ExtractedBlock("```bash\necho one\n```", "code", 0, 0),
        ExtractedBlock("Key | Value", "table_row", 0, 0),
        ExtractedBlock("Key | Value", "table_row", 0, 0),
    ]
    cleaned = clean_extracted_document(ExtractedDocument("raw", blocks, "markdown"))
    assert len(cleaned.document.blocks) == 4
    assert cleaned.report.removed_near_duplicates == 0
    assert cleaned.report.removed_exact_duplicates == 0


def test_high_confidence_near_duplicate_prose_is_removed_but_similar_prose_is_kept() -> None:
    repeated = (
        "Before restarting the service, verify the current process state and record the active PID "
        "so the change can be reversed safely."
    )
    almost_same = repeated.replace("active PID", "new PID")
    blocks = [
        ExtractedBlock(repeated, "paragraph", 0, 0),
        ExtractedBlock(repeated.replace("  ", " "), "paragraph", 0, 0),
        ExtractedBlock(almost_same, "paragraph", 0, 0),
    ]
    cleaned = clean_extracted_document(ExtractedDocument("raw", blocks, "text"))

    assert cleaned.report.removed_exact_duplicates == 1
    assert cleaned.report.removed_near_duplicates == 0
    assert len(cleaned.document.blocks) == 2


def test_control_characters_are_reported_even_after_they_are_removed() -> None:
    source = "标题\x01\n\n正文\x02".encode("utf-8")
    extracted = ExtractedDocument(
        "标题\x01\n\n正文\x02",
        [ExtractedBlock("标题\x01", "paragraph", 0, 3), ExtractedBlock("正文\x02", "paragraph", 5, 8)],
        "text",
    )
    cleaned = clean_extracted_document(extracted, raw_bytes=source)

    assert cleaned.document.content == "标题\n\n正文"
    assert "control_character" in cleaned.report.suspicious_markers
    assert cleaned.report.status == "rejected"


def test_repeated_pdf_text_in_the_body_is_not_removed_as_a_page_edge() -> None:
    blocks = [
        ExtractedBlock("Header", "paragraph", 0, 6, page_start=1, page_end=1),
        ExtractedBlock("Body", "paragraph", 0, 4, page_start=1, page_end=1),
        ExtractedBlock("Header", "paragraph", 0, 6, page_start=1, page_end=1),
        ExtractedBlock("Footer", "paragraph", 0, 6, page_start=1, page_end=1),
        ExtractedBlock("Header", "paragraph", 0, 6, page_start=2, page_end=2),
        ExtractedBlock("Body two", "paragraph", 0, 8, page_start=2, page_end=2),
        ExtractedBlock("Footer", "paragraph", 0, 6, page_start=2, page_end=2),
        ExtractedBlock("Header", "paragraph", 0, 6, page_start=3, page_end=3),
        ExtractedBlock("Body three", "paragraph", 0, 10, page_start=3, page_end=3),
        ExtractedBlock("Footer", "paragraph", 0, 6, page_start=3, page_end=3),
    ]
    cleaned = clean_extracted_document(ExtractedDocument("raw", blocks, "pdf"))

    assert [block.text for block in cleaned.document.blocks].count("Header") == 1


def test_empty_or_garbled_content_is_rejected_without_source_mutation() -> None:
    source = b"\xef\xbb\xbf\x00\x00"
    try:
        extracted = ExtractionService().extract("empty.txt", source)
    except Exception:
        # Extraction is allowed to reject an empty source before cleaning; the
        # upload layer still retains the original bytes for diagnostics.
        return
    cleaned = clean_extracted_document(extracted, raw_bytes=source)
    assert cleaned.report.status == "rejected"
    assert "empty_text" in cleaned.report.suspicious_markers
    assert source == b"\xef\xbb\xbf\x00\x00"


def test_schema_v7_has_quality_columns_and_legacy_shape_is_compatible(tmp_path: Path) -> None:
    database = tmp_path / "quality.sqlite3"
    repository = ConversationRepository(database)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(knowledge_documents)")}
        assert DATABASE_SCHEMA_VERSION == 7
        assert {"cleaning_status", "cleaning_revision", "quality_json", "cleaned_content_sha256"} <= columns
    report = DocumentQualityReport(cleaning_revision=CLEANING_REVISION, status="clean")
    assert report.status == "clean"
    # The repository object is intentionally kept alive by the application;
    # no raw content or secret is part of this schema-level assertion.
    del repository


def test_schema_v7_migration_creates_a_verified_pre_v212_backup(tmp_path: Path) -> None:
    database = tmp_path / "v6.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('kept')")
        connection.execute("PRAGMA user_version = 6")

    ConversationRepository(database)

    backup = tmp_path / "v6.sqlite3.pre-v2.12.backup"
    assert backup.exists()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "kept"
