from unittest.mock import patch

from app.config import get_settings
from app.llm_client import get_llm_client


def test_llm_client_uses_aipipe_configuration():
    get_llm_client.cache_clear()
    get_settings.cache_clear()

    with patch("app.llm_client.AsyncOpenAI") as mock_client:
        client = get_llm_client()

    settings = get_settings()
    mock_client.assert_called_once_with(
        api_key=settings.aipipe_api_key.get_secret_value(),
        base_url=settings.aipipe_base_url,
        timeout=30.0,
        max_retries=2,
    )
    assert client == mock_client.return_value

    get_llm_client.cache_clear()
