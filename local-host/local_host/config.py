"""Environment-backed settings for the local MailHub host."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True, slots=True)
class HostSettings:
    service_token: str
    encryption_secret: str
    database_path: Path
    mailhub_api_url: str
    gmail_enabled: bool
    gmail_client_id: str
    gmail_client_secret: str
    gmail_redirect_uri: str
    gmail_scopes: tuple[str, ...]
    ai_gateway_url: str | None
    ai_gateway_model: str
    ai_gateway_api_key: str

    @property
    def ai_gateway_enabled(self) -> bool:
        return bool(
            self.ai_gateway_url and self.ai_gateway_model and self.ai_gateway_api_key
        )

    @classmethod
    def from_env(cls) -> HostSettings:
        settings = cls(
            service_token=os.getenv("MAILHUB_HOST_SERVICE_TOKEN", ""),
            encryption_secret=os.getenv("HOST_ENCRYPTION_SECRET", ""),
            database_path=Path(os.getenv("HOST_DATABASE_PATH", "./data/local_host.db")),
            mailhub_api_url=os.getenv("HOST_MAILHUB_API_URL", "http://127.0.0.1:8000"),
            gmail_enabled=_env_bool("HOST_GMAIL_ENABLED", default=False),
            gmail_client_id=os.getenv("HOST_GMAIL_CLIENT_ID", ""),
            gmail_client_secret=os.getenv("HOST_GMAIL_CLIENT_SECRET", ""),
            gmail_redirect_uri=os.getenv(
                "HOST_GMAIL_REDIRECT_URI", "http://127.0.0.1:8090/oauth/gmail/callback"
            ),
            gmail_scopes=_env_tuple(
                "HOST_GMAIL_SCOPES", "https://www.googleapis.com/auth/gmail.readonly"
            ),
            ai_gateway_url=os.getenv("HOST_AI_GATEWAY_URL") or None,
            ai_gateway_model=os.getenv("HOST_AI_GATEWAY_MODEL", ""),
            ai_gateway_api_key=os.getenv("HOST_AI_GATEWAY_API_KEY", ""),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if len(self.service_token) < 32 or any(
            ord(char) < 33 or ord(char) == 127 for char in self.service_token
        ):
            raise ValueError(
                "MAILHUB_HOST_SERVICE_TOKEN must be at least 32 printable characters"
            )
        if len(self.encryption_secret) < 32:
            raise ValueError("HOST_ENCRYPTION_SECRET must be at least 32 characters")
        parsed = urlsplit(self.mailhub_api_url)
        local_http = (
            parsed.scheme == "http"
            and (parsed.hostname or "").casefold() in _LOCAL_HOSTS
        )
        if not parsed.hostname or (parsed.scheme != "https" and not local_http):
            raise ValueError("HOST_MAILHUB_API_URL must be an https or loopback URL")
        if self.gmail_enabled and (
            not self.gmail_client_id or not self.gmail_client_secret
        ):
            raise ValueError(
                "HOST_GMAIL_ENABLED requires HOST_GMAIL_CLIENT_ID and HOST_GMAIL_CLIENT_SECRET"
            )
        if self.ai_gateway_url:
            parsed_ai = urlsplit(self.ai_gateway_url)
            ai_local_http = (
                parsed_ai.scheme == "http"
                and (parsed_ai.hostname or "").casefold() in _LOCAL_HOSTS
            )
            if not parsed_ai.hostname or (
                parsed_ai.scheme != "https" and not ai_local_http
            ):
                raise ValueError("HOST_AI_GATEWAY_URL must be an https or loopback URL")
            if parsed_ai.query or parsed_ai.fragment:
                raise ValueError("HOST_AI_GATEWAY_URL must not carry query or fragment")
            if not self.ai_gateway_model or len(self.ai_gateway_model) > 200:
                raise ValueError("HOST_AI_GATEWAY_MODEL invalid")
            if not self.ai_gateway_api_key:
                raise ValueError("HOST_AI_GATEWAY_API_KEY required with gateway URL")


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}_invalid_boolean")


def _env_tuple(name: str, default: str) -> tuple[str, ...]:
    value = os.getenv(name, default)
    return tuple(
        dict.fromkeys(item for item in value.replace(",", " ").split() if item)
    )
