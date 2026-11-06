"""Host-side rules that hold even when the model is wrong or every prompt is approved.

The model only proposes tool calls. DENY cannot be overridden by approval; ASK
pauses for a person. Rules come from the environment profile, never from text
the model has read.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Callable, NamedTuple

ALLOW, ASK, DENY = "allow", "ask", "deny"
SOURCE_SUFFIXES = (".py", ".sh", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp")
GIT_UNFINISHED = ("HEAD detached", "You have unmerged paths", "still merging")

Backend = Callable[[str, dict], tuple[str, bool]]


@dataclass(frozen=True)
class Profile:
    """What the host knows about the machine, independent of the model."""
    services: tuple[str, ...] = ()
    health_urls: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    scripts: tuple[str, ...] = ()
    home: str = "/home/dev"


class Verdict(NamedTuple):
    action: str
    reason: str = ""


class Policy:
    def __init__(self, profile: Profile, tools: dict):
        self.profile = profile
        self.tools = tools

    def _normal(self, value: str) -> str:
        if value == "~" or value.startswith("~/"):
            value = self.profile.home + value[1:]
        return posixpath.normpath(posixpath.join(self.profile.home, value))

    def _protects(self, path: str, *, ancestors: bool) -> bool:
        for pattern in self.profile.protected:
            if fnmatch(path, pattern):
                return True
            if ancestors and (posixpath.dirname(pattern) + "/").startswith(path.rstrip("/") + "/"):
                return True
        return False

    def _managed_pids(self, backend: Backend) -> set[int] | None:
        """Return None when a service cannot be inspected, so callers fail closed."""
        pids = set()
        for service in self.profile.services:
            output, succeeded = backend("service_status", {"name": service})
            if not succeeded:
                return None
            pids.update(int(pid) for pid in re.findall(r"pid=(\d+)", output.split("\n", 1)[0]))
        return pids

    def health(self, backend: Backend) -> tuple[bool, list[str]]:
        """Run the declared probes. Returns (anything_checked, problems)."""
        problems = []
        for service in self.profile.services:
            output, _ = backend("service_status", {"name": service})
            if "运行中" not in output.split("\n", 1)[0]:
                problems.append(f"服务 {service} 未运行")
        for url in self.profile.health_urls:
            output, _ = backend("http_check", {"url": url})
            if not re.match(r"HTTP 2\d\d", output):
                problems.append(f"{url} 检查失败：{output[:120]}")
        for repo in self.profile.repos:
            output, _ = backend("git_inspect", {"repo": repo, "view": "status"})
            if any(marker in output for marker in GIT_UNFINISHED):
                problems.append(f"仓库 {repo} 处于未完成状态：{output[:120]}")
        return bool(self.profile.services or self.profile.health_urls or self.profile.repos), problems

    def review(self, name: str, arguments: dict, backend: Backend) -> Verdict:
        if not self.tools[name]["writes"]:
            return Verdict(ALLOW)
        path = self._normal(arguments["path"]) if "path" in arguments else ""
        if name == "delete_path" and self._protects(path, ancestors=True):
            return Verdict(DENY, f"{path} 是或包含业务数据，不能删除")
        if name in ("edit_file", "write_file"):
            if self._protects(path, ancestors=False):
                return Verdict(DENY, f"{path} 是业务数据，不能改写")
            if path.endswith(SOURCE_SUFFIXES):
                return Verdict(DENY, f"{path} 是源代码，排障只能修改配置和运行状态")
        if name == "chmod_path" and re.fullmatch(r"[0-7]{3,4}", arguments["mode"]) and int(arguments["mode"], 8) & 0o002:
            return Verdict(DENY, "不能设置所有用户可写的权限")
        if name == "kill_process":
            managed = self._managed_pids(backend)
            if managed is None:
                return Verdict(DENY, "无法确认该进程是否属于受管服务")
            if arguments["pid"] in managed:
                return Verdict(DENY, f"pid={arguments['pid']} 属于受管服务，不能结束。如果它占用了端口，先检查配置是否写错")
        if name == "run_script" and path not in self.profile.scripts:
            allowed = "、".join(self.profile.scripts) or "无"
            return Verdict(DENY, f"只能运行已声明的维护脚本：{allowed}。重启服务请用 service_restart")
        if name != "git_action":
            checked, problems = self.health(backend)
            if checked and not problems:
                return Verdict(DENY, "系统检查全部正常，没有修改的依据")
        return Verdict(ASK, f"{name} 会修改系统状态")
