"""Process configuration.

Every value is environment-driven. Nothing is read from a file at import time
so that the same image can run as API, worker or evaluation harness with only
the environment changing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_DIMENSIONS = 512


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IEP_", extra="ignore")

    database_url: str = "postgresql+psycopg://iep:iep@localhost:5432/iep"

    storage_root: Path = Path("var/objects")
    report_root: Path = Path("var/reports")

    # Ingestion limits. These are enforced before any parser touches the bytes;
    # see docs/threat-model.md for the reasoning behind each ceiling.
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pdf_pages: int = 100
    max_image_pixels: int = 40_000_000
    max_excel_cells: int = 200_000
    max_decompressed_bytes: int = 100 * 1024 * 1024

    worker_poll_seconds: float = 1.0
    job_max_attempts: int = 3
    job_lease_seconds: int = 120

    semantic_provider: str = "deterministic"
    llm_base_url: str = "https://api.anthropic.com"
    llm_model: str = "claude-sonnet-5"
    llm_timeout_seconds: float = 30.0
    llm_max_attempts: int = 2
    llm_api_key: str = ""
    llm_max_output_tokens: int = 1024

    # Retrieval remains available offline. The hashing provider is a
    # deterministic engineering baseline, not a learned semantic model.
    embedding_provider: str = "hashing"
    embedding_dimensions: int = EMBEDDING_DIMENSIONS
    embedding_batch_size: int = Field(default=64, ge=1, le=256)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openai_timeout_seconds: float = Field(default=20.0, gt=0.0, le=120.0)
    openai_max_attempts: int = Field(default=2, ge=1, le=5)
    rag_provider: str = "disabled"
    openai_rag_model: str = "gpt-4o-mini"
    rag_max_output_tokens: int = Field(default=600, ge=100, le=2000)
    rag_max_context_chars: int = Field(default=12_000, ge=1000, le=50_000)
    allow_external_ai: bool = False

    # `rag_provider="cli"` answers through an assistant CLI on the same host
    # instead of a metered API. Development only - see `retrieval/rag_cli.py`
    # for what that costs in exchange.
    rag_cli_tool: str = "claude"
    rag_cli_model: str = ""
    rag_cli_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    # A ceiling this process enforces on itself, counted from the audit trail
    # over a rolling window. It is not the subscription's remaining quota: no
    # API reports that, and inventing a number for it would be worse than
    # saying so. 0 means no ceiling.
    rag_call_budget: int = Field(default=40, ge=0, le=10_000)
    rag_budget_window_days: int = Field(default=7, ge=1, le=90)
    # The fraction of the ceiling at which calls stop rather than warn.
    rag_budget_stop_fraction: float = Field(default=0.90, gt=0.0, le=1.0)

    registry_api_base_url: str = "http://localhost:8080"
    # Development default. A deployment supplies this from the environment;
    # the value here only ever reaches the local source simulator.
    registry_api_token: str = "local-development-token"  # noqa: S105
    registry_api_page_size: int = 25
    registry_api_timeout_seconds: float = 10.0
    registry_api_max_attempts: int = 3
    registry_api_max_bytes: int = 2 * 1024 * 1024

    scraper_allowlist: str = "localhost,127.0.0.1"
    scraper_max_bytes: int = 1024 * 1024
    scraper_timeout_seconds: float = 10.0
    scraper_min_interval_seconds: float = 0.5
    scraper_user_agent: str = "innovation-evidence-pipeline/0.1 (+contact: set-in-deployment)"

    api_key: str = ""
    log_level: str = "INFO"

    ocr_language: str = "spa"
    ocr_min_word_confidence: float = 60.0
    ocr_dpi: int = 300
    ocr_timeout_seconds: float = 30.0

    # Confidence at or below which an extraction is sent to a human instead of
    # being trusted. Raising it trades reviewer time for fewer silent errors.
    review_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)

    @field_validator("semantic_provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        allowed = {"deterministic", "llm"}
        if value not in allowed:
            raise ValueError(f"semantic_provider must be one of {sorted(allowed)}")
        return value

    @field_validator("embedding_provider")
    @classmethod
    def _known_embedding_provider(cls, value: str) -> str:
        allowed = {"disabled", "hashing", "openai"}
        if value not in allowed:
            raise ValueError(f"embedding_provider must be one of {sorted(allowed)}")
        return value

    @field_validator("rag_provider")
    @classmethod
    def _known_rag_provider(cls, value: str) -> str:
        allowed = {"disabled", "openai", "cli"}
        if value not in allowed:
            raise ValueError(f"rag_provider must be one of {sorted(allowed)}")
        return value

    @field_validator("rag_cli_tool")
    @classmethod
    def _known_rag_cli_tool(cls, value: str) -> str:
        allowed = {"claude", "codex"}
        if value not in allowed:
            raise ValueError(f"rag_cli_tool must be one of {sorted(allowed)}")
        return value

    @field_validator("embedding_dimensions")
    @classmethod
    def _schema_embedding_dimensions(cls, value: int) -> int:
        if value != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"embedding_dimensions is fixed at {EMBEDDING_DIMENSIONS} by the database schema"
            )
        return value

    @property
    def scraper_allowed_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.scraper_allowlist.split(",") if h.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
