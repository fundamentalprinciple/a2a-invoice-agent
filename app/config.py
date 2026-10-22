from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    aipipe_api_key: SecretStr
    aipipe_base_url: str = "https://aipipe.org/openai/v1"
    a2a_bearer_token: SecretStr = SecretStr("")
    public_base_url: str = "https://example.com"
    database_path: str = "./data/agent.db"
    aipipe_model: str = "gpt-4o-mini"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
