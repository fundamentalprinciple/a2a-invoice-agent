from fastapi.testclient import TestClient

from app.auth import authenticate
from app.main import app


def test_cancel_task_and_reject_second_cancellation():
    app.dependency_overrides[authenticate] = lambda: "pytest-cancel-principal"

    try:
        with TestClient(app) as client:
            created = client.post(
                "/message:send",
                json={
                    "message": {
                        "messageId": "pytest-cancel-" + __import__("uuid").uuid4().hex,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "Cancellation test"}],
                    }
                },
            )
            assert created.status_code == 200
            task_id = created.json()["id"]

            canceled = client.post(f"/tasks/{task_id}:cancel")
            assert canceled.status_code == 200
            assert canceled.json()["status"]["state"] == "TASK_STATE_CANCELED"

            repeated = client.post(f"/tasks/{task_id}:cancel")
            assert repeated.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_cannot_cancel_another_principals_task():
    app.dependency_overrides[authenticate] = lambda: "pytest-owner"

    try:
        with TestClient(app) as client:
            created = client.post(
                "/message:send",
                json={
                    "message": {
                        "messageId": "pytest-cancel-00"
                        + __import__("uuid").uuid4().hex,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "Owner task"}],
                    }
                },
            )
            task_id = created.json()["id"]

            app.dependency_overrides[authenticate] = lambda: "pytest-other"
            response = client.post(f"/tasks/{task_id}:cancel")
            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()
