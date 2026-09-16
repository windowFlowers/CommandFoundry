from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from queue import Queue
from threading import RLock, Thread
from typing import Iterable

import numpy as np

from .generation import DeepSeekGenerator
from .memory import estimate_tokens
from .models import PersonalizationInfo, UserMemory, UserMemoryCreateRequest, UserMemoryUpdateRequest
from .repository import ConversationRepository
from .retrieval import EmbeddingEngine, tokenize


GLOBAL_CATEGORIES = {"response_style", "expertise"}
SCOPED_CATEGORIES = {"platform", "toolchain", "project_constraint"}
ALLOWED_CATEGORIES = GLOBAL_CATEGORIES | SCOPED_CATEGORIES
PERSISTENCE_SIGNAL = re.compile(
    r"(?:请记住|记住我|以后|今后|默认|通常|一直|主要|我的(?:系统|环境|项目|技术栈)|"
    r"这个项目|本项目|我们(?:的)?项目|我(?:更)?(?:喜欢|偏好|习惯)|"
    r"不是.{0,20}(?:而是|是))",
    re.IGNORECASE,
)
QUESTION_SIGNAL = re.compile(r"(?:怎么|如何|为什么|是否|能否|可以吗|是什么|有哪些|[?？])", re.IGNORECASE)
# A question can quote a durable word (most often “默认”) without being a
# durable preference.  Keep an explicit “remember this” request as the only
# exception: it is unambiguous even when phrased politely as a question.
EXPLICIT_SAVE_SIGNAL = re.compile(r"(?:请|帮我)?记住(?:我|一下|这(?:个|条))?", re.IGNORECASE)
CODE_PATTERN = re.compile(r"```|`[^`\r\n]+`")
# Long-term memory stores profile facts, not executable snippets.  Fenced and
# inline code are rejected above; these expressions cover common *unfenced*
# command/script shapes without classifying a bare tool name ("我使用 npm") as
# executable content.
UNFENCED_COMMAND_PATTERNS = (
    re.compile(
        r"(?<![\w-])git\s+(?:add|commit|status|reset|revert|checkout|switch|branch|push|pull|fetch|"
        r"merge|rebase|log|diff|restore|clean|clone|tag|stash)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w-])(?:docker(?:-compose)?|kubectl|helm)\s+(?:run|exec|ps|logs|build|pull|push|"
        r"compose|stop|start|restart|rm|rmi|system|volume|network|get|describe|apply|delete|rollout|"
        r"scale|port-forward|config|install|upgrade|uninstall)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w-])(?:npm|pnpm|yarn)\s+(?:install|add|remove|uninstall|run|exec|publish|update|"
        r"upgrade|audit|test|build|start)\b|(?<![\w-])pipx?\s+(?:install|uninstall|freeze|list|run)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w-])(?:python3?|node)\s+(?:-[cm]|[^\s]+\.(?:py|js|mjs|cjs))\b|"
        r"(?<![\w-])npx\s+\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w-])(?:curl|wget|ssh|scp|rsync)\s+(?:-{1,2}\w|\S+://|[^。！？!?\r\n]+[/\\])|"
        r"(?<![\w-])(?:cd|ls|cp|mv|mkdir|touch|cat|grep|rg|find)\s+(?:-{1,2}\w|\.{0,2}[/\\]|[/~]|\S+)|"
        r"(?<![\w-])(?:systemctl|apt|apt-get|dnf|yum|brew)\s+(?:install|remove|update|upgrade|"
        r"start|stop|restart|status|enable|disable)\b|"
        r"(?<![\w-])(?:mvn|gradle)\s+(?:-{1,2}\w|clean|test|build|install|package|assemble|check)\b|"
        r"(?<![\w-])(?:mysql|psql|redis-cli)\s+(?:-{1,2}\w|[/~]|\S+\.(?:sql|conf))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w-])[A-Z][A-Za-z]+-[A-Z][A-Za-z]+\s+(?:-{1,2}[A-Za-z]|\$|[A-Za-z0-9_'\"])",
    ),
    re.compile(
        r"(?<![\w-])(?:SELECT\s+.+\s+FROM|INSERT\s+INTO|UPDATE\s+\S+\s+SET|CREATE\s+"
        r"(?:TABLE|VIEW|INDEX)|ALTER\s+TABLE|DELETE\s+FROM|DROP\s+(?:TABLE|DATABASE|SCHEMA)|"
        r"TRUNCATE\s+TABLE)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:&&|\|\||;\s*(?:sudo|git|docker|kubectl|npm|pnpm|python|node)\b|^\s*#!)", re.MULTILINE),
)
COMMAND_PATTERNS = (
    # Long-term memory is profile data, never a place to persist executable
    # snippets. These patterns intentionally target command-shaped text with a
    # high signal instead of treating ordinary technology names as commands.
    re.compile(r"(?<![\w-])(?:sudo\s+)?rm\s+-(?:[A-Za-z]*r[A-Za-z]*f|[A-Za-z]*f[A-Za-z]*r)\b", re.IGNORECASE),
    re.compile(r"(?<![\w-])Remove-Item\b[^\r\n]{0,160}(?:-Recurse|-Force)\b", re.IGNORECASE),
    re.compile(r"(?<![\w-])git\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"(?<![\w-])(?:sudo\s+)?shutdown\b(?:\s+-[A-Za-z]+|\s+(?:now|/s|/r))", re.IGNORECASE),
    re.compile(r"(?<![\w-])(?:sudo\s+)?(?:chmod|chown)\s+(?:-[A-Za-z]+\s+)?(?:777|\S+\s+\S+)", re.IGNORECASE),
    re.compile(r"\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\s+[A-Za-z_][A-Za-z0-9_.]*\b", re.IGNORECASE),
)
SENSITIVE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"authorization\s*:\s*(?:bearer|basic)\s+\S+", re.IGNORECASE),
    re.compile(r"\b(?:password|passwd|token|api[_ -]?key|secret)\s*[:=]\s*\S+", re.IGNORECASE),
    # Chinese secret statements normally use “是 / 为 / ：”, rather than the
    # English ``key=value`` form above.  Require a value-bearing construction
    # so ordinary questions such as “API 密钥是什么？” do not become false
    # positives, while never forwarding an actual secret to the extractor.
    re.compile(
        r"(?:api\s*(?:key|密钥)|access[_ -]?token|refresh[_ -]?token|"
        r"password|passwd|secret|token|(?:访问|授权|登录|客户端)?(?:令牌|密钥)|"
        r"私钥|口令|密码|凭证|授权码)\s*(?:[:：=]|是|为|叫)\s*"
        r"(?![?？]|(?:什么|啥|哪个|哪一种|如何|怎么|为何)(?:[?？\s]|$))"
        r"[\"'`“”]?(?:[^\s，。！？!?；;、]){1,}",
        re.IGNORECASE,
    ),
    # People occasionally omit “是/为” after a possessive phrase.  This is
    # deliberately limited to an ownership prefix, avoiding broad matches for
    # documentation terms such as “密码管理器”.
    re.compile(
        r"(?:我的|账号(?:的)?|账户(?:的)?|登录(?:的)?|服务(?:的)?|数据库(?:的)?|项目(?:的)?|"
        r"系统(?:的)?)\s*(?:api\s*(?:key|密钥)|(?:访问|授权|登录|客户端)?(?:令牌|密钥)|"
        r"私钥|口令|密码|凭证)\s+[\"'`“”]?(?:[^\s，。！？!?；;、]){1,}",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b", re.IGNORECASE),
    re.compile(r"(?:postgres(?:ql)?|mysql|redis|mongodb)://[^\s:/]+:[^\s@]+@", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\r\n<>|?*]+"),
    re.compile(r"(?<![\\])\\\\[^\r\n<>|?*]+"),
    # A slash preceded by ':' or another slash belongs to an URL, not a local
    # POSIX path.  One-component roots such as /tmp are deliberately covered.
    re.compile(r"(?<![:\w/])/(?!/)[A-Za-z0-9._~+-]+(?:/[A-Za-z0-9._~+ -]+)*"),
    re.compile(r"(?<![\w])~[/\\][^\r\n\s<>|?*]+"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
)

CORRECTION_SIGNAL = re.compile(
    r"(?:不是|不要(?:再)?|别用|不再(?:使用|用)|改用|换成|切换到|而是|纠正为|从.{0,30}(?:改为|换到|切换到))",
    re.IGNORECASE,
)

TECH_GROUPS: dict[str, tuple[str, ...]] = {
    "os": ("Windows", "Linux", "macOS"),
    "shell": ("PowerShell", "pwsh", "bash", "zsh", "cmd"),
    "package_manager": ("pnpm", "npm", "yarn", "pip", "poetry", "uv", "Maven", "Gradle"),
    "database": ("PostgreSQL", "MySQL", "Redis", "MongoDB", "SQLite"),
    "runtime": ("Python", "Node.js", "Node", "Java", ".NET", "Go"),
    "container": ("Docker", "Kubernetes", "kubectl"),
}

DISPLAY_PREFIX = {
    "os": "首选系统",
    "shell": "首选 Shell",
    "package_manager": "首选包管理器",
    "database": "项目数据库",
    "runtime": "项目运行时",
    "container": "容器工具",
}

# These are the stable slots that participate in current-turn conflict
# handling.  User-facing manual memory currently supplies a timestamped
# ``manual.<category>.*`` key, and model output may use labels such as
# ``preferred_os``.  A slot inferred from the actual fact is therefore more
# trustworthy than the storage key alone.
TECH_SLOT_CATEGORIES = {
    "os": "platform",
    "shell": "platform",
    "package_manager": "toolchain",
    "database": "toolchain",
    "runtime": "toolchain",
    "container": "toolchain",
}

SEMANTIC_KEY_ALIASES: dict[str, frozenset[str]] = {
    "os": frozenset(
        {
            "os",
            "operating_system",
            "system",
            "preferred_os",
            "system_preference",
            "environment_os",
            "platform_os",
        }
    ),
    "shell": frozenset({"shell", "preferred_shell", "command_shell", "terminal_shell"}),
    "package_manager": frozenset(
        {"package_manager", "package_management", "preferred_package_manager", "pkg_manager"}
    ),
    "database": frozenset({"database", "db", "project_database", "preferred_database"}),
    "runtime": frozenset({"runtime", "runtime_version", "language_runtime", "preferred_runtime"}),
    "container": frozenset({"container", "container_tool", "container_platform", "orchestrator"}),
    "language": frozenset(
        {"language", "preferred_language", "response_language", "answer_language", "reply_language", "output_language", "locale"}
    ),
    "detail": frozenset(
        {"detail", "response_detail", "answer_detail", "response_length", "verbosity", "response_style"}
    ),
    "developer_level": frozenset(
        {"developer_level", "experience_level", "skill_level", "expertise", "proficiency", "level"}
    ),
}


@dataclass(frozen=True)
class MemoryCandidate:
    scope: str
    category: str
    key: str
    value: str
    display_text: str
    confidence: float
    action: str = "ADD"


@dataclass(frozen=True)
class GateDecision:
    sanitized_input: str
    local_candidates: tuple[MemoryCandidate, ...] = ()
    infer_with_model: bool = False
    rejected_sensitive: bool = False


@dataclass(frozen=True)
class BuiltPersonalizationContext:
    info: PersonalizationInfo
    prompt_payload: str
    retrieval_query: str


@dataclass(frozen=True)
class MemoryUpdateNotice:
    status: str = "noop"
    source_message_id: str | None = None
    memory_ids: tuple[str, ...] = ()
    operation: str = "NOOP"
    task_id: str | None = None

    def model_dump(self) -> dict:
        return {
            "status": self.status,
            "source_message_id": self.source_message_id,
            "memory_ids": list(self.memory_ids),
            "operation": self.operation,
            "task_id": self.task_id,
        }


def _clean_text(value: object, *, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _normalize_memory_key(value: object) -> str:
    return re.sub(r"[^a-z0-9_.:-]+", "_", str(value or "").casefold()).strip("_")[:80]


def _semantic_slot_from_key(value: object) -> str | None:
    key = _normalize_memory_key(value).replace("-", "_").replace(":", "_")
    for slot, aliases in SEMANTIC_KEY_ALIASES.items():
        if key in aliases:
            return slot
    return None


def contains_sensitive_data(value: str) -> bool:
    if CODE_PATTERN.search(value):
        return True
    return any(
        pattern.search(value)
        for pattern in (*SENSITIVE_PATTERNS, *COMMAND_PATTERNS, *UNFENCED_COMMAND_PATTERNS)
    )


def _technology_mentions(value: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, names in TECH_GROUPS.items():
        located: list[tuple[int, str]] = []
        for name in names:
            for match in re.finditer(
                rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])",
                value,
                re.IGNORECASE,
            ):
                canonical = "Node.js" if name == "Node" else "PowerShell" if name == "pwsh" else name
                located.append((match.start(), canonical))
        matches: list[str] = []
        for _, canonical in sorted(located):
            if canonical not in matches:
                matches.append(canonical)
        if matches:
            result[key] = matches
    return result


def _single_technology_slot(value: str) -> str | None:
    """Return a slot only when one technology family is unambiguous."""

    mentions = _technology_mentions(value)
    return next(iter(mentions)) if len(mentions) == 1 else None


def _response_style_slot(value: str) -> str | None:
    normalized = _clean_text(value, limit=160).casefold()
    if normalized in {"en", "en-us", "english", "英文", "英语"}:
        return "language"
    if normalized in {"zh", "zh-cn", "chinese", "中文", "汉语"}:
        return "language"
    if normalized in {"concise", "brief", "short", "简短", "简洁", "精炼"}:
        return "detail"
    if normalized in {"detailed", "verbose", "详细", "展开", "多解释"}:
        return "detail"
    return None


def _expertise_slot(value: str) -> str | None:
    normalized = _clean_text(value, limit=160).casefold()
    if normalized in {
        "beginner",
        "novice",
        "junior",
        "新手",
        "初学者",
        "入门",
        "intermediate",
        "mid",
        "中级",
        "advanced",
        "senior",
        "expert",
        "高级",
        "资深",
        "专家",
    }:
        return "developer_level"
    return None


def _semantic_slot_for_fields(category: str, key: object, value: str, display: str = "") -> str | None:
    """Infer the durable preference slot behind a legacy or model-provided row.

    A value is intentionally considered before a key for technology facts: a
    malformed model key must not turn ``Python`` into an operating-system
    preference merely because it called the field ``os``.
    """

    normalized_category = str(category or "").strip().casefold()
    key_slot = _semantic_slot_from_key(key)
    technology_slot = _single_technology_slot(value) or _single_technology_slot(display)
    if technology_slot:
        return technology_slot
    if key_slot in TECH_SLOT_CATEGORIES:
        return key_slot
    if normalized_category == "response_style":
        return _response_style_slot(value) or _response_style_slot(display) or (
            key_slot if key_slot in {"language", "detail"} else None
        )
    if normalized_category == "expertise":
        return _expertise_slot(value) or _expertise_slot(display) or (
            key_slot if key_slot == "developer_level" else None
        )
    if key_slot in {"language", "detail", "developer_level"}:
        return key_slot
    return None


def _canonical_style_value(slot: str, value: str) -> str:
    normalized = _clean_text(value, limit=160).casefold()
    if slot == "language":
        if normalized in {"en", "en-us", "english", "英文", "英语"}:
            return "en"
        if normalized in {"zh", "zh-cn", "chinese", "中文", "汉语"}:
            return "zh-CN"
    elif slot == "detail":
        if normalized in {"concise", "brief", "short", "简短", "简洁", "精炼"}:
            return "concise"
        if normalized in {"detailed", "verbose", "详细", "展开", "多解释"}:
            return "detailed"
    elif slot == "developer_level":
        if normalized in {"beginner", "novice", "junior", "新手", "初学者", "入门"}:
            return "beginner"
        if normalized in {"intermediate", "mid", "中级"}:
            return "intermediate"
        if normalized in {"advanced", "senior", "expert", "高级", "资深", "专家"}:
            return "advanced"
    return normalized


def _canonical_memory_value(slot: str, value: str) -> str:
    if slot in {"language", "detail", "developer_level"}:
        return _canonical_style_value(slot, value)
    if slot in TECH_SLOT_CATEGORIES:
        values = _technology_mentions(value).get(slot, [])
        if values:
            return values[0].casefold()
    return _clean_text(value, limit=160).casefold()


def _memory_semantic_key(memory: UserMemory) -> str:
    return _semantic_slot_for_fields(memory.category, memory.key, memory.value, memory.display_text) or memory.key


def _mention_is_negated(value: str, start: int) -> bool:
    prefix = value[max(0, start - 18) : start]
    return bool(
        re.search(
            r"(?:不是|不要(?:再)?(?:使用|用)?|别(?:再)?用|不用|不再(?:使用|用)|避免(?:使用)?|排除)\s*$",
            prefix,
            re.IGNORECASE,
        )
    )


def _ordered_explicit_mentions(
    value: str,
    aliases: tuple[tuple[str, str], ...],
) -> list[str]:
    located: list[tuple[int, str, bool]] = []
    for phrase, canonical in aliases:
        escaped = re.escape(phrase)
        pattern = (
            rf"(?<![A-Za-z0-9_.+-]){escaped}(?![A-Za-z0-9_.+-])"
            if re.fullmatch(r"[A-Za-z0-9_.+-]+", phrase)
            else escaped
        )
        for match in re.finditer(pattern, value, re.IGNORECASE):
            located.append((match.start(), canonical, _mention_is_negated(value, match.start())))
    if not located:
        return []
    ordered = sorted(located)
    positives = [canonical for _, canonical, negated in ordered if not negated]
    if CORRECTION_SIGNAL.search(value) and positives:
        # “不要 X，改用 Y” has one unambiguous winner.  Keeping X in the
        # explicit set would accidentally let the stale X memory survive.
        return [positives[-1]]
    result: list[str] = []
    for _, canonical, negated in ordered:
        token = f"!{canonical}" if negated else canonical
        if token not in result:
            result.append(token)
    return result


def _explicit_preferences(value: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, names in TECH_GROUPS.items():
        aliases = tuple(
            (
                name,
                "Node.js" if name == "Node" else "PowerShell" if name == "pwsh" else name,
            )
            for name in names
        )
        mentions = _ordered_explicit_mentions(value, aliases)
        if mentions:
            result[key] = mentions

    languages = _ordered_explicit_mentions(
        value,
        (("中文", "zh-cn"), ("汉语", "zh-cn"), ("英文", "en"), ("英语", "en")),
    )
    if languages:
        result["language"] = languages
    detail = _ordered_explicit_mentions(
        value,
        (
            ("简短", "concise"),
            ("简洁", "concise"),
            ("精炼", "concise"),
            ("少废话", "concise"),
            ("直接一点", "concise"),
            ("详细", "detailed"),
            ("展开", "detailed"),
            ("多解释", "detailed"),
            ("讲透", "detailed"),
            ("多给例子", "detailed"),
        ),
    )
    if detail:
        result["detail"] = detail
    return result


def _runtime_with_version(value: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}(?:\.js)?\s*(?:v|版本)?\s*(\d+(?:\.\d+){{0,2}})", value, re.IGNORECASE)
    canonical = "Node.js" if name.casefold() == "node" else name
    return f"{canonical} {match.group(1)}" if match else canonical


def _scope_for_category(category: str) -> str:
    return "global" if category in GLOBAL_CATEGORIES else "knowledge_base"


def validate_candidate(candidate: MemoryCandidate) -> MemoryCandidate | None:
    category = candidate.category.strip().casefold()
    if category not in ALLOWED_CATEGORIES:
        return None
    key = _normalize_memory_key(candidate.key)
    value = _clean_text(candidate.value, limit=160)
    display = _clean_text(candidate.display_text, limit=240)
    if not key or not value or not display or contains_sensitive_data(f"{value}\n{display}"):
        return None

    # Normalize the known preference families at the service boundary.  This
    # makes manual timestamp keys and plausible model labels (for example
    # ``preferred_os``) converge on the same upsert/conflict slot.  Project
    # constraints deliberately retain their own keys: “only use pnpm” is a
    # constraint in addition to, rather than a replacement for, a package
    # manager preference.
    slot = _semantic_slot_for_fields(category, key, value, display)
    if category != "project_constraint" and slot:
        if slot in TECH_SLOT_CATEGORIES:
            category = TECH_SLOT_CATEGORIES[slot]
        elif slot in {"language", "detail"}:
            category = "response_style"
            value = _canonical_style_value(slot, value)
        elif slot == "developer_level":
            category = "expertise"
            value = _canonical_style_value(slot, value)
        key = slot

    expected_scope = _scope_for_category(category)
    if candidate.scope != expected_scope:
        return None
    confidence = max(0.0, min(1.0, float(candidate.confidence)))
    if confidence < 0.8:
        return None
    action = candidate.action.upper()
    if action not in {"ADD", "UPDATE", "SUPERSEDE"}:
        return None
    return replace(
        candidate,
        scope=expected_scope,
        category=category,
        key=key,
        value=value,
        display_text=display,
        confidence=confidence,
        action=action,
    )


def local_signal_gate(user_text: str) -> GateDecision:
    text = _clean_text(user_text, limit=800)
    if not text:
        return GateDecision(sanitized_input="")
    if len(user_text) > 1200 or contains_sensitive_data(user_text):
        return GateDecision(sanitized_input="", rejected_sensitive=True)

    candidates: list[MemoryCandidate] = []
    explicit_preferences = _explicit_preferences(text)
    is_question = bool(QUESTION_SIGNAL.search(text))
    explicitly_requested_save = bool(EXPLICIT_SAVE_SIGNAL.search(text))
    durable_correction = bool(
        CORRECTION_SIGNAL.search(text)
        and explicit_preferences
        and not is_question
    )
    persistent = bool(PERSISTENCE_SIGNAL.search(text) or durable_correction)
    # “Docker 默认网络是什么？” is a request for information, not a request to
    # store Docker as a user preference.  Do not let generic durability words
    # make a question eligible for local extraction or the model queue.
    if is_question and not explicitly_requested_save:
        persistent = False

    language = None
    language_values = [
        value for value in explicit_preferences.get("language", []) if not value.startswith("!")
    ]
    if (persistent or re.search(r"(?:请|希望).{0,8}(?:回答|回复)", text)) and language_values:
        language = (
            ("en", "回答语言：英文")
            if language_values[-1] == "en"
            else ("zh-CN", "回答语言：中文")
        )
    if language:
        candidates.append(MemoryCandidate("global", "response_style", "language", language[0], language[1], 0.99))

    detail_values = [
        value for value in explicit_preferences.get("detail", []) if not value.startswith("!")
    ]
    if (persistent or re.search(r"(?:回答|回复).{0,8}(?:简短|简洁|详细)", text)) and detail_values:
        if detail_values[-1] == "concise":
            candidates.append(MemoryCandidate("global", "response_style", "detail", "concise", "回答偏好：简洁直接", 0.98))
        else:
            candidates.append(MemoryCandidate("global", "response_style", "detail", "detailed", "回答偏好：详细说明", 0.98))

    expertise = re.search(r"(?:我是|我的水平(?:是|偏)|我属于)(?:一名)?\s*(新手|初学者|入门|中级|高级|资深|专家)", text)
    if expertise:
        level = expertise.group(1)
        normalized = "beginner" if level in {"新手", "初学者", "入门"} else "intermediate" if level == "中级" else "advanced"
        candidates.append(MemoryCandidate("global", "expertise", "developer_level", normalized, f"开发熟练度：{level}", 0.98))

    strong_declaration = re.search(
        r"(?:我的(?:系统|环境|项目|技术栈)|项目(?:数据库|环境|技术栈)|这个项目|本项目|我们(?:的)?项目|"
        r"我(?:通常|一直|主要|默认)(?:使用|用的是|在用))",
        text,
    )
    bare_declaration = re.search(r"我(?:使用|用的是|在用)\s*", text)
    declarations = bool(not is_question and (strong_declaration or bare_declaration))
    if persistent or declarations:
        mentions = _technology_mentions(text)
        for group, values in mentions.items():
            # A correction such as “不是 npm，是 pnpm” should resolve to the last
            # mentioned value. Otherwise use the first explicit declaration.
            desired = [
                item
                for item in explicit_preferences.get(group, [])
                if not item.startswith("!")
            ]
            if explicit_preferences.get(group) and not desired:
                # “以后不要用 npm” is a negative constraint, not evidence that
                # npm should become the preferred package manager.
                continue
            value = desired[-1] if CORRECTION_SIGNAL.search(text) and desired else values[0]
            if group == "runtime" and value in {"Python", "Node.js", "Java"}:
                value = _runtime_with_version(text, "Node" if value == "Node.js" else value)
            category = "platform" if group in {"os", "shell"} else "toolchain"
            candidates.append(
                MemoryCandidate(
                    "knowledge_base",
                    category,
                    group,
                    value,
                    f"{DISPLAY_PREFIX[group]}：{value}",
                    0.98,
                    "SUPERSEDE" if CORRECTION_SIGNAL.search(text) or len(values) > 1 else "ADD",
                )
            )

    constraint = re.search(
        r"(?:这个项目|本项目|我们(?:的)?项目|项目).{0,20}(必须|不能|不要|只(?:能|用)|固定|要求)([^。！？!?]{2,120})",
        text,
    )
    if constraint and not is_question:
        statement = _clean_text(f"{constraint.group(1)}{constraint.group(2)}", limit=160)
        key_tokens = [token for token in tokenize(statement) if len(token) > 1][:4]
        key = "constraint:" + "_".join(key_tokens or ["project"])
        candidates.append(MemoryCandidate("knowledge_base", "project_constraint", key, statement, f"项目约束：{statement}", 0.96))

    unique: dict[tuple[str, str], MemoryCandidate] = {}
    for candidate in candidates:
        valid = validate_candidate(candidate)
        if valid:
            unique[(valid.category, valid.key)] = valid

    durable_signal = persistent or declarations
    infer = durable_signal and not unique and not is_question
    return GateDecision(
        sanitized_input=text,
        local_candidates=tuple(unique.values()),
        infer_with_model=infer,
    )


def _memory_age_multiplier(memory: UserMemory) -> float:
    if memory.pinned:
        decay = 1.0
    else:
        from datetime import UTC, datetime

        updated = memory.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        age_days = max(0.0, (datetime.now(UTC) - updated).total_seconds() / 86400)
        decay = max(0.85, min(1.0, 0.85 + 0.15 * (2 ** (-age_days / 180))))
    boost = min(1.05, 1.0 + 0.01 * math.log1p(max(0, memory.use_count)))
    return min(1.05, decay * boost)


def _overlap_score(query: str, memory: UserMemory) -> float:
    query_tokens = set(tokenize(query))
    memory_tokens = set(tokenize(f"{memory.key} {memory.value} {memory.display_text}"))
    if not query_tokens or not memory_tokens:
        return 0.0
    return len(query_tokens & memory_tokens) / max(1, len(query_tokens))


def _conflicts(memory: UserMemory, explicit: dict[str, list[str]]) -> bool:
    memory_key = _memory_semantic_key(memory)
    memory_value = _canonical_memory_value(memory_key, memory.value)
    if memory_key in explicit:
        values = [value.casefold() for value in explicit[memory_key]]
        desired = {value for value in values if not value.startswith("!")}
        excluded = {value[1:] for value in values if value.startswith("!")}

        def matches(value: str) -> bool:
            if memory_value == value:
                return True
            # Stored facts can preserve a version or product edition (for
            # example "Python 3.12" or "Windows 11") while a one-off query
            # only names the family.
            return memory_key in TECH_SLOT_CATEGORIES and (
                memory_value.startswith(f"{value} ") or value.startswith(f"{memory_value} ")
            )

        if any(matches(value) for value in excluded):
            return True
        if desired and not any(matches(value) for value in desired):
            return True

    operating_systems = {
        value.casefold() for value in explicit.get("os", []) if not value.startswith("!")
    }
    shells = {
        value.casefold() for value in explicit.get("shell", []) if not value.startswith("!")
    }
    if memory_key == "shell" and operating_systems:
        incompatible = {"powershell", "cmd"} if operating_systems <= {"linux", "macos"} else {"bash", "zsh"}
        if any(value in memory_value for value in incompatible):
            return True
    if memory_key == "os" and shells:
        if shells & {"bash", "zsh"} and memory_value == "windows":
            return True
        if shells & {"powershell", "cmd"} and memory_value in {"linux", "macos"}:
            return True
    return False


def _query_embedding(engine: EmbeddingEngine, text: str) -> np.ndarray | None:
    if not getattr(engine, "ready", False):
        return None
    try:
        vectors = engine.embed([text])
        if vectors is None or len(vectors) != 1:
            return None
        vector = np.asarray(vectors[0], dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if vector.ndim == 1 and norm > 1e-12 else None
    except Exception:
        return None


def _stored_cosine(query_vector: np.ndarray | None, record: dict | None, model_name: str) -> float:
    if query_vector is None or not record or not record.get("embedding"):
        return 0.0
    if record.get("embedding_version") != model_name:
        return 0.0
    try:
        dimension = int(record.get("embedding_dimension") or 0)
        vector = np.frombuffer(record["embedding"], dtype=np.float32)
        if dimension != len(vector) or dimension != len(query_vector):
            return 0.0
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-12:
            return 0.0
        return max(0.0, min(1.0, float((vector / norm) @ query_vector)))
    except (TypeError, ValueError):
        return 0.0


class PersonalizationService:
    def __init__(
        self,
        *,
        repository: ConversationRepository,
        generator: DeepSeekGenerator,
        embeddings: EmbeddingEngine,
        token_budget: int = 400,
        max_memories: int = 4,
    ) -> None:
        self.repository = repository
        self.generator = generator
        self.embeddings = embeddings
        self.token_budget = token_budget
        self.max_memories = max_memories
        self._queue: Queue[str] = Queue()
        self._queued: set[str] = set()
        self._queue_lock = RLock()
        self._worker = Thread(target=self._worker_loop, name="aegis-personalization-extractor", daemon=True)
        self._worker.start()
        for task_id in self.repository.list_pending_memory_task_ids():
            self.enqueue(task_id)

    def build_context(
        self,
        *,
        query: str,
        retrieval_query: str,
        knowledge_base_id: str,
    ) -> BuiltPersonalizationContext:
        profile = self.repository.get_profile()
        if not profile.personalization_enabled:
            return BuiltPersonalizationContext(PersonalizationInfo(), "", retrieval_query)

        current_explicit = _explicit_preferences(query)
        session_explicit = _explicit_preferences(retrieval_query)
        effective_explicit = {**session_explicit, **current_explicit}
        memories = self.repository.list_user_memories(status="active")
        records_by_id: dict[str, dict] = {}
        query_vector = _query_embedding(self.embeddings, retrieval_query)
        if query_vector is not None:
            try:
                records_by_id = {
                    str(record["id"]): record
                    for record in self.repository.list_user_memory_records(status="active")
                }
            except (AttributeError, KeyError, TypeError, ValueError):
                records_by_id = {}
        candidates: list[tuple[int, float, UserMemory]] = []
        for memory in memories:
            if memory.scope == "knowledge_base" and memory.knowledge_base_id != knowledge_base_id:
                continue
            if _conflicts(memory, effective_explicit):
                continue
            base = _overlap_score(retrieval_query, memory)
            base += 0.45 * _stored_cosine(
                query_vector,
                records_by_id.get(memory.id),
                str(getattr(self.embeddings, "model_name", "")),
            )
            if memory.category == "response_style":
                base += 0.72
            elif memory.category == "expertise":
                base += 0.42
            elif memory.scope == "knowledge_base":
                base += 0.55
            else:
                base += 0.25
            if memory.pinned:
                base += 0.35
            scope_priority = 1 if memory.scope == "knowledge_base" else 0
            candidates.append((scope_priority, base * _memory_age_multiplier(memory), memory))
        candidates.sort(key=lambda item: (item[0], item[1], item[2].pinned, item[2].updated_at), reverse=True)

        selected: list[UserMemory] = []
        payload_items: list[dict] = []
        for _, _, memory in candidates:
            item = {
                "id": memory.id,
                "scope": memory.scope,
                "category": memory.category,
                "preference": memory.display_text,
            }
            trial = json.dumps([*payload_items, item], ensure_ascii=False)
            if estimate_tokens(trial) > self.token_budget:
                continue
            selected.append(memory)
            payload_items.append(item)
            if len(selected) >= self.max_memories:
                break

        enrichments: list[str] = []
        for memory in selected:
            if memory.category not in {"platform", "toolchain", "project_constraint"}:
                continue
            memory_key = _memory_semantic_key(memory)
            if any(
                not value.startswith("!")
                for value in effective_explicit.get(memory_key, [])
            ):
                continue
            enrichments.append(memory.display_text)
        enriched_query = retrieval_query
        if enrichments:
            enriched_query = f"{retrieval_query}；用户环境偏好：{'；'.join(enrichments)}"

        ids = [memory.id for memory in selected]
        scoped_count = sum(memory.scope == "knowledge_base" for memory in selected)
        info = PersonalizationInfo(
            used=bool(selected),
            memory_ids=ids,
            global_count=len(selected) - scoped_count,
            scoped_count=scoped_count,
            sent_to_model=False,
            retrieval_query_enriched=enriched_query != retrieval_query,
        )
        return BuiltPersonalizationContext(
            info=info,
            prompt_payload=json.dumps(payload_items, ensure_ascii=False) if selected else "",
            retrieval_query=enriched_query,
        )

    def _embedding_fields(self, text: str) -> dict:
        if not self.embeddings.ready:
            return {}
        vectors = self.embeddings.embed([text])
        if vectors is None or not len(vectors):
            return {}
        vector = np.asarray(vectors[0], dtype=np.float32)
        return {
            "embedding": vector.tobytes(),
            "embedding_dimension": int(vector.shape[0]),
            "embedding_version": self.embeddings.model_name,
        }

    def _save_candidate(
        self,
        candidate: MemoryCandidate,
        *,
        knowledge_base_id: str,
        conversation_id: str | None,
        source_message_id: str | None,
        provider: str,
    ) -> UserMemory | None:
        valid = validate_candidate(candidate)
        if valid is None:
            return None
        memory_knowledge_base = knowledge_base_id if valid.scope == "knowledge_base" else None
        fields = self._embedding_fields(f"{valid.key} {valid.value} {valid.display_text}")
        return self.repository.upsert_user_memory(
            scope=valid.scope,
            knowledge_base_id=memory_knowledge_base,
            category=valid.category,
            key=valid.key,
            value=valid.value,
            display_text=valid.display_text,
            confidence=valid.confidence,
            source_conversation_id=conversation_id,
            source_message_id=source_message_id,
            extraction_provider=provider,
            **fields,
        )

    def _existing_memories_for_extraction(
        self,
        *,
        statement: str,
        knowledge_base_id: str,
    ) -> list[dict[str, str]]:
        """Return only the small, relevant and source-free memory view sent upstream."""

        context = self.build_context(
            query=statement,
            retrieval_query=statement,
            knowledge_base_id=knowledge_base_id,
        )
        if not context.info.memory_ids:
            return []
        selected_ids = context.info.memory_ids[: self.max_memories]
        memories_by_id = {
            memory.id: memory
            for memory in self.repository.list_user_memories(status="active", ids=selected_ids)
        }
        payload: list[dict[str, str]] = []
        # Keep the ordering chosen by build_context rather than repository
        # ordering, because scoped and query-relevant items have precedence.
        for memory_id in selected_ids:
            memory = memories_by_id.get(memory_id)
            if memory is None:
                continue
            item = {
                "scope": memory.scope,
                "category": memory.category,
                "key": memory.key,
                "value": memory.value,
            }
            serialized = json.dumps(item, ensure_ascii=False)
            if contains_sensitive_data(serialized):
                continue
            trial = json.dumps([*payload, item], ensure_ascii=False)
            if estimate_tokens(trial) > min(400, self.token_budget):
                continue
            payload.append(item)
            if len(payload) >= min(4, self.max_memories):
                break
        return payload

    def capture_user_message(
        self,
        *,
        conversation_id: str,
        source_message_id: str,
        knowledge_base_id: str,
        text: str,
    ) -> MemoryUpdateNotice:
        profile = self.repository.get_profile()
        if not profile.personalization_enabled or not profile.auto_memory_enabled:
            return MemoryUpdateNotice(source_message_id=source_message_id)
        decision = local_signal_gate(text)
        if decision.rejected_sensitive or not decision.sanitized_input:
            return MemoryUpdateNotice(source_message_id=source_message_id)
        if decision.local_candidates:
            active_by_identity = {
                (
                    memory.scope,
                    memory.knowledge_base_id,
                    memory.category,
                    memory.key,
                ): memory
                for memory in self.repository.list_user_memories(status="active")
            }
            saved: list[UserMemory] = []
            applied_operations: list[str] = []
            for candidate in decision.local_candidates:
                memory_knowledge_base = (
                    knowledge_base_id if candidate.scope == "knowledge_base" else None
                )
                existing = active_by_identity.get(
                    (
                        candidate.scope,
                        memory_knowledge_base,
                        candidate.category,
                        candidate.key,
                    )
                )
                memory = self._save_candidate(
                    candidate,
                    knowledge_base_id=knowledge_base_id,
                    conversation_id=conversation_id,
                    source_message_id=source_message_id,
                    provider="local",
                )
                if memory is None:
                    continue
                saved.append(memory)
                if existing is None:
                    applied_operations.append("ADD")
                elif existing.value.strip().casefold() == candidate.value.strip().casefold():
                    applied_operations.append("UPDATE")
                else:
                    applied_operations.append("SUPERSEDE")
            if applied_operations and all(value == "ADD" for value in applied_operations):
                operation = "ADD"
            elif "SUPERSEDE" in applied_operations:
                operation = "SUPERSEDE"
            elif applied_operations:
                operation = "UPDATE"
            else:
                operation = "NOOP"
            return MemoryUpdateNotice(
                status="saved" if saved else "noop",
                source_message_id=source_message_id,
                memory_ids=tuple(memory.id for memory in saved),
                operation=operation,
            )
        if decision.infer_with_model and self.generator.available:
            task = self.repository.create_memory_extraction_task(
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                knowledge_base_id=knowledge_base_id,
                sanitized_input=decision.sanitized_input,
            )
            if task is None:
                return MemoryUpdateNotice(source_message_id=source_message_id)
            task_id = str(task["id"] if isinstance(task, dict) else task)
            self.enqueue(task_id)
            return MemoryUpdateNotice(
                status="queued",
                source_message_id=source_message_id,
                task_id=task_id,
            )
        return MemoryUpdateNotice(source_message_id=source_message_id)

    def create_manual(self, request: UserMemoryCreateRequest) -> UserMemory:
        candidate = MemoryCandidate(
            scope=request.scope,
            category=request.category,
            key=request.key,
            value=request.value,
            display_text=request.display_text,
            confidence=1.0,
        )
        valid = validate_candidate(candidate)
        if valid is None:
            raise ValueError("记忆内容或作用域无效")
        if valid.scope == "knowledge_base" and not request.knowledge_base_id:
            raise ValueError("知识库级记忆必须选择知识库")
        memory = self._save_candidate(
            valid,
            knowledge_base_id=request.knowledge_base_id or "",
            conversation_id=None,
            source_message_id=None,
            provider="manual",
        )
        if memory is None:
            raise ValueError("记忆内容无效")
        if request.pinned and not memory.pinned:
            memory = self.repository.patch_user_memory(memory.id, pinned=True)
        return memory

    def update_manual(self, memory_id: str, request: UserMemoryUpdateRequest) -> UserMemory:
        current = self.repository.get_user_memory(memory_id)
        if current is None:
            raise KeyError(memory_id)
        value = request.value if request.value is not None else current.value
        display = request.display_text if request.display_text is not None else current.display_text
        if contains_sensitive_data(f"{value}\n{display}"):
            raise ValueError("记忆不能包含密钥、完整路径或个人敏感信息")
        fields = self._embedding_fields(f"{current.key} {value} {display}") if (value != current.value or display != current.display_text) else {}
        return self.repository.patch_user_memory(
            memory_id,
            value=value,
            display_text=display,
            pinned=request.pinned,
            **fields,
        )

    def enqueue(self, task_id: str) -> None:
        with self._queue_lock:
            if task_id in self._queued:
                return
            self._queued.add(task_id)
            self._queue.put(task_id)

    def _worker_loop(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                task = self.repository.claim_memory_extraction_task(task_id)
                if task is None:
                    continue
                expected_epoch = int(task["extraction_epoch"])
                try:
                    profile = self.repository.get_profile()
                    if (
                        not profile.personalization_enabled
                        or not profile.auto_memory_enabled
                        or profile.extraction_epoch != expected_epoch
                    ):
                        self.repository.complete_memory_extraction_task(
                            task_id,
                            memory_ids=[],
                            expected_epoch=expected_epoch,
                            expected_attempt=int(task["attempt_count"]),
                        )
                        continue
                    draft = self.generator.extract_user_memories(
                        {
                            "user_statement": task["sanitized_input"],
                            "existing_memories": self._existing_memories_for_extraction(
                                statement=task["sanitized_input"],
                                knowledge_base_id=task["knowledge_base_id"],
                            ),
                        }
                    )
                    candidates: list[dict] = []
                    for operation in draft.operations:
                        candidate = MemoryCandidate(
                            scope=operation.scope,
                            category=operation.category,
                            key=operation.key,
                            value=operation.value,
                            display_text=operation.display_text,
                            confidence=operation.confidence,
                            action=operation.action,
                        )
                        valid = validate_candidate(candidate)
                        if valid is None:
                            continue
                        prepared = {
                            "action": valid.action,
                            "scope": valid.scope,
                            "category": valid.category,
                            "key": valid.key,
                            "value": valid.value,
                            "display_text": valid.display_text,
                            "confidence": valid.confidence,
                        }
                        prepared.update(self._embedding_fields(f"{valid.key} {valid.value} {valid.display_text}"))
                        candidates.append(prepared)
                    self.repository.complete_memory_extraction_task(
                        task_id,
                        candidates=candidates,
                        expected_epoch=expected_epoch,
                        expected_attempt=int(task["attempt_count"]),
                    )
                except Exception as exc:
                    self.repository.fail_memory_extraction_task(
                        task_id,
                        error=type(exc).__name__,
                        expected_epoch=expected_epoch,
                        expected_attempt=int(task["attempt_count"]),
                    )
            finally:
                with self._queue_lock:
                    self._queued.discard(task_id)
                self._queue.task_done()


def memory_update_for_response(notice: MemoryUpdateNotice) -> dict:
    return notice.model_dump()
