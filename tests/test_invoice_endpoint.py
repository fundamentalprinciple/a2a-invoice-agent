from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.auth import authenticate
from app.config import get_settings
from app.main import app


def test_invoice_batch_proposal_and_replay():
    with TemporaryDirectory() as temp_dir:
        settings = get_settings()
        old_path = settings.database_path
        settings.database_path = str(Path(temp_dir) / "test.db")
        app.dependency_overrides[authenticate] = lambda: "invoice-test-user"

        proposals = {
            "batchId": "batch-001",
            "proposals": [{
                "packageId": "package-001",
                "actionId": "action-identifier-123",
                "action": "request_approval",
                "facts": {
                    "vendorName": "Acme",
                    "invoiceNumber": "INV-42",
                    "amountMinor": 125000,
                    "currency": "INR",
                },
                "evidenceRefs": ["[invoice:42]", "[vendor:7]", "[policy:3]"],
                "rationale": (
                    "Approval is required under policy. "
                    "[invoice:42] and [policy:3] support this action."
                ),
            }],
        }

        try:
            with (
                patch("app.main.decide_batch", new_callable=AsyncMock) as decide,
                TestClient(app) as client,
            ):
                decide.return_value = proposals
                message = {
                    "messageId": "invoice-message-001",
                    "role": "ROLE_USER",
                    "parts": [{
                        "mediaType": "application/vnd.ga5.invoice-claim-batch+json",
                        "data": {
                            "batchId": "batch-001",
                            "packages": [{
                                "packageId": "package-001",
                                "documents": ["[invoice:42]", "[vendor:7]", "[policy:3]"],
                            }],
                        },
                    }],
                }
                first = client.post("/message:send", json={"message": message})
                second = client.post("/message:send", json={"message": message})

                assert first.status_code == 200, first.text
                assert second.status_code == 200, second.text
                assert first.json()["id"] == second.json()["id"]
                artifact = first.json()["artifacts"][0]
                assert artifact["parts"][0]["mediaType"] == (
                    "application/vnd.ga5.invoice-action-proposals+json"
                )
                assert artifact["parts"][0]["data"] == proposals
                decide.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()
            settings.database_path = old_path

def test_failed_decision_does_not_leave_replayable_incomplete_task():
    from fastapi import HTTPException
    from app.decision_service import DecisionServiceError

    with TemporaryDirectory() as temp_dir:
        settings = get_settings()
        old_path = settings.database_path
        settings.database_path = str(Path(temp_dir) / "failure.db")
        app.dependency_overrides[authenticate] = lambda: "invoice-failure-user"

        message = {
            "messageId": "failed-invoice-message",
            "role": "ROLE_USER",
            "parts": [{
                "mediaType": "application/vnd.ga5.invoice-claim-batch+json",
                "data": {
                    "batchId": "batch-failure",
                    "packages": [{"packageId": "p-1", "documents": ["[invoice:1]"]}],
                },
            }],
        }

        try:
            with (
                patch(
                    "app.main.decide_batch",
                    new_callable=AsyncMock,
                    side_effect=DecisionServiceError("temporary failure"),
                ) as decide,
                TestClient(app, raise_server_exceptions=False) as client,
            ):
                first = client.post("/message:send", json={"message": message})
                second = client.post("/message:send", json={"message": message})

                assert first.status_code == 502
                assert second.status_code == 502
                assert decide.await_count == 2
        finally:
            app.dependency_overrides.clear()
            settings.database_path = old_path
