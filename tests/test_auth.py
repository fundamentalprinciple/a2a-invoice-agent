from fastapi.testclient import TestClient

from app.main import app


def test_health_is_public():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_protected_endpoint_rejects_missing_token():
    with TestClient(app) as client:
        response = client.get(
            "/tasks",
            headers={
                "A2A-Version": "1.0",
                "Content-Type": "application/a2a+json",
            },
        )
        assert response.status_code == 401
