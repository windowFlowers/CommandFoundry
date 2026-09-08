from __future__ import annotations

from io import BytesIO
from pathlib import Path

from .text import normalize_text

try:
    from pypdf import PdfReader
except ModuleNotFoundError:  # pragma: no cover - optional import is verified at packaging time
    PdfReader = None

try:
    from docx import Document as DocxDocument
except ModuleNotFoundError:  # pragma: no cover
    DocxDocument = None


class ExtractionError(ValueError):
    pass


class ExtractionService:
    supported_suffixes = {".txt", ".md", ".markdown", ".pdf", ".docx"}

    def extract(self, filename: str, content: bytes) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in self.supported_suffixes:
            raise ExtractionError(f"不支持的文件类型：{suffix or '未知类型'}")
        if suffix in {".txt", ".md", ".markdown"}:
            text = self._decode_text(content)
        elif suffix == ".pdf":
            text = self._extract_pdf(content)
        else:
            text = self._extract_docx(content)
        text = normalize_text(text)
        if not text:
            raise ExtractionError("文档没有可索引的文本")
        return text

    @staticmethod
    def _decode_text(content: bytes) -> str:
        for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ExtractionError("无法解码文本文件内容")

    @staticmethod
    def _extract_pdf(content: bytes) -> str:
        if PdfReader is None:
            raise ExtractionError("当前运行环境缺少 PDF 解析组件")
        try:
            reader = PdfReader(BytesIO(content))
            if reader.is_encrypted:
                raise ExtractionError("不支持加密 PDF")
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError("PDF 文件损坏或无法解析") from exc
        if not text.strip():
            raise ExtractionError("PDF 没有可提取文本，扫描型 PDF 暂不支持")
        return text

    @staticmethod
    def _extract_docx(content: bytes) -> str:
        if DocxDocument is None:
            raise ExtractionError("当前运行环境缺少 DOCX 解析组件")
        try:
            document = DocxDocument(BytesIO(content))
            parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
            for table in document.tables:
                for row in table.rows:
                    value = " | ".join(cell.text.strip() for cell in row.cells)
                    if value.strip(" |"): parts.append(value)
        except Exception as exc:
            raise ExtractionError("DOCX 文件损坏或无法解析") from exc
        if not parts:
            raise ExtractionError("DOCX 没有可提取文本")
        return "\n\n".join(parts)
