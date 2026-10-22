"""Runtime settings, read from environment variables or a local .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://rag:rag@localhost:5432/rag"
    # Restricted role (row-level security enforced) used by the API and ingestion
    app_database_url: str = "postgresql://rag_app:rag_app@localhost:5432/rag"
    ollama_host: str = "http://localhost:11434"
    llm_model: str = "llama3.1:8b"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    gmail_credentials_path: str = "secrets/credentials.json"
    token_dir: str = "secrets"

    # "local": HS256 bearer tokens issued by `rag token` (dev/demo, no Google needed)
    # "google": Google OpenID Connect ID tokens
    auth_mode: str = "local"
    local_jwt_secret: str = ""
    google_client_id: str = ""  # defaults to the client id inside credentials.json
    token_ttl_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
