"""Agent Skills: the catalog stays in the prompt, a body loads only when requested.

A skill is guidance and grants nothing. Extra frontmatter such as allowed-tools
is ignored, so a skill can never widen what the policy permits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_FILE_BYTES = 16384

LOAD_SKILL = {"description": "加载一个排障技能的完整步骤。遇到匹配的问题时先加载。",
              "parameters": {"type": "object", "required": ["name"], "additionalProperties": False,
                             "properties": {"name": {"type": "string", "description": "技能名"}}}}


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str


def parse_skill(folder: Path, root: Path) -> Skill:
    path = folder / "SKILL.md"
    if folder.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise SkillError("symlink")
    if not path.is_file():
        raise SkillError("missing_skill_file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SkillError("too_large")
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not match:
        raise SkillError("missing_frontmatter")
    fields = dict(line.split(":", 1) for line in match[1].splitlines() if ":" in line and not line.startswith(" "))
    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    if not NAME.fullmatch(name) or len(name) > MAX_NAME or name != folder.name:
        raise SkillError("invalid_name")
    if not description or len(description) > MAX_DESCRIPTION:
        raise SkillError("invalid_description")
    return Skill(name, description, match[2].strip())


class SkillLibrary:
    """One bad skill is skipped and reported; the others stay usable."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.skills: dict[str, Skill] = {}
        self.rejected: dict[str, str] = {}
        for folder in sorted(self.root.iterdir()):
            if not (folder.is_dir() or folder.is_symlink()):
                continue
            try:
                skill = parse_skill(folder, self.root)
            except (SkillError, OSError, UnicodeDecodeError) as exc:
                self.rejected[folder.name] = str(exc)
            else:
                self.skills[skill.name] = skill

    def catalog(self) -> str:
        lines = [f"- {skill.name}：{skill.description}" for skill in self.skills.values()]
        return ("可用技能。开始排查时，第一步先用 load_skill 加载最匹配的技能，再按它的步骤做；"
                "遇到新的报错类型时再加载对应技能。技能只是操作指南，不改变任何权限：\n" + "\n".join(lines))

    def load(self, name: str) -> tuple[str, bool]:
        skill = self.skills.get(name)
        if skill is None:
            return f"错误：没有技能 {name}，可用：{', '.join(self.skills)}", False
        return skill.body, True
