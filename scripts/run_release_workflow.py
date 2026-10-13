"""Run the repeatable CommandFoundry release workflow.

The workflow is deliberately explicit about the expensive boundary:

    offline checks -> real provider evaluation -> release gate -> Windows package

This module never reads a key from a file and never prints environment values.
Inject ``AEGIS_LLM_API_KEY`` in the calling process (or use a CI secret).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


def _resolve(path: Path | None) -> Path | None:
    if path is None:
        return None
    return path if path.is_absolute() else ROOT / path


def _run(label: str, command: Sequence[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print(f"[release] {label}")
    subprocess.run(list(command), cwd=cwd, env=env, check=True)


def _git_status() -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_baseline(path: Path | None, *, allow_new: bool, label: str) -> None:
    if path is None:
        if not allow_new:
            raise SystemExit(f"{label} baseline is required; use --allow-new-baseline only to create the first one")
        return
    if not path.is_file():
        raise SystemExit(f"{label} baseline does not exist: {path}")


def _report_path(output_dir: Path, name: str) -> Path:
    report_dir = output_dir / name
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir / "evaluation-report.json"


def _verify_report(report: Path, baseline: Path | None, *, mode: str) -> None:
    command = [str(PYTHON), str(ROOT / "scripts" / "check_release_evaluation.py"), str(report)]
    command.extend(["--require-mode", mode])
    if baseline is not None:
        command.extend(["--baseline", str(baseline), "--require-baseline"])
    _run("verify report identity and release gates", command)


def _offline(args: argparse.Namespace, output_dir: Path) -> Path:
    _run(
        "validate evaluation fixtures",
        [str(PYTHON), str(ROOT / "scripts" / "validate_evaluation_data.py")],
    )
    _run(
        "scan placeholder corruption",
        [str(PYTHON), str(ROOT / "scripts" / "check_placeholder_corruption.py")],
    )
    _run("backend tests", [str(PYTHON), "-m", "pytest", "-q"], cwd=ROOT / "backend")
    _run("frontend tests", ["npm.cmd" if platform.system() == "Windows" else "npm", "test"], cwd=ROOT / "frontend")
    _run("frontend production build", ["npm.cmd" if platform.system() == "Windows" else "npm", "run", "build"], cwd=ROOT / "frontend")
    _run("desktop tests", ["npm.cmd" if platform.system() == "Windows" else "npm", "test"], cwd=ROOT / "desktop")

    report = _report_path(output_dir, "offline")
    command = [
        str(PYTHON),
        str(ROOT / "scripts" / "run_evaluation.py"),
        "--suite",
        "all",
        "--mode",
        "offline",
        "--output",
        str(report),
        "--redact",
        "--fail-on-gate",
    ]
    if args.retrieval_profile == "hybrid":
        command.append("--hybrid")
        if args.allow_model_download:
            command.append("--allow-model-download")
    if args.offline_baseline is not None:
        command.extend(["--baseline", str(args.offline_baseline)])
    _run("offline RAG evaluation", command)
    _verify_report(report, args.offline_baseline, mode="offline")
    return report


def _provider_requires_key() -> bool:
    provider = os.getenv("AEGIS_LLM_PROVIDER", "").strip().lower()
    if not provider:
        provider = "deepseek" if os.getenv("AEGIS_LLM_API_KEY", "").strip() else "mock"
    if provider == "ollama":
        return False
    if provider == "mock":
        raise SystemExit(
            "Real model evaluation is disabled because AEGIS_LLM_PROVIDER resolves to mock. "
            "Set the intended provider and inject its API key."
        )
    if not os.getenv("AEGIS_LLM_API_KEY", "").strip():
        raise SystemExit(
            "Real model evaluation requires AEGIS_LLM_API_KEY. "
            "Set it in the current process or CI secret; do not commit it."
        )
    return True


def _model(args: argparse.Namespace, output_dir: Path) -> Path:
    _provider_requires_key()
    env = os.environ.copy()
    env.setdefault("AEGIS_EMBEDDING_ENABLED", "1")
    env.setdefault("AEGIS_EMBEDDING_LOCAL_ONLY", "0" if args.allow_model_download else "1")
    env["AEGIS_RERANKER_ENABLED"] = "1" if args.reranker else "0"
    # Model evaluation must be reproducible and must not read the developer's
    # real conversation/upload database.  The release candidate evaluates the
    # bundled knowledge snapshot in an isolated workspace, even when the
    # parent process has AEGIS_STORAGE_DIR configured for the desktop app.
    env["AEGIS_STORAGE_DIR"] = str(output_dir / "model-storage")

    report = _report_path(output_dir, "model")
    command = [
        str(PYTHON),
        str(ROOT / "scripts" / "run_evaluation.py"),
        "--suite",
        "all",
        "--mode",
        "model",
        "--provider",
        "current",
        "--output",
        str(report),
        "--redact",
        "--fail-on-gate",
    ]
    if args.retrieval_profile == "hybrid":
        command.append("--hybrid")
        if args.allow_model_download:
            command.append("--allow-model-download")
    if args.model_baseline is not None:
        command.extend(["--baseline", str(args.model_baseline)])
    _run("real provider RAG evaluation (API cost may apply)", command, env=env)
    _verify_report(report, args.model_baseline, mode="model")
    return report


def _package(args: argparse.Namespace, output_dir: Path) -> Path:
    if platform.system() != "Windows":
        raise SystemExit("Windows installer packaging must run on Windows")
    if args.model_report is None or not args.model_report.is_file():
        raise SystemExit("--model-report must point to a passing model evaluation report")
    if args.model_baseline is None:
        raise SystemExit("--model-baseline is required before packaging")
    _verify_report(args.model_report, args.model_baseline, mode="model")
    _run(
        "build signed-off Windows installer",
        ["npm.cmd", "run", "dist:win"],
        cwd=ROOT / "desktop",
    )
    installers = sorted((ROOT / "desktop" / "dist").glob("*.exe"), key=lambda item: item.stat().st_size, reverse=True)
    if not installers:
        raise SystemExit("electron-builder completed but no .exe installer was found")
    installer = installers[0]
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "git_revision": _git_revision(),
        "evaluation_report": str(args.model_report),
        "evaluation_report_sha256": _sha256(args.model_report),
        "installer": str(installer),
        "installer_sha256": _sha256(installer),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output_dir / "SHA256SUMS.txt").write_text(
        f"{manifest['installer_sha256']}  {installer.name}\n",
        encoding="utf-8",
    )
    print(f"[release] installer: {installer}")
    print(f"[release] sha256: {manifest['installer_sha256']}")
    return installer


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the fixed CommandFoundry release workflow")
    parser.add_argument("phase", choices=("offline", "model", "package", "all"))
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/release-workflow"))
    parser.add_argument("--offline-baseline", type=Path)
    parser.add_argument("--model-baseline", type=Path)
    parser.add_argument("--model-report", type=Path)
    parser.add_argument("--retrieval-profile", choices=("bm25", "hybrid"), default="hybrid")
    parser.add_argument("--allow-model-download", action="store_true")
    parser.add_argument("--reranker", action="store_true")
    parser.add_argument("--allow-new-baseline", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow local changes; never use for a release")
    parser.add_argument("--skip-package", action="store_true", help="Run all evaluation gates but do not build an installer")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.offline_baseline = _resolve(args.offline_baseline)
    args.model_baseline = _resolve(args.model_baseline)
    args.model_report = _resolve(args.model_report)
    args.output_dir = _resolve(args.output_dir) or ROOT / ".runtime" / "release-workflow"
    if not args.allow_dirty and _git_status():
        raise SystemExit("working tree is dirty; commit the release candidate or pass --allow-dirty for local checks")
    if args.phase in {"offline", "all"}:
        _check_baseline(args.offline_baseline, allow_new=args.allow_new_baseline, label="offline")
    if args.phase in {"model", "all"}:
        _check_baseline(args.model_baseline, allow_new=args.allow_new_baseline, label="model")
    if args.phase == "package" and args.model_baseline is None:
        raise SystemExit("model baseline is required before packaging")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.phase == "offline":
        _offline(args, args.output_dir)
    elif args.phase == "model":
        _model(args, args.output_dir)
    elif args.phase == "package":
        _package(args, args.output_dir)
    else:
        _offline(args, args.output_dir / "offline")
        report = _model(args, args.output_dir / "model")
        if not args.skip_package:
            args.model_report = report
            _package(args, args.output_dir / "package")
    print("[release] workflow completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
