from fastapi.testclient import TestClient
from tempfile import TemporaryDirectory
from pathlib import Path

from app.auth import authenticate
from app.config import get_settings
from app.main import app


def run_with_fresh_database(test_function):
    with TemporaryDirectory() as temp_dir:
        settings = get_settings()
        original_path = settings.database_path
        settings.database_path = str(Path(temp_dir) / "test.db")
        app.dependency_overrides[authenticate] = lambda: "pytest-principal"

        try:
            test_function()
        finally:
            app.dependency_overrides.clear()
            settings.database_path = original_path


def test_same_message_returns_same_task():
    def check():
        with TestClient(app) as client:
            message = {
                "messageId": "pytest-idempotency-001",
                "role": "ROLE_USER",
                "parts": [{"kind": "text", "text": "Test invoice"}],
            }
            first = client.post("/message:send", json={"message": message})
            second = client.post("/message:send", json={"message": message})

            assert first.status_code == 200
            assert second.status_code == 200
            assert first.json()["id"] == second.json()["id"]

    run_with_fresh_database(check)


def test_reusing_message_id_with_different_content_conflicts():
    def check():
        with TestClient(app) as client:
            first_message = {
                "messageId": "pytest-idempotency-002",
                "role": "ROLE_USER",
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

    run_with_fresh_database(check)

