from __future__ import annotations

import asyncio

import pytest

from mailhub.config import MailHubSettings
from mailhub.domain import ProviderName
from mailhub.hosts.http import HttpCredentialBrokerAdapter, HttpHostIdentityAdapter
from mailhub.persistence.sqlalchemy import SqlAlchemyMailRepository
from mailhub.runtime import create_durable_app


def _production_settings(**updates: object) -> MailHubSettings:
    values: dict[str, object] = {
        "environment": "staging",
        "database_url": "postgresql+asyncpg://mailhub:secret@db.test:5432/mailhub",
        "object_store_endpoint": "https://host.test",
        "kms_key_ref": "kms://mailhub/test",
        "av_scanner_endpoint": "https://host.test",
        "dlp_endpoint": "https://host.test",
        "credential_broker_endpoint": "https://host.test",
        "host_service_token": "h" * 32,
        "host_identity_endpoint": "https://host.test",
        "approval_endpoint": "https://host.test",
        "ai_execution_endpoint": "https://host.test",
        "host_action_endpoint": "https://host.test",
        "knowledge_endpoint": "https://host.test",
        "telemetry_endpoint": "https://host.test",
        "event_publisher_endpoint": "https://host.test",
        "quota_endpoint": "https://host.test",
        "allow_sandbox": False,
    }
    values.update(updates)
    return MailHubSettings.model_validate(values)


def test_durable_runtime_uses_postgres_and_authenticated_host_ports() -> None:
    app = create_durable_app(_production_settings())
    service = app.state.mail_service
    repository = app.state.mail_repository

    assert isinstance(repository, SqlAlchemyMailRepository)
    assert service.repository is repository
    assert service.connectors == {}
    assert isinstance(service.credential_broker, HttpCredentialBrokerAdapter)
    assert service.credential_broker.headers == {"Authorization": f"Bearer {'h' * 32}"}
    assert isinstance(service.host_identity, HttpHostIdentityAdapter)
    assert service.host_identity.headers == {"Authorization": f"Bearer {'h' * 32}"}
    asyncio.run(repository.dispose())


def test_durable_runtime_registers_only_explicit_real_provider() -> None:
    app = create_durable_app(
        _production_settings(
            gmail_enabled=True,
            gmail_client_id="gmail-client",
            gmail_authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            gmail_redirect_uris=("https://app.example.test/mail/oauth/gmail/callback",),
            gmail_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            oauth_state_signing_secret="s" * 32,
        )
    )
    service = app.state.mail_service
    repository = app.state.mail_repository

    assert set(service.connectors) == {ProviderName.GMAIL}
    assert ProviderName.SANDBOX not in service.connectors
    asyncio.run(repository.dispose())


def test_durable_runtime_fails_closed_outside_production_like_mode() -> None:
    with pytest.raises(
        RuntimeError,
        match="mailhub_durable_runtime_requires_production_like_environment",
    ):
        create_durable_app(MailHubSettings())


def test_durable_runtime_fails_closed_when_oauth_host_boundary_is_incomplete() -> None:
    with pytest.raises(RuntimeError, match="mailhub_oauth_host_boundary_not_ready"):
        create_durable_app(_production_settings(gmail_enabled=True))
