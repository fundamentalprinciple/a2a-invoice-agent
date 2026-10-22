import secrets

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)


async def authenticate(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    a2a_version: str | None = Header(default=None, alias="A2A-Version"),
    content_type: str | None = Header(default=None, alias="Content-Type"),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    expected = get_settings().a2a_bearer_token.get_secret_value()
    if not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=401, detail="Invalid Bearer token")

    if a2a_version != "1.0":
        raise HTTPException(status_code=400, detail="A2A-Version must be 1.0")

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "application/a2a+json":
        raise HTTPException(status_code=415, detail="Unsupported Content-Type")

    return credentials.credentials
