from __future__ import annotations

import json
import re
from time import perf_counter

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from .models import CommandBlock, GenerationInfo, RetrievalHit


class ModelAnswerDraft(BaseModel):
    summary: str = Field(min_length=1, max_length=600)
    commands: list[CommandBlock] = Field(default_factory=list, max_length=6)
    notes: list[str] = Field(default_factory=list, max_length=8)
    generation: GenerationInfo = Field(default_factory=GenerationInfo, exclude=True)

    @field_validator("commands", mode="before")
    @classmethod
    def normalize_commands(cls, value):
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("commands 必须是数组")
        normalized = []
        language_aliases = {
            "sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash",
            "mysql": "sql", "postgresql": "sql", "postgres": "sql",
            "ps1": "powershell", "pwsh": "powershell", "cmd": "powershell",
        }
        for item in value:
            if isinstance(item, BaseModel):
                item = item.model_dump(mode="python")
            if not isinstance(item, dict) or not str(item.get("code") or "").strip():
                continue
            command = dict(item)
            language = str(command.get("language") or "text").lower()
            command["language"] = language_aliases.get(language, language)
            if command["language"] not in {"bash", "sql", "powershell", "text"}:
                command["language"] = "text"
            for field in ("platforms", "prerequisites"):
                if command.get(field) is None:
                    command[field] = []
                elif isinstance(command[field], str):
                    command[field] = [command[field]]
            if command.get("risk") not in {"low", "medium", "high"}:
                command["risk"] = "low"
            command["label"] = str(command.get("label") or "可复制命令")
            command["warning"] = command.get("warning") or None
            normalized.append(command)
        return normalized

    @field_validator("notes", mode="before")
    @classmethod
    def normalize_notes(cls, value):
        if value is None:
            return []
        return [value] if isinstance(value, str) else value


SYSTEM_PROMPT = """你是 AegisCopilot，一个谨慎的开发者命令助手。
只能根据给出的本地知识片段回答，不得编造命令、参数或来源。
输出严格 JSON，字段仅允许 summary、commands、notes。
commands 每项必须包含 label、language、code、platforms、prerequisites、risk、warning。
回答中文简洁明确；命令保留原始语法和占位符。不要使用 Markdown 代码围栏。
如果候选中没有可靠的命令，commands 必须返回空数组，禁止为了满足格式编造命令。
如果候选里有多种做法，优先给安全且可逆的方案。"""


class DeepSeekGenerator:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _strip_code_fence(content: str) -> str:
        content = content.strip()
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.S | re.I)
        if match:
            return match.group(1)
        start, end = content.find("{"), content.rfind("}")
        return content[start : end + 1] if start >= 0 and end > start else content

    def _context(self, hits: list[RetrievalHit]) -> str:
        return json.dumps(
            [
                {
                    "topic_id": hit.topic.id,
                    "title": hit.topic.title,
                    "summary": hit.topic.summary,
                    "commands": [item.model_dump(mode="json") for item in hit.topic.commands[:4]],
                    "notes": hit.topic.notes,
                }
                for hit in hits
            ],
            ensure_ascii=False,
        )

    def generate(self, query: str, hits: list[RetrievalHit]) -> ModelAnswerDraft:
        if not self.available:
            raise RuntimeError("未配置模型 API Key")
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"用户问题：{query}\n本地知识片段：{self._context(hits)}"},
            ],
        }
        started = perf_counter()
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        latency_ms = max(1, round((perf_counter() - started) * 1000))
        try:
            content = data["choices"][0]["message"]["content"]
            draft = ModelAnswerDraft.model_validate_json(self._strip_code_fence(content))
            usage = data.get("usage") or {}
            draft.generation = GenerationInfo(
                attempted=True,
                provider="deepseek",
                model=str(data.get("model") or self.model),
                request_id=str(data["id"]) if data.get("id") else None,
                latency_ms=latency_ms,
                prompt_tokens=_optional_int(usage.get("prompt_tokens")),
                completion_tokens=_optional_int(usage.get("completion_tokens")),
                total_tokens=_optional_int(usage.get("total_tokens")),
            )
            return draft
        except (ValidationError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("模型未返回有效的结构化答案") from exc


def _optional_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
