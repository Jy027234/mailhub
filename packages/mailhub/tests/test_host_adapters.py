from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from mailhub.domain import (
    ActionType,
    AgentActionContext,
    AgentActionRequest,
    CandidateType,
    MailActionCandidate,
    MailMessageProjection,
    PolicyDecision,
    ProviderName,
    digest_text,
)
from mailhub.errors import AuthorizationError, ProviderFailureError
from mailhub.events import build_event
from mailhub.hosts.http import (
    HttpAgentMemoryAdapter,
    HttpAiExecutionAdapter,
    HttpApprovalAdapter,
    HttpAuditAdapter,
    HttpCredentialBrokerAdapter,
    HttpEventPublisherAdapter,
    HttpHostIdentityAdapter,
    HttpKillSwitchAdapter,
    HttpKnowledgeLifecycleAdapter,
    HttpKnowledgeSafetyAdapter,
    HttpKnowledgeSink,
    HttpNotificationAdapter,
    HttpOAuthCallbackAdapter,
    HttpOAuthStateStore,
    HttpObjectStoreAdapter,
    HttpProviderNotificationVerifierAdapter,
    HttpProviderSubscriptionAdapter,
    HttpQuotaAdapter,
    HttpSenderInterlockAdapter,
    HttpTelemetryAdapter,
)
from mailhub.memory import ApprovedKnowledgeReference
from mailhub.notifications import NotificationChangeKind, ProviderNotification
from mailhub.oauth import OAuthCallbackContext, OAuthProvider, OAuthStateRecord
from mailhub.ports import ProviderNotificationDelivery
from mailhub.quota import QuotaLimits


def _action() -> AgentActionRequest:
    return AgentActionRequest(
        action_id=uuid4(),
        action_type=ActionType.DRAFT_REPLY,
        context=AgentActionContext(
            tenant_id="tenant-1",
            agent_subject_id="agent-1",
            connection_id=uuid4(),
            folder_ref=None,
            thread_id=uuid4(),
        ),
        input_digest="a" * 64,
    )


class _FakeClient:
    requests: list[tuple[str, str, dict[str, object] | None]] = []
    last_headers: dict[str, str] = {}
    response_status = 200
    response_json: dict[str, object] = {"allowed": True}

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object] | None,
    ) -> httpx.Response:
        assert headers["Accept"] == "application/json"
        type(self).last_headers = dict(headers)
        self.requests.append((method, url, json))
        return httpx.Response(
            self.response_status,
            json=self.response_json,
            request=httpx.Request(method, url),
        )

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object] | None,
    ) -> httpx.Response:
        return await self.request("POST", url, headers=headers, json=json)


@pytest.mark.asyncio
async def test_http_host_ports_preserve_scope_and_redact_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    _FakeClient.response_json = {"allowed": True}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    connection_id = uuid4()
    assert await HttpHostIdentityAdapter(base_url="https://host.example.test").authorize(
        tenant_id="tenant-1",
        subject_id="user-1",
        capability="mail.read",
        connection_id=connection_id,
    )
    assert _FakeClient.requests[-1][2] == {
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
        "capability": "mail.read",
        "connection_id": str(connection_id),
    }

    _FakeClient.response_json = {"credentials": {"access_token": "short-lived"}}
    credentials = await HttpCredentialBrokerAdapter(base_url="https://host.example.test").resolve(
        credential_ref="ref-1", tenant_id="tenant-1", subject_id="user-1"
    )
    assert credentials == {"access_token": "short-lived"}

    _FakeClient.response_json = {
        "metadata": {
            "credential_ref": "ref-2",
            "provider_account_id": "account-1",
            "credential_version": 2,
        }
    }
    refreshed = await HttpCredentialBrokerAdapter(base_url="https://host.example.test").refresh(
        credential_ref="ref-1",
        tenant_id="tenant-1",
        subject_id="user-1",
        reason="scheduled_refresh",
    )
    assert refreshed == {
        "credential_ref": "ref-2",
        "provider_account_id": "account-1",
        "credential_version": "2",
    }
    assert _FakeClient.requests[-1][1].endswith("/v1/mail-host/credentials/refresh")
    assert _FakeClient.requests[-1][2] == {
        "credential_ref": "ref-1",
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
        "reason": "scheduled_refresh",
    }

    _FakeClient.response_json = {
        "data": {"status": "revoked", "provider_request_id": "provider-revoke-1"}
    }
    revoked = await HttpCredentialBrokerAdapter(base_url="https://host.example.test").revoke(
        credential_ref="ref-1", tenant_id="tenant-1", subject_id="user-1"
    )
    assert revoked == {"status": "revoked", "provider_request_id": "provider-revoke-1"}
    assert _FakeClient.requests[-1][1].endswith("/v1/mail-host/credentials/revoke")
    assert _FakeClient.requests[-1][2] == {
        "credential_ref": "ref-1",
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
    }

    _FakeClient.response_json = {"data": {"status": "revoked", "access_token": "secret"}}
    with pytest.raises(ProviderFailureError, match="credential_revoke_response_invalid"):
        await HttpCredentialBrokerAdapter(base_url="https://host.example.test").revoke(
            credential_ref="ref-1", tenant_id="tenant-1", subject_id="user-1"
        )

    _FakeClient.response_json = {"object_ref": "object://1"}
    ref = await HttpObjectStoreAdapter(base_url="https://host.example.test").put_text(
        tenant_id="tenant-1",
        subject_id="user-1",
        purpose="message_body",
        content="private",
        content_sha256="a" * 64,
        expires_at=datetime.now(UTC),
    )
    assert ref == "object://1"


@pytest.mark.asyncio
async def test_http_oauth_adapters_keep_state_and_tokens_inside_host_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    expires_at = datetime.now(UTC)
    reauth_connection_id = uuid4()
    record = OAuthStateRecord(
        state_id="state-1",
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=OAuthProvider.GMAIL,
        redirect_uri="https://app.example.test/mail/oauth/callback",
        code_verifier="verifier-1",
        nonce="nonce-1",
        expires_at=expires_at,
        connection_id=reauth_connection_id,
        expected_revision=7,
        requested_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    _FakeClient.response_json = {"stored": True}
    store = HttpOAuthStateStore(base_url="https://host.example.test")
    await store.save(record)
    assert _FakeClient.requests[-1][2] == {
        "state_id": "state-1",
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
        "provider": "gmail",
        "redirect_uri": record.redirect_uri,
        "code_verifier": "verifier-1",
        "nonce": "nonce-1",
        "expires_at": expires_at.isoformat(),
        "connection_id": str(reauth_connection_id),
        "expected_revision": 7,
        "requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }
    assert "Idempotency-Key" in _FakeClient.last_headers

    _FakeClient.response_json = {
        "record": {
            "state_id": "state-1",
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "provider": "gmail",
            "redirect_uri": record.redirect_uri,
            "code_verifier": "verifier-1",
            "nonce": "nonce-1",
            "expires_at": expires_at.isoformat(),
            "connection_id": str(reauth_connection_id),
            "expected_revision": 7,
            "requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        }
    }
    consumed = await store.consume("state-1")
    assert consumed == record

    context = OAuthCallbackContext(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=OAuthProvider.GMAIL,
        redirect_uri=record.redirect_uri,
        code_verifier="verifier-1",
        nonce="nonce-1",
        connection_id=reauth_connection_id,
        expected_revision=7,
        requested_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    _FakeClient.response_json = {
        "metadata": {
            "email_address": "user@example.test",
            "credential_ref": "secret-ref-1",
            "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "provider_account_id": "account-1",
            "credential_version": 3,
        }
    }
    metadata = await HttpOAuthCallbackAdapter(base_url="https://host.example.test").exchange(
        context=context, code="one-time-code"
    )
    assert metadata["credential_ref"] == "secret-ref-1"
    assert metadata["granted_scopes"] == ("https://www.googleapis.com/auth/gmail.readonly",)
    assert metadata["provider_account_id"] == "account-1"
    assert metadata["credential_version"] == 3
    assert "one-time-code" in str(_FakeClient.requests[-1][2])
    assert _FakeClient.requests[-1][2]["connection_id"] == str(reauth_connection_id)
    assert _FakeClient.requests[-1][2]["expected_revision"] == 7
    assert _FakeClient.requests[-1][2]["requested_scopes"] == [
        "https://www.googleapis.com/auth/gmail.readonly"
    ]


@pytest.mark.asyncio
async def test_http_oauth_callback_rejects_state_bound_scope_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.response_status = 200
    _FakeClient.response_json = {
        "metadata": {
            "email_address": "user@example.test",
            "credential_ref": "secret-ref-1",
            "granted_scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            ],
            "provider_account_id": "account-1",
        }
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    context = OAuthCallbackContext(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=OAuthProvider.GMAIL,
        redirect_uri="https://app.example.test/mail/oauth/callback",
        code_verifier="verifier-1",
        nonce="nonce-1",
        requested_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )

    with pytest.raises(ProviderFailureError, match="write_scope_disallowed"):
        await HttpOAuthCallbackAdapter(base_url="https://host.example.test").exchange(
            context=context, code="one-time-code"
        )


@pytest.mark.asyncio
async def test_http_oauth_state_rejects_legacy_real_provider_record_without_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.response_status = 200
    _FakeClient.response_json = {
        "record": {
            "state_id": "legacy-state",
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "provider": "microsoft_graph",
            "redirect_uri": "https://app.example.test/mail/oauth/callback",
            "code_verifier": "verifier-1",
            "nonce": "nonce-1",
            "expires_at": datetime.now(UTC).isoformat(),
        }
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    with pytest.raises(ProviderFailureError, match="requested_scopes_missing"):
        await HttpOAuthStateStore(base_url="https://host.example.test").consume("legacy-state")


@pytest.mark.asyncio
async def test_http_oauth_callback_rejects_token_shaped_host_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.response_status = 200
    _FakeClient.response_json = {
        "metadata": {
            "email_address": "user@example.test",
            "credential_ref": "secret-ref-1",
            "granted_scopes": [],
            "access_token": "must-not-cross",
        }
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    context = OAuthCallbackContext(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=OAuthProvider.MICROSOFT_GRAPH,
        redirect_uri="https://app.example.test/mail/oauth/callback",
        code_verifier="verifier-1",
        nonce="nonce-1",
    )

    with pytest.raises(ProviderFailureError, match="oauth_exchange_secret_leak"):
        await HttpOAuthCallbackAdapter(base_url="https://host.example.test").exchange(
            context=context, code="one-time-code"
        )


@pytest.mark.asyncio
async def test_http_oauth_callback_rejects_invalid_credential_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.response_status = 200
    _FakeClient.response_json = {
        "metadata": {
            "email_address": "user@example.test",
            "credential_ref": "secret-ref-1",
            "granted_scopes": [],
            "provider_account_id": "account-1",
            "credential_version": 0,
        }
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    context = OAuthCallbackContext(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=OAuthProvider.GMAIL,
        redirect_uri="https://app.example.test/mail/oauth/callback",
        code_verifier="verifier-1",
        nonce="nonce-1",
    )

    with pytest.raises(ProviderFailureError, match="oauth_exchange_credential_version_invalid"):
        await HttpOAuthCallbackAdapter(base_url="https://host.example.test").exchange(
            context=context, code="one-time-code"
        )


@pytest.mark.asyncio
async def test_http_oauth_callback_requires_provider_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.response_status = 200
    _FakeClient.response_json = {
        "metadata": {
            "email_address": "user@example.test",
            "credential_ref": "secret-ref-1",
            "granted_scopes": [],
        }
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    context = OAuthCallbackContext(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=OAuthProvider.MICROSOFT_GRAPH,
        redirect_uri="https://app.example.test/mail/oauth/callback",
        code_verifier="verifier-1",
        nonce="nonce-1",
    )

    with pytest.raises(ProviderFailureError, match="oauth_exchange_provider_account_id_required"):
        await HttpOAuthCallbackAdapter(base_url="https://host.example.test").exchange(
            context=context, code="one-time-code"
        )


@pytest.mark.asyncio
async def test_http_provider_subscription_adapter_returns_non_secret_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    connection_id = uuid4()
    expiry = datetime.now(UTC) + timedelta(hours=1)
    _FakeClient.response_json = {
        "subscription": {
            "provider": "gmail",
            "subscription_ref": "projects/p/subscriptions/s-1",
            "status": "active",
            "expires_at": expiry.isoformat(),
            "callback_endpoint": "https://mail.example.test/v1/mail/webhooks/gmail",
            "provider_request_id": "provider-request-1",
            "client_state_ref": "state-ref-1",
        }
    }
    adapter = HttpProviderSubscriptionAdapter(base_url="https://host.example.test")
    lease = await adapter.ensure_subscription(
        tenant_id="tenant-1",
        subject_id="user-1",
        connection_id=connection_id,
        provider=ProviderName.GMAIL,
        callback_endpoint="https://mail.example.test/v1/mail/webhooks/gmail",
        desired_expiry=expiry,
        client_state_ref="state-ref-1",
        idempotency_key="subscription-ensure-1",
    )
    assert lease.subscription_ref.endswith("s-1")
    assert lease.provider is ProviderName.GMAIL
    assert _FakeClient.last_headers["Idempotency-Key"] == "subscription-ensure-1"
    request_payload = _FakeClient.requests[-1][2]
    assert isinstance(request_payload, dict)
    assert request_payload["client_state_ref"] == "state-ref-1"

    _FakeClient.response_json = {
        "status": "cancel_requested",
        "cancel_ref": "cancel-1",
        "provider_request_id": "provider-request-2",
    }
    result = await adapter.cancel_subscription(
        tenant_id="tenant-1",
        subject_id="user-1",
        connection_id=connection_id,
        provider=ProviderName.GMAIL,
        subscription_ref=lease.subscription_ref,
        request_id="subscription-cancel-1",
    )
    assert result == {
        "status": "cancel_requested",
        "cancel_ref": "cancel-1",
        "provider_request_id": "provider-request-2",
    }


@pytest.mark.asyncio
async def test_http_provider_subscription_adapter_rejects_secret_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.response_status = 200
    _FakeClient.response_json = {
        "subscription": {
            "provider": "microsoft_graph",
            "subscription_ref": "subscription-1",
            "status": "active",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "callback_endpoint": "https://mail.example.test/v1/mail/webhooks/graph",
            "access_token": "must-not-cross",
        }
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    with pytest.raises(ProviderFailureError, match="provider_subscription_response_invalid"):
        await HttpProviderSubscriptionAdapter(
            base_url="https://host.example.test"
        ).ensure_subscription(
            tenant_id="tenant-1",
            subject_id="user-1",
            connection_id=uuid4(),
            provider=ProviderName.MICROSOFT_GRAPH,
            callback_endpoint="https://mail.example.test/v1/mail/webhooks/graph",
            desired_expiry=datetime.now(UTC) + timedelta(hours=1),
            idempotency_key="subscription-ensure-secret",
        )


@pytest.mark.asyncio
async def test_http_provider_subscription_adapter_rejects_expiry_outside_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    with pytest.raises(ProviderFailureError, match="expiry_out_of_bounds"):
        await HttpProviderSubscriptionAdapter(
            base_url="https://host.example.test"
        ).ensure_subscription(
            tenant_id="tenant-1",
            subject_id="user-1",
            connection_id=uuid4(),
            provider=ProviderName.GMAIL,
            callback_endpoint="https://mail.example.test/v1/mail/webhooks/gmail",
            desired_expiry=datetime.now(UTC) + timedelta(days=32),
            idempotency_key="subscription-expiry-too-long",
        )


@pytest.mark.asyncio
async def test_http_provider_notification_verifier_returns_scoped_body_free_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    received_at = datetime.now(UTC).isoformat()
    connection_id = uuid4()
    _FakeClient.response_json = {
        "notifications": [
            {
                "verified": True,
                "route": {
                    "tenant_id": "tenant-1",
                    "subject_id": "user-1",
                    "connection_id": str(connection_id),
                    "folder_ref": "INBOX",
                },
                "notification": {
                    "provider": "gmail",
                    "notification_id": "provider-event-1",
                    "subscription_ref": "projects/p/subscriptions/s-1",
                    "resource_ref": "gmail-account:account-digest",
                    "change_kind": "updated",
                    "received_at": received_at,
                    "cursor_hint": "123",
                },
            }
        ]
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    adapter = HttpProviderNotificationVerifierAdapter(base_url="https://host.example.test")
    deliveries = await adapter.verify_and_route(
        provider=ProviderName.GMAIL,
        body=b'{"message":{}}',
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer should-not-forward",
            "X-Goog-Resource-State": "exists",
        },
    )
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery.tenant_id == "tenant-1"
    assert delivery.connection_id == connection_id
    assert delivery.notification.change_kind is NotificationChangeKind.UPDATED
    payload = _FakeClient.requests[-1][2]
    assert isinstance(payload, dict)
    assert "Bearer should-not-forward" not in str(payload)
    assert payload["provider"] == "gmail"


@pytest.mark.asyncio
async def test_http_provider_notification_verifier_rejects_secret_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.response_status = 200
    _FakeClient.response_json = {"notifications": [], "access_token": "must-not-cross"}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    with pytest.raises(ProviderFailureError, match="provider_notification_secret_leak"):
        await HttpProviderNotificationVerifierAdapter(
            base_url="https://host.example.test"
        ).verify_and_route(
            provider=ProviderName.MICROSOFT_GRAPH,
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )


def test_provider_notification_delivery_rejects_control_characters() -> None:
    with pytest.raises(ValueError, match="provider_notification_folder_ref_invalid"):
        ProviderNotificationDelivery(
            tenant_id="tenant-1",
            subject_id="user-1",
            connection_id=uuid4(),
            folder_ref="INBOX\nX-Injected: yes",
            notification=ProviderNotification(
                provider=ProviderName.GMAIL,
                notification_id="event-1",
                subscription_ref="subscription-1",
                resource_ref="gmail-account:digest",
                change_kind=NotificationChangeKind.UPDATED,
                received_at=datetime.now(UTC),
            ),
        )


@pytest.mark.asyncio
async def test_http_approval_ai_and_audit_ports_use_typed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    action = _action()

    _FakeClient.response_json = {"confirmation_ref": "approval-1"}
    approval = HttpApprovalAdapter(base_url="https://host.example.test")
    reference = await approval.require_confirmation(
        tenant_id="tenant-1",
        subject_id="agent-1",
        action=action,
        decision=PolicyDecision(False, "approval_required"),
    )
    assert reference == "approval-1"

    _FakeClient.response_json = {"verified": True}
    assert await approval.verify_confirmation(confirmation_ref=reference, action=action)

    _FakeClient.response_json = {"result": {"summary": "bounded"}}
    result = await HttpAiExecutionAdapter(base_url="https://host.example.test").structure(
        tenant_id="tenant-1",
        subject_id="user-1",
        operation="summarize",
        source={"message_ref": "message-1"},
        schema={"type": "object"},
    )
    assert result == {"summary": "bounded"}

    _FakeClient.response_json = {}
    await HttpAuditAdapter(base_url="https://host.example.test").append_audit(
        {"tenant_id": "tenant-1", "event_type": "draft.created", "body_text": "private"}
    )
    assert _FakeClient.requests[-1][2] == {
        "event": {
            "tenant_id": "tenant-1",
            "event_type": "draft.created",
            "body_text": "[REDACTED]",
        }
    }

    await HttpNotificationAdapter(base_url="https://host.example.test").notify(
        {"kind": "reauthorization_required", "connection_ref": "connection-1"}
    )
    assert _FakeClient.requests[-1][2] == {
        "notification": {
            "kind": "reauthorization_required",
            "connection_ref": "connection-1",
        }
    }

    await HttpTelemetryAdapter(base_url="https://host.example.test").record(
        name="mail.test", fields={"status": "ok", "body_text": "private"}
    )
    assert _FakeClient.requests[-1][2] == {
        "name": "mail.test",
        "fields": {"status": "ok", "body_text": "[REDACTED]"},
    }

    event = build_event(
        "mail.sync.started",
        tenant_id="tenant-1",
        subject_id="user-1",
        trace_id="trace-1",
        idempotency_key="sync:1",
        data={"connection_id": str(uuid4()), "mode": "incremental"},
    )
    _FakeClient.response_json = {}
    await HttpEventPublisherAdapter(base_url="https://host.example.test").publish(event)
    assert _FakeClient.requests[-1][2] == event
    assert _FakeClient.last_headers["Idempotency-Key"] == f"mailhub-event:{event['event_id']}"

    with pytest.raises(ValueError, match="event_schema_version_invalid"):
        await HttpEventPublisherAdapter(base_url="https://host.example.test").publish(
            {**event, "schema_version": "mailhub.event_envelope.v2"}
        )


@pytest.mark.asyncio
async def test_http_knowledge_safety_adapter_returns_explicit_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    _FakeClient.response_json = {
        "decision": {
            "security_state": "cleared",
            "rights_state": "approved",
            "gate_ref": "gate-1",
        }
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    body = "governed body"
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-1",
        connection_id=uuid4(),
        thread_id=uuid4(),
        provider_message_ref="message-1",
        internet_message_id=None,
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject="Subject",
        received_at=datetime.now(UTC),
        body_text=body,
        content_sha256=digest_text(body),
    )
    candidate = MailActionCandidate(
        candidate_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="user-1",
        message_id=message.message_id,
        candidate_type=CandidateType.KNOWLEDGE,
        payload={"security_state": "pending_scan", "rights_state": "awaiting_review"},
        evidence=(),
        confidence=0.8,
    )

    result = await HttpKnowledgeSafetyAdapter(
        base_url="https://host.example.test"
    ).evaluate_candidate(
        tenant_id="tenant-1",
        subject_id="user-1",
        message=message,
        candidate=candidate,
    )

    assert result == {
        "security_state": "cleared",
        "rights_state": "approved",
        "gate_ref": "gate-1",
    }
    payload = _FakeClient.requests[-1][2]
    assert isinstance(payload, dict)
    assert payload["content_sha256"] == message.content_sha256


@pytest.mark.asyncio
async def test_http_knowledge_sink_forwards_governed_object_ref_without_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    _FakeClient.response_json = {"status": "awaiting_host_review"}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    body = "private governed body"
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-1",
        connection_id=uuid4(),
        thread_id=uuid4(),
        provider_message_ref="message-knowledge",
        internet_message_id=None,
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject="Knowledge source",
        received_at=datetime.now(UTC),
        body_text=None,
        body_object_ref="object://tenant-1/message-1",
        content_sha256=digest_text(body),
    )
    candidate = MailActionCandidate(
        candidate_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="user-1",
        message_id=message.message_id,
        candidate_type=CandidateType.KNOWLEDGE,
        payload={"title": "Knowledge source"},
        evidence=({"locator": "body:1"},),
        confidence=0.9,
    )

    result = await HttpKnowledgeSink(base_url="https://host.example.test").submit_candidate(
        tenant_id="tenant-1",
        subject_id="user-1",
        message=message,
        candidate=candidate,
    )

    assert result["status"] == "awaiting_host_review"
    payload = _FakeClient.requests[-1][2]
    assert isinstance(payload, dict)
    assert payload["body_object_ref"] == "object://tenant-1/message-1"
    assert "body_text" not in payload
    assert body not in str(payload)
    assert (
        _FakeClient.last_headers["Idempotency-Key"] == f"mailhub-knowledge:{candidate.candidate_id}"
    )


@pytest.mark.asyncio
async def test_http_kill_switch_requires_bounded_decision_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    _FakeClient.response_json = {
        "allowed": False,
        "reason": "tenant_kill_switch_active",
        "scope": "tenant",
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = await HttpKillSwitchAdapter(base_url="https://host.example.test").check(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=ProviderName.SANDBOX,
        operation="mail.outbound.send",
    )
    assert result["allowed"] is False
    assert _FakeClient.requests[-1][2] == {
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
        "provider": "sandbox",
        "operation": "mail.outbound.send",
    }

    _FakeClient.response_json = {"scope": "tenant"}
    with pytest.raises(ProviderFailureError, match="kill_switch_response_invalid"):
        await HttpKillSwitchAdapter(base_url="https://host.example.test").check(
            tenant_id="tenant-1",
            subject_id=None,
            provider=None,
            operation="mail.rule.execute",
        )


@pytest.mark.asyncio
async def test_http_knowledge_lifecycle_sends_only_source_refs_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    _FakeClient.response_json = {"status": "revoke_requested", "revoke_ref": "revoke-1"}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    connection_id = uuid4()
    message_id = uuid4()
    result = await HttpKnowledgeLifecycleAdapter(
        base_url="https://host.example.test"
    ).revoke_source(
        tenant_id="tenant-1",
        subject_id="user-1",
        connection_id=connection_id,
        message_ids=(message_id,),
        reason="mail_connection_deleted",
        request_id="knowledge-revoke-1",
    )

    assert result["status"] == "revoke_requested"
    payload = _FakeClient.requests[-1][2]
    assert payload == {
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
        "connection_id": str(connection_id),
        "message_ids": [str(message_id)],
        "reason": "mail_connection_deleted",
        "request_id": "knowledge-revoke-1",
    }
    assert "body_text" not in str(payload)
    assert _FakeClient.last_headers["Idempotency-Key"] == "knowledge-revoke-1"


@pytest.mark.asyncio
async def test_http_agent_memory_accepts_only_approved_body_free_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    _FakeClient.response_json = {"status": "stored", "memory_ref": "memory:1"}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    reference = ApprovedKnowledgeReference(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=uuid4(),
        source_message_id=uuid4(),
        knowledge_ref="knowledge:1",
        approval_ref="approval:1",
        content_sha256="a" * 64,
        scope="project",
        rights_state="approved",
        security_state="cleared",
        approved_at=datetime.now(UTC),
    )

    result = await HttpAgentMemoryAdapter(
        base_url="https://host.example.test"
    ).store_approved_reference(reference=reference)

    assert result["memory_ref"] == "memory:1"
    payload = _FakeClient.requests[-1][2]
    assert payload is not None
    assert "body_text" not in str(payload)
    assert "raw_mime" not in str(payload)
    assert _FakeClient.last_headers["Idempotency-Key"] == f"mailhub-memory:{reference.candidate_id}"


@pytest.mark.asyncio
async def test_http_quota_adapter_round_trips_durable_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_id = uuid4()
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    _FakeClient.response_json = {
        "lease": {"lease_id": str(lease_id), "acquired_at": datetime.now(UTC).isoformat()}
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    quota = HttpQuotaAdapter(base_url="https://host.example.test")
    lease = await quota.acquire(
        tenant_id="tenant-1",
        subject_id="user-1",
        account_id=uuid4(),
        operation="mail.send",
        limits=QuotaLimits(max_concurrent=1),
    )
    assert lease.lease_id == lease_id
    await quota.release(lease, consume=False)
    assert _FakeClient.requests[-1][2] == {"lease_id": str(lease_id), "consume": False}


@pytest.mark.asyncio
async def test_http_sender_interlock_maps_claim_conflict_and_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    _FakeClient.response_status = 200
    _FakeClient.response_json = {"acquired": True}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    interlock = HttpSenderInterlockAdapter(base_url="https://host.example.test")

    assert await interlock.claim(tenant_id="tenant-1", account_ref="account-1", owner="mailhub")
    assert _FakeClient.requests[-1][2] == {
        "tenant_id": "tenant-1",
        "account_ref": "account-1",
        "owner": "mailhub",
        "purpose": "email",
    }

    _FakeClient.response_status = 409
    assert not await interlock.claim(tenant_id="tenant-1", account_ref="account-1", owner="legacy")

    _FakeClient.response_status = 200
    _FakeClient.response_json = {}
    await interlock.release(tenant_id="tenant-1", account_ref="account-1", owner="mailhub")
    assert _FakeClient.requests[-1][2] == {
        "tenant_id": "tenant-1",
        "account_ref": "account-1",
        "owner": "mailhub",
        "purpose": "email",
    }


@pytest.mark.asyncio
async def test_http_host_ports_fail_closed_on_auth_invalid_json_and_bad_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.response_status = 403
    with pytest.raises(AuthorizationError):
        await HttpHostIdentityAdapter(base_url="https://host.example.test").authorize(
            tenant_id="tenant-1",
            subject_id="user-1",
            capability="mail.read",
        )

    _FakeClient.response_status = 200
    _FakeClient.response_json = {"credentials": {"access_token": 123}}
    with pytest.raises(AuthorizationError, match="credential_broker_response_invalid"):
        await HttpCredentialBrokerAdapter(base_url="https://host.example.test").resolve(
            credential_ref="ref-1", tenant_id="tenant-1", subject_id="user-1"
        )

    _FakeClient.response_json = {"metadata": {"access_token": "must-not-cross"}}
    with pytest.raises(ProviderFailureError, match="credential_refresh_secret_leak"):
        await HttpCredentialBrokerAdapter(base_url="https://host.example.test").refresh(
            credential_ref="ref-1",
            tenant_id="tenant-1",
            subject_id="user-1",
            reason="manual_refresh",
        )

    class InvalidJsonClient(_FakeClient):
        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object] | None,
        ) -> httpx.Response:
            del headers, json
            return httpx.Response(200, content=b"not-json", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "AsyncClient", InvalidJsonClient)
    with pytest.raises(ProviderFailureError, match="host_identity_invalid_json"):
        await HttpHostIdentityAdapter(base_url="https://host.example.test").authorize(
            tenant_id="tenant-1",
            subject_id="user-1",
            capability="mail.read",
        )


@pytest.mark.asyncio
async def test_http_host_ports_reject_plaintext_and_unsafe_paths() -> None:
    with pytest.raises(ValueError, match="host_base_url_must_be_tls"):
        HttpAuditAdapter(base_url="http://host.example.test")
    with pytest.raises(ValueError, match="host_base_url_must_be_tls"):
        HttpAuditAdapter(base_url="http://localhost.evil.example.test")
    with pytest.raises(ValueError, match="host_headers_invalid"):
        HttpAuditAdapter(base_url="https://host.example.test", headers={"X-Test": "bad\nvalue"})
    with pytest.raises(ValueError, match="host_path_invalid"):
        await HttpAuditAdapter(
            base_url="https://host.example.test", append_path="//audit"
        ).append_audit({"event_type": "test"})
    with pytest.raises(ValueError, match="host_path_invalid"):
        await HttpAuditAdapter(
            base_url="https://host.example.test", append_path="/v1/../audit"
        ).append_audit({"event_type": "test"})
