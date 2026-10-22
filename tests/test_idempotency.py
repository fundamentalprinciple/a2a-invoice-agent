from fastapi.testclient import TestClient

from app.auth import authenticate
from app.main import app


def test_same_message_returns_same_task():
    app.dependency_overrides[authenticate] = lambda: "pytest-principal"

    try:
        with TestClient(app) as client:
            message = {
                "messageId": "pytest-idempotency-001",
                "role": "user",
                "parts": [{"kind": "text", "text": "Test invoice"}],
            }
            first = client.post("/message:send", json={"message": message})
            second = client.post("/message:send", json={"message": message})

            assert first.status_code == 200
            assert second.status_code == 200
            assert first.json()["id"] == second.json()["id"]
    finally:
        app.dependency_overrides.clear()


def test_reusing_message_id_with_different_content_conflicts():
    app.dependency_overrides[authenticate] = lambda: "pytest-principal"

    try:
        with TestClient(app) as client:
            first_message = {
                "messageId": "pytest-idempotency-002",
                "role": "user",
                "parts": [{"kind": "text", "text": "Invoice A"}],
            }
            changed_message = {
                **first_message,
                "parts": [{"kind": "text", "text": "Invoice B"}],
            }

            first = client.post("/message:send", json={"message": first_message})
            second = client.post("/message:send", json={"message": changed_message})

            assert first.status_code == 200
            assert second.status_code == 409
            assert second.json()["detail"] == "IDEMPOTENCY_CONFLICT"
    finally:
        app.dependency_overrides.clear()
