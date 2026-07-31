from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from caplatform_bff.config import Settings
from caplatform_bff.mailhub_credentials import (
    CredentialRefresh,
    CredentialResolve,
    EncryptedSQLiteMailCredentialBroker,
    MailHubCredentialBrokerError,
    OAuthExchange,
    OAuthStateWrite,
    ProviderOAuthRegistration,
)
from caplatform_bff.main import create_app

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GRAPH_TENANT = "11111111-2222-3333-4444-555555555555"


def _gmail_registration() -> ProviderOAuthRegistration:
    return ProviderOAuthRegistration(
        provider="gmail",
        enabled=True,
        client_id="gmail-client",
        client_secret="gmail-client-secret",
        redirect_uri="https://app.example.test/mail/oauth/gmail/callback",
        scopes=(GMAIL_SCOPE,),
    )


def _provider_transport(requests: list[dict[str, Any]]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        requests.append({"method": request.method, "url": str(request.url), "body": body})
        if str(request.url) == "https://oauth2.googleapis.com/token":
            if "grant_type=authorization_code" in body:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "gmail-access-one",
                        "refresh_token": "gmail-refresh-one",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": GMAIL_SCOPE,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "gmail-access-two",
                    "refresh_token": "gmail-refresh-two",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": GMAIL_SCOPE,
                },
            )
        if str(request.url) == "https://gmail.googleapis.com/gmail/v1/users/me/profile":
            assert request.headers["authorization"] == "Bearer gmail-access-one"
            return httpx.Response(
                200,
                json={"emailAddress": "Pilot@Example.com", "historyId": "123"},
            )
        if str(request.url) == "https://oauth2.googleapis.com/revoke":
            return httpx.Response(200, headers={"x-request-id": "google-revoke-1"})
        raise AssertionError(f"unexpected provider request: {request.method} {request.url}")

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_broker_consumes_state_once_and_rotates_then_revokes(tmp_path: Path) -> None:
    provider_requests: list[dict[str, Any]] = []
    database = tmp_path / "credentials.sqlite3"
    broker = EncryptedSQLiteMailCredentialBroker(
        database_path=str(database),
        encryption_secret="credential-encryption-secret-at-least-32-characters",
        registrations={"gmail": _gmail_registration()},
        transport=_provider_transport(provider_requests),
    )
    await broker.initialize()
    state = OAuthStateWrite(
        state_id="state-id-at-least-sixteen",
        tenant_id="tenant-1",
        subject_id="user-1",
        provider="gmail",
        redirect_uri=_gmail_registration().redirect_uri,
        code_verifier="v" * 64,
        nonce="nonce-at-least-sixteen",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        requested_scopes=[GMAIL_SCOPE],
    )
    await broker.save_state(state)
    await broker.save_state(state)  # idempotent save for the same signed binding

    consumed = await broker.consume_state(state.state_id)
    assert consumed is not None
    assert consumed["tenant_id"] == "tenant-1"
    assert await broker.consume_state(state.state_id) is None

    metadata = await broker.exchange(
        OAuthExchange(
            tenant_id="tenant-1",
            subject_id="user-1",
            provider="gmail",
            redirect_uri=_gmail_registration().redirect_uri,
            code="one-time-code",
            code_verifier="v" * 64,
            nonce="nonce-at-least-sixteen",
            requested_scopes=[GMAIL_SCOPE],
        )
    )
    credential_ref = str(metadata["credential_ref"])
    assert metadata == {
        "credential_ref": credential_ref,
        "email_address": "pilot@example.com",
        "provider_account_id": "Pilot@Example.com",
        "granted_scopes": [GMAIL_SCOPE],
        "credential_version": 1,
    }
    owner = CredentialResolve(
        credential_ref=credential_ref,
        tenant_id="tenant-1",
        subject_id="user-1",
    )
    assert await broker.resolve(owner) == {
        "access_token": "gmail-access-one",
        "token_type": "Bearer",
    }
    with pytest.raises(MailHubCredentialBrokerError, match="credential_not_found"):
        await broker.resolve(
            CredentialResolve(
                credential_ref=credential_ref,
                tenant_id="tenant-2",
                subject_id="user-1",
            )
        )

    refreshed = await broker.refresh(
        CredentialRefresh(
            credential_ref=credential_ref,
            tenant_id="tenant-1",
            subject_id="user-1",
            reason="operator_rotation_test",
        )
    )
    assert refreshed["credential_version"] == 2
    assert (await broker.resolve(owner))["access_token"] == "gmail-access-two"

    revoked = await broker.revoke(owner)
    assert revoked == {
        "status": "revoked",
        "revocation_id": revoked["revocation_id"],
        "provider_request_id": "google-revoke-1",
    }
    with pytest.raises(MailHubCredentialBrokerError, match="credential_revoked"):
        await broker.resolve(owner)

    raw_database = database.read_bytes()
    for secret in (
        b"one-time-code",
        b"gmail-access-one",
        b"gmail-refresh-one",
        b"gmail-access-two",
        b"gmail-refresh-two",
    ):
        assert secret not in raw_database
    assert any("grant_type=authorization_code" in item["body"] for item in provider_requests)
    assert any("grant_type=refresh_token" in item["body"] for item in provider_requests)


@pytest.mark.asyncio
async def test_graph_exchange_binds_configured_tenant_and_local_revocation(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "graph-access-one",
                    "refresh_token": "graph-refresh-one",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    # Entra can omit offline_access from this resource-scope field.
                    "scope": "Mail.Read",
                },
            )
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(
                200,
                json={
                    "id": "graph-account-1",
                    "mail": "pilot@contoso.example",
                    "userPrincipalName": "pilot@contoso.example",
                },
            )
        raise AssertionError(f"unexpected provider request: {request.url}")

    registration = ProviderOAuthRegistration(
        provider="microsoft_graph",
        enabled=True,
        client_id="graph-client",
        client_secret="graph-client-secret",
        redirect_uri="https://app.example.test/mail/oauth/microsoft_graph/callback",
        scopes=("Mail.Read", "offline_access"),
        authority_tenant=GRAPH_TENANT,
    )
    broker = EncryptedSQLiteMailCredentialBroker(
        database_path=str(tmp_path / "graph.sqlite3"),
        encryption_secret="credential-encryption-secret-at-least-32-characters",
        registrations={"microsoft_graph": registration},
        transport=httpx.MockTransport(handle),
    )
    await broker.initialize()
    metadata = await broker.exchange(
        OAuthExchange(
            tenant_id="tenant-1",
            subject_id="user-1",
            provider="microsoft_graph",
            redirect_uri=registration.redirect_uri,
            code="one-time-graph-code",
            code_verifier="v" * 64,
            nonce="nonce-at-least-sixteen",
            requested_scopes=["Mail.Read", "offline_access"],
        )
    )
    assert metadata["provider_account_id"] == "graph-account-1"
    assert metadata["provider_tenant_id"] == GRAPH_TENANT
    assert metadata["granted_scopes"] == ["Mail.Read", "offline_access"]

    owner = CredentialResolve(
        credential_ref=str(metadata["credential_ref"]),
        tenant_id="tenant-1",
        subject_id="user-1",
    )
    assert (await broker.revoke(owner))["status"] == "revoked"
    with pytest.raises(MailHubCredentialBrokerError, match="credential_revoked"):
        await broker.resolve(owner)


def test_mailhost_routes_require_service_token_and_never_echo_secret(tmp_path: Path) -> None:
    provider_requests: list[dict[str, Any]] = []
    broker = EncryptedSQLiteMailCredentialBroker(
        database_path=str(tmp_path / "credentials.sqlite3"),
        encryption_secret="credential-encryption-secret-at-least-32-characters",
        registrations={"gmail": _gmail_registration()},
        transport=_provider_transport(provider_requests),
    )
    service_token = "mailhub-service-token-at-least-32-characters"
    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            state_database_path=str(tmp_path / "state.sqlite3"),
            mailhub_host_service_token=service_token,
        ),
        platform_core=object(),
        mailhub_credential_broker=broker,
    )
    state_payload = {
        "state_id": "state-id-at-least-sixteen",
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
        "provider": "gmail",
        "redirect_uri": _gmail_registration().redirect_uri,
        "code_verifier": "v" * 64,
        "nonce": "nonce-at-least-sixteen",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "connection_id": str(uuid4()),
        "expected_revision": 1,
        "requested_scopes": [GMAIL_SCOPE],
    }
    with TestClient(app) as client:
        assert client.post("/v1/mail-host/oauth/state", json=state_payload).status_code == 401
        headers = {"Authorization": f"Bearer {service_token}"}
        saved = client.post("/v1/mail-host/oauth/state", headers=headers, json=state_payload)
        assert saved.status_code == 204
        consumed = client.post(
            "/v1/mail-host/oauth/state/consume",
            headers=headers,
            json={"state_id": state_payload["state_id"]},
        )
        assert consumed.status_code == 200
        assert consumed.headers["cache-control"] == "no-store"
        assert consumed.json()["record"]["tenant_id"] == "tenant-1"
        replayed = client.post(
            "/v1/mail-host/oauth/state/consume",
            headers=headers,
            json={"state_id": state_payload["state_id"]},
        )
        assert replayed.json() == {"record": None}

    assert service_token not in consumed.text


def test_real_mail_readiness_requires_broker_secrets_and_redacts_repr() -> None:
    missing = Settings(
        mailhub_gmail_enabled=True,
        mailhub_gmail_client_id="gmail-client",
        mailhub_gmail_authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        mailhub_gmail_redirect_uri="https://app.example.test/mail/oauth/gmail/callback",
        mailhub_gmail_scopes=(GMAIL_SCOPE,),
    )

    assert "mailhub_host_service_token_insecure" in missing.readiness_issues()
    assert "mailhub_credential_encryption_secret_insecure" in missing.readiness_issues()
    assert "mailhub_gmail_client_secret_missing" in missing.readiness_issues()

    configured = Settings(
        mailhub_gmail_enabled=True,
        mailhub_gmail_client_id="gmail-client",
        mailhub_gmail_client_secret="gmail-client-secret-value",
        mailhub_gmail_authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        mailhub_gmail_redirect_uri="https://app.example.test/mail/oauth/gmail/callback",
        mailhub_gmail_scopes=(GMAIL_SCOPE,),
        mailhub_host_service_token="h" * 32,
        mailhub_credential_encryption_secret="e" * 32,
    )

    assert not {
        "mailhub_host_service_token_insecure",
        "mailhub_credential_encryption_secret_insecure",
        "mailhub_gmail_client_secret_missing",
    }.intersection(configured.readiness_issues())
    assert "gmail-client-secret-value" not in repr(configured)
    assert "h" * 32 not in repr(configured)
    assert "e" * 32 not in repr(configured)
