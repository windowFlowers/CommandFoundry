from __future__ import annotations

import json
import re

import httpx
from pydantic import BaseModel, Field, ValidationError

from .models import CommandBlock, RetrievalHit


class ModelAnswerDraft(BaseModel):
    summary: str = Field(min_length=1, max_length=600)
    commands: list[CommandBlock] = Field(min_length=1, max_length=6)
    notes: list[str] = Field(default_factory=list, max_length=8)


SYSTEM_PROMPT = """你是 AegisCopilot，一个谨慎的开发者命令助手。
只能根据给出的本地知识片段回答，不得编造命令、参数或来源。
输出严格 JSON，字段仅允许 summary、commands、notes。
commands 每项必须包含 label、language、code、platforms、prerequisites、risk、warning。
回答中文简洁明确；命令保留原始语法和占位符。不要使用 Markdown 代码围栏。
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
        return match.group(1) if match else content

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
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
            return ModelAnswerDraft.model_validate_json(self._strip_code_fence(content))
        except (ValidationError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("模型未返回有效的结构化答案") from exc
