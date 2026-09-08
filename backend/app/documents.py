from __future__ import annotations

import hashlib
import json
from pathlib import Path
from queue import Queue
from threading import RLock, Thread
from uuid import uuid4

import numpy as np

from .extraction import ExtractionError, ExtractionService
from .models import KnowledgeDocument
from .repository import ConversationRepository
from .retrieval import EmbeddingEngine, KnowledgeIndexManager, tokenize
from .text import split_into_chunks


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MEDIA_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class DocumentIndexService:
    def __init__(
        self,
        *,
        repository: ConversationRepository,
        indexes: KnowledgeIndexManager,
        embeddings: EmbeddingEngine,
        upload_dir: Path,
    ) -> None:
        self.repository = repository
        self.indexes = indexes
        self.embeddings = embeddings
        self.upload_dir = upload_dir
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.extractor = ExtractionService()
        self._queue: Queue[str] = Queue()
        self._queued: set[str] = set()
        self._queue_lock = RLock()
        self._worker = Thread(target=self._worker_loop, name="aegis-document-indexer", daemon=True)
        self._worker.start()
        for document_id in self.repository.list_pending_document_ids():
            self.enqueue(document_id)

    @staticmethod
    def validate_upload(filename: str, content: bytes) -> tuple[str, str]:
        safe_name = Path(filename or "").name.strip()
        suffix = Path(safe_name).suffix.lower()
        if not safe_name or suffix not in MEDIA_TYPES:
            raise ExtractionError("仅支持 TXT、Markdown、PDF 和 DOCX 文件")
        if not content:
            raise ExtractionError("不能上传空文件")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ExtractionError("单个文件不能超过 20 MB")
        return safe_name, MEDIA_TYPES[suffix]

    def create_upload(self, knowledge_base_id: str, filename: str, content: bytes) -> KnowledgeDocument:
        safe_name, media_type = self.validate_upload(filename, content)
        file_sha = hashlib.sha256(content).hexdigest()
        token = str(uuid4())
        stored_path = self.upload_dir / knowledge_base_id / token / safe_name
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        stored_path.write_bytes(content)
        try:
            document = self.repository.create_document(
                knowledge_base_id=knowledge_base_id,
                filename=safe_name,
                media_type=media_type,
                size_bytes=len(content),
                sha256=file_sha,
                stored_path=str(stored_path),
            )
        except Exception:
            stored_path.unlink(missing_ok=True)
            try:
                stored_path.parent.rmdir()
            except OSError:
                pass
            raise
        self.enqueue(document.id)
        return document

    def enqueue(self, document_id: str) -> None:
        with self._queue_lock:
            if document_id in self._queued:
                return
            self._queued.add(document_id)
            self._queue.put(document_id)

    def reindex(self, document_id: str) -> KnowledgeDocument:
        document = self.repository.get_document(document_id)
        if document is None:
            raise KeyError(document_id)
        self.repository.update_document(document_id, status="pending", error="")
        self.enqueue(document_id)
        return self.repository.get_document(document_id)  # type: ignore[return-value]

    def delete_document(self, document_id: str) -> bool:
        document = self.repository.get_document(document_id)
        if document is None:
            return False
        stored_path = self.repository.delete_document(document_id)
        if stored_path:
            path = Path(stored_path)
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        self.indexes.invalidate(document.knowledge_base_id)
        return True

    def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        paths = self.repository.delete_knowledge_base(knowledge_base_id)
        for value in paths:
            path = Path(value)
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        root = self.upload_dir / knowledge_base_id
        try:
            root.rmdir()
        except OSError:
            pass
        self.indexes.invalidate(knowledge_base_id)

    def _worker_loop(self) -> None:
        while True:
            document_id = self._queue.get()
            try:
                self._index(document_id)
            finally:
                with self._queue_lock:
                    self._queued.discard(document_id)
                self._queue.task_done()

    def _index(self, document_id: str) -> None:
        source = self.repository.get_document_source(document_id)
        if source is None:
            return
        document, stored_path = source
        try:
            self.repository.update_document(document_id, status="indexing", error="")
            content = Path(stored_path).read_bytes()
            text = self.extractor.extract(document.filename, content)
            chunk_texts = split_into_chunks(text, chunk_size=800, overlap=120)
            if not chunk_texts:
                raise ExtractionError("文档没有可索引的文本")
            vectors = self.embeddings.embed(chunk_texts) if self.embeddings.enabled else None
            version = self.embeddings.model_name if vectors is not None else ""
            rows: list[tuple[str, str, bytes | None, int | None, str]] = []
            for index, chunk_text in enumerate(chunk_texts):
                vector = np.asarray(vectors[index], dtype=np.float32) if vectors is not None else None
                rows.append(
                    (
                        chunk_text,
                        json.dumps(tokenize(chunk_text), ensure_ascii=False),
                        vector.tobytes() if vector is not None else None,
                        int(vector.shape[0]) if vector is not None else None,
                        version,
                    )
                )
            self.repository.replace_chunks(document_id, document.knowledge_base_id, rows)
            self.repository.update_document(document_id, status="ready", content=text, error="")
            self.indexes.invalidate(document.knowledge_base_id)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            try:
                self.repository.update_document(document_id, status="failed", error=message[:500])
            except KeyError:
                return
            self.indexes.invalidate(document.knowledge_base_id)
