"""Shared text safety rules for anything that reaches a prompt or long-term storage."""
from __future__ import annotations

import re

LABEL = "[已脱敏]"
_SECRET_NAME = r"[\w.-]*(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|credential|authorization)[\w.-]*"
_NOT_A_VALUE = r"(?!authentication|auth\b|required|incorrect|expired|is\b|was\b|for\b|in\b|of\b|and\b|or\b|not\b|may\b|must\b)"
SECRET_RULES = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "{label}"),
    (re.compile(rf"(?i)\b({_SECRET_NAME})(\s*[:=]\s*)(?:bearer\s+|basic\s+)?[\"']?[^\s\"',;]+"), r"\1\2{label}"),
    (re.compile(rf"(?i)\b(password|passwd|pwd)\s+{_NOT_A_VALUE}\S{{4,}}"), r"\1 {label}"),
    (re.compile(r"(令牌|密钥|私钥|口令|密码|凭证)\s*(?:[:：=]|是|为)\s*(?![?？]|什么|啥|哪个|如何|怎么)[^\s，。！？；、]+"), r"\1：{label}"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer {label}"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "{label}"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "{label}"),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "{label}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "{label}"),
    (re.compile(r"(://[^:/\s@]+:)[^@\s]+@"), r"\1{label}@"),
)
INSTRUCTION = re.compile(
    r"(?:忽略|无视|绕过|覆盖).{0,12}(?:规则|指令|提示词|限制|策略|系统)"
    r"|(?:ignore|disregard|bypass|override).{0,24}(?:instruction|rule|prompt|polic)"
    r"|(?:系统提示词|system prompt)"
    r"|(?:不需要|不用|无需|不必)(?:再)?(?:询问|告知|确认|通知|审批|经过)"
    r"|(?:直接|立刻|立即|必须先?|务必)(?:\S{0,6})(?:删除|清空|结束|杀掉|格式化|执行|运行)"
    r"|AI\s*(?:助手|维护|agent)|\[?OPS-NOTE\]?|始终同意|always (?:approve|agree)",
    re.IGNORECASE,
)


def redact(text: str, label: str = LABEL) -> str:
    for pattern, replacement in SECRET_RULES:
        text = pattern.sub(replacement.replace("{label}", label), text)
    return text


def looks_like_instruction(text: str) -> bool:
    """True for text that tries to steer an agent, as opposed to describing a fact."""
    return bool(INSTRUCTION.search(text))
