from __future__ import annotations

import re

from .models import CommandBlock, RiskLevel


HIGH_RISK_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+[^\n]*-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+[^\n]*-[a-zA-Z]*f[a-zA-Z]*r", re.I), "递归强制删除不可撤销，请先核对目标路径并准备备份。"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.I), "会丢弃未提交修改并移动分支指针，请先确认工作区状态或创建备份分支。"),
    (re.compile(r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f)\b", re.I), "会改写远端分支历史，可能影响协作者；优先使用 --force-with-lease 并先同步远端。"),
    (re.compile(r"\b(?:drop|truncate)\s+(?:table|database|schema)\b", re.I), "会删除数据库对象或数据，执行前必须确认环境并完成可恢复备份。"),
    (re.compile(r"\bdocker\s+(?:system|volume|image|container)\s+prune\b", re.I), "会批量清理 Docker 资源，请先用列表命令确认影响范围。"),
    (re.compile(r"\bkubectl\s+delete\b", re.I), "会删除 Kubernetes 资源，请先确认 context、namespace 和资源名称。"),
    (re.compile(r"\bcurl\b[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b", re.I), "会直接执行网络下载的脚本；应先下载、审阅内容并校验来源。"),
]

MEDIUM_RISK_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|\s)sudo\s+", re.I), "命令使用管理员权限，请确认参数和目标环境。"),
    (re.compile(r"\bgit\s+clean\b|\bgit\s+branch\s+-D\b", re.I), "可能删除未跟踪文件或分支，请先预览并确认已有备份。"),
    (re.compile(r"\bdocker\s+(?:rm|rmi)\b", re.I), "会删除容器或镜像，请先确认目标标识。"),
    (re.compile(r"\bdelete\s+from\b", re.I), "会删除匹配的数据行，请先用相同 WHERE 条件执行 SELECT 预览。"),
]


def review_command(command: CommandBlock) -> CommandBlock:
    reviewed = command.model_copy(deep=True)
    for pattern, warning in HIGH_RISK_RULES:
        if pattern.search(reviewed.code):
            reviewed.risk = RiskLevel.high
            reviewed.warning = warning
            return reviewed
    for pattern, warning in MEDIUM_RISK_RULES:
        if pattern.search(reviewed.code):
            if reviewed.risk == RiskLevel.low:
                reviewed.risk = RiskLevel.medium
            reviewed.warning = reviewed.warning or warning
            return reviewed
    if reviewed.risk != RiskLevel.low and not reviewed.warning:
        reviewed.warning = "该命令会改变本地或远端状态，执行前请确认参数与环境。"
    return reviewed


def review_commands(commands: list[CommandBlock]) -> list[CommandBlock]:
    return [review_command(command) for command in commands]
