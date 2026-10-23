from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    parser_interval_seconds: int = 300
    playwright_timeout_ms: int = 45000
    playwright_headless: bool = True
    sid_cache_ttl_seconds: int = 600
    log_level: str = "INFO"
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0


settings = Settings()
