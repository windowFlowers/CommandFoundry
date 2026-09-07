from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = ROOT / "knowledge"
TLDR_REPOSITORY = "https://github.com/tldr-pages/tldr.git"
TLDR_REVISION = "d7f4fcb00a22fa5e5a595323705df61e1ecff707"
TLDR_LICENSE = "CC BY 4.0"


def item(domain: str, page: str, title: str, aliases: list[str], path: str | None = None) -> dict[str, object]:
    return {
        "domain": domain,
        "page": page,
        "title": title,
        "aliases": aliases,
        "path": path or f"pages/common/{page}.md",
    }


CURATED = [
    # Linux / Shell (20)
    item("linux", "cd", "切换工作目录", ["切换目录", "进入文件夹", "返回上级目录", "Linux cd"]),
    item("linux", "ls", "列出目录内容", ["查看目录文件", "显示隐藏文件", "Linux ls"]),
    item("linux", "mkdir", "创建目录", ["新建文件夹", "递归创建目录"]),
    item("linux", "cp", "复制文件与目录", ["复制文件", "复制文件夹", "保留文件属性"]),
    item("linux", "mv", "移动或重命名文件", ["移动文件", "重命名文件", "覆盖前询问"]),
    item("linux", "rm", "删除文件与目录", ["删除文件", "递归删除目录", "rm 命令"]),
    item("linux", "find", "查找文件", ["按名称找文件", "递归查找文件", "find 命令"]),
    item("linux", "grep", "搜索文本内容", ["在文件中搜索", "递归搜索文本", "grep 命令"]),
    item("linux", "rg", "使用 ripgrep 搜索", ["快速搜索代码", "ripgrep", "rg 命令"]),
    item("linux", "chmod", "修改文件权限", ["给脚本执行权限", "修改读写权限", "chmod 命令"]),
    item("linux", "chown", "修改文件所有者", ["更改文件用户", "递归修改所有者", "chown 命令"]),
    item("linux", "ps", "查看进程", ["列出进程", "查看进程号", "ps 命令"]),
    item("linux", "kill", "终止进程", ["结束进程", "发送信号", "kill 命令"]),
    item("linux", "top", "实时查看系统进程", ["查看 CPU 内存占用", "实时进程监控", "top 命令"], "pages/linux/top.md"),
    item("linux", "df", "查看磁盘空间", ["磁盘剩余空间", "文件系统容量", "df 命令"]),
    item("linux", "du", "查看目录占用", ["文件夹大小", "磁盘占用排序", "du 命令"]),
    item("linux", "tar", "打包与解压 tar", ["创建 tar.gz", "解压缩 tar", "tar 命令"]),
    item("linux", "zip", "创建 ZIP 压缩包", ["压缩目录", "zip 命令", "创建 zip"]),
    item("linux", "ssh", "通过 SSH 连接服务器", ["远程登录 Linux", "指定 SSH 端口", "ssh 命令"]),
    item("linux", "scp", "通过 SSH 复制文件", ["上传文件到服务器", "下载远程文件", "scp 命令"]),
    # Git (20)
    item("git", "git-init", "初始化 Git 仓库", ["创建 Git 仓库", "git init"]),
    item("git", "git-clone", "克隆 Git 仓库", ["下载 Git 仓库", "克隆指定分支", "git clone"]),
    item("git", "git-status", "查看工作区状态", ["查看修改文件", "git status"]),
    item("git", "git-add", "暂存文件", ["添加文件到暂存区", "git add"]),
    item("git", "git-commit", "提交代码变更", ["创建 Git 提交", "修改最后提交", "git commit"]),
    item("git", "git-branch", "管理分支", ["新建分支", "删除分支", "查看分支", "git branch"]),
    item("git", "git-switch", "切换 Git 分支", ["切换分支", "创建并切换分支", "git switch"]),
    item("git", "git-merge", "合并 Git 分支", ["合并分支", "取消合并", "git merge"]),
    item("git", "git-rebase", "变基 Git 分支", ["整理提交历史", "交互式 rebase", "git rebase"]),
    item("git", "git-stash", "暂存未提交修改", ["临时保存修改", "恢复 stash", "git stash"]),
    item("git", "git-log", "查看提交历史", ["查看 Git 日志", "图形化提交记录", "git log"]),
    item("git", "git-diff", "比较代码差异", ["查看未提交修改", "比较两个提交", "git diff"]),
    item("git", "git-reset", "重置提交或暂存区", ["Git 回滚并改写历史", "撤销 commit 保留修改", "git reset"]),
    item("git", "git-restore", "恢复工作区文件", ["丢弃文件修改", "取消暂存", "git restore"]),
    item("git", "git-revert", "安全撤销 Git 提交", ["Git 安全回滚", "撤销已推送提交", "git revert"]),
    item("git", "git-reflog", "查找丢失的 Git 提交", ["恢复误删提交", "查看 HEAD 操作记录", "git reflog"]),
    item("git", "git-cherry-pick", "拣选 Git 提交", ["复制某个提交", "git cherry-pick"]),
    item("git", "git-tag", "管理 Git 标签", ["创建版本标签", "推送 tag", "git tag"]),
    item("git", "git-remote", "管理远程仓库", ["查看远程地址", "添加 origin", "git remote"]),
    item("git", "git-pull", "拉取远程更新", ["拉取并变基", "更新本地分支", "git pull"]),
    # Docker (15)
    item("docker", "docker", "Docker 命令入口", ["Docker 帮助", "查看 Docker 信息"]),
    item("docker", "docker-build", "构建 Docker 镜像", ["使用 Dockerfile 构建", "docker build"]),
    item("docker", "docker-run", "运行 Docker 容器", ["启动容器", "映射端口和目录", "docker run"]),
    item("docker", "docker-ps", "查看 Docker 容器", ["列出运行中的容器", "查看全部容器", "docker ps"]),
    item("docker", "docker-logs", "查看容器日志", ["持续查看 Docker 日志", "docker logs"]),
    item("docker", "docker-exec", "在容器中执行命令", ["进入 Docker 容器", "docker exec"]),
    item("docker", "docker-stop", "停止 Docker 容器", ["停止容器", "docker stop"]),
    item("docker", "docker-rm", "删除 Docker 容器", ["移除容器", "强制删除容器", "docker rm"]),
    item("docker", "docker-images", "查看 Docker 镜像", ["列出本地镜像", "docker images"]),
    item("docker", "docker-compose", "管理 Compose 服务", ["启动 compose", "停止 compose", "docker compose"]),
    item("docker", "docker-volume", "管理 Docker 数据卷", ["查看数据卷", "删除数据卷", "docker volume"]),
    item("docker", "docker-network", "管理 Docker 网络", ["查看 Docker 网络", "连接容器网络"]),
    item("docker", "docker-inspect", "检查 Docker 对象", ["查看容器详细配置", "docker inspect"]),
    item("docker", "docker-cp", "容器与主机复制文件", ["从容器复制文件", "复制文件到容器", "docker cp"]),
    item("docker", "docker-system", "查看和清理 Docker 资源", ["清理 Docker 磁盘", "docker system prune"]),
    # HTTP / cURL (10)
    item("http", "curl", "使用 cURL 发起 HTTP 请求", ["curl GET", "curl POST JSON", "下载文件", "请求头"]),
    item("http", "wget", "使用 wget 下载文件", ["断点续传下载", "递归下载网站", "wget"]),
    item("http", "http", "使用 HTTPie 调试接口", ["HTTPie GET", "HTTPie POST", "接口调试"]),
    item("http", "xh", "使用 xh 调试 HTTP", ["xh 请求", "HTTPie 替代工具"]),
    item("http", "httping", "检测 HTTP 服务延迟", ["HTTP ping", "检查网页响应时间"]),
    item("http", "ab", "使用 ApacheBench 压测", ["HTTP 并发压测", "ab 压测"]),
    item("http", "grpcurl", "调试 gRPC 接口", ["调用 gRPC 服务", "列出 gRPC 方法"]),
    item("http", "nmap", "扫描网络端口", ["检查开放端口", "扫描主机服务", "nmap"]),
    item("http", "openssl", "检查 TLS 证书与连接", ["查看 HTTPS 证书", "测试 TLS", "openssl s_client"]),
    item("http", "jq", "处理 JSON 响应", ["格式化 JSON", "提取 JSON 字段", "jq"]),
    # Python / Node (20)
    item("toolchain", "python", "运行 Python", ["执行 Python 脚本", "启动 Python", "python 命令"]),
    item("toolchain", "python3", "运行 Python 3", ["执行 Python3 脚本", "python3"]),
    item("toolchain", "pip", "管理 Python 包", ["安装 Python 依赖", "pip install", "导出 requirements"]),
    item("toolchain", "pip3", "管理 Python 3 包", ["pip3 install", "升级 Python 包"]),
    item("toolchain", "pipx", "隔离安装 Python CLI", ["安装 Python 命令行工具", "pipx"]),
    item("toolchain", "virtualenv", "创建 Python 虚拟环境", ["Python 虚拟环境", "virtualenv"]),
    item("toolchain", "pytest", "运行 Python 测试", ["运行单元测试", "pytest"]),
    item("toolchain", "ruff", "检查和格式化 Python", ["Python lint", "ruff check", "ruff format"]),
    item("toolchain", "black", "格式化 Python 代码", ["Python 格式化", "black"]),
    item("toolchain", "uv", "使用 uv 管理 Python 项目", ["uv 安装依赖", "uv 虚拟环境", "uv run"]),
    item("toolchain", "node", "运行 Node.js", ["执行 JavaScript 文件", "node 命令"]),
    item("toolchain", "npm", "管理 npm 项目", ["npm 安装依赖", "运行 npm script", "npm install"]),
    item("toolchain", "npx", "临时运行 npm 工具", ["不安装运行 npm 包", "npx"]),
    item("toolchain", "yarn", "使用 Yarn 管理依赖", ["yarn install", "yarn script"]),
    item("toolchain", "pnpm", "使用 pnpm 管理依赖", ["pnpm install", "pnpm workspace"]),
    item("toolchain", "corepack", "管理 Node 包管理器", ["启用 pnpm yarn", "corepack"]),
    item("toolchain", "eslint", "检查 JavaScript 代码", ["JavaScript lint", "eslint"]),
    item("toolchain", "prettier", "格式化前端代码", ["格式化 JavaScript", "prettier"]),
    item("toolchain", "vite", "运行 Vite 项目", ["启动 Vite 开发服务器", "构建 Vite", "vite"]),
    item("toolchain", "ts-node", "直接运行 TypeScript", ["执行 TypeScript 文件", "ts-node"]),
]


SQL_TOPICS = [
    ("mysql-connect", "连接 MySQL 数据库", ["登录 MySQL", "连接远程 MySQL", "mysql 客户端"], "bash", "mysql --host=<host> --user=<user> --password <database>", ["已安装 MySQL Client", "准备好数据库账号"], "low", None, "https://dev.mysql.com/doc/refman/8.4/en/connecting.html"),
    ("create-database", "创建 MySQL 数据库", ["MySQL 新建数据库", "CREATE DATABASE"], "sql", "CREATE DATABASE `<database>` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;", ["账号具有 CREATE 权限"], "medium", "执行前确认数据库名，避免与现有库冲突。", "https://dev.mysql.com/doc/refman/8.4/en/create-database.html"),
    ("create-table", "创建 MySQL 数据表", ["MySQL 新建表", "CREATE TABLE"], "sql", "CREATE TABLE `<table>` (\n  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n  `name` VARCHAR(100) NOT NULL,\n  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;", ["已经选择目标数据库"], "medium", "上线前确认字段类型、索引和字符集。", "https://dev.mysql.com/doc/refman/8.4/en/create-table.html"),
    ("insert-row", "向 MySQL 表插入数据", ["MySQL 插入记录", "INSERT INTO"], "sql", "INSERT INTO `<table>` (`name`) VALUES ('<value>');", ["目标表和字段已经存在"], "medium", "生产环境建议先在事务中验证。", "https://dev.mysql.com/doc/refman/8.4/en/insert.html"),
    ("select-rows", "查询和排序 MySQL 数据", ["MySQL 查询数据", "SELECT WHERE ORDER BY"], "sql", "SELECT `id`, `name`\nFROM `<table>`\nWHERE `name` LIKE '%<keyword>%'\nORDER BY `id` DESC\nLIMIT 20;", ["目标表和字段已经存在"], "low", None, "https://dev.mysql.com/doc/refman/8.4/en/select.html"),
    ("join-tables", "连接多个 MySQL 表", ["MySQL JOIN", "关联查询", "LEFT JOIN"], "sql", "SELECT a.`id`, b.`name`\nFROM `<table_a>` AS a\nLEFT JOIN `<table_b>` AS b ON b.`id` = a.`<foreign_key>`\nLIMIT 20;", ["确认关联字段及其索引"], "low", None, "https://dev.mysql.com/doc/refman/8.4/en/join.html"),
    ("update-rows", "更新 MySQL 数据", ["MySQL 修改记录", "UPDATE SET WHERE"], "sql", "UPDATE `<table>`\nSET `name` = '<new_value>'\nWHERE `id` = <id>\nLIMIT 1;", ["先用相同 WHERE 条件执行 SELECT"], "high", "缺少或写错 WHERE 会批量修改数据；建议在事务中执行。", "https://dev.mysql.com/doc/refman/8.4/en/update.html"),
    ("delete-rows", "删除 MySQL 数据", ["MySQL 删除记录", "DELETE FROM WHERE"], "sql", "DELETE FROM `<table>`\nWHERE `id` = <id>\nLIMIT 1;", ["先备份并用相同 WHERE 条件执行 SELECT"], "high", "DELETE 不可直接撤销；生产环境请使用事务并确认影响行数。", "https://dev.mysql.com/doc/refman/8.4/en/delete.html"),
    ("create-index", "创建 MySQL 索引", ["MySQL 添加索引", "CREATE INDEX", "查询优化"], "sql", "CREATE INDEX `idx_<table>_<column>` ON `<table>` (`<column>`);", ["确认字段选择性和现有索引"], "medium", "大表建索引可能长时间占用 IO 或阻塞操作。", "https://dev.mysql.com/doc/refman/8.4/en/create-index.html"),
    ("create-view", "创建 MySQL 视图", ["MySQL 怎么创建视图", "CREATE VIEW", "替换视图"], "sql", "CREATE OR REPLACE VIEW `<view_name>` AS\nSELECT `id`, `name`\nFROM `<table>`\nWHERE `<condition>`;", ["账号具有 CREATE VIEW 权限", "底层表和字段已经存在"], "medium", "修改视图前确认依赖方；复杂视图可能影响查询性能。", "https://dev.mysql.com/doc/refman/8.4/en/create-view.html"),
    ("transaction", "使用 MySQL 事务", ["MySQL 开启事务", "COMMIT ROLLBACK", "事务回滚"], "sql", "START TRANSACTION;\n\n-- 在这里执行 INSERT / UPDATE / DELETE\n\nCOMMIT;\n-- 如需撤销，使用：ROLLBACK;", ["表使用支持事务的 InnoDB 引擎"], "medium", "提交 COMMIT 后不能再用 ROLLBACK 撤销。", "https://dev.mysql.com/doc/refman/8.4/en/commit.html"),
    ("explain-query", "分析 MySQL 查询计划", ["MySQL EXPLAIN", "查看 SQL 是否走索引", "查询计划"], "sql", "EXPLAIN ANALYZE\nSELECT * FROM `<table>` WHERE `<column>` = '<value>';", ["MySQL 8.0.18 及以上版本支持 EXPLAIN ANALYZE"], "low", None, "https://dev.mysql.com/doc/refman/8.4/en/explain.html"),
    ("create-user", "创建 MySQL 用户并授权", ["MySQL 创建账号", "GRANT 权限", "CREATE USER"], "sql", "CREATE USER '<user>'@'<host>' IDENTIFIED BY '<strong_password>';\nGRANT SELECT, INSERT, UPDATE, DELETE ON `<database>`.* TO '<user>'@'<host>';", ["使用具有 CREATE USER 和 GRANT OPTION 的管理员账号"], "high", "遵循最小权限原则，不要授予 ALL 或使用 '%' 主机范围，除非确有需要。", "https://dev.mysql.com/doc/refman/8.4/en/create-user.html"),
    ("backup", "使用 mysqldump 备份数据库", ["备份 MySQL", "导出 SQL 文件", "mysqldump"], "bash", "mysqldump --host=<host> --user=<user> --password --single-transaction --routines --triggers <database> > <backup.sql>", ["账号具有读取、视图和触发器所需权限", "确认磁盘空间充足"], "low", None, "https://dev.mysql.com/doc/refman/8.4/en/mysqldump.html"),
    ("restore", "恢复 MySQL 备份", ["导入 MySQL 备份", "恢复 SQL 文件", "mysql source"], "bash", "mysql --host=<host> --user=<user> --password <database> < <backup.sql>", ["已验证备份文件", "目标数据库已创建"], "high", "恢复会写入或覆盖数据；先在隔离环境验证并备份目标库。", "https://dev.mysql.com/doc/refman/8.4/en/mysql-batch-commands.html"),
]


DOMAIN_PLATFORM = {
    "linux": ["Linux", "macOS", "WSL"],
    "git": ["Windows", "macOS", "Linux"],
    "docker": ["Windows", "macOS", "Linux"],
    "http": ["Windows", "macOS", "Linux"],
    "toolchain": ["Windows", "macOS", "Linux"],
    "sql": ["MySQL 8.x"],
}

DOMAIN_PREREQUISITE = {
    "linux": ["在兼容 POSIX 的 Shell 中运行"],
    "git": ["已安装 Git", "当前目录是 Git 仓库（初始化和克隆命令除外）"],
    "docker": ["Docker Engine 或 Docker Desktop 正在运行"],
    "http": ["已安装对应的命令行工具"],
    "toolchain": ["已安装对应运行时或包管理器"],
}


def normalize_placeholders(command: str) -> str:
    command = re.sub(r"\{\{\[-[^|]+\|(--[^\]]+)\]\}\}", r"\1", command)
    command = re.sub(r"\{\{([^{}]+)\}\}", lambda match: f"<{match.group(1)}>", command)
    return command


def risk_for(command: str) -> tuple[str, str | None]:
    normalized = command.lower()
    high_patterns = (
        "rm -rf",
        "git reset --hard",
        "git push --force",
        "git push -f",
        "docker system prune",
        "docker volume rm",
        "docker rm --force",
        "docker rm -f",
        "curl | sh",
        "curl | bash",
        "sudo ",
    )
    if any(pattern in normalized for pattern in high_patterns):
        return "high", "该命令可能删除数据、覆盖状态或提升权限，请先核对目标并准备回滚方案。"
    medium_patterns = (" rm ", "kill ", "chmod ", "chown ", "git rebase", "git clean", "docker stop", "docker rm")
    padded = f" {normalized} "
    if any(pattern in padded for pattern in medium_patterns):
        return "medium", "该命令会修改本地状态，执行前请确认目标和当前工作区。"
    return "low", None


def parse_tldr(spec: dict[str, object], source_root: Path) -> dict[str, object]:
    relative_path = str(spec["path"])
    path = source_root / relative_path
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    descriptions = [
        line[1:].strip()
        for line in lines
        if line.startswith(">") and "More information:" not in line and "See also:" not in line
    ]
    commands: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        if not line.startswith("- "):
            continue
        label = line[2:].rstrip(":").strip()
        code = ""
        for following in lines[index + 1 : index + 5]:
            stripped = following.strip()
            if stripped.startswith("`") and stripped.endswith("`"):
                code = stripped.strip("`")
                break
        if not code:
            continue
        code = normalize_placeholders(code)
        risk, warning = risk_for(code)
        commands.append(
            {
                "label": label,
                "language": "bash",
                "code": code,
                "platforms": DOMAIN_PLATFORM[str(spec["domain"])],
                "prerequisites": DOMAIN_PREREQUISITE[str(spec["domain"])],
                "risk": risk,
                "warning": warning,
            }
        )
    if not commands:
        raise ValueError(f"No commands parsed from {relative_path}")
    source_bytes = path.read_bytes()
    topic_id = f"{spec['domain']}.{spec['page']}"
    return {
        "id": topic_id,
        "domain": spec["domain"],
        "title": spec["title"],
        "aliases": spec["aliases"],
        "summary": " ".join(descriptions) or f"{spec['title']} 的常用命令。",
        "commands": commands[:8],
        "notes": ["将尖括号中的占位参数替换为你的实际值。"],
        "source": {
            "source_id": topic_id,
            "title": f"tldr: {spec['page']}",
            "source_url": f"https://github.com/tldr-pages/tldr/blob/{TLDR_REVISION}/{relative_path}",
            "license": TLDR_LICENSE,
            "revision": TLDR_REVISION,
            "path": relative_path,
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "kind": "vendored",
        },
    }


def build_sql_topics() -> list[dict[str, object]]:
    topics = []
    for topic_id, title, aliases, language, code, prerequisites, risk, warning, source_url in SQL_TOPICS:
        topics.append(
            {
                "id": f"sql.{topic_id}",
                "domain": "sql",
                "title": title,
                "aliases": aliases,
                "summary": f"{title}的可执行模板，适用于 MySQL 8.x。",
                "commands": [
                    {
                        "label": title,
                        "language": language,
                        "code": code,
                        "platforms": DOMAIN_PLATFORM["sql"],
                        "prerequisites": prerequisites,
                        "risk": risk,
                        "warning": warning,
                    }
                ],
                "notes": ["将尖括号中的占位参数替换为你的实际值。"],
                "source": {
                    "source_id": f"sql.{topic_id}",
                    "title": "AegisCopilot MySQL 种子知识",
                    "source_url": source_url,
                    "license": "MIT (project-authored)",
                    "revision": "v2.0.0",
                    "path": "generated/mysql",
                    "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                    "kind": "generated_seed",
                },
            }
        )
    return topics


def ensure_source(source_root: Path) -> Path:
    if not (source_root / ".git").exists():
        source_root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--filter=blob:none", TLDR_REPOSITORY, str(source_root)], check=True)
    subprocess.run(["git", "-C", str(source_root), "fetch", "origin", TLDR_REVISION], check=True)
    subprocess.run(["git", "-C", str(source_root), "checkout", TLDR_REVISION], check=True)
    return source_root


def write_outputs(topics: list[dict[str, object]]) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    topics.sort(key=lambda entry: str(entry["id"]))
    (KNOWLEDGE_DIR / "topics.json").write_text(
        json.dumps(topics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "topic_count": len(topics),
        "domains": {
            domain: sum(1 for topic in topics if topic["domain"] == domain)
            for domain in ("linux", "git", "sql", "docker", "http", "toolchain")
        },
        "sources": [topic["source"] for topic in topics],
    }
    (KNOWLEDGE_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    eval_cases = [
        {
            "id": f"case-{index:03d}",
            "query": str(topic["aliases"][0]),
            "expected_topic_id": topic["id"],
            "domain": topic["domain"],
        }
        for index, topic in enumerate(topics, start=1)
    ]
    (KNOWLEDGE_DIR / "eval_dataset.json").write_text(
        json.dumps(eval_cases, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    notices = f"""# Third-party notices

## tldr-pages/tldr

- Repository: {TLDR_REPOSITORY}
- Revision: `{TLDR_REVISION}`
- License: Creative Commons Attribution 4.0 International (CC BY 4.0)
- Copyright: tldr-pages team and contributors

Selected command examples are adapted into AegisCopilot's structured knowledge format.

## Architecture references

- datawhalechina/all-in-rag — CC BY-NC-SA 4.0. Used as an architecture and evaluation reference only; its tutorial content is not redistributed in the application.
- FastEmbed — Apache-2.0.
- rank-bm25 — Apache-2.0.
- BAAI/bge-small-zh-v1.5 model — MIT License, distributed through the FastEmbed model catalog.

## Official documentation references

Project-authored MySQL seed topics link to the MySQL 8.4 Reference Manual. The manual itself is not redistributed.
"""
    (KNOWLEDGE_DIR / "THIRD_PARTY_NOTICES.md").write_text(notices, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build AegisCopilot's curated offline knowledge bundle")
    parser.add_argument("--tldr-root", type=Path, default=ROOT / ".runtime" / "sources" / "tldr")
    args = parser.parse_args()
    source_root = ensure_source(args.tldr_root.resolve())
    topics = [parse_tldr(spec, source_root) for spec in CURATED]
    topics.extend(build_sql_topics())
    if len(topics) != 100:
        raise RuntimeError(f"Expected 100 topics, got {len(topics)}")
    write_outputs(topics)
    print(f"Wrote {len(topics)} topics to {KNOWLEDGE_DIR}")


if __name__ == "__main__":
    main()
