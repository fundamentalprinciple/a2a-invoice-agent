from fastapi.testclient import TestClient

from app.main import app


def test_wrong_protocol_version_rejected():
    with TestClient(app) as client:
        response = client.get(
            "/tasks",
            headers={
                "Authorization": "Bearer test-token",
                "A2A-Version": "2.0",
                "Content-Type": "application/a2a+json",
            },
        )
        assert response.status_code == 400


def test_agent_card_is_public():
    with TestClient(app) as client:
        response = client.get("/.well-known/agent-card.json")
        assert response.status_code == 200
        assert response.json()["name"] == "A2A Invoice Agent"


def test_wrong_content_type_rejected():
    with TestClient(app) as client:
        response = client.post(
            "/message:send",
            headers={
                "Authorization": "Bearer test-token",
                "A2A-Version": "1.0",
                "Content-Type": "application/json",
            },
            json={
                "message": {
                    "messageId": "pytest-protocol-001",
                    "role": "ROLE_USER",
                    "parts": [{"kind": "text", "text": "Test"}],
                }
            },
        )
        assert response.status_code == 415
