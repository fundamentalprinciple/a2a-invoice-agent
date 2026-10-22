from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from openai import APIError

from app.decision import InvoiceDecision
from app.decision_service import DecisionServiceError, decide_invoice


@pytest.fixture
def valid_decision():
    return InvoiceDecision(
        action="request_approval",
        evidence_refs=["[invoice:42]", "[vendor:7]", "[policy:3]"],
        rationale=(
            "The invoice requires approval under the supplied policy. "
            "[invoice:42] and [policy:3] support this decision."
        ),
    )


@pytest.mark.asyncio
async def test_decide_invoice_returns_parsed_decision(valid_decision):
    parsed_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(parsed=valid_decision, refusal=None)
            )
        ]
    )

    mock_parse = AsyncMock(return_value=parsed_response)
    mock_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=mock_parse))
    )

    with (
        patch("app.decision_service.get_llm_client", return_value=mock_client),
        patch("app.decision_service.get_settings") as mock_settings,
    ):
        mock_settings.return_value.aipipe_model = "test-model"

        result = await decide_invoice({"invoice_id": "42"})

    assert result == valid_decision
    mock_parse.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_invoice_rejects_model_refusal():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(parsed=None, refusal="Request refused")
            )
        ]
    )
    mock_parse = AsyncMock(return_value=response)
    mock_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=mock_parse))
    )

    with (
        patch("app.decision_service.get_llm_client", return_value=mock_client),
        patch("app.decision_service.get_settings") as mock_settings,
    ):
        mock_settings.return_value.aipipe_model = "test-model"

        with pytest.raises(DecisionServiceError, match="refused"):
            await decide_invoice({"invoice_id": "42"})


@pytest.mark.asyncio
async def test_decide_invoice_wraps_api_errors():
    error = APIError(
        message="API unavailable",
        request=None,
        body=None,
    )
    mock_parse = AsyncMock(side_effect=error)
    mock_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=mock_parse))
    )

    with (
        patch("app.decision_service.get_llm_client", return_value=mock_client),
        patch("app.decision_service.get_settings") as mock_settings,
    ):
        mock_settings.return_value.aipipe_model = "test-model"

        with pytest.raises(DecisionServiceError, match="request failed"):
            await decide_invoice({"invoice_id": "42"})
