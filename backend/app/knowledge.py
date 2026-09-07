from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .models import KnowledgeTopic


class KnowledgeCatalog:
    def __init__(self, knowledge_dir: Path) -> None:
        self.knowledge_dir = knowledge_dir
        self.topics: list[KnowledgeTopic] = []
        self.manifest: dict = {}

    def load(self) -> list[KnowledgeTopic]:
        topics_path = self.knowledge_dir / "topics.json"
        manifest_path = self.knowledge_dir / "manifest.json"
        if not topics_path.exists() or not manifest_path.exists():
            raise FileNotFoundError(f"知识库文件不完整：{self.knowledge_dir}")
        self.topics = [
            KnowledgeTopic.model_validate(item)
            for item in json.loads(topics_path.read_text(encoding="utf-8"))
        ]
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_ids = {str(item["source_id"]) for item in self.manifest.get("sources", [])}
        missing = [topic.source.source_id for topic in self.topics if topic.source.source_id not in source_ids]
        if missing:
            raise ValueError(f"以下知识来源无法追溯到 manifest：{', '.join(missing[:5])}")
        if len({topic.id for topic in self.topics}) != len(self.topics):
            raise ValueError("topics.json 包含重复主题 id")
        return self.topics

    @property
    def domain_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(topic.domain for topic in self.topics).items()))
