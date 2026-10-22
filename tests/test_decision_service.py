
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from openai import APIError

from app.decision import BatchDecisions
from app.decision_service import DecisionServiceError, decide_batch


@pytest.fixture
def valid_batch():
    return {
        "batchId": "batch-001",
        "packages": [
            {
                "packageId": "package-001",
                "documents": [
                    "[invoice:42] Invoice INV-42",
                    "[vendor:7] Vendor Acme Supplies",
                    "[policy:3] Approval required above the delegated limit",
                ],
            }
        ],
    }


@pytest.fixture
def valid_decisions():
    return BatchDecisions.model_validate(
        {
            "batchId": "batch-001",
            "decisions": [
                {
                    "packageId": "package-001",
                    "action": "request_approval",
                    "facts": {
                        "vendorName": "Acme Supplies",
                        "invoiceNumber": "INV-42",
                        "amountMinor": 125000,
                        "currency": "INR",
                    },
                    "evidenceRefs": [
                        "[invoice:42]",
                        "[vendor:7]",
                        "[policy:3]",
                    ],
                    "rationale": (
                        "The request_approval action is required under the supplied policy. "
                        "[invoice:42] and [policy:3] support this decision."
                    ),
                }
            ],
        }
    )


def mock_client(parsed=None, refusal=None, side_effect=None):
    content = parsed.model_dump_json(by_alias=True) if parsed is not None else None
    message = SimpleNamespace(content=content, refusal=refusal)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    mock_create = AsyncMock(return_value=response, side_effect=side_effect)
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=mock_create)
        )
    )
    return client, mock_create


@pytest.mark.asyncio
async def test_decide_batch_returns_proposals(valid_batch, valid_decisions):
    client, mock_create = mock_client(parsed=valid_decisions)

    with (
        patch("app.decision_service.get_llm_client", return_value=client),
        patch("app.decision_service.get_settings") as mock_settings,
    ):
        mock_settings.return_value.aipipe_model = "test-model"
        result = await decide_batch(valid_batch)

    assert result["batchId"] == "batch-001"
    assert len(result["proposals"]) == 1
    proposal = result["proposals"][0]
    assert proposal["packageId"] == "package-001"
    assert proposal["action"] == "request_approval"
    assert len(proposal["actionId"]) >= 12
    mock_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_batch_rejects_model_refusal(valid_batch):
    client, _ = mock_client(refusal="Request refused")

    with (
        patch("app.decision_service.get_llm_client", return_value=client),
        patch("app.decision_service.get_settings") as mock_settings,
    ):
        mock_settings.return_value.aipipe_model = "test-model"

        with pytest.raises(DecisionServiceError, match="refused"):
            await decide_batch(valid_batch)


@pytest.mark.asyncio
async def test_decide_batch_wraps_api_errors(valid_batch):
    error = APIError(message="API unavailable", request=None, body=None)
    client, _ = mock_client(side_effect=error)

    with (
        patch("app.decision_service.get_llm_client", return_value=client),
        patch("app.decision_service.get_settings") as mock_settings,
    ):
        mock_settings.return_value.aipipe_model = "test-model"

        with pytest.raises(DecisionServiceError, match="request failed"):
            await decide_batch(valid_batch)


@pytest.mark.asyncio
async def test_decide_batch_rejects_missing_package_decision(
    valid_batch, valid_decisions
):
    valid_decisions.decisions[0].package_id = "unknown-package"
    client, _ = mock_client(parsed=valid_decisions)

    with (
        patch("app.decision_service.get_llm_client", return_value=client),
        patch("app.decision_service.get_settings") as mock_settings,
    ):
        mock_settings.return_value.aipipe_model = "test-model"

        with pytest.raises(DecisionServiceError, match="exactly one decision"):
            await decide_batch(valid_batch)


@pytest.mark.asyncio
async def test_decide_batch_rejects_unsupported_evidence(
    valid_batch, valid_decisions
):
    valid_decisions.decisions[0].evidence_refs[0] = "[invented:99]"
    client, _ = mock_client(parsed=valid_decisions)

    with (
        patch("app.decision_service.get_llm_client", return_value=client),
        patch("app.decision_service.get_settings") as mock_settings,
    ):
        mock_settings.return_value.aipipe_model = "test-model"

        with pytest.raises(DecisionServiceError, match="unsupported evidence"):
            await decide_batch(valid_batch)

