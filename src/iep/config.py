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

# Where to put the relevance floor for a learned embedding model: nowhere.
#
# This number was measured three times and was wrong twice, which is the whole
# finding. Top-hit cosine similarity on INN-2025-042, widening the query set
# each time:
#
#   8 queries   bge-m3              relevant 0.5365+, irrelevant 0.4376-  ->  gap +0.099
#   13 queries  qwen3-embedding:4b  relevant 0.5649+, irrelevant 0.5671-  ->  gap -0.002
#   17 queries  qwen3-embedding:4b  relevant 0.4968+, irrelevant 0.5671-  ->  gap -0.070
#
# Every widening lowered the worst relevant score and the overlap grew. A
# threshold set from any one of those samples cuts a legitimate question asked
# slightly differently, and 0.50 - fitted to the second - did exactly that:
# "¿Cuántas personas tienen dedicación al proyecto?" scores 0.4968 and came
# back empty, while two irrelevant queries sailed over it.
#
# So the floor ships off, and the asymmetry is the reason. Noise reaching the
# generator is recoverable: it answers "the evidence does not support this",
# which is what it did. Evidence removed before the generator sees it is not
# recoverable by anything - it cannot report an absence it was never shown.
#
# The knob remains for somebody who has measured their own corpus and their
# own queries. `docs/rag.md` carries all three tables, including the two that
# were wrong.
LEARNED_MIN_SIMILARITY = 0.0


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
    # Cosine similarity below which a vector hit is not returned at all.
    #
    # `None` means "whatever suits the configured provider". Measured, that is
    # zero for every provider available here - see `LEARNED_MIN_SIMILARITY`
    # for the three measurements that led there. The setting exists so a
    # deployment that has measured its own corpus can turn it on, and because
    # the filter itself is worth having even when its default is off: without
    # it, vector search has no way to answer "nothing here matches".
    retrieval_min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
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
    # A local Ollama server. The one backend here that is genuinely local -
    # nothing leaves the machine - and the only one whose reply shape the
    # server can enforce with a JSON schema.
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = ""
    # A learned multilingual embedding model, locally. This is what turns the
    # vector path from a demonstration of the plumbing into actual semantic
    # retrieval - `bge-m3` is 1024 dimensions and the column no longer fixes
    # a width, so it just works. See docs/rag.md for the measurements.
    # `qwen3-embedding:4b` rather than `bge-m3`: measured on this corpus it
    # scores every relevant query higher and orders them better - worst
    # relevant 0.5649 against 0.4480 - for 2.5 GB and about half a second more
    # per batch. `bge-m3` stays a good smaller choice.
    ollama_embedding_model: str = "qwen3-embedding:4b"
    # A model on a laptop CPU takes tens of seconds for a five-segment
    # context, so this is not the CLI's timeout.
    ollama_timeout_seconds: float = Field(default=300.0, gt=0.0, le=900.0)
    # How much context to ask Ollama for. Sent explicitly because the default
    # is the model's maximum, and on `qwen3.5:9b` that is 262144 tokens - a KV
    # cache that does not fit in 16 GB alongside the weights, so it spills and
    # every token is then paid for at host-memory speed. Measured on this
    # machine, same request, same 68 output tokens:
    #
    #   num_ctx default (262144)   ~35 s of generation, 14.0 GB of VRAM
    #   num_ctx 8192                ~1.2 s of generation,  5.7 GB of VRAM
    #
    # A copilot answer took 97-151 seconds because of this, which reads as a
    # hang and is why nobody waited for the answer.
    #
    # 8192 is sized from the prompt this system actually sends, not picked for
    # roundness: `rag_max_context_chars` is 12000 characters, and Spanish runs
    # about 3.5 characters per token, so evidence is ~3400 tokens, the system
    # prompt ~500, the answer up to `rag_max_output_tokens`. That is under 4600
    # with the question included, and the remainder is headroom.
    ollama_num_ctx: int = Field(default=8192, ge=2048, le=131_072)

    # Where to find the browser that renders the report to PDF. Empty means
    # "look on the PATH", which is what the image relies on - it installs
    # `chromium`. A developer running the API on Windows has Chrome in
    # `C:\Program Files\...` and not on the PATH, so without this the endpoint
    # is unavailable on the one machine where somebody is most likely to try
    # it.
    pdf_renderer: str = ""
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
        allowed = {"disabled", "hashing", "openai", "ollama"}
        if value not in allowed:
            raise ValueError(f"embedding_provider must be one of {sorted(allowed)}")
        return value

    @field_validator("rag_provider")
    @classmethod
    def _known_rag_provider(cls, value: str) -> str:
        allowed = {"disabled", "openai", "cli", "ollama"}
        if value not in allowed:
            raise ValueError(f"rag_provider must be one of {sorted(allowed)}")
        return value

    @field_validator("retrieval_min_similarity", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """An empty environment variable means "not set", not "not a number".

        Environment variables are strings, and `""` is how "unset" arrives
        from Compose, from a `.env` line with nothing after the `=`, and from
        a shell that exports an empty value. Without this the process refused
        to start at all: `IEP_RETRIEVAL_MIN_SIMILARITY=` made the API
        container exit before the first request, which is a worse failure than
        anything the setting could cause.
        """
        if isinstance(value, str) and not value.strip():
            return None
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
    def _baseline_embedding_dimensions(cls, value: int) -> int:
        """Only the hashing baseline takes its width from configuration.

        It used to be refused unless it equalled the column's fixed 512. The
        column has no fixed width now, and a learned model's width is a
        property of the model rather than a setting - so this bounds the one
        provider that has a choice, and the rest report their own.
        """
        if not 64 <= value <= 4096:
            raise ValueError("embedding_dimensions must be between 64 and 4096")
        return value

    @property
    def effective_min_similarity(self) -> float:
        """The floor to apply, resolved against the provider in use.

        Set explicitly, it is honoured. Unset, it is 0.0 for the baseline -
        whose scores do not separate anything, so any threshold would discard
        real evidence - and `LEARNED_MIN_SIMILARITY` for a learned model.

        Having one global default was the trap: a number that protects a
        learned model destroys the baseline, and a number safe for the
        baseline does nothing at all.
        """
        if self.retrieval_min_similarity is not None:
            return self.retrieval_min_similarity
        if self.embedding_provider in ("hashing", "disabled"):
            return 0.0
        return LEARNED_MIN_SIMILARITY

    @property
    def scraper_allowed_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.scraper_allowlist.split(",") if h.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
