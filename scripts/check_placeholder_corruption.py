from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".js", ".cjs", ".jsx", ".json", ".md", ".toml", ".yml", ".yaml", ".css", ".html", ".ps1"}
SKIP_PARTS = {".git", ".runtime", ".venv", "node_modules", "dist", "build", "storage", "cache"}


def main() -> int:
    failures: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"{path.relative_to(ROOT)}: not valid UTF-8")
            continue
        if "\ufffd" in text:
            failures.append(f"{path.relative_to(ROOT)}: contains Unicode replacement character")
    if failures:
        print("Repository text-integrity guard failed:")
        print("\n".join(f"- {item}" for item in failures))
        return 1
    print("Repository text-integrity guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
