"""Environment-driven configuration. No credentials, endpoints or model ids live in code (D4)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    service_name: str = "ecc-api"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./data/app.db"
    redis_url: str = ""
    celery_eager: bool = False

    admin_api_key: str = ""
    api_keys: str = ""  # comma separated; when set, X-API-Key is required on public endpoints

    # LLM provider (OpenAI-compatible HTTP or the in-process deterministic stub)
    llm_provider: str = "stub_inprocess"  # stub_inprocess | openai_compatible
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_s: float = 20.0
    llm_max_retries: int = 2
    llm_backoff_base_s: float = 0.4
    breaker_failure_threshold: int = 3
    breaker_reset_s: float = 15.0
    price_prompt_per_1k: float = 0.0005  # configured example prices, set to your provider's
    price_completion_per_1k: float = 0.0015

    # agent loop bounds (A1)
    max_agent_steps: int = 12
    max_tool_calls: int = 30
    run_timeout_s: float = 60.0
    tool_timeout_s: float = 5.0
    max_research_attempts: int = 3

    # corpus / retrieval
    embedding_backend: str = "auto"  # auto | st | hashing
    embedding_model: str = ""
    corpus_dir: str = "corpus"
    index_dir: str = "data/index"
    prompt_version: str = "v2"
    retrieval_mode: str = "hybrid"  # hybrid | semantic | lexical

    # cache / rate limit (D7)
    cache_ttl_s: int = 3600
    run_cache_enabled: bool = True
    rate_limit_per_min: int = 30

    verify_enabled: bool = True  # False only for the ablation study in docs/EVALUATION.md; never in production
    live_score_sample_rate: float = 0.25
    otel_exporter_otlp_endpoint: str = ""
    worker_metrics_port: int = 9101

    # stub LLM service
    stub_delay_ms: int = 600
    stub_jitter_ms: int = 200

    slo_p95_seconds: float = 20.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
