from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .models import KnowledgeTopic
from .recipes import CommandRecipe, RecipeCatalog


class KnowledgeCatalog:
    def __init__(self, knowledge_dir: Path) -> None:
        self.knowledge_dir = knowledge_dir
        self.topics: list[KnowledgeTopic] = []
        self.manifest: dict = {}
        # Recipes are a separate, optional allow-list of derived executable
        # variants.  Keeping the catalog here makes the bundle available to
        # the command planner without changing the citation-backed topic
        # model.  Older installations simply expose an empty list.
        self.recipe_catalog = RecipeCatalog(knowledge_dir)
        self.recipes: list[CommandRecipe] = []

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
        self.recipes = self.recipe_catalog.load()
        source_ids = {str(item["source_id"]) for item in self.manifest.get("sources", [])}
        missing = [topic.source.source_id for topic in self.topics if topic.source.source_id not in source_ids]
        if missing:
            raise ValueError(f"以下知识来源无法追溯到 manifest：{', '.join(missing[:5])}")
        if len({topic.id for topic in self.topics}) != len(self.topics):
            raise ValueError("topics.json 包含重复主题 id")
        manifest_recipe_ids = {
            str(item.get("recipe_id"))
            for item in self.manifest.get("recipes", [])
            if isinstance(item, dict) and item.get("recipe_id")
        }
        loaded_recipe_ids = {recipe.recipe_id for recipe in self.recipes}
        if manifest_recipe_ids and manifest_recipe_ids != loaded_recipe_ids:
            raise ValueError("recipes.json 与 manifest.json 的 recipe_id 不一致")
        return self.topics

    @property
    def domain_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(topic.domain for topic in self.topics).items()))
