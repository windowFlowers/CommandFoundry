"""Local-first command planning and safe rendering.

The command planner is intentionally independent of the cloud model.  It takes
only the current user message, a trusted retrieval result and (when resuming)
the server-side plan state.  A model may explain a result elsewhere in the
pipeline, but it cannot fill a slot or replace a rendered command.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from dataclasses import dataclass, replace
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from .evidence import evidence_records
from .models import (
    Answer,
    AnswerSegment,
    Citation,
    ClarificationInfo,
    ClarificationOption,
    CommandBlock,
    CommandPlanInfo,
    ContextInfo,
    GenerationInfo,
    PersonalizationInfo,
    RetrievalHit,
)
from .risk import review_commands
from .recipes import CommandRecipe


Shell = Literal["powershell", "cmd", "bash", "posix"]


_WINDOWS_DRIVE = re.compile(r"(?i)(?<![A-Za-z0-9_])([A-Za-z]:[\\/][^\r\n,，。；;！？!?]+)")
_WINDOWS_UNC = re.compile(r"(\\\\[^\r\n,，。；;！？!?]+)")
_PATH_SHELL_MARKER = re.compile(r"(?i)\s+(?:powershell|pwsh|cmd(?:\.exe)?|bash|zsh|posix)(?=\s|$|[,，。；;！？!?])")
_PATH_ACTION_MARKER = re.compile(r"(?i)\s+(?:创建|新建|激活|运行|执行|查看|切换|生成|打开|安装|启动|删除|回滚|虚拟环境|命令|create|activate|run|execute|view|switch|generate|open|install|start|delete|revert|environment|command)(?=\s|$|[,，。；;！？!?])")
_PATH_TRAILING_CONNECTOR = re.compile(r"(?i)\s+(?:下(?:用)?|中(?:用)?|里(?:用)?|用|using|with|in|under|on)\s*$")
_POSIX_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9_:/\.])(/[^\r\n,，。；;！？!?]+)")
_SAFE_ENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$|^\.[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A natural-language word such as ``safely`` must never become a commit
# argument merely because it is made of ASCII letters.  Accept immutable Git
# object ids, HEAD-relative refs, and explicit refs/heads or refs/tags names.
_SAFE_COMMIT = re.compile(
    r"^(?:[0-9a-f]{4,64}|HEAD(?:~[0-9]+|\^[0-9]+)?|refs/(?:heads|tags)/[A-Za-z0-9_.\-/]+)$",
    re.I,
)
# Docker image and container names are intentionally narrower than a generic
# identifier.  A value is only accepted when it is either preceded by an
# explicit marker (``镜像 nginx:latest``) or occurs in a clearly recognisable
# ``docker run`` command.  This prevents an arbitrary sentence from becoming
# an image name while still making pasted commands immediately actionable.
_SAFE_DOCKER_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,254}$")
_SAFE_DOCKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DOCKER_VALUE_OPTIONS = {
    "-e",
    "--env",
    "--env-file",
    "--expose",
    "--hostname",
    "--ip",
    "--label",
    "--log-opt",
    "--memory",
    "--mount",
    "--name",
    "--network",
    "--platform",
    "--publish",
    "-p",
    "--restart",
    "--tmpfs",
    "--user",
    "-u",
    "--volume",
    "-v",
    "--workdir",
    "-w",
    "--entrypoint",
    "--add-host",
    "--cap-add",
    "--device",
}
# Options without a following value.  Docker has many flags; this allow-list
# intentionally covers only the common run forms that the lightweight parser
# can identify confidently.  An unknown option is fail-closed below instead of
# letting its following token be mistaken for the image name.
_DOCKER_FLAG_OPTIONS = {
    "-d",
    "--detach",
    "--rm",
    "--init",
    "-i",
    "--interactive",
    "-t",
    "--tty",
    "-it",
    "--privileged",
    "--read-only",
    "-P",
    "--publish-all",
    "--no-healthcheck",
    "--quiet",
    "-q",
    "--disable-content-trust",
}
_CANCEL = re.compile(r"(?:^|\s)(?:取消|重新开始|换个问题|算了|不用了|cancel|start over|new question)(?:$|\s|[，。,.！!])", re.I)
_SKIP = re.compile(r"^(?:不知道|不清楚|不确定|随便|跳过|不提供|不给|没有|无|skip|unknown|n/?a|none)$", re.I)
_SENSITIVE_INPUT_PATTERNS = (
    re.compile(
        r"(?i)(?P<label>(?:password|passwd|token|api[_ -]?key|secret|private[_ -]?key|credential|authorization|"
        r"access[_ -]?token|refresh[_ -]?token|令牌|密钥|私钥|口令|密码|凭证))"
        r"\s*(?:[:：=]|是|为|叫)\s*(?P<value>[^\s，。！？!?；;、]+)"
    ),
    re.compile(r"(?i)(?P<prefix>bearer|basic)\s+(?P<value>[A-Za-z0-9._~+/=-]{8,})"),
    re.compile(r"(?i)(?P<scheme>(?:postgres(?:ql)?|mysql|redis|mongodb)://[^\s:/]+:)(?P<value>[^\s@]+)(?P<suffix>@)"),
    re.compile(r"(?i)\b(?:sk|gh[pousr])[-_][A-Za-z0-9_-]{8,}\b|\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_SENSITIVE_SLOT = re.compile(
    r"(?:password|passwd|token|secret|private[_ -]?key|api[_ -]?key|credential|auth|authorization|"
    r"令牌|密钥|私钥|口令|密码|凭证)",
    re.IGNORECASE,
)


def _clean_scalar(value: str) -> str:
    """Strip presentation quotes/punctuation without interpreting a command."""

    value = str(value or "").strip()
    value = value.strip("\"'` ")
    return value.rstrip("，。；;！？!?、,\n\r")


def contains_sensitive_input(text: str) -> bool:
    """Detect credential-bearing input without treating ordinary paths as secrets."""

    value = str(text or "")
    return any(pattern.search(value) for pattern in _SENSITIVE_INPUT_PATTERNS)


def redact_sensitive_input(text: str, *, whole: bool = False) -> str:
    """Remove credential values before persistence, retrieval, or model calls.

    ``whole`` is used when the planner is explicitly asking for a credential
    slot: a bare reply such as ``p@ss`` has no label to match, so the entire
    turn is replaced with a marker.  Labelled secrets in otherwise useful
    prose are replaced in-place and the surrounding intent is retained.
    """

    value = str(text or "")
    if whole and value.strip():
        return "[敏感参数已省略]"
    redacted = value
    for pattern in _SENSITIVE_INPUT_PATTERNS:
        if pattern.groups:
            def replace(match: re.Match[str]) -> str:
                groups = match.groupdict()
                if "label" in groups:
                    return f"{groups['label']}=[敏感参数已省略]"
                if "prefix" in groups:
                    return f"{groups['prefix']} [敏感参数已省略]"
                if "scheme" in groups:
                    return f"{groups['scheme']}[敏感参数已省略]{groups['suffix']}"
                return "[敏感参数已省略]"
            redacted = pattern.sub(replace, redacted)
        else:
            redacted = pattern.sub("[敏感参数已省略]", redacted)
    return redacted


def infer_platform(path_or_text: str) -> Literal["windows", "posix", "unknown"]:
    """Infer only from strong path/platform signals.

    A relative path is deliberately ``unknown``: it is not safe to infer which
    shell will consume it, especially on Windows where both PowerShell and cmd
    are common.
    """

    text = str(path_or_text or "").strip()
    if re.match(r"(?i)^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
        return "windows"
    if text.startswith("/"):
        return "posix"
    lowered = text.casefold()
    if re.search(r"\b(?:windows|win32|win11|win10|powershell|cmd(?:\.exe)?)\b|windows系统", lowered):
        return "windows"
    if re.search(r"\b(?:linux|macos|mac|unix|posix|ubuntu|debian|bash|zsh)\b", lowered):
        return "posix"
    return "unknown"


def detect_shell(text: str) -> Shell | None:
    """Return an explicitly named shell, never a guess from a drive letter."""

    value = str(text or "")
    if re.search(r"(?i)\b(?:powershell|pwsh|power shell|ps1)\b|PowerShell|Windows\s*PowerShell", value):
        return "powershell"
    if re.search(r"(?i)\b(?:cmd(?:\.exe)?|命令提示符|command prompt)\b", value):
        return "cmd"
    if re.search(r"(?i)\b(?:bash|zsh|sh|posix)\b", value):
        return "bash"
    return None


def _trim_path_context(candidate: str) -> str:
    """Remove an immediately-following task phrase from a path candidate.

    The path regex intentionally preserves spaces and CJK characters, which
    is important for real Windows paths but means an unpunctuated sentence
    such as ``F:\\work PowerShell 创建 .venv`` would otherwise be swallowed as
    one path.  Only trim a shell/action marker with a clear command-like
    suffix; directory names that merely contain ``PowerShell`` remain intact.
    """

    value = candidate
    shell_matches = list(_PATH_SHELL_MARKER.finditer(value))
    for match in reversed(shell_matches):
        suffix = value[match.end() :]
        # A path may legitimately end in a directory named ``PowerShell`` or
        # ``bash``.  Without a following task signal, preserve it and ask for
        # the shell separately instead of truncating the user's path.
        if _PATH_ACTION_MARKER.search(suffix) or ".venv" in suffix.casefold():
            value = value[: match.start()]
            break
    action = _PATH_ACTION_MARKER.search(value)
    if action:
        value = value[: action.start()]
    # Natural connectors commonly sit between a target directory and the
    # explicit shell/action (“目录下用 PowerShell”, “in bash”).
    value = _PATH_TRAILING_CONNECTOR.sub("", value)
    return value.strip()


def extract_path(text: str) -> str | None:
    """Extract a path token from natural language, preserving spaces."""

    value = str(text or "")
    matches = [
        match.group(1)
        for match in _WINDOWS_DRIVE.finditer(value)
        if not value[max(0, match.start() - 3) : match.start()].endswith("://")
    ]
    matches += [match.group(1) for match in _WINDOWS_UNC.finditer(value)]
    matches += [match.group(1) for match in _POSIX_ABSOLUTE.finditer(value)]
    if not matches:
        return None
    # A path in a sentence is normally the longest candidate.  Do not accept
    # command fragments after the path because the regex stops at prose marks.
    candidate = max(matches, key=len)
    candidate = _trim_path_context(_clean_scalar(candidate))
    if not candidate or "\x00" in candidate or "\n" in candidate or "\r" in candidate:
        return None
    if any(char in candidate for char in "<>|\"`"):
        return None
    return candidate


def _safe_path(value: str) -> str | None:
    value = _clean_scalar(value)
    if not value or len(value) > 1000 or "\x00" in value or "\n" in value or "\r" in value:
        return None
    if any(char in value for char in "<>|\"`"):
        return None
    return value


def _safe_relative_path(value: str, *, allow_bare: bool = False) -> str | None:
    """Accept a relative path only with a path-shaped signal.

    A free-form English sentence consists of the same ASCII characters as a
    relative path, so accepting every ``[A-Za-z ]+`` value would turn prompts
    such as ``Python create and activate virtualenv`` into a directory.  On an
    initial turn require a separator, dot/tilde prefix, or file extension;
    after the planner asks the path slot, a single bare token such as
    ``reports`` is unambiguous and may be accepted.
    """

    candidate = _clean_scalar(value)
    if not candidate or re.match(r"(?i)^[A-Za-z][A-Za-z0-9+.-]*://", candidate):
        return None
    if not re.fullmatch(r"[.A-Za-z0-9_~+@%:=/\\ -]{1,1000}", candidate):
        return None
    if any(char in candidate for char in "<>\"`\x00\r\n"):
        return None
    path_signal = (
        "/" in candidate
        or "\\" in candidate
        or candidate.startswith((".", "~"))
        or bool(re.search(r"(?i)\.[A-Za-z0-9]{1,12}$", candidate))
    )
    if path_signal or (allow_bare and not re.search(r"\s", candidate)):
        return _safe_path(candidate)
    return None


def _safe_env_name(value: str) -> str | None:
    value = _clean_scalar(value)
    if not value or len(value) > 64 or "/" in value or "\\" in value:
        return None
    if not _SAFE_ENV_NAME.fullmatch(value) or value in {".", ".."}:
        return None
    return value


def _safe_commit(value: str) -> str | None:
    value = _clean_scalar(value)
    if not value or len(value) > 160 or "\n" in value or "\r" in value:
        return None
    return value if _SAFE_COMMIT.fullmatch(value) else None


def _shellish_tokens(text: str) -> list[str]:
    """Tokenize the small shell fragment used by Docker command extraction.

    This is not a shell parser.  It only preserves simple quoted tokens and
    stops at sentence punctuation, so a prose sentence cannot be interpreted
    as a command merely because it contains the word ``docker``.
    """

    return [
        _clean_scalar(token)
        for token in re.findall(r'''"[^"\r\n]*"|'[^'\r\n]*'|[^\s，。；;！？!?]+''', str(text or ""))
        if _clean_scalar(token)
    ]


def _safe_docker_image(value: str) -> str | None:
    value = _clean_scalar(value).strip("'\"")
    if (
        not value
        or re.match(r"(?i)^[A-Za-z][A-Za-z0-9+.-]*://", value)
        or not _SAFE_DOCKER_IMAGE.fullmatch(value)
        or value.startswith((".", "/", "@"))
        or value.endswith(("/", ":", "@"))
    ):
        return None
    return value


def _safe_docker_name(value: str) -> str | None:
    value = _clean_scalar(value).strip("'\"")
    return value if _SAFE_DOCKER_NAME.fullmatch(value) else None


def _parse_docker_run(text: str) -> tuple[str | None, str | None]:
    """Extract an image and optional ``--name`` from an explicit docker run.

    Only a small allow-list of options which consume a following value is
    skipped.  Unknown options are treated as flags; if they are followed by a
    value Docker itself will reject the command rather than us silently
    guessing a different image.  The first positional token is the image.
    """

    match = re.search(r"(?i)\bdocker(?:\.exe)?\s+(?:(?:container)\s+)?run\b(?P<rest>[^\r\n]*)", str(text or ""))
    if not match:
        return None, None
    tokens = _shellish_tokens(match.group("rest"))
    image: str | None = None
    container_name: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            if index < len(tokens):
                image = _safe_docker_image(tokens[index])
            break
        if token.startswith("-"):
            option, equals, attached = token.partition("=")
            if option == "--name":
                candidate = attached if equals else (tokens[index + 1] if index + 1 < len(tokens) else "")
                container_name = _safe_docker_name(candidate)
                if not equals:
                    index += 1
            elif option in _DOCKER_VALUE_OPTIONS and not equals:
                # Consume the value of a known option (for example ``-p
                # 8080:80``) so it cannot be mistaken for the image.
                if index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
                    index += 1
            elif option not in _DOCKER_FLAG_OPTIONS:
                # Do not guess whether an unfamiliar option is a flag or
                # consumes a value.  In either case, selecting the next token
                # as an image could produce a different command than the user
                # wrote; the caller will fall back to the cited template.
                return None, None
            index += 1
            continue
        image = _safe_docker_image(token)
        break
    return image, container_name


def parse_slot_value(slot: str, text: str) -> str | None:
    """Parse and validate exactly one planner slot from the current turn."""

    value = str(text or "").strip()
    if not value or _SKIP.fullmatch(value):
        return None
    # A connection string or labelled token must never become a harmless
    # looking host/key/value slot.  The caller may still keep the redacted
    # turn for context, but it cannot be interpolated into a command plan.
    if contains_sensitive_input(value):
        return None
    if slot in {"path", "target_path", "environment_path"}:
        extracted = extract_path(value)
        if extracted:
            return _safe_path(extracted)
        # A follow-up may be a relative directory such as ``build/output``.
        # Do not interpret a full natural-language sentence as a path.
        # URI-looking values are never local filesystem paths, even though
        # their characters happen to fit the relative-path allow-list.
        if re.match(r"(?i)^[A-Za-z][A-Za-z0-9+.-]*://", value):
            return None
        return _safe_relative_path(value)
    if slot == "shell":
        shell = detect_shell(value)
        if shell:
            return shell
        lowered = value.casefold()
        if lowered in {"powershell", "pwsh", "ps1"}:
            return "powershell"
        if lowered in {"cmd", "cmd.exe"}:
            return "cmd"
        if lowered in {"bash", "zsh", "sh", "posix"}:
            return "bash"
        return None
    if slot in {"image", "docker_image"}:
        image, _ = _parse_docker_run(value)
        if image:
            return image
        # Marker forms are preferred for prose (``镜像 nginx:latest``).  The
        # value-before-marker form is also common in Chinese and remains safe
        # because it must match the strict Docker image grammar.
        marker = re.search(
            r"(?i)(?:镜像|image|镜像名)\s*(?:是|为|叫做?|name|is|:|：)?\s*([A-Za-z0-9][A-Za-z0-9._/@:-]{0,254})",
            value,
        )
        if marker:
            return _safe_docker_image(marker.group(1))
        reverse = re.search(
            r"(?i)([A-Za-z0-9][A-Za-z0-9._/@:-]{0,254})\s*(?:镜像|image)(?:$|[，。；;！？!?\s])",
            value,
        )
        if reverse:
            return _safe_docker_image(reverse.group(1))
        # Natural Chinese requests often omit the literal ``run`` command:
        # ``Docker 运行 nginx:latest`` or ``Docker 启动镜像 nginx``.  Require
        # an explicit Docker token and an action immediately before the image
        # so a general sentence cannot turn an arbitrary noun into an image.
        if re.search(r"(?i)\bdocker(?:\.exe)?\b", value):
            action = re.search(
                r"(?i)(?:运行|启动|run)\s*(?:镜像(?:名)?\s*)?([A-Za-z0-9][A-Za-z0-9._/@:-]{0,254})",
                value,
            )
            if action:
                return _safe_docker_image(action.group(1))
        return None
    if slot in {"container_name", "docker_name"}:
        _, container_name = _parse_docker_run(value)
        if container_name:
            return container_name
        marker = re.search(
            r"(?i)(?:容器名|容器名称|container[_ -]?name|name)\s*(?:是|为|叫做?|name|is|:|：)?\s*([A-Za-z0-9][A-Za-z0-9_.-]{0,127})",
            value,
        )
        return _safe_docker_name(marker.group(1)) if marker else None
    if slot in {"env_name", "environment_name"}:
        # The quick option is sent as the value itself.  Natural language can
        # use “名称为 …”, “叫 …” or “name …”.
        marker = re.search(
            r"(?i)(?:名称|名字|目录名|环境名|命名|叫|name|directory)\s*(?:是|为|叫做|叫)?\s*([.A-Za-z0-9_-]+)",
            value,
        )
        # ``.venv`` is the sole implicit shortcut exposed by the recipe.  It
        # is unambiguous and safe to recognize in an otherwise complete first
        # turn; arbitrary bare names still require the explicit environment
        # name marker or a later clarification reply.
        quick = re.search(r"(?<![A-Za-z0-9_.-])\.venv(?![A-Za-z0-9_.-])", value, re.IGNORECASE)
        return _safe_env_name(marker.group(1) if marker else quick.group(0) if quick else value)
    if slot == "commit":
        marker = re.search(r"(?i)(?:提交|commit|哈希|hash)\s*(?:是|为|叫)?\s*([A-Za-z0-9_.\-/~^]+)", value)
        if marker:
            return _safe_commit(marker.group(1))
        # A short object id or a HEAD-relative ref may be embedded in an
        # otherwise natural-language request (for example ``git revert
        # 0c01a9``).  Do not treat arbitrary prose as a commit value.
        candidate = re.search(
            r"(?i)(?<![A-Za-z0-9])(?:[0-9a-f]{4,64}|HEAD(?:~[0-9]+|\^[0-9]+)?|refs/(?:heads|tags)/[A-Za-z0-9_.\-/]+)(?![A-Za-z0-9])",
            value,
        )
        return _safe_commit(candidate.group(0)) if candidate else None
    if slot in {"port", "port_number", "seconds"}:
        candidates = list(re.finditer(r"(?<!\d)(\d{1,5})(?!\d)", value))
        if not candidates:
            return None
        if slot in {"port", "port_number"}:
            # A platform version such as ``Windows 11`` is not a port.  On an
            # initial natural-language turn, accept a number only when it is
            # immediately adjacent to an explicit port marker (``端口 8080``
            # or ``8080 端口``).  A later one-slot reply such as ``8080`` is
            # intentionally accepted as a bare value.
            markers = list(re.finditer(r"(?i)(?:localport|port|端口(?:号)?)", value))
            if markers:
                related: list[re.Match[str]] = []
                for candidate in candidates:
                    before_marker = any(marker.start() >= candidate.end() and re.fullmatch(r"\s*", value[candidate.end() : marker.start()]) for marker in markers)
                    after_marker = any(marker.end() <= candidate.start() for marker in markers)
                    if before_marker or after_marker:
                        related.append(candidate)
                if not related:
                    return None
                candidate = related[-1]
                # ``Windows 11 端口`` is still a version-only phrase when the
                # number occurs before the marker.  Require a real port value
                # after the marker in that case.
                if not any(marker.end() <= candidate.start() for marker in markers):
                    prefix = value[: candidate.end()]
                    if re.search(r"(?i)\b(?:windows|win(?:dows)?|server)\s+(?:server\s+)?\d{1,4}\s*$", prefix):
                        return None
            elif len(candidates) == 1 and re.fullmatch(r"\s*\d{1,5}\s*(?:/(?:tcp|udp))?\s*", value, re.IGNORECASE):
                candidate = candidates[0]
            else:
                return None
        else:
            # Positive-integer recipe validators do the stricter unit-aware
            # check in ``_parse_recipe_slot``; this keeps this generic helper
            # compatible with a bare numeric follow-up.
            candidate = candidates[-1]
        port = int(candidate.group(1))
        return str(port) if 1 <= port <= 65535 else None
    return _clean_scalar(value)[:1000] or None


def _parse_recipe_slot(spec: SlotSpec, text: str, *, allow_bare: bool = False) -> str | None:
    """Conservatively parse a catalog slot without asking an LLM to guess."""

    value = str(text or "").strip()
    if contains_sensitive_input(value):
        return None

    # Credentials are intentionally never accepted as planner values.  The
    # user may decline or provide one, but the resulting answer must remain a
    # source template that asks the target CLI to prompt securely.
    if spec.secret or spec.slot_type == "credential" or _SENSITIVE_SLOT.search(spec.name):
        return None
    if spec.name in {
        "target_path",
        "environment_name",
        "env_name",
        "path",
        "port",
        "commit",
        "image",
        "docker_image",
        "container_name",
        "docker_name",
    }:
        parsed = parse_slot_value(spec.name, text)
        if parsed is not None:
            return parsed
        # A clarification reply is allowed to be the bare, type-validated
        # value itself (for example ``nginx:latest`` or ``web``).  The first
        # turn remains marker/command-only so ordinary prose cannot be
        # mistaken for a Docker parameter.
        if allow_bare and spec.name in {"image", "docker_image"}:
            return _safe_docker_image(value)
        if allow_bare and spec.name in {"container_name", "docker_name"}:
            return _safe_docker_name(value)
        if allow_bare and spec.name in {"path", "target_path", "environment_path"}:
            bare_path = _safe_relative_path(value, allow_bare=True)
            if bare_path is not None:
                return bare_path
        if spec.name in {"path", "target_path", "environment_path"}:
            marker = re.search(
                r"(?i)(?:目标目录|目标路径|文件路径|路径|目录|path|directory|folder)\s*(?:是|为|叫做?|name|is|:|：)?\s*(.+)",
                value,
            )
            if marker:
                candidate = re.split(r"[，。；;！？!?]", marker.group(1), maxsplit=1)[0].strip()
                extracted = extract_path(candidate)
                if extracted:
                    return _safe_path(extracted)
                # The marker itself is the explicit path signal, so a simple
                # relative directory such as ``目标目录 reports`` is safe to
                # accept even on the initial turn.
                return _safe_relative_path(candidate, allow_bare=True)
        return None
    if _SKIP.fullmatch(value):
        return None
    if spec.input_type == "number":
        return parse_slot_value("port", value)
    if spec.input_type == "path":
        return parse_slot_value("path", value)
    if spec.input_type == "select":
        # Preserve the canonical value declared by the recipe.  In
        # particular, catalog recipes use ``posix`` while the generic parser
        # normalizes Bash/Zsh aliases to ``bash``.
        option_value = next(
            (candidate[1] for candidate in spec.options if candidate[0].casefold() == value.casefold() or candidate[1].casefold() == value.casefold()),
            None,
        )
        parsed_shell = parse_slot_value("shell", value)
        # A catalog recipe uses ``posix`` as its canonical template key while
        # users naturally answer “Bash” or “Zsh”.
        if parsed_shell == "bash" and any(candidate[1] == "posix" for candidate in spec.options):
            parsed_shell = "posix"
        return option_value or parsed_shell
    # ``validation`` is data in the recipe bundle, but it still needs to be
    # enforced before interpolation.  Keep this deliberately small and
    # conservative: unknown validators remain template-only rather than
    # becoming an implicit trust boundary.
    if spec.validation == "positive_integer":
        number = re.fullmatch(r"\s*([1-9]\d{0,8})\s*(?:秒|seconds?|s)?\s*", value, re.IGNORECASE)
        if number:
            return number.group(1)
        marker_number = re.search(
            r"(?i)(?:seconds?|过期秒数|过期时间|秒数)\s*(?:是|为|叫做?|is|:|：)?\s*([1-9]\d{0,8})",
            value,
        )
        return marker_number.group(1) if marker_number else None

    # A reply to a one-slot question is allowed to be a simple value.  On the
    # initial turn, only accept it when the query names the slot; this avoids
    # interpreting arbitrary prose as a Docker image/database identifier.
    marker_aliases = {
        "host": ("host", "hostname", "主机", "主机名", "服务器", "地址", "ip"),
        "user": ("user", "username", "用户", "用户名", "账号", "账户"),
        "database": ("database", "db", "数据库", "数据库名", "库"),
        "image": ("image", "镜像", "镜像名"),
        "container_name": ("container_name", "container name", "name", "容器名", "容器名称"),
        "seconds": ("seconds", "second", "过期秒数", "过期时间", "秒数"),
        "key": ("key", "redis key", "键", "键名"),
    }
    aliases = marker_aliases.get(spec.name, (spec.name, "参数", "值", "名称", "名字"))
    marker = "|".join(re.escape(alias) for alias in aliases)
    match = re.search(
        rf"(?i)(?:{marker})\s*(?:是|为|叫做?|name|is|:|：)?\s*([A-Za-z0-9_.:/@+\-]+)",
        value,
    )
    if match:
        return match.group(1)
    if not allow_bare:
        return None
    bare = _clean_scalar(value)
    # Follow-up replies may be a single image/key/value token.  Do not accept
    # an entire natural-language sentence as an identifier.
    if spec.input_type == "text" and spec.slot_type == "value":
        if not bare or len(bare) > 1000 or any(char in bare for char in "\x00\r\n<>`;&|^%"):
            return None
        return bare
    return bare if re.fullmatch(r"[A-Za-z0-9_.:/@+\-]+", bare) else None


def source_revision(hits: list[RetrievalHit]) -> str:
    """Produce a stable evidence revision used to invalidate stale plans."""

    values: list[str] = []
    for hit in hits:
        source = hit.topic.source
        values.append(
            ":".join(
                str(item or "")
                for item in (source.source_id, source.revision, source.index_revision, source.sha256)
            )
        )
    return "|".join(sorted(dict.fromkeys(values)))


def _quote_powershell(value: str) -> str:
    # Single-quoted PowerShell strings are literal; two apostrophes encode one.
    return "'" + value.replace("'", "''") + "'"


def _quote_posix(value: str) -> str:
    return shlex.quote(value)


def _quote_cmd(value: str) -> str | None:
    # cmd.exe's metacharacters are not made safe by ordinary double quotes.
    # Refuse them and let the caller show one source template instead.
    if not value or any(char in value for char in "&|<>^%!()\"\r\n"):
        return None
    return f'"{value}"' if any(char.isspace() for char in value) else value


def render_virtualenv(path: str, env_name: str, shell: Shell) -> CommandBlock | None:
    """Render the sourced Python venv recipe for one explicit shell."""

    base = _safe_path(path)
    name = _safe_env_name(env_name)
    if not base or not name:
        return None
    platform = infer_platform(base)
    if shell in {"powershell", "cmd"}:
        if platform == "posix":
            return None
        env_path = ntpath.join(base, name)
        q = _quote_cmd if shell == "cmd" else _quote_powershell
        if shell == "cmd":
            quoted_env = q(env_path)
            if quoted_env is None:
                return None
            code = "\n".join(
                [
                    f"python -m venv {quoted_env}",
                    f"call {q(ntpath.join(env_path, 'Scripts', 'activate.bat'))}",
                ]
            )
            return CommandBlock(
                label="创建并激活 Python 虚拟环境（CMD）",
                language="text",
                shell="cmd",
                code=code,
                platforms=["Windows"],
                prerequisites=["已安装 Python"],
            )
        code = "\n".join(
            [
                f"python -m venv {_quote_powershell(env_path)}",
                f"& {_quote_powershell(ntpath.join(env_path, 'Scripts', 'Activate.ps1'))}",
            ]
        )
        return CommandBlock(
            label="创建并激活 Python 虚拟环境（PowerShell）",
            language="powershell",
            shell="powershell",
            code=code,
            platforms=["Windows"],
            prerequisites=["已安装 Python，且 PowerShell 允许运行本地脚本"],
        )
    if shell not in {"bash", "posix"}:
        return None
    if platform == "windows":
        return None
    env_path = posixpath.join(base, name)
    code = "\n".join(
        [
            f"python3 -m venv {_quote_posix(env_path)}",
            f"source {_quote_posix(posixpath.join(env_path, 'bin', 'activate'))}",
        ]
    )
    return CommandBlock(
        label="创建并激活 Python 虚拟环境（POSIX Shell）",
        language="bash",
        shell="posix" if shell == "posix" else "bash",
        code=code,
        platforms=["macOS", "Linux"],
        prerequisites=["已安装 Python 3"],
    )


@dataclass(frozen=True)
class SlotSpec:
    name: str
    input_type: str
    question: str
    options: tuple[tuple[str, str], ...] = ()
    max_attempts: int = 1
    slot_type: str = "value"
    secret: bool = False
    validation: str = ""
    # ``required`` describes whether a recipe cannot render without this
    # value.  Optional slots are still parsed when explicitly supplied, but a
    # missing optional value must not create a needless clarification turn.
    required: bool = True
    prompt_if_absent: bool = False
    default: str | None = None


@dataclass(frozen=True)
class Recipe:
    template_id: str
    topic_ids: tuple[str, ...]
    slots: tuple[SlotSpec, ...]
    catalog_recipe: CommandRecipe | None = None


VIRTUALENV_RECIPE = Recipe(
    template_id="python.virtualenv.create_activate",
    topic_ids=("python.virtualenv",),
    slots=(
        SlotSpec("target_path", "path", "请提供 Python 虚拟环境所在的目标目录路径。", max_attempts=1),
        SlotSpec(
            "shell",
            "select",
            "你准备在哪个 Shell 中运行？",
            (("PowerShell", "powershell"), ("CMD", "cmd"), ("Bash / POSIX Shell", "posix")),
            max_attempts=1,
        ),
        SlotSpec(
            "environment_name",
            "text",
            "虚拟环境目录名是什么？可以直接选择 .venv。",
            ((".venv（推荐）", ".venv"),),
            max_attempts=1,
        ),
    ),
)

GIT_REVERT_RECIPE = Recipe(
    template_id="git.git-revert.specific_commit",
    topic_ids=("git.git-revert",),
    slots=(SlotSpec("commit", "text", "要回滚的提交哈希或引用是什么？", max_attempts=1),),
)


class CommandPlanner:
    """Resolve one command recipe through deterministic, resumable slots."""

    def __init__(self, catalog: Any | None = None, repository: Any | None = None) -> None:
        self.catalog = catalog
        self.repository = repository

    @staticmethod
    def is_cancel_request(query: str) -> bool:
        return bool(_CANCEL.search(str(query or "").strip()))

    @staticmethod
    def is_template_request(query: str) -> bool:
        return bool(
            re.search(r"(?i)(?:模板|范例|示例|占位符|placeholder|template|example)", str(query or ""))
        )

    @staticmethod
    def pending_slot_is_sensitive(plan: dict | None) -> bool:
        if not plan or plan.get("status") != "pending":
            return False
        slot_name = str(plan.get("next_slot") or "")
        slots = plan.get("slots") or {}
        spec = slots.get(slot_name) if isinstance(slots, dict) else None
        return bool(
            _SENSITIVE_SLOT.search(slot_name)
            or (isinstance(spec, dict) and (spec.get("secret") or spec.get("slot_type") == "credential"))
        )

    @staticmethod
    def _ui_input_type(slot_type: str) -> str:
        return {
            "path": "path",
            "port": "number",
            "choice": "select",
            "identifier": "text",
            "value": "text",
            "credential": "text",
        }.get(slot_type, "text")

    def _catalog_recipe(self, recipe: CommandRecipe) -> Recipe:
        specs: list[SlotSpec] = []
        for raw in recipe.required_slots:
            specs.append(
                SlotSpec(
                    raw.name,
                    self._ui_input_type(raw.type),
                    raw.question or f"请提供{raw.label}。",
                    tuple((item.get("label", item.get("value", "")), item.get("value", "")) for item in raw.options),
                    slot_type=raw.type,
                    secret=raw.secret,
                    validation=raw.validation,
                    required=raw.required,
                    prompt_if_absent=raw.prompt_if_absent,
                    default=raw.default,
                )
            )
        for raw in recipe.optional_slots:
            # Keep optional slots in the plan even when they are not prompted
            # by default.  This lets an explicit ``--name``/``容器名`` fact be
            # captured and rendered while still allowing the recipe's safe
            # no-name variant when the user omits it.
            specs.append(
                SlotSpec(
                    raw.name,
                    self._ui_input_type(raw.type),
                    raw.question or f"请提供{raw.label}。",
                    tuple((item.get("label", item.get("value", "")), item.get("value", "")) for item in raw.options),
                    slot_type=raw.type,
                    secret=raw.secret,
                    validation=raw.validation,
                    required=raw.required,
                    prompt_if_absent=raw.prompt_if_absent,
                    default=raw.default,
                )
            )
        # Keep the established venv interaction order (target directory,
        # shell, environment name) even though the data bundle groups its
        # optional fields by declaration rather than UX sequence.
        if recipe.recipe_id == VIRTUALENV_RECIPE.template_id:
            order = {"target_path": 0, "shell": 1, "environment_name": 2}
            specs.sort(key=lambda item: order.get(item.name, len(order)))
        # A shell is a required execution decision whenever a recipe supports
        # more than one shell and did not model it explicitly.
        if "shell" not in {spec.name for spec in specs} and len(recipe.shells) > 1:
            shell_labels = {
                "powershell": "PowerShell",
                "cmd": "CMD",
                "posix": "Bash / POSIX Shell",
                "bash": "Bash / POSIX Shell",
            }
            shell_options = tuple((shell_labels.get(shell, shell), shell) for shell in recipe.shells)
            specs.append(
                SlotSpec(
                    "shell",
                    "select",
                    "你准备在哪个 Shell 中运行？",
                    shell_options,
                    required=True,
                    prompt_if_absent=True,
                )
            )
        return Recipe(
            template_id=recipe.recipe_id,
            topic_ids=(recipe.topic_id,),
            slots=tuple(specs),
            catalog_recipe=recipe,
        )

    @staticmethod
    def _catalog_intent(query: str, topic_id: str) -> bool:
        """Require an intent signal in the current turn before starting a plan.

        Contextual retrieval may deliberately surface the previous command for
        a follow-up such as “这个命令安全吗？”.  That evidence should answer
        the follow-up, not reopen a new slot sequence.  Keep this allow-list
        narrow and topic-specific; explicit facts such as an image, port or
        database name still match the corresponding action signal.
        """

        signals = {
            "docker.docker-run": r"(?:docker(?:\.exe)?\s+(?:container\s+)?run)|(?:docker|容器|镜像).{0,24}(?:运行|启动|镜像|容器|run)|(?:运行|启动).{0,24}(?:docker|容器|镜像)",
            "windows.netstat": r"端口|port|netstat|get-nettcpconnection|占用",
            "sql.mysql-connect": r"mysql.{0,24}(?:连接|登录|数据库|主机|用户|connect)|(?:连接|登录).{0,24}mysql|连接.{0,20}数据库",
            "redis.string-set": r"redis.{0,24}(?:键|值|写入|设置|过期|set|string)|(?:写入|设置|过期).{0,24}redis",
            "windows.new-item": r"new[- ]?item|文件|创建|新建",
        }
        pattern = signals.get(topic_id)
        return bool(pattern and re.search(pattern, str(query or ""), re.IGNORECASE))

    def _recipe_for(self, query: str, hits: list[RetrievalHit], plan: dict | None = None) -> Recipe | None:
        topic_ids = {hit.topic.id for hit in hits}
        if plan:
            template_id = str(plan.get("template_id") or "")
            # Prefer the versioned catalog recipe so its source revision is
            # part of the persisted plan.  The hard-coded recipes remain a
            # compatibility fallback for pre-2.5 installations whose bundle
            # has no recipes.json yet.
            if self.catalog is not None:
                for candidate in getattr(self.catalog, "recipes", []):
                    if candidate.recipe_id == template_id:
                        return self._catalog_recipe(candidate)
            if template_id in {VIRTUALENV_RECIPE.template_id, "python.virtualenv.direct"}:
                return VIRTUALENV_RECIPE
            if template_id in {GIT_REVERT_RECIPE.template_id, "git.revert.direct"}:
                return GIT_REVERT_RECIPE
        lowered = str(query or "").casefold()
        # Do not let a contextual hit alone reopen a completed plan.  The
        # current turn must itself mention the virtual-environment task.
        if re.search(r"(?:虚拟环境|virtualenv|python\s*(?:-m\s*)?venv|venv)", lowered):
            if self.catalog is not None:
                candidate = next(
                    (item for item in getattr(self.catalog, "recipes", []) if item.recipe_id == VIRTUALENV_RECIPE.template_id),
                    None,
                )
                if candidate is not None:
                    return self._catalog_recipe(candidate)
            return VIRTUALENV_RECIPE
        if "git.git-revert" in topic_ids and re.search(r"(?:回滚|撤销|revert)", lowered):
            # Keep the old generic Git answer for an underspecified request;
            # once a commit is explicit this recipe can render one direct command.
            if parse_slot_value("commit", query):
                if self.catalog is not None:
                    candidate = next(
                        (item for item in getattr(self.catalog, "recipes", []) if item.recipe_id == GIT_REVERT_RECIPE.template_id),
                        None,
                    )
                    if candidate is not None:
                        return self._catalog_recipe(candidate)
                return GIT_REVERT_RECIPE
        # All other curated recipes participate in the same planner.  A recipe
        # may still decline to render later if its type-specific values are not
        # safely available; that is the intentional template fallback.
        if self.catalog is not None:
            lexical_topic = None
            if re.search(r"(?i)new[- ]?item|(?:创建|新建|生成).{0,16}(?:文件|file)|(?:文件|file).{0,16}(?:创建|新建)", lowered):
                lexical_topic = "windows.new-item"
            elif re.search(
                r"(?i)\bdocker(?:\.exe)?\s+(?:(?:container)\s+)?run\b|docker.{0,20}(?:运行|启动).{0,20}(?:镜像|容器)|(?:运行|启动).{0,20}docker",
                lowered,
            ):
                lexical_topic = "docker.docker-run"
            for candidate in getattr(self.catalog, "recipes", []):
                # The Git catalogue contains a zero-slot "latest" recipe and
                # a specific-commit recipe.  Without an explicit commit or
                # shell request, preserve the legacy RAG answer (which already
                # gives the safe ``git revert HEAD`` command) instead of
                # opening a pointless shell clarification.
                if candidate.topic_id == "git.git-revert":
                    continue
                if (
                    candidate.topic_id in topic_ids
                    and (lexical_topic is None or candidate.topic_id == lexical_topic)
                    and self._catalog_intent(query, candidate.topic_id)
                ):
                    return self._catalog_recipe(candidate)
            # Some vendored pages retain an English command name while users
            # ask in Chinese (for example “Windows 创建文件” rather than
            # “new-item”).  A narrow lexical hint is safe here because the
            # topic is still resolved from the local catalog below; it merely
            # prevents a low-scoring unrelated hit from selecting another
            # recipe.
            if lexical_topic:
                candidate = next(
                    (item for item in getattr(self.catalog, "recipes", []) if item.topic_id == lexical_topic),
                    None,
                )
                if candidate is not None:
                    return self._catalog_recipe(candidate)
        return None

    def _topic_hit(self, recipe: Recipe, hits: list[RetrievalHit], plan: dict | None) -> RetrievalHit | None:
        for hit in hits:
            if hit.topic.id in recipe.topic_ids or str(hit.topic.id).removeprefix("parent.") in recipe.topic_ids:
                return hit
        topic_id = None
        if plan:
            slots = plan.get("slots") or {}
            meta = slots.get("__meta__") if isinstance(slots, dict) else None
            topic_id = meta.get("topic_id") if isinstance(meta, dict) else None
        if topic_id and self.catalog is not None:
            for topic in getattr(self.catalog, "topics", []):
                if topic.id == topic_id:
                    return RetrievalHit(topic=topic, score=0.0)
        if recipe.catalog_recipe is not None and self.catalog is not None:
            topic_ids = set(recipe.topic_ids)
            for topic in getattr(self.catalog, "topics", []):
                if topic.id in topic_ids:
                    return RetrievalHit(topic=topic, score=0.0)
        return None

    @staticmethod
    def _revision_for_hit(hit: RetrievalHit | None, hits: list[RetrievalHit]) -> str:
        return source_revision([hit]) if hit is not None else source_revision(hits)

    @staticmethod
    def _revision_for_recipe(recipe: Recipe, hit: RetrievalHit | None, hits: list[RetrievalHit]) -> str:
        evidence_revision = CommandPlanner._revision_for_hit(hit, hits)
        recipe_revision = recipe.catalog_recipe.source_revision if recipe.catalog_recipe else ""
        return "|".join(value for value in (evidence_revision, recipe_revision) if value)

    @staticmethod
    def _spec_payload(recipe: Recipe, topic_id: str | None) -> dict:
        payload: dict[str, Any] = {
            "__meta__": {"topic_id": topic_id or ""},
        }
        for spec in recipe.slots:
            payload[spec.name] = {
                "input_type": spec.input_type,
                "slot_type": spec.slot_type,
                "secret": spec.secret,
                "validation": spec.validation,
                "question": spec.question,
                "options": [{"label": label, "value": value} for label, value in spec.options],
                "required": spec.required,
                "prompt_if_absent": spec.prompt_if_absent,
                "default": spec.default,
                "attempt": 1,
                "max_attempts": spec.max_attempts,
            }
        return payload

    @staticmethod
    def _slot_needs_value(spec: SlotSpec, values: dict[str, str]) -> bool:
        """Return whether this slot must be resolved before rendering.

        Required slots and explicitly prompted optional slots are interactive.
        An optional slot with a recipe-declared default is resolved by that
        default at plan creation; an optional slot without either is a true
        omission (for example Docker's auto-generated container name).
        """

        if spec.name in values:
            return False
        return bool(spec.required or spec.prompt_if_absent)

    @classmethod
    def _next_slot(cls, recipe: Recipe, values: dict[str, str]) -> str | None:
        return next(
            (spec.name for spec in recipe.slots if cls._slot_needs_value(spec, values)),
            None,
        )

    @staticmethod
    def _plan_values(plan: dict | None) -> dict[str, str]:
        values = (plan or {}).get("values") or {}
        return {str(k): str(v) for k, v in values.items() if str(k) != "__meta__"}

    def _citation_payload(self, hits: list[RetrievalHit], hit: RetrievalHit | None) -> tuple[list[Citation], list[str]]:
        chosen = [hit] if hit is not None else hits
        citations: list[Citation] = []
        ids: list[str] = []
        for record in evidence_records(chosen):
            source = record["hit"].topic.source
            citation_id = record["citation_id"]
            ids.append(citation_id)
            citations.append(
                Citation(
                    source_id=source.source_id,
                    title=source.title,
                    source_url=source.source_url,
                    license=source.license,
                    revision=source.revision,
                    excerpt=str(record["text"])[:360],
                    score=record.get("score", record["hit"].score),
                    domain=record["hit"].topic.domain,
                    document_id=source.document_id,
                    knowledge_base_id=source.knowledge_base_id,
                    source_kind=source.kind,
                    citation_id=citation_id,
                    chunk_id=record.get("chunk_id"),
                    parent_id=record.get("parent_id"),
                    document_sha256=source.sha256,
                    index_revision=source.index_revision,
                    locator=record.get("locator"),
                )
            )
        return citations, list(dict.fromkeys(ids))

    def _answer_base(
        self,
        *,
        kind: Literal["direct", "clarification", "template"],
        summary: str,
        commands: list[CommandBlock],
        citations: list[Citation],
        plan_id: str,
        recipe: Recipe,
        revision: str,
        values: dict[str, str],
        unresolved: list[str],
        clarification: ClarificationInfo | None = None,
        context: ContextInfo | None = None,
        personalization: PersonalizationInfo | None = None,
        notes: list[str] | None = None,
    ) -> Answer:
        citation_ids = [item.citation_id for item in citations if item.citation_id]
        segments = [AnswerSegment(text=summary, citation_ids=citation_ids[:1])] if citation_ids else []
        info = CommandPlanInfo(
            plan_id=plan_id,
            template_id=recipe.template_id,
            source_revision=revision,
            citation_ids=citation_ids,
            collected_slots={key: value for key, value in values.items() if key not in {"credential", "token", "password"}},
            unresolved_slots=unresolved,
        )
        return Answer(
            mode="local_fallback",
            answer_kind=kind,
            summary=summary,
            commands=commands,
            notes=notes or ["命令仅供复制，不会自动执行。"],
            citations=citations,
            generation=GenerationInfo(provider="local"),
            context=context or ContextInfo(),
            personalization=personalization or PersonalizationInfo(),
            summary_segments=segments,
            clarification=clarification,
            command_plan=info,
        )

    def _clarification(
        self,
        *,
        plan: dict,
        recipe: Recipe,
        spec: SlotSpec,
        revision: str,
        citations: list[Citation],
        values: dict[str, str],
        context: ContextInfo | None,
        personalization: PersonalizationInfo | None,
    ) -> Answer:
        # Strong path signals narrow the shell choices without silently
        # selecting one.  This keeps a Windows path from offering a POSIX
        # command and keeps the final renderer deterministic.
        shell_spec = spec
        if spec.name == "shell":
            path = values.get("target_path") or values.get("path") or ""
            if not path:
                for candidate in recipe.slots:
                    if candidate.input_type == "path" or candidate.slot_type == "path":
                        path = values.get(candidate.name, "")
                        if path:
                            break
            platform = infer_platform(path)
            allowed = {
                "windows": {"powershell", "cmd"},
                "posix": {"bash", "posix"},
            }.get(platform)
            if allowed:
                filtered = tuple(item for item in spec.options if item[1] in allowed)
                if filtered:
                    shell_spec = replace(spec, options=filtered)
        options = [ClarificationOption(label=label, value=value) for label, value in shell_spec.options]
        clarification = ClarificationInfo(
            plan_id=str(plan["id"]),
            slot=shell_spec.name,
            question=shell_spec.question,
            input_type=shell_spec.input_type,  # type: ignore[arg-type]
            options=options,
            attempt=int((plan.get("slots") or {}).get(shell_spec.name, {}).get("attempt", 1)),
            max_attempts=shell_spec.max_attempts,
            total_slots=sum(1 for item in recipe.slots if item.required or item.prompt_if_absent),
        )
        summary = shell_spec.question
        return self._answer_base(
            kind="clarification",
            summary=summary,
            commands=[],
            citations=citations,
            plan_id=str(plan["id"]),
            recipe=recipe,
            revision=revision,
            values=values,
            unresolved=[item.name for item in recipe.slots if self._slot_needs_value(item, values)],
            clarification=clarification,
            context=context,
            personalization=personalization,
            notes=["为了生成可直接复制的命令，请补充这一项信息。", "命令仅供复制，不会自动执行。"],
        )

    def _template(
        self,
        *,
        plan: dict,
        recipe: Recipe,
        hit: RetrievalHit | None,
        hits: list[RetrievalHit],
        revision: str,
        context: ContextInfo | None,
        personalization: PersonalizationInfo | None,
    ) -> Answer:
        citations, citation_ids = self._citation_payload(hits, hit)
        topic = hit.topic if hit else None
        command: CommandBlock | None = None
        if topic and topic.commands:
            canonical = next((item for item in topic.commands if "<" in item.code), topic.commands[0])
            command = canonical.model_copy(update={"citation_ids": citation_ids[:1]})
        if command is None:
            command = CommandBlock(
                label="模板示例",
                language="bash",
                shell="bash",
                code="python -m venv <path/to/venv>",
                platforms=["Windows", "macOS", "Linux"],
                prerequisites=["已安装 Python"],
                citation_ids=citation_ids[:1],
            )
        summary = "以下是一个模板示例；请将占位参数替换为实际值后再复制。"
        return self._answer_base(
            kind="template",
            summary=summary,
            commands=[command],
            citations=citations,
            plan_id=str(plan["id"]),
            recipe=recipe,
            revision=revision,
            values=self._plan_values(plan),
            unresolved=[
                item.name
                for item in recipe.slots
                if self._slot_needs_value(item, self._plan_values(plan))
            ],
            context=context,
            personalization=personalization,
            notes=["模板示例 · 需要替换参数。", "命令仅供复制，不会自动执行。"],
        )

    @staticmethod
    def _derived_values(recipe: CommandRecipe, values: dict[str, str]) -> dict[str, str]:
        result = dict(values)
        for derived in recipe.derived_slots:
            expression = derived.expression.strip()
            if expression == "join_path(target_path, environment_name)":
                base = result.get("target_path")
                name = result.get("environment_name")
                if base and name:
                    platform = infer_platform(base)
                    result[derived.name] = (
                        ntpath.join(base, name) if platform == "windows" else posixpath.join(base, name)
                    )
        return result

    @staticmethod
    def _template_value(value: str, *, slot_type: str, shell: str, quoted: str | None) -> str | None:
        if slot_type == "credential" or "\x00" in value or "\r" in value or "\n" in value:
            return None
        # Double quotes do not neutralize cmd.exe metacharacters (and ``!``
        # is expanded when delayed expansion is enabled).  Apply the same
        # fail-closed policy to values inside recipe-owned quotes as to
        # unquoted identifier/path values.
        if shell == "cmd" and any(char in value for char in "&|<>^%!()\"\r\n"):
            return None
        if any(char in value for char in "<>`;&|^%"):
            return None
        if quoted == "'":
            if shell == "powershell":
                return value.replace("'", "''")
            return value.replace("'", "'\\''")
        if quoted == '"':
            if '"' in value:
                return None
            return value
        if slot_type in {"identifier", "path"}:
            if shell == "powershell":
                return _quote_powershell(value)
            if shell == "posix":
                return _quote_posix(value)
            return _quote_cmd(value)
        return value

    def _render_catalog_recipe(self, recipe: Recipe, values: dict[str, str]) -> CommandBlock | None:
        source_recipe = recipe.catalog_recipe
        if source_recipe is None:
            return None
        shell = values.get("shell") or (source_recipe.shells[0] if len(source_recipe.shells) == 1 else "")
        # ``bash`` is the user-facing alias accepted by the generic parser;
        # recipe bundles use the canonical ``posix`` key for POSIX variants.
        # Normalise both newly collected and legacy persisted plan values
        # before looking up templates.
        if shell == "bash" and "bash" not in source_recipe.shells and "posix" in source_recipe.shells:
            shell = "posix"
        if shell not in source_recipe.shells:
            return None
        all_values = self._derived_values(source_recipe, values)
        slot_types = {slot.name: slot.type for slot in source_recipe.slots}
        slot_types.update({slot.name: slot.type for slot in source_recipe.derived_slots})
        optional_names = {slot.name for slot in source_recipe.optional_slots}
        rendered_steps: list[tuple[str, str, str]] = []
        for step in source_recipe.steps:
            template = (step.templates or {}).get(shell)
            if not template:
                return None

            # Optional command fragments are represented in the data bundle
            # with a normal placeholder so the source remains readable.  The
            # Docker recipe's ``--name`` flag is the one currently supported
            # omission: Docker itself generates a name when it is absent.  Do
            # not generalise this into silently deleting arbitrary arguments;
            # unknown optional placeholders remain template-only.
            for optional_name in optional_names:
                if optional_name in all_values:
                    continue
                marker = rf"\{{\{{\s*{re.escape(optional_name)}\s*\}}\}}"
                if optional_name == "container_name":
                    template, removed = re.subn(
                        rf"(?:--name(?:=|\s+))?(?:['\"])?{marker}(?:['\"])?\s*",
                        "",
                        template,
                    )
                    if removed == 0 and re.search(marker, template):
                        return None
                elif re.search(marker, template):
                    return None

            def replace(match: re.Match[str]) -> str:
                name = match.group(1)
                value = all_values.get(name)
                if value is None:
                    raise ValueError(name)
                # A recipe template owns its surrounding quotes.  Escape only
                # the interior in that case; otherwise type-specific quoting
                # keeps unquoted identifiers/path values safe.
                start = max(0, match.start() - 1)
                end = min(len(template), match.end() + 1)
                before = template[start : match.start()]
                after = template[match.end() : end]
                quote = before if before in {"'", '"'} and after in {"'", '"'} and before == after else None
                rendered = self._template_value(
                    str(value), slot_type=slot_types.get(name, "value"), shell=shell, quoted=quote
                )
                if rendered is None:
                    raise ValueError(name)
                return rendered

            try:
                rendered = re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", replace, template)
            except ValueError:
                return None
            if "{{" in rendered or "}}" in rendered or "<" in rendered or ">" in rendered:
                return None
            language = (step.languages or {}).get(shell) or ("powershell" if shell == "powershell" else "bash")
            rendered_steps.append((step.merge, step.label, rendered))
        if not rendered_steps:
            return None
        code = ""
        labels: list[str] = []
        for index, (merge, label, rendered) in enumerate(rendered_steps):
            labels.append(label)
            if index:
                code += "\n" if merge == "newline" else " "
            code += rendered
        return CommandBlock(
            label="；".join(labels),
            language=language,  # type: ignore[arg-type]
            shell=shell,  # type: ignore[arg-type]
            code=code,
            platforms=list(source_recipe.platforms),
            prerequisites=["已安装相关工具"],
        )

    def _direct(
        self,
        *,
        plan: dict,
        recipe: Recipe,
        hit: RetrievalHit | None,
        hits: list[RetrievalHit],
        revision: str,
        values: dict[str, str],
        context: ContextInfo | None,
        personalization: PersonalizationInfo | None,
    ) -> Answer | None:
        citations, citation_ids = self._citation_payload(hits, hit)
        if recipe.template_id == VIRTUALENV_RECIPE.template_id:
            command = render_virtualenv(values["target_path"], values["environment_name"], values["shell"])  # type: ignore[arg-type]
            if command is None:
                return None
            command = command.model_copy(update={"citation_ids": citation_ids[:1]})
            summary = "已根据你确认的目录、Shell 和环境名生成可直接复制的命令。"
        elif recipe.template_id == GIT_REVERT_RECIPE.template_id:
            command = CommandBlock(
                label="安全回滚提交",
                language="bash",
                shell="bash",
                code=f"git revert {values['commit']}",
                platforms=["Windows", "macOS", "Linux"],
                prerequisites=["当前目录是 Git 仓库"],
                citation_ids=citation_ids[:1],
            )
            summary = "已根据你提供的提交引用生成可直接复制的 Git 回滚命令。"
        elif recipe.catalog_recipe is not None:
            command = self._render_catalog_recipe(recipe, values)
            if command is None:
                return None
            # Rendered recipe variants inherit the exact evidence citation of
            # the selected topic/chunk.  The recipe revision remains in the
            # command-plan metadata; citation IDs must stay compatible with
            # the existing parent/child evidence UI.
            command = command.model_copy(update={"citation_ids": citation_ids[:1]})
            summary = f"已根据你确认的参数生成“{recipe.catalog_recipe.title}”命令。"
        else:
            return None
        # Re-run the same risk review used for model/local-RAG commands after
        # interpolation.  A recipe is not an exemption from safety checks.
        reviewed = review_commands([command])
        if not reviewed or any("<" in item.code or ">" in item.code for item in reviewed):
            return None
        return self._answer_base(
            kind="direct",
            summary=summary,
            commands=reviewed,
            citations=citations,
            plan_id=str(plan["id"]),
            recipe=recipe,
            revision=revision,
            values=values,
            unresolved=[],
            context=context,
            personalization=personalization,
            notes=["已使用当前对话中明确提供的参数生成；命令仅供复制，不会自动执行。"],
        )

    def handle(
        self,
        query: str,
        hits: list[RetrievalHit],
        *,
        conversation_id: str,
        knowledge_base_id: str,
        context: ContextInfo | None = None,
        personalization: PersonalizationInfo | None = None,
    ) -> Answer | None:
        """Handle a recipe request, returning ``None`` for ordinary RAG."""

        if self.repository is None:
            return None
        existing = self.repository.get_pending_command_plan(conversation_id, knowledge_base_id=knowledge_base_id)
        if existing and self.is_cancel_request(query):
            self.repository.cancel_command_plan(conversation_id)
            return None
        recipe = self._recipe_for(query, hits, existing)
        if existing and recipe is None:
            self.repository.cancel_command_plan(conversation_id)
            existing = None
        if self.is_template_request(query):
            if existing:
                # A template request is an explicit end to the interactive
                # plan.  Mark it terminal before rendering so a later message
                # cannot accidentally consume the old slot sequence.
                self.repository.cancel_command_plan(conversation_id, status="completed")
                hit = self._topic_hit(recipe or VIRTUALENV_RECIPE, hits, existing)
                return self._template(
                    plan=existing,
                    recipe=recipe or VIRTUALENV_RECIPE,
                    hit=hit,
                    hits=hits,
                    revision=source_revision([hit]) if hit else str(existing.get("source_revision") or ""),
                    context=context,
                    personalization=personalization,
                )
            # Explicit template requests intentionally use the legacy RAG
            # answer path; it still limits output to one template command.
            return None
        if recipe is None:
            return None

        hit = self._topic_hit(recipe, hits, existing)
        if hit is None:
            return None
        revision = self._revision_for_recipe(recipe, hit, hits)
        if existing and existing.get("source_revision") and revision and existing["source_revision"] != revision:
            self.repository.cancel_command_plan(conversation_id, status="expired")
            # A source/revision change invalidates the old interactive
            # sequence.  Do not immediately recreate it from the user's
            # answer: that answer may have been intended for the now-stale
            # prompt and must not be consumed as a new slot value.
            return None
        created = existing is None
        if created:
            topic_id = hit.topic.id.removeprefix("parent.")
            # Accept multiple facts that the user explicitly supplied in the
            # first turn, while still exposing only the first missing slot.
            initial_values = {
                spec.name: parsed
                for spec in recipe.slots
                if (parsed := _parse_recipe_slot(spec, query, allow_bare=False)) is not None
            }
            for spec in recipe.slots:
                if spec.name not in initial_values and spec.default is not None:
                    initial_values[spec.name] = spec.default
            first_missing = self._next_slot(recipe, initial_values)
            plan = self.repository.create_command_plan(
                conversation_id,
                knowledge_base_id,
                recipe.template_id,
                self._spec_payload(recipe, topic_id),
                values=initial_values,
                next_slot=first_missing,
                source_revision=revision,
                citation_ids=self._citation_payload(hits, hit)[1],
            )
        else:
            plan = existing
        values = self._plan_values(plan)
        slot_name = plan.get("next_slot")
        if not slot_name:
            return self._direct(
                plan=plan,
                recipe=recipe,
                hit=hit,
                hits=hits,
                revision=revision,
                values=values,
                context=context,
                personalization=personalization,
            )
        spec = next((item for item in recipe.slots if item.name == slot_name), None)
        if spec is None:
            self.repository.cancel_command_plan(conversation_id, status="expired")
            return None
        # Newly-created plans already accepted every unambiguous fact in the
        # first message.  Resumed plans parse only the one currently requested
        # slot, preventing a reply from silently jumping ahead.
        if created:
            return self._clarification(
                plan=plan,
                recipe=recipe,
                spec=spec,
                revision=revision,
                citations=self._citation_payload(hits, hit)[0],
                values=values,
                context=context,
                personalization=personalization,
            )
        candidate = _parse_recipe_slot(spec, query, allow_bare=True)
        if candidate:
            values[slot_name] = candidate
            next_slot = self._next_slot(recipe, values)
            updated = self.repository.update_command_plan(
                str(plan["id"]),
                values={slot_name: candidate},
                next_slot=next_slot,
                status="completed" if next_slot is None else "pending",
                expected_state_version=int(plan.get("state_version") or 1),
            )
            if updated is not None:
                plan = updated
            if next_slot is None:
                answer = self._direct(
                    plan=plan,
                    recipe=recipe,
                    hit=hit,
                    hits=hits,
                    revision=revision,
                    values=values,
                    context=context,
                    personalization=personalization,
                )
                if answer is not None:
                    return answer
            else:
                next_spec = next(item for item in recipe.slots if item.name == next_slot)
                return self._clarification(
                    plan=plan,
                    recipe=recipe,
                    spec=next_spec,
                    revision=revision,
                    citations=self._citation_payload(hits, hit)[0],
                    values=values,
                    context=context,
                    personalization=personalization,
                )
        # One invalid answer is enough to stop asking.  The template is marked
        # completed so a stale UI cannot continue the old plan indefinitely.
        slots = plan.get("slots") or {}
        slot_state = dict(slots.get(slot_name) or {})
        slot_state["attempt"] = int(slot_state.get("attempt") or 1) + 1
        slots[slot_name] = slot_state
        updated = self.repository.update_command_plan(
            str(plan["id"]),
            status="completed",
            next_slot=None,
            expected_state_version=int(plan.get("state_version") or 1),
        )
        if updated is not None:
            plan = updated
        return self._template(
            plan=plan,
            recipe=recipe,
            hit=hit,
            hits=hits,
            revision=revision,
            context=context,
            personalization=personalization,
        )
