from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "RenderBrain"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost:5432/renderbrain"
    REDIS_URL: str = "redis://localhost:6379/0"

    EVENT_BUS_STREAM: str = "renderbrain:events"
    WORKER_GROUP_NAME: str = "renderbrain:workers"

    # ---------------------------------------------------------------------------
    # Apify — proveedor externo de scraping (infraestructura, no dominio)
    # APIFY_API_TOKEN: token de autenticación; tipado como SecretStr para que
    #   Pydantic nunca lo exponga en repr(), logs ni mensajes de excepción.
    # APIFY_INSTAGRAM_ACTOR_ID: ID del Actor público de Apify para Instagram.
    #   Actor oficial: apify/instagram-scraper (versión pública sin login).
    # ---------------------------------------------------------------------------
    APIFY_API_TOKEN: SecretStr | None = None
    APIFY_INSTAGRAM_ACTOR_ID: str = "apify/instagram-scraper"

    # ---------------------------------------------------------------------------
    # OpenAI — proveedor de LLM (S3.2)
    # OPENAI_API_KEY: token de autenticación tipado como SecretStr.
    # OPENAI_MODEL: modelo a utilizar (por defecto un modelo rápido y económico).
    # ---------------------------------------------------------------------------
    OPENAI_API_KEY: SecretStr | None = None
    OPENAI_MODEL: str = "gpt-4o-mini"

    # ---------------------------------------------------------------------------
    # Dashboard Auth (S6.3)
    # Autenticación administrativa para proteger la API y el Dashboard.
    # ---------------------------------------------------------------------------
    RENDERBRAIN_ADMIN_USERNAME: str | None = None
    RENDERBRAIN_ADMIN_PASSWORD: SecretStr | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()

def validate_api_settings() -> None:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required for API.")
    if not settings.RENDERBRAIN_ADMIN_USERNAME or not settings.RENDERBRAIN_ADMIN_PASSWORD:
        raise RuntimeError("RENDERBRAIN_ADMIN_USERNAME and RENDERBRAIN_ADMIN_PASSWORD are required for API.")

def validate_scheduler_settings() -> None:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required for Scheduler.")
    if not settings.REDIS_URL:
        raise RuntimeError("REDIS_URL is required for Scheduler.")
    if not settings.APIFY_API_TOKEN:
        raise RuntimeError("APIFY_API_TOKEN is required for Scheduler.")

def validate_worker_settings() -> None:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required for Worker.")
    if not settings.REDIS_URL:
        raise RuntimeError("REDIS_URL is required for Worker.")
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is required for Worker.")
