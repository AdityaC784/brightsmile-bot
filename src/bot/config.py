"""Central settings: secrets from env/.env, tunables from configs/*.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5433/brightsmile"
    groq_api_key: str = ""
    # Optional second Groq key for the generator role (own rate-limit bucket). Falls back to
    # GROQ_API_KEY when empty.
    groq_generator_api_key: str = ""
    gemini_api_key: str = ""
    # Set true if GEMINI_API_KEY is a Vertex AI (express mode) key rather than an AI Studio key.
    gemini_vertexai: bool = False
    admin_api_key: str = "change-me"
    log_level: str = "INFO"
    llm_cache_dir: str = ".cache/llm"
    # Chat-response caching (embeddings are always cached). The eval runner turns this on.
    llm_cache_enabled: bool = False
    tenant_id: str = "default"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_yaml(name: str) -> dict[str, Any]:
    """Load configs/<name>.yaml (cached)."""
    with open(CONFIG_DIR / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)
