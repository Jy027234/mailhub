import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mailhub.api import _build_default_oauth, create_app
from mailhub.config import MailHubSettings
from mailhub.connectors.http_providers import GmailConnector
from mailhub.connectors.sandbox import SandboxConnector
from mailhub.domain import (
    CandidateType,
    ConnectionStatus,
    MailActionCandidate,
    MailboxConnection,
    MailMessageProjection,
    MailOutboxOperation,
    MailThread,
    ProviderName,
    digest_text,
)
from mailhub.notifications import NotificationChangeKind, ProviderNotification
from mailhub.oauth import (
    InMemoryOAuthStateStore,
    OAuthAuthorizationService,
    OAuthCallbackContext,
    OAuthProvider,
)
from mailhub.ports import ProviderNotificationDelivery, ProviderSubscriptionLease
from mailhub.service import MailService, StaticCredentialBroker
from mailhub.storage import InMemoryMailRepository, RepositoryConflictError


class ScopeEscalatingOAuthCallback:
    async def exchange(self, *, context: OAuthCallbackContext, code: str) -> dict[str, object]:
        del code
        assert context.requested_scopes == ("https://www.googleapis.com/auth/gmail.readonly",)
        return {
            "email_address": "user@example.test",
            "credential_ref": "credential-ref",
            "granted_scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            ],
            "provider_account_id": "account-1",
        }


class ValidOAuthCallback:
    async def exchange(self, *, context: OAuthCallbackContext, code: str) -> dict[str, object]:
        del context, code
        return {
            "email_address": "user@example.test",
            "credential_ref": "credential-ref",
            "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "provider_account_id": "account-1",
        }


class DenyIdentity:
    async def authorize(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        capability: str,
        connection_id: UUID | None = None,
    ) -> bool:
        del tenant_id, subject_id, capability, connection_id
        return False


class RecordingIdentity:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.capabilities: list[str] = []

    async def authorize(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        capability: str,
        connection_id: UUID | None = None,
    ) -> bool:
        del tenant_id, subject_id, connection_id
        self.capabilities.append(capability)
        return self.allowed


def test_api_requires_host_identity_headers() -> None:
    client = TestClient(create_app())

    response = client.get("/v1/mail/connections")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "host_identity_context_required"


def test_api_exposes_outcome_unknown_as_non_retryable_reconciliation_required() -> None:
    class OutcomeUnknownService(MailService):
        async def get_operation(
            self, *, tenant_id: str, subject_id: str, operation_id: UUID
        ) -> MailOutboxOperation:
            del tenant_id, subject_id, operation_id
            raise RepositoryConflictError("operation_outcome_unknown_reconciliation_required")

    service = OutcomeUnknownService(
        InMemoryMailRepository(), connectors={ProviderName.SANDBOX: SandboxConnector()}
    )
    client = TestClient(create_app(service))
    response = client.get(
        "/v1/mail/actions/00000000-0000-0000-0000-000000000001",
        headers={"X-MailHub-Tenant": "tenant-1", "X-MailHub-Subject": "user-1"},
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "outcome_unknown",
        "message": "operation_outcome_unknown_reconciliation_required",
        "retryable": False,
        "outcome_unknown": True,
        "details": {},
    }


def test_api_never_boots_in_memory_dependencies_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_ENV", "production")
    with pytest.raises(RuntimeError, match="mailhub_runtime_blocked"):
        create_app()


def test_api_creates_sandbox_connection_with_scoped_context() -> None:
    client = TestClient(create_app())
    headers = {"X-MailHub-Tenant": "tenant-1", "X-MailHub-Subject": "user-1"}

    response = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "user@example.test",
            "credential_ref": "credential-ref",
        },
    )

    assert response.status_code == 201
    assert response.json()["data"]["tenant_id"] == "tenant-1"
    assert "credential_ref" in response.json()["data"]


def test_api_updates_connection_scopes_only_by_narrowing() -> None:
    client = TestClient(create_app())
    headers = {"X-MailHub-Tenant": "tenant-scope-api", "X-MailHub-Subject": "user-scope-api"}
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "scope-api@example.test",
            "credential_ref": "credential-ref",
            "granted_scopes": ["mail.read", "mail.labels"],
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["data"]["connection_id"]
    revision = created.json()["data"]["revision"]

    narrowed = client.post(
        f"/v1/mail/connections/{connection_id}:scopes",
        headers=headers,
        json={"expected_revision": revision, "granted_scopes": ["mail.read"]},
    )
    assert narrowed.status_code == 200
    assert narrowed.json()["data"]["granted_scopes"] == ["mail.read"]
    assert narrowed.json()["data"]["revision"] == revision + 1

    expanded = client.post(
        f"/v1/mail/connections/{connection_id}:scopes",
        headers=headers,
        json={
            "expected_revision": narrowed.json()["data"]["revision"],
            "granted_scopes": ["mail.read", "mail.send"],
        },
    )
    assert expanded.status_code == 409
    assert expanded.json()["error"]["message"] == "scope_expansion_requires_reauthorization"


def test_api_returns_projection_only_connection_impact_preview() -> None:
    client = TestClient(create_app())
    headers = {"X-MailHub-Tenant": "tenant-impact-api", "X-MailHub-Subject": "subject-impact-api"}
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "impact-api@example.test",
            "credential_ref": "credential-ref",
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["data"]["connection_id"]

    preview = client.get(
        f"/v1/mail/connections/{connection_id}/impact-preview",
        headers=headers,
        params=[("folder_ref", "INBOX"), ("folder_ref", "Projects")],
    )

    assert preview.status_code == 200
    payload = preview.json()["data"]
    assert payload["schema_version"] == "mailhub.connection_impact_preview.v1"
    assert payload["provider_query_performed"] is False
    assert payload["scope"]["added_folder_refs"] == ["Projects"]


def test_api_refreshes_connection_through_host_metadata_only() -> None:
    broker = StaticCredentialBroker(
        {
            "api-refresh-1": {
                "credential_ref": "api-refresh-2",
                "provider_account_id": "api-account-1",
                "credential_version": "2",
            }
        }
    )
    repository = InMemoryMailRepository()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        credential_broker=broker,
        credential_refresh=broker,
    )
    client = TestClient(create_app(service))
    headers = {"X-MailHub-Tenant": "tenant-refresh-api", "X-MailHub-Subject": "user-refresh-api"}
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "refresh-api@example.test",
            "credential_ref": "api-refresh-1",
            "provider_account_id": "api-account-1",
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["data"]["connection_id"]
    refreshed = client.post(
        f"/v1/mail/connections/{connection_id}:refresh",
        headers=headers,
        json={"expected_revision": created.json()["data"]["revision"], "reason": "scheduled"},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["credential_ref"] == "api-refresh-2"
    assert refreshed.json()["data"]["credential_version"] == 2


def test_api_exposes_sync_state_without_cross_subject_access() -> None:
    client = TestClient(create_app())
    headers = {"X-MailHub-Tenant": "tenant-sync-state", "X-MailHub-Subject": "user-1"}
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "sync-state@example.test",
            "credential_ref": "credential-ref",
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["data"]["connection_id"]

    own = client.get(f"/v1/mail/connections/{connection_id}/sync-state", headers=headers)
    assert own.status_code == 200
    assert own.json()["data"] == []

    other = client.get(
        f"/v1/mail/connections/{connection_id}/sync-state",
        headers={"X-MailHub-Tenant": "tenant-sync-state", "X-MailHub-Subject": "user-2"},
    )
    assert other.status_code == 404


def test_api_exposes_owner_scoped_typed_candidate_views() -> None:
    repository = InMemoryMailRepository()
    connection = MailboxConnection(
        connection_id=uuid4(),
        tenant_id="tenant-candidate-api",
        subject_id="user-candidate-api",
        provider=ProviderName.SANDBOX,
        email_address="candidate@example.test",
        credential_ref="sandbox",
        status=ConnectionStatus.ACTIVE,
    )
    thread = MailThread(
        thread_id=uuid4(),
        tenant_id=connection.tenant_id,
        connection_id=connection.connection_id,
        provider_thread_ref="thread-candidate-api",
        normalized_subject="candidate",
        participant_addresses=("sender@example.test", "candidate@example.test"),
        latest_at=datetime.now(UTC),
        message_count=1,
    )
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id=connection.tenant_id,
        connection_id=connection.connection_id,
        thread_id=thread.thread_id,
        provider_message_ref="message-candidate-api",
        internet_message_id=None,
        sender_address="sender@example.test",
        recipient_addresses=("candidate@example.test",),
        subject="Candidate",
        received_at=datetime.now(UTC),
        body_text="bounded body",
        content_sha256=digest_text("bounded body"),
    )
    candidate = MailActionCandidate(
        candidate_id=uuid4(),
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        message_id=message.message_id,
        candidate_type=CandidateType.PROJECT,
        payload={"project_ref": "project-1"},
        evidence=({"message_id": str(message.message_id)},),
        confidence=0.9,
    )

    async def seed() -> None:
        await repository.save_connection(connection)
        await repository.save_thread(thread)
        await repository.save_message(message)
        await repository.save_candidate(candidate)

    import asyncio

    asyncio.run(seed())
    service = MailService(repository, connectors={ProviderName.SANDBOX: SandboxConnector()})
    client = TestClient(create_app(service))
    headers = {
        "X-MailHub-Tenant": connection.tenant_id,
        "X-MailHub-Subject": connection.subject_id,
    }
    project = client.get("/v1/mail/candidates/projects", headers=headers)
    assert project.status_code == 200
    assert len(project.json()["data"]) == 1
    assert project.json()["data"][0]["candidate_type"] == "project"
    detail = client.get(f"/v1/mail/candidates/{candidate.candidate_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["candidate_id"] == str(candidate.candidate_id)
    revoked = client.post(
        f"/v1/mail/candidates/{candidate.candidate_id}:revoke",
        headers=headers,
        json={"expected_revision": 1, "reason": "source permission changed"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["data"]["status"] == "revoked"
    knowledge = client.get("/v1/mail/candidates/knowledge", headers=headers)
    assert knowledge.status_code == 200
    assert knowledge.json()["data"] == []


def test_api_controls_subscription_lifecycle_with_explicit_idempotency() -> None:
    class FakeSubscriptionPort:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        async def ensure_subscription(self, **kwargs: object) -> ProviderSubscriptionLease:
            desired = kwargs["desired_expiry"]
            assert isinstance(desired, datetime)
            provider = kwargs["provider"]
            connection_id = kwargs["connection_id"]
            callback = kwargs["callback_endpoint"]
            assert isinstance(provider, ProviderName)
            assert isinstance(connection_id, UUID)
            assert isinstance(callback, str)
            state_ref = kwargs.get("client_state_ref")
            assert state_ref is None or isinstance(state_ref, str)
            return ProviderSubscriptionLease(
                provider=provider,
                connection_id=connection_id,
                subscription_ref="subscription-api-1",
                status="active",
                expires_at=desired,
                callback_endpoint=callback,
                provider_request_id="host-request-1",
                client_state_ref=state_ref,
            )

        async def cancel_subscription(self, **kwargs: object) -> dict[str, object]:
            subscription_ref = kwargs["subscription_ref"]
            assert isinstance(subscription_ref, str)
            self.cancelled.append(subscription_ref)
            return {"status": "cancelled", "provider_request_id": "host-cancel-1"}

    subscription_port = FakeSubscriptionPort()
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.GMAIL: GmailConnector()},
        credential_broker=StaticCredentialBroker(),
        provider_subscription_port=subscription_port,
    )
    client = TestClient(create_app(service))
    headers = {
        "X-MailHub-Tenant": "tenant-subscription-api",
        "X-MailHub-Subject": "user-subscription-api",
    }
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "gmail",
            "email_address": "subscription@example.test",
            "credential_ref": "credential-ref",
            "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["data"]["connection_id"]
    activated = client.post(
        f"/v1/mail/connections/{connection_id}:activate",
        headers=headers,
        json={"expected_revision": 1},
    )
    assert activated.status_code == 200

    expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    missing_key = client.post(
        f"/v1/mail/connections/{connection_id}/subscription:ensure",
        headers=headers,
        json={"callback_endpoint": "https://host.example.test/mailhook", "desired_expiry": expiry},
    )
    assert missing_key.status_code == 422
    assert missing_key.json()["detail"]["code"] == "subscription_idempotency_key_required"

    ensured = client.post(
        f"/v1/mail/connections/{connection_id}/subscription:ensure",
        headers=headers | {"Idempotency-Key": "subscription-api-ensure"},
        json={
            "callback_endpoint": "https://host.example.test/mailhook",
            "desired_expiry": expiry,
            "client_state_ref": "state-ref-1",
        },
    )
    assert ensured.status_code == 200
    assert ensured.json()["data"]["subscription_status"] == "active"
    assert "subscription_client_state_ref" not in ensured.json()["data"]
    assert ensured.json()["data"]["subscription_client_state_ref_present"] is True
    assert (
        client.get(f"/v1/mail/connections/{connection_id}/sync-state", headers=headers).json()[
            "data"
        ][0]["subscription_ref"]
        == "subscription-api-1"
    )

    cancelled = client.post(
        f"/v1/mail/connections/{connection_id}/subscription:cancel",
        headers=headers | {"Idempotency-Key": "subscription-api-cancel"},
        json={},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["subscription_status"] == "cancelled"
    assert subscription_port.cancelled == ["subscription-api-1"]


def test_verified_provider_notification_routes_by_host_delivery_and_is_idempotent() -> None:
    class FakeSubscriptionPort:
        async def ensure_subscription(self, **kwargs: object) -> ProviderSubscriptionLease:
            desired = kwargs["desired_expiry"]
            assert isinstance(desired, datetime)
            return ProviderSubscriptionLease(
                provider=kwargs["provider"],  # type: ignore[arg-type]
                connection_id=kwargs["connection_id"],  # type: ignore[arg-type]
                subscription_ref="subscription-notification-api",
                status="active",
                expires_at=desired,
                callback_endpoint=kwargs["callback_endpoint"],  # type: ignore[arg-type]
            )

        async def cancel_subscription(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            return {"status": "cancelled"}

    class FakeVerifier:
        def __init__(self) -> None:
            self.calls: list[tuple[ProviderName, bytes]] = []
            self.delivery: ProviderNotificationDelivery | None = None

        async def verify_and_route(
            self,
            *,
            provider: ProviderName,
            body: bytes,
            headers: dict[str, str],
        ) -> tuple[ProviderNotificationDelivery, ...]:
            del headers
            self.calls.append((provider, body))
            assert self.delivery is not None
            return (self.delivery,)

    verifier = FakeVerifier()
    repository = InMemoryMailRepository()
    service = MailService(
        repository,
        connectors={ProviderName.GMAIL: GmailConnector()},
        credential_broker=StaticCredentialBroker(),
        provider_subscription_port=FakeSubscriptionPort(),
    )
    client = TestClient(
        create_app(service, provider_notification_verifier=verifier)  # type: ignore[arg-type]
    )
    headers = {
        "X-MailHub-Tenant": "tenant-notification-api",
        "X-MailHub-Subject": "subject-notification-api",
    }
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "gmail",
            "email_address": "notification@example.test",
            "credential_ref": "credential-ref",
            "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        },
    )
    assert created.status_code == 201
    connection_id = UUID(created.json()["data"]["connection_id"])
    activated = client.post(
        f"/v1/mail/connections/{connection_id}:activate",
        headers=headers,
        json={"expected_revision": 1},
    )
    assert activated.status_code == 200
    expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    ensured = client.post(
        f"/v1/mail/connections/{connection_id}/subscription:ensure",
        headers=headers | {"Idempotency-Key": "notification-subscription"},
        json={
            "callback_endpoint": "https://host.example.test/provider-notifications",
            "desired_expiry": expiry,
        },
    )
    assert ensured.status_code == 200
    now = datetime.now(UTC)
    verifier.delivery = ProviderNotificationDelivery(
        tenant_id=headers["X-MailHub-Tenant"],
        subject_id=headers["X-MailHub-Subject"],
        connection_id=connection_id,
        folder_ref="INBOX",
        notification=ProviderNotification(
            provider=ProviderName.GMAIL,
            notification_id="provider-notification-1",
            subscription_ref="subscription-notification-api",
            resource_ref="gmail-account:account-digest",
            change_kind=NotificationChangeKind.UPDATED,
            received_at=now,
            cursor_hint="123",
        ),
    )
    body = b'{"message":{}}'
    first = client.post(
        "/v1/mail/provider-notifications/gmail",
        headers={"Content-Type": "application/json", "X-Trace-Id": "trace-notification"},
        content=body,
    )
    assert first.status_code == 202
    assert first.json()["data"]["accepted"] == 1
    assert first.json()["data"]["notifications"][0]["sync_job_status"] == "queued"
    second = client.post(
        "/v1/mail/provider-notifications/gmail",
        headers={"Content-Type": "application/json"},
        content=body,
    )
    assert second.status_code == 202
    assert second.json()["data"]["duplicates"] == 1
    assert len(verifier.calls) == 2
    assert any(
        event["event_type"] == "mail.provider.notification.duplicate" for event in repository.audits
    )
    assert any(
        event["event_type"] == "mail.provider.notification.ack"
        and isinstance(event.get("ack_latency_ms"), int)
        and cast(int, event["ack_latency_ms"]) >= 1
        for event in repository.audits
    )

    replay = client.post(
        "/v1/mail/webhooks/receipts/gmail/provider-notification-1:replay",
        headers=headers | {"Idempotency-Key": "receipt-replay-1"},
        json={
            "expected_body_sha256": hashlib.sha256(body).hexdigest(),
            "reason": "recover route failure",
        },
    )
    assert replay.status_code == 202
    assert replay.json()["data"]["mode"] == "reconcile"
    replay_again = client.post(
        "/v1/mail/webhooks/receipts/gmail/provider-notification-1:replay",
        headers=headers | {"Idempotency-Key": "receipt-replay-1"},
        json={
            "expected_body_sha256": hashlib.sha256(body).hexdigest(),
            "reason": "retry same operator request",
        },
    )
    assert replay_again.status_code == 202
    assert replay_again.json()["data"]["job_ref"] == replay.json()["data"]["job_ref"]
    mismatch = client.post(
        "/v1/mail/webhooks/receipts/gmail/provider-notification-1:replay",
        headers=headers | {"Idempotency-Key": "receipt-replay-bad"},
        json={"expected_body_sha256": "0" * 64, "reason": "wrong digest"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "webhook_receipt_body_digest_mismatch"

    verifier.delivery = ProviderNotificationDelivery(
        tenant_id=headers["X-MailHub-Tenant"],
        subject_id=headers["X-MailHub-Subject"],
        connection_id=connection_id,
        folder_ref="INBOX",
        notification=ProviderNotification(
            provider=ProviderName.GMAIL,
            notification_id="provider-lifecycle-subscription-removed",
            subscription_ref="subscription-notification-api",
            resource_ref="subscription:subscription-notification-api",
            change_kind=NotificationChangeKind.LIFECYCLE,
            received_at=now + timedelta(minutes=1),
            lifecycle_event="subscriptionRemoved",
        ),
    )
    removed_response = client.post(
        "/v1/mail/provider-notifications/gmail",
        headers={"Content-Type": "application/json"},
        content=b'{"message":{"lifecycle":"subscriptionRemoved"}}',
    )
    assert removed_response.status_code == 202
    removed_result = removed_response.json()["data"]["notifications"][0]
    assert removed_result["sync_job_status"] == "queued"
    assert repository.connections[connection_id].status is ConnectionStatus.DEGRADED
    assert removed_result["sync_state"]["subscription_status"] == "renewal_required"

    verifier.delivery = ProviderNotificationDelivery(
        tenant_id=headers["X-MailHub-Tenant"],
        subject_id=headers["X-MailHub-Subject"],
        connection_id=connection_id,
        folder_ref="INBOX",
        notification=ProviderNotification(
            provider=ProviderName.GMAIL,
            notification_id="provider-lifecycle-missed",
            subscription_ref="subscription-notification-api",
            resource_ref="subscription:subscription-notification-api",
            change_kind=NotificationChangeKind.LIFECYCLE,
            received_at=now + timedelta(minutes=2),
            lifecycle_event="missed",
        ),
    )
    missed_response = client.post(
        "/v1/mail/provider-notifications/gmail",
        headers={"Content-Type": "application/json"},
        content=b'{"message":{"lifecycle":"missed"}}',
    )
    assert missed_response.status_code == 202
    missed_result = missed_response.json()["data"]["notifications"][0]
    assert missed_result.get("sync_job_status") == "queued", missed_response.json()
    assert repository.connections[connection_id].status is ConnectionStatus.DEGRADED

    verifier.delivery = ProviderNotificationDelivery(
        tenant_id=headers["X-MailHub-Tenant"],
        subject_id=headers["X-MailHub-Subject"],
        connection_id=connection_id,
        folder_ref="INBOX",
        notification=ProviderNotification(
            provider=ProviderName.GMAIL,
            notification_id="provider-lifecycle-reauthorization",
            subscription_ref="subscription-notification-api",
            resource_ref="subscription:subscription-notification-api",
            change_kind=NotificationChangeKind.LIFECYCLE,
            received_at=now + timedelta(minutes=1),
            lifecycle_event="reauthorizationRequired",
        ),
    )
    lifecycle_response = client.post(
        "/v1/mail/provider-notifications/gmail",
        headers={"Content-Type": "application/json"},
        content=b'{"message":{"lifecycle":"reauthorizationRequired"}}',
    )
    assert lifecycle_response.status_code == 202
    lifecycle_result = lifecycle_response.json()["data"]["notifications"][0]
    assert lifecycle_result["sync_job_status"] == "blocked"
    assert lifecycle_result["sync_job_error_code"] == "connection_reauthorization_required"
    lifecycle_connection = repository.connections.get(connection_id)
    assert lifecycle_connection is not None
    assert lifecycle_connection.status is ConnectionStatus.REAUTHORIZATION_REQUIRED


def test_provider_notification_verifier_failure_is_fail_closed_without_receipt() -> None:
    class RejectingVerifier:
        async def verify_and_route(
            self,
            *,
            provider: ProviderName,
            body: bytes,
            headers: dict[str, str],
        ) -> tuple[ProviderNotificationDelivery, ...]:
            del provider, body, headers
            raise ValueError("untrusted_provider_callback")

    repository = InMemoryMailRepository()
    service = MailService(repository, connectors={ProviderName.GMAIL: GmailConnector()})
    client = TestClient(
        create_app(
            service,
            provider_notification_verifier=RejectingVerifier(),  # type: ignore[arg-type]
        )
    )
    response = client.post(
        "/v1/mail/provider-notifications/gmail",
        headers={"Content-Type": "application/json"},
        content=b"{}",
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "provider_notification_verification_failed"
    assert repository.webhook_receipts == {}


def test_api_deletes_connection_and_exports_user_scoped_data() -> None:
    client = TestClient(create_app())
    headers = {"X-MailHub-Tenant": "tenant-1", "X-MailHub-Subject": "user-1"}
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "user@example.test",
            "credential_ref": "credential-ref",
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["data"]["connection_id"]
    revision = created.json()["data"]["revision"]

    exported = client.get("/v1/mail/data-export", headers=headers)
    assert exported.status_code == 200
    assert exported.json()["data"]["schema_version"] == "mailhub.data_export.v1"
    assert "credential_ref" not in str(exported.json())

    deleted = client.post(
        f"/v1/mail/connections/{connection_id}:delete",
        headers=headers,
        json={"expected_revision": revision},
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["status"] == "deleted"


def test_api_rule_contract_exposes_explicit_l3a_level() -> None:
    client = TestClient(create_app())
    headers = {"X-MailHub-Tenant": "tenant-1", "X-MailHub-Subject": "user-1"}

    response = client.post(
        "/v1/mail/rules",
        headers=headers,
        json={
            "name": "label triage",
            "conditions": [{"field": "subject", "operator": "contains", "value": "urgent"}],
            "action_type": "label",
            "action_params": {"label": "triage"},
            "automation_level": "l3a_bounded_organize",
        },
    )

    assert response.status_code == 201
    assert response.json()["data"]["automation_level"] == "l3a_bounded_organize"


def test_policy_and_delegation_lists_are_owner_scoped() -> None:
    client = TestClient(create_app())
    headers = {"X-MailHub-Tenant": "tenant-policy", "X-MailHub-Subject": "owner-1"}
    created_policy = client.post(
        "/v1/mail/agent-policies",
        headers=headers,
        json={"allowed_actions": ["label"], "valid_days": 7},
    )
    assert created_policy.status_code == 201
    policy_id = created_policy.json()["data"]["policy_id"]

    created_grant = client.post(
        "/v1/mail/delegations",
        headers=headers,
        json={
            "policy_id": policy_id,
            "agent_subject_id": "agent-1",
            "capability_ids": ["label"],
            "valid_days": 2,
        },
    )
    assert created_grant.status_code == 201

    assert client.get("/v1/mail/agent-policies", headers=headers).json()["data"]
    assert client.get("/v1/mail/delegations", headers=headers).json()["data"]
    other_scope = {
        "X-MailHub-Tenant": "tenant-policy",
        "X-MailHub-Subject": "owner-2",
    }
    assert client.get("/v1/mail/agent-policies", headers=other_scope).json()["data"] == []
    assert client.get("/v1/mail/delegations", headers=other_scope).json()["data"] == []


def test_api_readiness_labels_default_graph_as_sandbox() -> None:
    response = TestClient(create_app()).get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "sandbox"


def test_api_readiness_degrades_real_provider_on_default_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")

    response = TestClient(create_app()).get("/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert "real_provider_requires_injected_runtime" in payload["missing"]
    assert "oauth_host_boundary" in payload["missing"]
    assert "credential_broker_endpoint" in payload["missing"]
    assert "object_store_endpoint" in payload["missing"]


def test_api_readiness_requires_host_ports_when_provider_push_is_enabled() -> None:
    app = create_app(
        runtime_settings=MailHubSettings(
            gmail_enabled=True,
            gmail_push_enabled=True,
        )
    )

    payload = TestClient(app).get("/health/ready").json()

    assert payload["status"] == "degraded"
    assert "provider_subscription_endpoint" in payload["missing"]
    assert "provider_notification_verifier_endpoint" in payload["missing"]


def test_event_publisher_endpoint_wires_default_graph() -> None:
    app = create_app(
        runtime_settings=MailHubSettings(event_publisher_endpoint="https://events.example.test")
    )
    assert app.state.mail_service.event_publisher is not None


def test_default_graph_wires_configured_telemetry_and_quota_endpoints() -> None:
    app = create_app(
        runtime_settings=MailHubSettings(
            telemetry_endpoint="https://telemetry.example.test",
            quota_endpoint="https://quota.example.test",
        )
    )

    assert app.state.mail_service.telemetry is not None
    assert app.state.mail_service.quota_port is not None


def test_default_graph_does_not_advertise_disabled_real_providers() -> None:
    response = TestClient(create_app()).get("/v1/mail/providers/capabilities")

    assert response.status_code == 200
    providers = {item["provider"] for item in response.json()["data"]}
    assert providers == {"sandbox"}


def test_search_declares_metadata_coverage_and_provider_mode_is_degraded() -> None:
    client = TestClient(create_app())
    headers = {"X-MailHub-Tenant": "tenant-search", "X-MailHub-Subject": "user-search"}

    metadata = client.get("/v1/mail/search?q=RFQ", headers=headers)

    assert metadata.status_code == 200
    assert metadata.json()["mode"] == "metadata"
    assert metadata.json()["complete"] is False
    assert metadata.json()["incomplete_reason"] == "projection_metadata_only"
    assert "body" not in metadata.json()["coverage"]
    assert "recipient_headers" in metadata.json()["coverage"]

    provider = client.get("/v1/mail/search?q=RFQ&mode=provider", headers=headers)

    assert provider.status_code == 503
    assert provider.json()["error"]["message"] == "provider_search_unavailable"
    assert provider.json()["error"]["details"]["available_mode"] == "metadata"


def test_real_provider_switches_are_needed_before_capability_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_MICROSOFT_GRAPH_ENABLED", "true")

    response = TestClient(create_app()).get("/v1/mail/providers/capabilities")

    assert response.status_code == 200
    providers = {item["provider"] for item in response.json()["data"]}
    assert providers == {"sandbox", "gmail", "microsoft_graph"}


def test_complete_provider_registration_builds_host_oauth_boundary() -> None:
    oauth_service, callback = _build_default_oauth(
        MailHubSettings(
            gmail_enabled=True,
            credential_broker_endpoint="https://broker.example.test",
            host_service_token=SecretStr("host-service-token-at-least-32-characters"),
            oauth_state_signing_secret=SecretStr("s" * 32),
            gmail_client_id="gmail-client",
            gmail_authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            gmail_redirect_uris=("https://app.example.test/callback",),
            gmail_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        )
    )

    assert oauth_service is not None
    assert callback is not None
    assert cast(Any, callback).headers == {
        "Authorization": "Bearer host-service-token-at-least-32-characters"
    }


def test_oauth_callback_rejects_scope_escalation_from_host_exchange() -> None:
    import asyncio

    redirect_uri = "https://app.example.test/mail/oauth/callback"
    oauth_service = OAuthAuthorizationService(
        signing_secret=b"s" * 32,
        state_store=InMemoryOAuthStateStore(),
        allowed_redirect_uris={OAuthProvider.GMAIL: (redirect_uri,)},
    )
    request = asyncio.run(
        oauth_service.begin(
            provider=OAuthProvider.GMAIL,
            tenant_id="tenant-oauth-scope",
            subject_id="subject-oauth-scope",
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            client_id="gmail-client",
            redirect_uri=redirect_uri,
            scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        )
    )
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.GMAIL: GmailConnector()},
    )
    client = TestClient(
        create_app(
            service,
            oauth_service=oauth_service,
            oauth_callback_port=ScopeEscalatingOAuthCallback(),
        )
    )

    response = client.post(
        "/v1/mail/oauth/gmail:callback",
        headers={
            "X-MailHub-Tenant": "tenant-oauth-scope",
            "X-MailHub-Subject": "subject-oauth-scope",
        },
        json={"state": request.state, "code": "one-time-code", "redirect_uri": redirect_uri},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "oauth_exchange_write_scope_disallowed"


def test_oauth_callback_audits_redacted_flow_and_replay_rejection() -> None:
    import asyncio

    redirect_uri = "https://app.example.test/mail/oauth/callback"
    oauth_service = OAuthAuthorizationService(
        signing_secret=b"s" * 32,
        state_store=InMemoryOAuthStateStore(),
        allowed_redirect_uris={OAuthProvider.GMAIL: (redirect_uri,)},
    )
    request = asyncio.run(
        oauth_service.begin(
            provider=OAuthProvider.GMAIL,
            tenant_id="tenant-oauth-audit",
            subject_id="subject-oauth-audit",
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            client_id="gmail-client",
            redirect_uri=redirect_uri,
            scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        )
    )
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.GMAIL: GmailConnector()},
    )
    client = TestClient(
        create_app(
            service,
            oauth_service=oauth_service,
            oauth_callback_port=ValidOAuthCallback(),
        )
    )
    headers = {
        "X-MailHub-Tenant": "tenant-oauth-audit",
        "X-MailHub-Subject": "subject-oauth-audit",
    }
    body = {"state": request.state, "code": "one-time-code", "redirect_uri": redirect_uri}

    completed = client.post("/v1/mail/oauth/gmail:callback", headers=headers, json=body)
    replayed = client.post("/v1/mail/oauth/gmail:callback", headers=headers, json=body)

    assert completed.status_code == 200
    assert replayed.status_code == 400
    assert replayed.json()["detail"]["code"] == "oauth_state_replayed_or_missing"
    events = client.get("/v1/mail/audit", headers=headers).json()["data"]
    oauth_completed = next(item for item in events if item["event_type"] == "mail.oauth.completed")
    oauth_rejected = next(
        item for item in events if item["event_type"] == "mail.oauth.callback_rejected"
    )
    assert oauth_completed["requested_scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert oauth_completed["granted_scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert oauth_completed["scope_subset_verified"] is True
    assert oauth_completed["pkce_verified"] is True
    assert oauth_completed["oauth_flow_ref_sha256"] == oauth_rejected["oauth_flow_ref_sha256"]
    assert oauth_rejected["reason"] == "oauth_state_replayed_or_missing"
    assert "credential-ref" not in str(events)
    assert "one-time-code" not in str(events)


@pytest.mark.parametrize(
    ("provider_config",),
    [
        (
            {
                "gmail_enabled": True,
                "gmail_client_id": "gmail-client",
                "gmail_authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "gmail_redirect_uris": ("https://app.example.test/callback",),
                "gmail_scopes": (
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/gmail.modify",
                ),
            },
        ),
        (
            {
                "gmail_enabled": True,
                "gmail_read_only": False,
                "gmail_client_id": "gmail-client",
                "gmail_authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "gmail_redirect_uris": ("https://app.example.test/callback",),
                "gmail_scopes": ("https://www.googleapis.com/auth/gmail.readonly",),
            },
        ),
        (
            {
                "gmail_enabled": True,
                "gmail_client_id": "gmail-client",
                "gmail_authorization_endpoint": (
                    "https://accounts.google.com/o/oauth2/v2/auth?unexpected=query"
                ),
                "gmail_redirect_uris": ("https://app.example.test/callback",),
                "gmail_scopes": ("https://www.googleapis.com/auth/gmail.readonly",),
            },
        ),
        (
            {
                "gmail_enabled": True,
                "gmail_client_id": "gmail-client",
                "gmail_authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "gmail_redirect_uris": ("https://app.example.test/callback?next=evil",),
                "gmail_scopes": ("https://www.googleapis.com/auth/gmail.readonly",),
            },
        ),
        (
            {
                "microsoft_graph_enabled": True,
                "graph_client_id": "graph-client",
                "graph_authorization_endpoint": (
                    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
                ),
                "graph_redirect_uris": ("https://app.example.test/callback",),
                "graph_scopes": ("mail.read", "offline_access", "Mail.Send"),
            },
        ),
        (
            {
                "microsoft_graph_enabled": True,
                "graph_client_id": "graph-client",
                "graph_authorization_endpoint": (
                    "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
                ),
                "graph_authority_tenant": "common",
                "graph_redirect_uris": ("https://app.example.test/callback",),
                "graph_scopes": ("mail.read", "offline_access"),
            },
        ),
    ],
)
def test_default_oauth_rejects_write_capable_or_non_read_only_registration(
    provider_config: dict[str, Any],
) -> None:
    oauth_service, callback = _build_default_oauth(
        MailHubSettings(
            credential_broker_endpoint="https://broker.example.test",
            oauth_state_signing_secret=SecretStr("s" * 32),
            **provider_config,
        )
    )

    assert oauth_service is None
    assert callback is None


def test_api_delegates_identity_to_host_port_when_configured() -> None:
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        host_identity=DenyIdentity(),
    )
    client = TestClient(create_app(service))
    response = client.get(
        "/v1/mail/connections",
        headers={"X-MailHub-Tenant": "tenant-1", "X-MailHub-Subject": "user-1"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "host_identity_denied"


def test_webhook_receipt_list_requires_audit_entitlement() -> None:
    identity = RecordingIdentity()
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        host_identity=identity,
    )
    client = TestClient(create_app(service))
    headers = {"X-MailHub-Tenant": "tenant-audit", "X-MailHub-Subject": "user-audit"}

    response = client.get("/v1/mail/webhooks/receipts", headers=headers)

    assert response.status_code == 200
    assert identity.capabilities == ["mail.audit"]


def test_api_exposes_pauseable_autonomy_run_contract() -> None:
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        credential_broker=StaticCredentialBroker({"credential-ref": {"mode": "sandbox"}}),
    )
    client = TestClient(create_app(service))
    headers = {"X-MailHub-Tenant": "tenant-auto-api", "X-MailHub-Subject": "owner-auto-api"}
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "owner@auto-api.example.test",
            "credential_ref": "credential-ref",
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["data"]["connection_id"]

    queued = client.post(
        "/v1/mail/autonomy/runs",
        headers=headers,
        json={"connection_id": connection_id, "replay_key": "api-cycle"},
    )
    assert queued.status_code == 202
    run_id = queued.json()["data"]["run_id"]
    assert queued.json()["data"]["status"] == "queued"

    idem_headers = headers | {"Idempotency-Key": "api-idempotent-cycle"}
    first_idempotent = client.post(
        "/v1/mail/autonomy/runs",
        headers=idem_headers,
        json={"connection_id": connection_id},
    )
    second_idempotent = client.post(
        "/v1/mail/autonomy/runs",
        headers=idem_headers,
        json={"connection_id": connection_id},
    )
    assert first_idempotent.status_code == second_idempotent.status_code == 202
    assert first_idempotent.json()["data"]["run_id"] == second_idempotent.json()["data"]["run_id"]

    paused = client.post(
        f"/v1/mail/autonomy/runs/{run_id}:pause",
        headers=headers,
        json={"reason": "owner_pause"},
    )
    assert paused.status_code == 200
    assert paused.json()["data"]["status"] == "paused"

    resumed = client.post(
        f"/v1/mail/autonomy/runs/{run_id}:resume",
        headers=headers,
    )
    assert resumed.status_code == 200
    assert resumed.json()["data"]["status"] == "queued"

    completed = client.post(
        f"/v1/mail/autonomy/runs/{run_id}:run",
        headers=headers,
    )
    assert completed.status_code == 200
    assert completed.json()["data"]["status"] == "completed"
    assert client.get("/v1/mail/autonomy/runs", headers=headers).json()["data"]
