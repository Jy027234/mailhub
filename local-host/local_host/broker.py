"""Reference-broker wiring for the local host.

The archive's CAPlatform BFF contains the reference MailHub credential broker
(``caplatform_bff.mailhub_credentials``).  It is deliberately reused instead of
forked: the same encrypted-SQLite OAuth state store, token exchange, refresh,
revoke and provider identity checks serve the local B4 walk.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from local_host import _bff  # noqa: F401  (bootstrap apps/bff/src on sys.path)
from local_host.config import HostSettings

from caplatform_bff.mailhub_credentials import (  # noqa: E402
    EncryptedSQLiteMailCredentialBroker,
    ProviderOAuthRegistration,
)

_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def build_broker(
    settings: HostSettings,
    database_path: Path,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EncryptedSQLiteMailCredentialBroker:
    registrations: dict[str, ProviderOAuthRegistration] = {}
    if settings.gmail_enabled:
        registrations["gmail"] = ProviderOAuthRegistration(
            provider="gmail",
            enabled=True,
            client_id=settings.gmail_client_id,
            client_secret=settings.gmail_client_secret,
            redirect_uri=settings.gmail_redirect_uri,
            scopes=tuple(settings.gmail_scopes),
        )
    return EncryptedSQLiteMailCredentialBroker(
        database_path=str(database_path),
        encryption_secret=settings.encryption_secret,
        registrations=registrations,
        transport=transport,
    )


def broker_is_gmail_ready(settings: HostSettings) -> bool:
    return bool(
        settings.gmail_enabled
        and settings.gmail_client_id
        and settings.gmail_client_secret
        and settings.gmail_scopes
        and any(
            scope.casefold() == _GMAIL_READ_SCOPE.casefold()
            for scope in settings.gmail_scopes
        )
    )
