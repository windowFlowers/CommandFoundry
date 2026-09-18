from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _path_from_env(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser().resolve()


def _allowed_origins() -> list[str]:
    defaults = ["http://127.0.0.1:5173", "http://localhost:5173", "aegis://app"]
    custom = [item.strip() for item in os.getenv("AEGIS_ALLOWED_ORIGINS", "").split(",") if item.strip()]
    return list(dict.fromkeys([*defaults, *custom]))


class Settings(BaseModel):
    app_name: str = "AegisCopilot API"
    app_version: str = "2.9.0"
    environment: str = os.getenv("AEGIS_ENV", "local")
    storage_dir: Path = Field(default_factory=lambda: _path_from_env("AEGIS_STORAGE_DIR", PROJECT_ROOT / "backend" / "storage"))
    knowledge_dir: Path = Field(default_factory=lambda: _path_from_env("AEGIS_KNOWLEDGE_DIR", PROJECT_ROOT / "knowledge"))
    upload_dir: Path = Field(
        default_factory=lambda: _path_from_env(
            "AEGIS_UPLOAD_DIR",
            _path_from_env("AEGIS_STORAGE_DIR", PROJECT_ROOT / "backend" / "storage") / "uploads",
        )
    )
    model_cache_dir: Path = Field(default_factory=lambda: _path_from_env("AEGIS_MODEL_DIR", PROJECT_ROOT / "models" / "cache"))
    embedding_model: str = os.getenv("AEGIS_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
    embedding_enabled: bool = os.getenv("AEGIS_EMBEDDING_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
    embedding_local_only: bool = os.getenv("AEGIS_EMBEDDING_LOCAL_ONLY", "0").strip().lower() in {"1", "true", "yes"}
    llm_provider: str = os.getenv("AEGIS_LLM_PROVIDER", "deepseek" if os.getenv("AEGIS_LLM_API_KEY") else "mock")
    llm_model: str = os.getenv("AEGIS_LLM_MODEL", "deepseek-chat")
    llm_base_url: str = os.getenv("AEGIS_LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_api_key: str = os.getenv("AEGIS_LLM_API_KEY", "")
    llm_timeout_seconds: int = int(os.getenv("AEGIS_LLM_TIMEOUT_SECONDS", "45"))
    retrieval_top_k: int = int(os.getenv("AEGIS_TOP_K", "4"))
    retrieval_candidate_k: int = int(os.getenv("AEGIS_CANDIDATE_K", "20"))
    allowed_origins: list[str] = Field(default_factory=_allowed_origins)


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
