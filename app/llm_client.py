from functools import lru_cache

from openai import AsyncOpenAI

from app.config import get_settings


@lru_cache
def get_llm_client() -> AsyncOpenAI:
    settings = get_settings()

    return AsyncOpenAI(
        api_key=settings.aipipe_api_key.get_secret_value(),
        base_url=settings.aipipe_base_url,
        timeout=30.0,
        max_retries=2,
    )
