"""Contract tests for the local MailHub host (B4 activation walk)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from local_host.app import create_host_app
from local_host.broker import build_broker
from local_host.config import HostSettings
from local_host.stores import LocalStores

_TOKEN = "b4-service-token-0123456789abcdef0123456789abcdef"
_SECRET = "b4-encryption-secret-0123456789abcdef0123456789abcdef"
_VERIFIER = "verifier-" + "b" * 40
_NONCE = "nonce-" + "c" * 40


@pytest.fixture()
def settings(tmp_path: Path) -> HostSettings:
    return HostSettings(
        service_token=_TOKEN,
        encryption_secret=_SECRET,
        database_path=tmp_path / "host.db",
        mailhub_api_url="http://127.0.0.1:8000",
        gmail_enabled=False,
        gmail_client_id="",
        gmail_client_secret="",
        gmail_redirect_uri="http://127.0.0.1:8090/oauth/gmail/callback",
        gmail_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )


@pytest.fixture()
def stores(settings: HostSettings) -> LocalStores:
    store = LocalStores(settings.database_path, settings.encryption_secret)
    store.initialize()
    return store


@pytest.fixture()
async def client(settings: HostSettings, stores: LocalStores) -> httpx.AsyncClient:
    broker = build_broker(settings, settings.database_path)
    await broker.initialize()
    app = create_host_app(settings, stores, broker)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://host.test")


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


@pytest.mark.asyncio
async def test_service_token_is_enforced(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/mail-host/identity/authorize",
        json={"tenant_id": "t1", "subject_id": "u1", "capability": "mail.read"},
    )
    assert response.status_code == 401
    response = await client.post(
        "/v1/mail-host/identity/authorize",
        headers={"Authorization": "Bearer wrong-token"},
        json={"tenant_id": "t1", "subject_id": "u1", "capability": "mail.read"},
    )
    assert response.status_code == 401
    response = await client.post(
        "/v1/mail-host/identity/authorize",
        headers=_auth(),
        json={"tenant_id": "t1", "subject_id": "u1", "capability": "mail.read"},
    )
    assert response.status_code == 200
    assert response.json() == {"allowed": True}


@pytest.mark.asyncio
async def test_identity_denies_missing_scope(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/mail-host/identity/authorize",
        headers=_auth(),
        json={"tenant_id": "", "subject_id": "u1", "capability": "mail.read"},
    )
    assert response.status_code == 200
    assert response.json() == {"allowed": False}


@pytest.mark.asyncio
async def test_oauth_state_is_saved_and_consumed_once(
    client: httpx.AsyncClient,
) -> None:
    record = {
        "state_id": "state-" + "a" * 32,
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
        "provider": "gmail",
        "redirect_uri": "http://127.0.0.1:8090/oauth/gmail/callback",
        "code_verifier": _VERIFIER,
        "nonce": _NONCE,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }
    response = await client.post(
        "/v1/mail-host/oauth/state",
        headers={**_auth(), "Idempotency-Key": "mailhub-oauth-state:state-1"},
        json=record,
    )
    assert response.status_code == 200
    response = await client.post(
        "/v1/mail-host/oauth/state/consume",
        headers=_auth(),
        json={"state_id": record["state_id"]},
    )
    assert response.status_code == 200
    consumed = response.json()["record"]
    assert consumed["tenant_id"] == "tenant-1"
    assert consumed["provider"] == "gmail"
    response = await client.post(
        "/v1/mail-host/oauth/state/consume",
        headers=_auth(),
        json={"state_id": record["state_id"]},
    )
    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.asyncio
async def test_oauth_exchange_fails_closed_without_registration(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/mail-host/oauth/exchange",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "provider": "gmail",
            "redirect_uri": "http://127.0.0.1:8090/oauth/gmail/callback",
            "code": "code-1",
            "code_verifier": _VERIFIER,
            "nonce": _NONCE,
            "requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        },
    )
    assert response.status_code == 503
    assert response.json()["code"] in {
        "mailhub_provider_unconfigured",
        "mailhub_provider_disabled",
    }


@pytest.mark.asyncio
async def test_objects_roundtrip_is_tenant_scoped_and_digest_checked(
    client: httpx.AsyncClient,
) -> None:
    import hashlib

    content = "邮件正文只进加密对象存储"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    response = await client.post(
        "/v1/mail-host/objects",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "purpose": "mail_body",
            "content": content,
            "content_sha256": digest,
        },
    )
    assert response.status_code == 200
    object_ref = response.json()["object_ref"]

    response = await client.post(
        "/v1/mail-host/objects/read",
        headers=_auth(),
        json={
            "tenant_id": "tenant-2",
            "subject_id": "user-1",
            "object_ref": object_ref,
        },
    )
    assert response.status_code == 404

    response = await client.post(
        "/v1/mail-host/objects/read",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "object_ref": object_ref,
        },
    )
    assert response.status_code == 200
    assert response.json()["content"] == content

    response = await client.post(
        "/v1/mail-host/objects",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "purpose": "mail_body",
            "content": content,
            "content_sha256": "0" * 64,
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "object_store_digest_mismatch"

    response = await client.post(
        "/v1/mail-host/objects/delete",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "object_ref": object_ref,
        },
    )
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_quota_acquire_release_and_limit(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/mail-host/quota/acquire",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "account_id": None,
            "operation": "sync",
            "limits": {"max_concurrent": 1, "max_per_hour": 10, "max_per_day": 100},
        },
    )
    assert response.status_code == 200
    lease_id = response.json()["lease"]["lease_id"]

    response = await client.post(
        "/v1/mail-host/quota/acquire",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "account_id": None,
            "operation": "sync",
            "limits": {"max_concurrent": 1, "max_per_hour": 10, "max_per_day": 100},
        },
    )
    assert response.status_code == 429

    response = await client.post(
        "/v1/mail-host/quota/release",
        headers=_auth(),
        json={"lease_id": lease_id, "consume": True},
    )
    assert response.status_code == 200

    response = await client.post(
        "/v1/mail-host/quota/acquire",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "account_id": None,
            "operation": "sync",
            "limits": {"max_concurrent": 1, "max_per_hour": 1, "max_per_day": 100},
        },
    )
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_events_are_idempotent_by_event_id(
    client: httpx.AsyncClient, stores: LocalStores
) -> None:
    envelope = {
        "event_id": "event-1",
        "event_type": "mail.sync_job.completed",
        "occurred_at": datetime.now(UTC).isoformat(),
        "tenant_id": "tenant-1",
    }
    for _ in range(2):
        response = await client.post(
            "/v1/mail-host/events",
            headers={**_auth(), "Idempotency-Key": "mailhub-event:event-1"},
            json=envelope,
        )
        assert response.status_code == 200
    with stores._connect() as db:
        count = db.execute("SELECT COUNT(*) AS count FROM host_events").fetchone()
    assert int(count["count"]) == 1


@pytest.mark.asyncio
async def test_approvals_bind_action_identity(client: httpx.AsyncClient) -> None:
    action = {
        "action_id": "action-1",
        "action_type": "draft_reply",
        "context": {"tenant_id": "tenant-1", "agent_subject_id": "user-1"},
        "input_digest": "a" * 64,
    }
    response = await client.post(
        "/v1/mail-host/approvals/request",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "action": action,
            "decision": {},
        },
    )
    assert response.status_code == 200
    confirmation_ref = response.json()["confirmation_ref"]

    response = await client.post(
        "/v1/mail-host/approvals/verify",
        headers=_auth(),
        json={"confirmation_ref": confirmation_ref, "action": action},
    )
    assert response.json()["verified"] is True

    tampered = {**action, "action_id": "action-2"}
    response = await client.post(
        "/v1/mail-host/approvals/verify",
        headers=_auth(),
        json={"confirmation_ref": confirmation_ref, "action": tampered},
    )
    assert response.json()["verified"] is False


@pytest.mark.asyncio
async def test_knowledge_safety_and_unconfigured_surfaces(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/mail-host/knowledge/safety/evaluate",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "message_id": "00000000-0000-0000-0000-000000000001",
            "content_sha256": "a" * 64,
            "candidate_id": "candidate-1",
            "candidate": {},
            "evidence": [],
        },
    )
    assert response.status_code == 200
    assert response.json()["decision"]["security_state"] == "cleared"
    assert response.json()["decision"]["rights_state"] == "approved"

    for path in (
        "/v1/mail-host/security/av-scan",
        "/v1/mail-host/security/dlp-check",
    ):
        response = await client.post(path, headers=_auth(), json={})
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_ai_structure_is_deterministic_rules_pass_through(
    client: httpx.AsyncClient,
) -> None:
    from mailhub.domain import digest_text

    body = (
        "决定：下周一前完成供应商切换。\n"
        "风险：库存不足。\n"
        "承诺：我们将在周三前发出询价。\n"
        "项目 ref: PRJ-2026-08"
    )
    response = await client.post(
        "/v1/mail-host/ai/structure",
        headers=_auth(),
        json={
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "operation": "mail.message.analyze",
            "source": {
                "message_id": "00000000-0000-0000-0000-000000000001",
                "content_sha256": digest_text(body),
                "subject": "项目A周会：决定将供应商切换至B公司",
                "body_text": body,
                "baseline": {},
            },
            "schema": {},
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["model_ref"] == "mailhub-rules-pass-through-v1"
    assert result["summary"]
    assert result["decisions"]
    assert result["risks"]
    assert result["commitments"]
    assert 0 <= result["confidence"] <= 1


@pytest.mark.asyncio
async def test_broker_lifecycle_through_http(settings: HostSettings) -> None:
    """The durable broker path must accept the same contract the adapters send."""

    def provider_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "short-lived-token",
                    "refresh_token": "refresh-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                },
            )
        if request.url.path.endswith("/revoke"):
            return httpx.Response(200)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "isolated@example.test"})
        return httpx.Response(404)

    enabled = HostSettings(
        service_token=_TOKEN,
        encryption_secret=_SECRET,
        database_path=settings.database_path,
        mailhub_api_url=settings.mailhub_api_url,
        gmail_enabled=True,
        gmail_client_id="client-1",
        gmail_client_secret="secret-1",
        gmail_redirect_uri=settings.gmail_redirect_uri,
        gmail_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    stores = LocalStores(enabled.database_path, enabled.encryption_secret)
    stores.initialize()
    broker = build_broker(
        enabled, enabled.database_path, transport=httpx.MockTransport(provider_handler)
    )
    await broker.initialize()
    app = create_host_app(enabled, stores, broker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://host.test"
    ) as client:
        exchange_body = {
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "provider": "gmail",
            "redirect_uri": enabled.gmail_redirect_uri,
            "code": "one-time-code",
            "code_verifier": _VERIFIER,
            "nonce": _NONCE,
            "requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        }
        response = await client.post(
            "/v1/mail-host/oauth/exchange", headers=_auth(), json=exchange_body
        )
        assert response.status_code == 200
        metadata = response.json()["metadata"]
        assert metadata["email_address"] == "isolated@example.test"
        assert metadata["granted_scopes"] == [
            "https://www.googleapis.com/auth/gmail.readonly"
        ]
        credential_ref = metadata["credential_ref"]

        response = await client.post(
            "/v1/mail-host/credentials/resolve",
            headers=_auth(),
            json={
                "credential_ref": credential_ref,
                "tenant_id": "tenant-1",
                "subject_id": "user-1",
            },
        )
        assert response.status_code == 200
        assert response.json()["credentials"]["access_token"] == "short-lived-token"

        response = await client.post(
            "/v1/mail-host/credentials/refresh",
            headers=_auth(),
            json={
                "credential_ref": credential_ref,
                "tenant_id": "tenant-1",
                "subject_id": "user-1",
                "reason": "b4_refresh_observation",
            },
        )
        assert response.status_code == 200
        assert response.json()["metadata"]["credential_version"] == 2

        response = await client.post(
            "/v1/mail-host/credentials/revoke",
            headers=_auth(),
            json={
                "credential_ref": credential_ref,
                "tenant_id": "tenant-1",
                "subject_id": "user-1",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "revoked"

        response = await client.post(
            "/v1/mail-host/credentials/resolve",
            headers=_auth(),
            json={
                "credential_ref": credential_ref,
                "tenant_id": "tenant-1",
                "subject_id": "user-1",
            },
        )
        assert response.status_code == 403
        assert response.json()["code"] == "credential_revoked"


@pytest.mark.asyncio
async def test_imap_app_password_intake_resolve_refresh_and_revoke(
    client: httpx.AsyncClient,
) -> None:
    intake = await client.post(
        "/v1/mail-host/admin/credentials",
        headers=_auth(),
        json={
            "provider": "imap_smtp",
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "username": "isolated@gmail.test",
            "password": "app-password-1234",
        },
    )
    assert intake.status_code == 200
    credential_ref = intake.json()["credential_ref"]
    assert credential_ref.startswith("imapcred_")

    resolved = await client.post(
        "/v1/mail-host/credentials/resolve",
        headers=_auth(),
        json={
            "credential_ref": credential_ref,
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["credentials"] == {
        "username": "isolated@gmail.test",
        "password": "app-password-1234",
    }

    # Cross-tenant resolution fails closed.
    denied = await client.post(
        "/v1/mail-host/credentials/resolve",
        headers=_auth(),
        json={
            "credential_ref": credential_ref,
            "tenant_id": "tenant-2",
            "subject_id": "user-1",
        },
    )
    assert denied.status_code == 404

    refreshed = await client.post(
        "/v1/mail-host/credentials/refresh",
        headers=_auth(),
        json={
            "credential_ref": credential_ref,
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "reason": "b4_imap_refresh",
        },
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["metadata"]["email_address"] == "isolated@gmail.test"

    revoked = await client.post(
        "/v1/mail-host/credentials/revoke",
        headers=_auth(),
        json={
            "credential_ref": credential_ref,
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
        },
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    resolved = await client.post(
        "/v1/mail-host/credentials/resolve",
        headers=_auth(),
        json={
            "credential_ref": credential_ref,
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
        },
    )
    assert resolved.status_code == 404
