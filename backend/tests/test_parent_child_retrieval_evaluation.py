from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.chunking import INDEX_SCHEMA_VERSION, chunk_document
from app.extraction import ExtractionService
from app.repository import ConversationRepository
from app.retrieval import EmbeddingEngine, KnowledgeIndexManager, tokenize


@dataclass(frozen=True)
class EvalTopic:
    slug: str
    title: str
    intent: str
    keyword_a: str
    keyword_b: str
    command: str
    language: str = "bash"


TOPICS = (
    EvalTopic("k8s-rollout", "Kubernetes 发布状态", "确认 Deployment 滚动发布完成", "kubectl", "rollout status", "kubectl rollout status deployment/api"),
    EvalTopic("compose-worker", "Docker Compose Worker", "重启 worker 服务", "docker compose", "restart worker", "docker compose restart worker"),
    EvalTopic("git-revert", "Git 安全回滚", "用反向提交撤销一次提交", "git revert", "commit", "git revert <commit>"),
    EvalTopic("powershell-service", "PowerShell 服务检查", "查看打印服务状态", "Get-Service", "Spooler", "Get-Service -Name Spooler", "powershell"),
    EvalTopic("postgres-index", "PostgreSQL 在线索引", "并发创建索引减少锁表", "CREATE INDEX", "CONCURRENTLY", "CREATE INDEX CONCURRENTLY idx_orders_created_at ON orders(created_at);", "sql"),
    EvalTopic("redis-scan", "Redis 渐进扫描", "避免阻塞地遍历键", "Redis SCAN", "MATCH", "SCAN 0 MATCH session:* COUNT 100"),
    EvalTopic("nginx-test", "Nginx 配置校验", "重载前检查配置语法", "nginx", "-t", "nginx -t"),
    EvalTopic("python-venv", "Python 虚拟环境", "创建隔离的虚拟环境", "python", "venv", "python -m venv .venv"),
    EvalTopic("npm-audit", "npm 生产依赖审计", "只检查生产依赖漏洞", "npm audit", "omit dev", "npm audit --omit=dev"),
    EvalTopic("pnpm-lock", "pnpm 锁文件安装", "严格按锁文件安装依赖", "pnpm install", "frozen-lockfile", "pnpm install --frozen-lockfile"),
    EvalTopic("journal-unit", "systemd 单元日志", "查看指定服务最近日志", "journalctl", "unit", "journalctl -u api.service -n 100"),
    EvalTopic("curl-headers", "HTTP 响应头检查", "只获取响应头", "curl", "headers", "curl -I https://example.com"),
    EvalTopic("ssh-permission", "SSH 私钥权限", "收紧私钥文件权限", "chmod 600", "id_rsa", "chmod 600 ~/.ssh/id_rsa"),
    EvalTopic("mysql-explain", "MySQL 执行计划", "查看查询执行计划", "MySQL EXPLAIN", "SELECT", "EXPLAIN SELECT * FROM orders WHERE id = 42;", "sql"),
    EvalTopic("helm-rollback", "Helm 发布回滚", "回滚到指定修订版本", "helm rollback", "revision", "helm rollback api 3"),
    EvalTopic("maven-skip-tests", "Maven 跳过测试打包", "临时跳过测试完成打包", "mvn package", "skipTests", "mvn package -DskipTests"),
    EvalTopic("gradle-deps", "Gradle 依赖树", "输出运行时依赖关系", "gradle dependencies", "runtimeClasspath", "./gradlew dependencies --configuration runtimeClasspath"),
    EvalTopic("node-heap", "Node.js 堆内存", "提高进程最大堆内存", "node", "max-old-space-size", "node --max-old-space-size=4096 app.js"),
    EvalTopic("windows-port", "Windows 端口进程", "定位监听端口所属进程", "Get-NetTCPConnection", "OwningProcess", "Get-NetTCPConnection -LocalPort 8080 | Select-Object OwningProcess", "powershell"),
    EvalTopic("tar-gzip", "Linux tar 归档", "创建 gzip 压缩归档", "tar", "gzip", "tar -czf backup.tar.gz ./data"),
)


QUESTION_TEMPLATES = (
    "{title}场景中，怎样{intent}？请使用 {keyword_a} 与 {keyword_b}。",
    "我需要{intent}，{keyword_a} 的 {keyword_b} 参数应该怎么写？",
    "排查操作时想{intent}，请给出包含 {keyword_a}、{keyword_b} 的做法。",
    "关于{title}：{keyword_a} 和 {keyword_b} 对应的正确命令是什么？",
)


class EmptyCatalog:
    topics: list = []
    domain_counts: dict[str, int] = {}


def _document_text(topic: EvalTopic) -> str:
    overview = (
        "这一节保存部署前的通用检查背景，包括确认目标环境、记录当前版本、检查权限与可用空间。"
        "这些背景步骤用于形成独立的非答案子块，不应替代后面的精确操作。"
    ) * 14
    troubleshooting = (
        "执行后应观察退出状态和应用健康度；若结果异常，先保存日志，再按团队变更流程决定是否恢复。"
        "不要把未经验证的输出当成成功信号。"
    ) * 7
    return f"""# {topic.title}

## 操作前概览

{overview}

## 精确操作

要{topic.intent}，关键字是 {topic.keyword_a} 和 {topic.keyword_b}。只使用下面经过核对的命令：

```{topic.language}
{topic.command}
```

## 执行后排查

{troubleshooting}
"""


def _rows(document_id: str, chunked) -> tuple[list[dict], list[dict]]:
    parent_id_map = {parent.id: f"{document_id}:{parent.id}" for parent in chunked.parents}
    parents = [
        {
            "id": parent_id_map[parent.id],
            "stable_id": parent.id,
            "index": parent.index,
            "text": parent.text,
            "locator": parent.locator.as_dict(),
            "content_hash": parent.content_hash,
        }
        for parent in chunked.parents
    ]
    children = [
        {
            "id": f"{document_id}:{child.id}",
            "stable_id": child.id,
            "parent_id": parent_id_map[child.parent_id],
            "index": child.index,
            "text": child.text,
            "retrieval_text": child.retrieval_text,
            "locator": child.locator.as_dict(),
            "content_hash": child.content_hash,
            "command_evidence": [item.code for item in child.command_evidence],
            "tokens_json": json.dumps(tokenize(child.retrieval_text), ensure_ascii=False),
            "embedding": None,
            "embedding_dimension": None,
            "embedding_version": "",
        }
        for child in chunked.children
    ]
    return parents, children


def test_bm25_child_then_parent_retrieval_reaches_v23_targets(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "evaluation.sqlite3")
    knowledge_base = repository.create_knowledge_base("父子检索离线评测")
    extractor = ExtractionService()
    expected: dict[str, tuple[str, str]] = {}

    for topic in TOPICS:
        content = _document_text(topic).encode("utf-8")
        sha256 = hashlib.sha256(content).hexdigest()
        stored_path = tmp_path / f"{topic.slug}.md"
        stored_path.write_bytes(content)
        document = repository.create_document(
            knowledge_base_id=knowledge_base.id,
            filename=stored_path.name,
            media_type="text/markdown",
            size_bytes=len(content),
            sha256=sha256,
            stored_path=str(stored_path),
        )
        extracted = extractor.extract(stored_path.name, content)
        chunked = chunk_document(extracted, sha256)
        assert len(chunked.parents) >= 2
        assert len(chunked.children) >= 4
        parents, children = _rows(document.id, chunked)
        revision = hashlib.sha256(
            "\x00".join(
                [str(INDEX_SCHEMA_VERSION), sha256, *(item["stable_id"] for item in children)]
            ).encode("utf-8")
        ).hexdigest()
        repository.replace_document_index(
            document_id=document.id,
            knowledge_base_id=knowledge_base.id,
            content=extracted.content,
            index_revision=revision,
            parents=parents,
            children=children,
        )
        command_child = next(item for item in children if topic.command in item["text"])
        for template in QUESTION_TEMPLATES:
            question = template.format(**topic.__dict__)
            expected[question] = (command_child["id"], command_child["parent_id"])

    engine = EmbeddingEngine(
        "disabled-for-bm25-evaluation",
        tmp_path / "models",
        enabled=False,
        local_files_only=True,
    )
    indexes = KnowledgeIndexManager(
        catalog=EmptyCatalog(),  # type: ignore[arg-type]
        repository=repository,
        embedding_engine=engine,
        candidate_k=20,
    )

    parent_hits = 0
    child_hits = 0
    raw_retriever = indexes.get(knowledge_base.id)
    for question, (expected_child_id, expected_parent_id) in expected.items():
        child_ids = [
            hit.topic.source.chunk_id
            for hit in raw_retriever.retrieve(question, top_k=5)
        ]
        parent_ids = [
            hit.topic.source.parent_id
            for hit in indexes.retrieve(knowledge_base.id, question, top_k=3)
        ]
        child_hits += expected_child_id in child_ids
        parent_hits += expected_parent_id in parent_ids

    case_count = len(expected)
    parent_recall = parent_hits / case_count
    child_recall = child_hits / case_count
    print(
        f"v2.3 parent/child evaluation: cases={case_count}, "
        f"parent_recall_at_3={parent_recall:.2%}, child_recall_at_5={child_recall:.2%}"
    )

    assert case_count == 80
    assert parent_recall >= 0.92
    assert child_recall >= 0.90
