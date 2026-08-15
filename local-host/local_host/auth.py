"""Constant-time service-token authentication for the local host."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, Header, HTTPException, status


def make_service_auth(service_token: str) -> Any:
    """Return a FastAPI dependency enforcing the shared host service token."""

    def require_service_token(authorization: str | None = Header(default=None)) -> None:
        if authorization is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "host_service_token_missing"},
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "host_service_token_malformed"},
            )
        if not hmac.compare_digest(token.encode("utf-8"), service_token.encode("utf-8")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "host_service_token_invalid"},
            )

    return Depends(require_service_token)
