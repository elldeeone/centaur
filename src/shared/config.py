from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Base settings shared by API and ETL services."""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # Database
    database_url: str = Field(alias="DATABASE_URL")

    # Embeddings
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # API
    api_secret_key: str = Field(default="", alias="API_SECRET_KEY")
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["*"]

    # UI password for the thread viewer (set UI_PASSWORD env var)
    ui_password: str = Field(default="", alias="UI_PASSWORD")



settings = Settings()
