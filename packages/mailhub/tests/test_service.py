import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from mailhub.connectors.http_providers import GmailConnector, MicrosoftGraphConnector
from mailhub.connectors.sandbox import SandboxConnector
from mailhub.domain import (
    ActionType,
    AutomationLevel,
    ConnectionStatus,
    DelegationGrant,
    DeliveryStatus,
    MailAgentPolicy,
    MailboxConnection,
    MailMessageProjection,
    MailThread,
    ProviderName,
    SyncJobStatus,
    digest_addresses,
    digest_recipient_headers,
    digest_text,
)
from mailhub.errors import (
    ApprovalRequiredError,
    AuthorizationError,
    ConflictError,
    ProviderFailureError,
    RateLimitedError,
)
from mailhub.hosts.inmemory import RecordingEventPublisher
from mailhub.observability import InMemoryTelemetryPort
from mailhub.ports import (
    ProviderCapabilities,
    ProviderMessage,
    ProviderSendReceipt,
    ProviderSendRequest,
    ProviderSubscriptionLease,
    ProviderSyncFilter,
    ProviderSyncPage,
)
from mailhub.quota import InMemoryQuotaPort, QuotaLimits
from mailhub.service import (
    InMemoryApprovalPort,
    MailService,
    StaticCredentialBroker,
    _contains_high_risk_terms,
    _validate_credential_binding,
)
from mailhub.storage import InMemoryMailRepository, InMemoryObjectStore


class RepeatedThreadConnector:
    """Provider double that returns two messages from one thread on every page."""

    def __init__(self, messages: tuple[ProviderMessage, ...]) -> None:
        self.messages = messages

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=ProviderName.SANDBOX,
            authorization_modes=("fixture",),
            supports_push=False,
            supports_incremental=True,
            supports_backfill=True,
            supports_draft=False,
            supports_send=False,
            supports_labels=False,
            supports_attachments=False,
            supports_search=False,
        )

    async def sync(
        self,
        _connection: object,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        del cursor, credential, sync_filter
        return ProviderSyncPage(
            messages=self.messages[:limit],
            next_cursor="cursor-1",
            provider_cursor_kind="fixture",
            provider_request_id="fixture-sync",
        )

    async def send(
        self, request: ProviderSendRequest, *, credential: Mapping[str, str]
    ) -> ProviderSendReceipt:
        del request, credential
        raise AssertionError("fixture_send_not_expected")

    async def health_check(self, _connection: object) -> dict[str, object]:
        return {"status": "ok"}


class ReauthorizationConnector(RepeatedThreadConnector):
    def __init__(
        self,
        messages: tuple[ProviderMessage, ...],
        *,
        failure_reason: str = "http_401",
    ) -> None:
        super().__init__(messages)
        self.failure_reason = failure_reason

    async def sync(
        self,
        _connection: object,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        del cursor, limit, credential, sync_filter
        raise ProviderFailureError(self.failure_reason)


class RateLimitedConnector(RepeatedThreadConnector):
    async def sync(
        self,
        _connection: object,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        del cursor, limit, credential, sync_filter
        raise RateLimitedError(
            "provider_sync_rate_limited",
            details={
                "provider_status": 403,
                "provider_reason": "userratelimitexceeded",
                "retry_after_seconds": 17,
            },
        )


class TransientRateLimitedConnector(RepeatedThreadConnector):
    def __init__(self, *, success_after: int | None = 1) -> None:
        super().__init__(messages=())
        self.calls = 0
        self.success_after = success_after

    async def sync(
        self,
        _connection: object,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        del cursor, limit, credential, sync_filter
        self.calls += 1
        if self.success_after is None or self.calls <= self.success_after:
            raise RateLimitedError(
                "provider_sync_rate_limited",
                details={"provider_status": 429, "retry_after_seconds": 1},
            )
        return ProviderSyncPage(
            messages=(),
            next_cursor="cursor-after-retry",
            provider_cursor_kind="fixture",
            provider_request_id="fixture-retry-success",
        )


class DeletionConnector(RepeatedThreadConnector):
    """Provider double that reports one message and then its deletion."""

    def __init__(self, message: ProviderMessage) -> None:
        super().__init__((message,))
        self.calls = 0

    async def sync(
        self,
        _connection: object,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        del cursor, limit, credential, sync_filter
        self.calls += 1
        if self.calls == 1:
            return ProviderSyncPage(
                messages=self.messages,
                next_cursor="cursor-1",
                provider_cursor_kind="fixture",
                provider_request_id="fixture-sync-1",
            )
        return ProviderSyncPage(
            messages=(),
            next_cursor=f"cursor-{self.calls}",
            provider_cursor_kind="fixture",
            provider_request_id=f"fixture-sync-{self.calls}",
            deleted_message_refs=(self.messages[0].provider_message_ref,),
        )


class BlockingSyncConnector(RepeatedThreadConnector):
    """Provider double used to exercise durable cancellation checkpoints."""

    def __init__(self) -> None:
        super().__init__(messages=())
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.provider_calls = 0

    async def sync(
        self,
        _connection: object,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        del cursor, limit, credential, sync_filter
        self.provider_calls += 1
        self.started.set()
        await self.release.wait()
        return ProviderSyncPage(
            messages=(),
            next_cursor="cancelled-cursor",
            provider_cursor_kind="fixture",
            provider_request_id="fixture-cancelled",
        )


class LifecycleSubscriptionPort:
    def __init__(self) -> None:
        self.ensure_calls = 0
        self.subscription_ref = "graph-subscription-lifecycle"

    async def ensure_subscription(self, **kwargs: object) -> ProviderSubscriptionLease:
        self.ensure_calls += 1
        return ProviderSubscriptionLease(
            provider=kwargs["provider"],  # type: ignore[arg-type]
            connection_id=kwargs["connection_id"],  # type: ignore[arg-type]
            subscription_ref=self.subscription_ref,
            status="active",
            expires_at=kwargs["desired_expiry"],  # type: ignore[arg-type]
            callback_endpoint=kwargs["callback_endpoint"],  # type: ignore[arg-type]
            client_state_ref=kwargs.get("client_state_ref"),  # type: ignore[arg-type]
        )

    async def cancel_subscription(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"status": "cancelled"}


def test_high_risk_terms_are_conservative_and_body_free_in_evidence() -> None:
    assert _contains_high_risk_terms("Re: supplier contract", "Please review") is True
    assert _contains_high_risk_terms("普通更新", "请查看进度") is False
    assert _contains_high_risk_terms("报价审批", None) is True


@pytest.mark.asyncio
async def test_sync_auth_failure_marks_reauthorization_and_publishes_event() -> None:
    events = RecordingEventPublisher()
    service = MailService(
        InMemoryMailRepository(),
        connectors={
            ProviderName.SANDBOX: ReauthorizationConnector(messages=()),
        },
        event_publisher=events,
    )
    connection = await service.create_connection(
        tenant_id="tenant-reauth",
        subject_id="user-reauth",
        provider=ProviderName.SANDBOX,
        email_address="user@reauth.example.test",
        credential_ref="cred-reauth",
    )

    with pytest.raises(ProviderFailureError, match="http_401"):
        await service.sync_connection(
            tenant_id="tenant-reauth",
            subject_id="user-reauth",
            connection_id=connection.connection_id,
        )

    stored = await service.repository.get_connection(
        tenant_id="tenant-reauth", connection_id=connection.connection_id
    )
    assert stored is not None
    assert stored.status.value == "reauthorization_required"
    reauth = [
        event
        for event in events.events
        if event["event_type"] == "mail.connection.reauthorization_required"
    ]
    assert len(reauth) == 1
    assert reauth[0]["data"]["error_code"] == "http_401"  # type: ignore[index]

    permission_service = MailService(
        InMemoryMailRepository(),
        connectors={
            ProviderName.SANDBOX: ReauthorizationConnector(
                messages=(), failure_reason="provider_sync_permission_denied"
            ),
        },
    )
    permission_connection = await permission_service.create_connection(
        tenant_id="tenant-permission",
        subject_id="user-permission",
        provider=ProviderName.SANDBOX,
        email_address="user@permission.example.test",
        credential_ref="cred-permission",
    )
    with pytest.raises(ProviderFailureError, match="permission_denied"):
        await permission_service.sync_connection(
            tenant_id="tenant-permission",
            subject_id="user-permission",
            connection_id=permission_connection.connection_id,
        )
    permission_stored = await permission_service.repository.get_connection(
        tenant_id="tenant-permission", connection_id=permission_connection.connection_id
    )
    assert permission_stored is not None
    assert permission_stored.status is ConnectionStatus.REAUTHORIZATION_REQUIRED

    rate_events = RecordingEventPublisher()
    rate_telemetry = InMemoryTelemetryPort()
    rate_service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: RateLimitedConnector(messages=())},
        event_publisher=rate_events,
        telemetry=rate_telemetry,
    )
    rate_connection = await rate_service.create_connection(
        tenant_id="tenant-rate-limit",
        subject_id="user-rate-limit",
        provider=ProviderName.SANDBOX,
        email_address="user@rate-limit.example.test",
        credential_ref="cred-rate-limit",
    )
    with pytest.raises(RateLimitedError, match="rate_limited"):
        await rate_service.sync_connection(
            tenant_id="tenant-rate-limit",
            subject_id="user-rate-limit",
            connection_id=rate_connection.connection_id,
        )
    failed_event = [
        event for event in rate_events.events if event["event_type"] == "mail.sync.failed"
    ][-1]
    assert failed_event["data"]["retry_after_seconds"] == 17  # type: ignore[index]
    assert failed_event["data"]["provider_reason"] == "userratelimitexceeded"  # type: ignore[index]
    failed_telemetry = [
        record for record in rate_telemetry.records if record.name == "mail.sync.failed"
    ]
    assert failed_telemetry[-1].fields["provider_status"] == 403


@pytest.mark.asyncio
async def test_provider_lifecycle_reauthorization_fences_connection_and_subscription() -> None:
    repository = InMemoryMailRepository()
    events = RecordingEventPublisher()
    subscription_port = LifecycleSubscriptionPort()
    service = MailService(
        repository,
        connectors={ProviderName.GMAIL: GmailConnector()},
        provider_subscription_port=subscription_port,
        event_publisher=events,
    )
    connection = await service.create_connection(
        tenant_id="tenant-provider-lifecycle",
        subject_id="user-provider-lifecycle",
        provider=ProviderName.GMAIL,
        email_address="lifecycle@example.test",
        credential_ref="credential-ref",
        granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    connection = await service.activate_connection(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
    )
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    await service.ensure_provider_subscription(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        callback_endpoint="https://host.example.test/provider-notifications",
        desired_expiry=now + timedelta(days=7),
        idempotency_key="provider-lifecycle-subscription",
        folder_ref="INBOX",
    )

    state = await service.record_provider_notification(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        subscription_ref=subscription_port.subscription_ref,
        received_at=now + timedelta(minutes=1),
        notification_id="graph-lifecycle-1",
        change_kind="lifecycle",
        lifecycle_event="reauthorizationRequired",
        trace_id="trace-provider-lifecycle",
    )
    assert state.subscription_status.value == "renewal_required"
    stored = await repository.get_connection(
        tenant_id=connection.tenant_id, connection_id=connection.connection_id
    )
    assert stored is not None
    assert stored.status is ConnectionStatus.REAUTHORIZATION_REQUIRED
    assert any(
        event["event_type"] == "mail.connection.reauthorization_required" for event in events.events
    )
    assert any(
        audit["event_type"] == "mail.provider.lifecycle_applied"
        and audit["lifecycle_event"] == "reauthorization_required"
        for audit in repository.audits
    )


@pytest.mark.asyncio
async def test_running_sync_cancel_is_cooperative_and_fenced() -> None:
    connector = BlockingSyncConnector()
    repository = InMemoryMailRepository()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
    )
    connection = await service.create_connection(
        tenant_id="tenant-sync-cancel",
        subject_id="subject-sync-cancel",
        provider=ProviderName.SANDBOX,
        email_address="user@sync-cancel.example.test",
        credential_ref="fixture",
    )
    queued = await service.enqueue_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        idempotency_key="sync-cancel-running",
    )
    worker = asyncio.create_task(
        service.run_sync_job(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            job_id=queued.job_id,
            worker_id="worker-sync-cancel",
        )
    )
    await connector.started.wait()

    requested = await service.cancel_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        job_id=queued.job_id,
    )
    assert requested.status is SyncJobStatus.CANCELLING
    assert requested.error_code == "sync_cancel_requested"
    assert requested.lease_owner == "worker-sync-cancel"

    connector.release.set()
    completed = await worker
    assert completed.status is SyncJobStatus.CANCELLED
    assert completed.error_code == "sync_job_cancelled"
    assert completed.lease_owner is None
    assert connector.provider_calls == 1
    assert (
        await repository.get_cursor(
            tenant_id=connection.tenant_id,
            connection_id=connection.connection_id,
            folder_ref="INBOX",
        )
        is None
    )


@pytest.mark.asyncio
async def test_sync_job_persists_bounded_retry_wait_and_reclaims_when_due() -> None:
    connector = TransientRateLimitedConnector()
    repository = InMemoryMailRepository()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
    )
    connection = await service.create_connection(
        tenant_id="tenant-sync-retry",
        subject_id="subject-sync-retry",
        provider=ProviderName.SANDBOX,
        email_address="user@sync-retry.example.test",
        credential_ref="fixture",
    )
    queued = await service.enqueue_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        idempotency_key="sync-retry-once",
    )

    retry_wait = await service.run_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        job_id=queued.job_id,
        worker_id="worker-sync-retry",
    )
    assert retry_wait.status is SyncJobStatus.RETRY_WAIT
    assert retry_wait.attempt_count == 1
    assert retry_wait.next_attempt_at is not None
    assert retry_wait.finished_at is None

    repository.sync_jobs[queued.job_id] = replace(
        retry_wait,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    completed = await service.run_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        job_id=queued.job_id,
        worker_id="worker-sync-retry-2",
    )
    assert completed.status is SyncJobStatus.SUCCEEDED
    assert completed.attempt_count == 2
    assert completed.next_attempt_at is None
    assert connector.calls == 2
    assert any(audit["event_type"] == "mail.sync.retry_scheduled" for audit in repository.audits)


@pytest.mark.asyncio
async def test_sync_retry_budget_counts_total_provider_calls() -> None:
    connector = TransientRateLimitedConnector(success_after=None)
    repository = InMemoryMailRepository()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
    )
    connection = await service.create_connection(
        tenant_id="tenant-sync-retry-budget",
        subject_id="subject-sync-retry-budget",
        provider=ProviderName.SANDBOX,
        email_address="user@sync-retry-budget.example.test",
        credential_ref="fixture",
    )
    current = await service.enqueue_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        idempotency_key="sync-retry-budget",
    )

    for attempt in range(1, 6):
        current = await service.run_sync_job(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            job_id=current.job_id,
            worker_id=f"worker-sync-retry-budget-{attempt}",
        )
        assert current.attempt_count == attempt
        if attempt < 5:
            assert current.status is SyncJobStatus.RETRY_WAIT
            assert current.next_attempt_at is not None
            repository.sync_jobs[current.job_id] = replace(
                current,
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        else:
            assert current.status is SyncJobStatus.FAILED
            assert current.next_attempt_at is None

    assert connector.calls == 5


@pytest.mark.asyncio
async def test_provider_health_never_calls_connector_after_connection_revoke() -> None:
    class CountingHealthConnector(RepeatedThreadConnector):
        def __init__(self) -> None:
            super().__init__(messages=())
            self.health_calls = 0

        async def health_check(self, _connection: object) -> dict[str, object]:
            self.health_calls += 1
            return {"status": "healthy"}

    connector = CountingHealthConnector()
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: connector},
    )
    connection = await service.create_connection(
        tenant_id="tenant-health-revoke",
        subject_id="user-health-revoke",
        provider=ProviderName.SANDBOX,
        email_address="user@health-revoke.example.test",
        credential_ref="cred-health-revoke",
    )
    revoked = await service.revoke_connection(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
    )

    report = await service.provider_health(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
    )

    assert revoked.status.value == "revoked"
    assert report == (
        {
            "connection_id": str(connection.connection_id),
            "provider": "sandbox",
            "status": "revoked",
            "reason": "connection_not_active",
        },
    )
    assert connector.health_calls == 0


@pytest.mark.asyncio
async def test_sync_enqueue_records_negative_access_after_revoke() -> None:
    repository = InMemoryMailRepository()
    service = MailService(repository, connectors={ProviderName.SANDBOX: SandboxConnector()})
    connection = await service.create_connection(
        tenant_id="tenant-sync-revoke-audit",
        subject_id="subject-sync-revoke-audit",
        provider=ProviderName.SANDBOX,
        email_address="user@sync-revoke-audit.example.test",
        credential_ref="sandbox-ref",
    )
    revoked = replace(
        connection,
        status=ConnectionStatus.REVOKED,
        revision=connection.revision + 1,
    )
    await repository.save_connection(revoked)

    with pytest.raises(AuthorizationError, match="connection_not_active"):
        await service.enqueue_sync_job(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            connection_id=connection.connection_id,
            idempotency_key="post-revoke-sync",
        )

    negative = next(
        event
        for event in repository.audits
        if event["event_type"] == "mail.sync.rejected_after_revoke"
    )
    assert negative["provider_requests_after_revoke"] == 0
    assert negative["broker_requests_after_revoke"] == 0


@pytest.mark.asyncio
async def test_reauthorize_connection_fences_identity_and_credential_version() -> None:
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: SandboxConnector()},
    )
    connection = await service.create_connection(
        tenant_id="tenant-reauth-contract",
        subject_id="user-reauth-contract",
        provider=ProviderName.SANDBOX,
        email_address="user@reauth-contract.example.test",
        credential_ref="cred-reauth-1",
        provider_account_id="account-1",
        provider_tenant_id="tenant-1",
        credential_version=2,
    )
    updated = await service.reauthorize_connection(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
        email_address=connection.email_address,
        credential_ref="cred-reauth-2",
        granted_scopes=connection.granted_scopes,
        provider_account_id="account-1",
        provider_tenant_id="tenant-1",
        credential_version=3,
    )
    assert updated.revision == connection.revision + 1
    assert updated.credential_version == 3

    with pytest.raises(ProviderFailureError, match="provider_account_identity_mismatch"):
        await service.reauthorize_connection(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            connection_id=connection.connection_id,
            expected_revision=updated.revision,
            email_address=connection.email_address,
            credential_ref="cred-reauth-3",
            granted_scopes=connection.granted_scopes,
            provider_account_id="account-other",
            credential_version=4,
        )
    with pytest.raises(ConflictError, match="credential_version_rollback"):
        await service.reauthorize_connection(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            connection_id=connection.connection_id,
            expected_revision=updated.revision,
            email_address=connection.email_address,
            credential_ref="cred-reauth-3",
            granted_scopes=connection.granted_scopes,
            provider_account_id="account-1",
            credential_version=1,
        )


@pytest.mark.asyncio
async def test_real_provider_connection_paths_require_read_only_scopes() -> None:
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.GMAIL: GmailConnector()},
    )
    with pytest.raises(ValueError, match="provider_read_only_scopes_invalid"):
        await service.create_connection(
            tenant_id="tenant-scope-gate",
            subject_id="subject-scope-gate",
            provider=ProviderName.GMAIL,
            email_address="scope-gate@example.test",
            credential_ref="credential-ref",
            granted_scopes=("https://www.googleapis.com/auth/gmail.modify",),
        )

    pending = await service.create_connection(
        tenant_id="tenant-scope-gate",
        subject_id="subject-scope-gate",
        provider=ProviderName.GMAIL,
        email_address="pending-scope-gate@example.test",
        credential_ref="credential-ref",
    )
    with pytest.raises(ValueError, match="provider_read_only_scopes_invalid"):
        await service.activate_connection(
            tenant_id=pending.tenant_id,
            subject_id=pending.subject_id,
            connection_id=pending.connection_id,
            expected_revision=pending.revision,
        )

    valid = await service.create_connection(
        tenant_id="tenant-scope-gate",
        subject_id="subject-scope-gate",
        provider=ProviderName.GMAIL,
        email_address="valid-scope-gate@example.test",
        credential_ref="credential-ref",
        granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    activated = await service.activate_connection(
        tenant_id=valid.tenant_id,
        subject_id=valid.subject_id,
        connection_id=valid.connection_id,
        expected_revision=valid.revision,
    )
    assert activated.status is ConnectionStatus.ACTIVE

    graph_service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.MICROSOFT_GRAPH: MicrosoftGraphConnector()},
    )
    with pytest.raises(ValueError, match="provider_read_only_scopes_invalid"):
        await graph_service.create_connection(
            tenant_id="tenant-graph-scope-gate",
            subject_id="subject-graph-scope-gate",
            provider=ProviderName.MICROSOFT_GRAPH,
            email_address="graph-scope-gate@example.test",
            credential_ref="graph-credential-ref",
            granted_scopes=("Mail.Send",),
        )
    graph = await graph_service.create_connection(
        tenant_id="tenant-graph-scope-gate",
        subject_id="subject-graph-scope-gate",
        provider=ProviderName.MICROSOFT_GRAPH,
        email_address="graph-valid-scope-gate@example.test",
        credential_ref="graph-credential-ref",
        granted_scopes=("Mail.Read", "offline_access"),
    )
    graph_active = await graph_service.activate_connection(
        tenant_id=graph.tenant_id,
        subject_id=graph.subject_id,
        connection_id=graph.connection_id,
        expected_revision=graph.revision,
    )
    with pytest.raises(ValueError, match="provider_read_only_scopes_invalid"):
        await graph_service.update_connection_scopes(
            tenant_id=graph_active.tenant_id,
            subject_id=graph_active.subject_id,
            connection_id=graph_active.connection_id,
            expected_revision=graph_active.revision,
            granted_scopes=("Mail.Send",),
        )
    with pytest.raises(ValueError, match="provider_read_only_scopes_invalid"):
        await graph_service.reauthorize_connection(
            tenant_id=graph_active.tenant_id,
            subject_id=graph_active.subject_id,
            connection_id=graph_active.connection_id,
            expected_revision=graph_active.revision,
            email_address=graph_active.email_address,
            credential_ref="graph-credential-rotated",
            granted_scopes=("Mail.Read", "offline_access", "Mail.Send"),
        )


@pytest.mark.asyncio
async def test_refresh_connection_rotates_host_metadata_without_tokens() -> None:
    broker = StaticCredentialBroker(
        {
            "cred-refresh-1": {
                "credential_ref": "cred-refresh-2",
                "provider_account_id": "account-refresh",
                "credential_version": "3",
            }
        }
    )
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        credential_broker=broker,
        credential_refresh=broker,
    )
    connection = await service.create_connection(
        tenant_id="tenant-refresh",
        subject_id="user-refresh",
        provider=ProviderName.SANDBOX,
        email_address="user@refresh.example.test",
        credential_ref="cred-refresh-1",
        provider_account_id="account-refresh",
        credential_version=2,
    )
    refreshed = await service.refresh_connection(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
        reason="scheduled_refresh",
    )
    assert refreshed.credential_ref == "cred-refresh-2"
    assert refreshed.credential_version == 3
    assert refreshed.revision == connection.revision + 1
    audits = await service.repository.list_audit_events(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
    )
    refresh_audit = next(
        audit for audit in audits if audit["event_type"] == "mail.connection.credentials.refreshed"
    )
    assert "reason_sha256" in refresh_audit
    assert "scheduled_refresh" not in refresh_audit

    unavailable = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: SandboxConnector()},
    )
    pending = await unavailable.create_connection(
        tenant_id="tenant-refresh-unconfigured",
        subject_id="user-refresh-unconfigured",
        provider=ProviderName.SANDBOX,
        email_address="user@refresh-unconfigured.example.test",
        credential_ref="cred-refresh",
    )
    with pytest.raises(ProviderFailureError, match="credential_refresh_unconfigured"):
        await unavailable.refresh_connection(
            tenant_id=pending.tenant_id,
            subject_id=pending.subject_id,
            connection_id=pending.connection_id,
            expected_revision=pending.revision,
        )


@pytest.mark.asyncio
async def test_update_connection_scopes_only_narrows_and_is_revision_fenced() -> None:
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: SandboxConnector()},
    )
    connection = await service.create_connection(
        tenant_id="tenant-scope-update",
        subject_id="user-scope-update",
        provider=ProviderName.SANDBOX,
        email_address="user@scope-update.example.test",
        credential_ref="cred-scope-update",
        granted_scopes=("mail.read", "mail.labels"),
    )

    updated = await service.update_connection_scopes(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
        granted_scopes=(" mail.read ", "mail.read"),
    )
    assert updated.granted_scopes == ("mail.read",)
    assert updated.revision == connection.revision + 1

    with pytest.raises(ConflictError, match="scope_expansion_requires_reauthorization"):
        await service.update_connection_scopes(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            connection_id=connection.connection_id,
            expected_revision=updated.revision,
            granted_scopes=("mail.read", "mail.send"),
        )
    with pytest.raises(ValueError, match="connection_scopes_invalid"):
        await service.update_connection_scopes(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            connection_id=connection.connection_id,
            expected_revision=updated.revision,
            granted_scopes=("mail.read", "\nmail.labels"),
        )


def test_resolved_credential_metadata_cannot_cross_account_or_roll_back() -> None:
    connection = MailboxConnection(
        connection_id=uuid4(),
        tenant_id="tenant-credential-binding",
        subject_id="subject-credential-binding",
        provider=ProviderName.GMAIL,
        email_address="user@credential-binding.example.test",
        credential_ref="credential-ref",
        provider_account_id="account-1",
        provider_tenant_id="tenant-1",
        credential_version=4,
    )
    _validate_credential_binding(
        connection,
        {
            "access_token": "opaque",
            "credential_version": "4",
            "provider_account_id": "account-1",
            "provider_tenant_id": "tenant-1",
        },
    )
    with pytest.raises(AuthorizationError, match="credential_version_stale"):
        _validate_credential_binding(connection, {"credential_version": "3"})
    with pytest.raises(AuthorizationError, match="provider_account_identity_mismatch"):
        _validate_credential_binding(
            connection,
            {"credential_version": "4", "provider_account_id": "account-other"},
        )


class AcceptThenDisconnectConnector(SandboxConnector):
    """Provider double that accepts once, then loses the worker response."""

    def __init__(self) -> None:
        super().__init__()
        self.disconnect_after_accept = True

    async def send(
        self,
        request: ProviderSendRequest,
        *,
        credential: Mapping[str, str],
    ) -> ProviderSendReceipt:
        receipt = await super().send(request, credential=credential)
        if self.disconnect_after_accept:
            self.disconnect_after_accept = False
            raise TimeoutError("worker_crashed_after_provider_accept")
        return receipt


@pytest.mark.asyncio
async def test_connection_impact_preview_is_projection_only_and_scope_bound() -> None:
    repository = InMemoryMailRepository()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: SandboxConnector()},
    )
    connection = await service.create_connection(
        tenant_id="tenant-impact",
        subject_id="subject-impact",
        provider=ProviderName.SANDBOX,
        email_address="impact@example.test",
        credential_ref="credential-impact",
    )
    thread_id = uuid4()
    await repository.save_thread(
        MailThread(
            thread_id=thread_id,
            tenant_id="tenant-impact",
            connection_id=connection.connection_id,
            provider_thread_ref="thread-impact",
            normalized_subject="Impact preview",
            participant_addresses=("sender@example.test", "impact@example.test"),
            latest_at=datetime.now(UTC),
            message_count=1,
        )
    )
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-impact",
        connection_id=connection.connection_id,
        thread_id=thread_id,
        provider_message_ref="provider-impact",
        internet_message_id="<impact@example.test>",
        sender_address="sender@example.test",
        recipient_addresses=("impact@example.test",),
        subject="Impact preview",
        received_at=datetime.now(UTC),
        body_text="bounded metadata",
        content_sha256=digest_text("bounded metadata"),
    )
    await repository.save_message(message)

    preview = await service.connection_impact_preview(
        tenant_id="tenant-impact",
        subject_id="subject-impact",
        connection_id=connection.connection_id,
        requested_folder_refs=("INBOX", "Projects"),
    )

    assert preview["schema_version"] == "mailhub.connection_impact_preview.v1"
    assert preview["mode"] == "projection_only"
    assert preview["provider_query_performed"] is False
    scope = preview["scope"]
    assert isinstance(scope, Mapping)
    assert scope["current_folder_refs"] == ["INBOX"]
    assert scope["requested_folder_refs"] == ["INBOX", "Projects"]
    assert scope["added_folder_refs"] == ["Projects"]
    assert scope["remote_message_delta_estimate"] is None
    counts = preview["counts"]
    assert isinstance(counts, Mapping)
    assert counts["messages"] == 1
    assert counts["threads"] == 1
    assert any(
        event.get("event_type") == "mail.connection.impact_previewed" for event in repository.audits
    )


@pytest.mark.asyncio
async def test_sync_deduplicates_provider_identity_and_commits_cursor() -> None:
    repository = InMemoryMailRepository()
    connector = SandboxConnector()
    telemetry = InMemoryTelemetryPort()
    events = RecordingEventPublisher()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        telemetry=telemetry,
        event_publisher=events,
        quota_port=InMemoryQuotaPort(limits=QuotaLimits(max_concurrent=1)),
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="cred-1",
    )
    body = "RFQ deadline Friday"
    await connector.seed(
        ProviderMessage(
            provider_message_ref="provider-message-1",
            provider_thread_ref="provider-thread-1",
            internet_message_id="<message-1@example.test>",
            sender_address="buyer@example.test",
            recipient_addresses=("user@example.test",),
            cc_addresses=("project-team@example.test",),
            subject="RFQ",
            received_at=datetime.now(UTC),
            body_text=body,
            body_object_ref=None,
            content_sha256=digest_text(body),
        )
    )

    first = await service.sync_connection(
        tenant_id="tenant-1", subject_id="user-1", connection_id=connection.connection_id
    )
    second = await service.sync_connection(
        tenant_id="tenant-1", subject_id="user-1", connection_id=connection.connection_id
    )

    assert first["saved_count"] == 1
    assert second["saved_count"] == 0
    assert second["duplicate_count"] == 0
    assert [record.name for record in telemetry.snapshot()].count("mail.sync.completed") == 2
    event_types = [str(event["event_type"]) for event in events.events]
    assert event_types.count("mail.connection.authorized") == 1
    assert event_types.count("mail.sync.started") == 2
    assert event_types.count("mail.sync.completed") == 2
    assert event_types.count("mail.message.observed") == 1
    observed = next(
        event for event in events.events if event["event_type"] == "mail.message.observed"
    )
    assert observed["data"]["content_sha256"] == digest_text(body)  # type: ignore[index]
    assert "body_text" not in observed["data"]  # type: ignore[operator]
    assert service.quota_port is not None
    searched = await service.search_messages(tenant_id="tenant-1", subject_id="user-1", query="RFQ")
    assert len(searched) == 1
    searched_header = await service.search_messages(
        tenant_id="tenant-1", subject_id="user-1", query="project-team"
    )
    assert len(searched_header) == 1
    assert searched_header[0].cc_addresses == ("project-team@example.test",)


@pytest.mark.asyncio
async def test_sync_removes_provider_deletions_idempotently_and_cleans_body_object() -> None:
    body = "message body removed by provider"
    connector = DeletionConnector(
        ProviderMessage(
            provider_message_ref="deleted-message",
            provider_thread_ref="deleted-thread",
            internet_message_id="<deleted-message@example.test>",
            sender_address="sender@example.test",
            recipient_addresses=("user@example.test",),
            subject="To be deleted",
            received_at=datetime(2026, 7, 29, tzinfo=UTC),
            body_text=body,
            body_object_ref=None,
            content_sha256=digest_text(body),
        )
    )
    repository = InMemoryMailRepository()
    object_store = InMemoryObjectStore()
    events = RecordingEventPublisher()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        object_store=object_store,
        event_publisher=events,
    )
    connection = await service.create_connection(
        tenant_id="tenant-delete",
        subject_id="subject-delete",
        provider=ProviderName.SANDBOX,
        email_address="user@delete.example.test",
        credential_ref="fixture",
    )

    first = await service.sync_connection(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
    )
    stored_messages = await repository.list_messages_for_connection(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
    )
    assert first["saved_count"] == 1
    assert len(stored_messages) == 1
    stored = stored_messages[0]
    assert stored.body_object_ref is not None
    assert (
        await repository.delete_message_by_provider_ref(
            tenant_id="other-tenant",
            connection_id=connection.connection_id,
            provider_message_ref="deleted-message",
        )
        is None
    )
    assert (
        await repository.list_messages_for_connection(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            connection_id=connection.connection_id,
        )
        == stored_messages
    )
    assert (
        await object_store.get_text(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            object_ref=stored.body_object_ref,
        )
        == body
    )

    second = await service.sync_connection(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
    )
    assert second["deleted_count"] == 1
    assert (
        await repository.list_messages_for_connection(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            connection_id=connection.connection_id,
        )
        == ()
    )
    threads = await repository.list_threads(
        tenant_id=connection.tenant_id, subject_id=connection.subject_id, limit=50
    )
    assert len(threads) == 1
    assert threads[0].message_count == 0
    with pytest.raises(KeyError, match="object_not_found"):
        await object_store.get_text(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            object_ref=stored.body_object_ref,
        )

    # A replayed deletion is a durable no-op and must not emit a second
    # deletion event or fail the sync transaction.
    third = await service.sync_connection(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
    )
    assert third["deleted_count"] == 0
    deleted_events = [
        event for event in events.events if event["event_type"] == "mail.message.deleted"
    ]
    assert len(deleted_events) == 1
    deleted_audits = [
        audit for audit in repository.audits if audit["event_type"] == "mail.message.deleted"
    ]
    assert len(deleted_audits) == 1


@pytest.mark.asyncio
async def test_durable_sync_job_persists_deleted_count_from_provider_page() -> None:
    body = "durable deletion accounting"
    connector = DeletionConnector(
        ProviderMessage(
            provider_message_ref="job-deleted-message",
            provider_thread_ref="job-deleted-thread",
            internet_message_id="<job-deleted-message@example.test>",
            sender_address="sender@example.test",
            recipient_addresses=("user@example.test",),
            subject="Durable deletion",
            received_at=datetime(2026, 7, 29, tzinfo=UTC),
            body_text=body,
            body_object_ref=None,
            content_sha256=digest_text(body),
        )
    )
    repository = InMemoryMailRepository()
    service = MailService(repository, connectors={ProviderName.SANDBOX: connector})
    connection = await service.create_connection(
        tenant_id="tenant-delete-job",
        subject_id="subject-delete-job",
        provider=ProviderName.SANDBOX,
        email_address="user@delete-job.example.test",
        credential_ref="fixture",
    )

    first_job = await service.enqueue_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        idempotency_key="delete-job-first",
    )
    first_completed = await service.run_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        job_id=first_job.job_id,
        worker_id="worker-delete-job",
    )
    assert first_completed.saved_count == 1
    assert first_completed.deleted_count == 0

    second_job = await service.enqueue_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        idempotency_key="delete-job-second",
    )
    second_completed = await service.run_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        job_id=second_job.job_id,
        worker_id="worker-delete-job",
    )
    assert second_completed.fetched_count == 1
    assert second_completed.deleted_count == 1
    persisted = await service.get_sync_job(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        job_id=second_job.job_id,
    )
    assert persisted.deleted_count == 1


@pytest.mark.asyncio
async def test_sync_merges_thread_participants_and_count_without_replay_inflation() -> None:
    repository = InMemoryMailRepository()
    body_one = "first"
    body_two = "second"
    connector = RepeatedThreadConnector(
        (
            ProviderMessage(
                provider_message_ref="thread-message-1",
                provider_thread_ref="thread-shared",
                internet_message_id="<one@example.test>",
                sender_address="first@example.test",
                recipient_addresses=("user@example.test",),
                subject="Thread",
                received_at=datetime(2026, 1, 1, tzinfo=UTC),
                body_text=body_one,
                body_object_ref=None,
                content_sha256=digest_text(body_one),
            ),
            ProviderMessage(
                provider_message_ref="thread-message-2",
                provider_thread_ref="thread-shared",
                internet_message_id="<two@example.test>",
                sender_address="second@example.test",
                recipient_addresses=("user@example.test",),
                subject="Re: Thread",
                received_at=datetime(2026, 1, 2, tzinfo=UTC),
                body_text=body_two,
                body_object_ref=None,
                content_sha256=digest_text(body_two),
            ),
        )
    )
    service = MailService(repository, connectors={ProviderName.SANDBOX: connector})
    connection = await service.create_connection(
        tenant_id="tenant-thread",
        subject_id="user-thread",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="fixture",
    )
    first = await service.sync_connection(
        tenant_id="tenant-thread", subject_id="user-thread", connection_id=connection.connection_id
    )
    second = await service.sync_connection(
        tenant_id="tenant-thread", subject_id="user-thread", connection_id=connection.connection_id
    )
    assert first["saved_count"] == 2
    assert second["saved_count"] == 0
    thread = (await service.list_threads(tenant_id="tenant-thread", subject_id="user-thread"))[0]
    assert thread.message_count == 2
    assert thread.latest_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert thread.participant_addresses == (
        "first@example.test",
        "user@example.test",
        "second@example.test",
    )


@pytest.mark.asyncio
async def test_bounded_backfill_filters_without_advancing_incremental_cursor() -> None:
    repository = InMemoryMailRepository()
    connector = SandboxConnector()
    service = MailService(repository, connectors={ProviderName.SANDBOX: connector})
    connection = await service.create_connection(
        tenant_id="tenant-backfill-service",
        subject_id="subject-backfill-service",
        provider=ProviderName.SANDBOX,
        email_address="user@backfill-service.example.test",
        credential_ref="fixture",
    )
    for ref, received_at, labels in (
        ("backfill-match", datetime(2026, 7, 1, tzinfo=UTC), ("project",)),
        ("backfill-ignore", datetime(2026, 1, 1, tzinfo=UTC), ("other",)),
    ):
        await connector.seed(
            ProviderMessage(
                provider_message_ref=ref,
                provider_thread_ref=f"thread-{ref}",
                internet_message_id=f"<{ref}@example.test>",
                sender_address="sender@example.test",
                recipient_addresses=("user@backfill-service.example.test",),
                subject=ref,
                received_at=received_at,
                body_text=f"body-{ref}",
                body_object_ref=None,
                content_sha256=digest_text(f"body-{ref}"),
                labels=labels,
            )
        )

    result = await service.sync_connection(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection_id=connection.connection_id,
        mode="backfill",
        limit=10,
        sync_filter=ProviderSyncFilter(
            label_refs=("project",),
            received_after=datetime(2026, 6, 1, tzinfo=UTC),
            received_before=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    )
    assert result["saved_count"] == 1
    assert result["filter_applied"] is True
    assert (
        await repository.get_cursor(
            tenant_id=connection.tenant_id,
            connection_id=connection.connection_id,
            folder_ref="INBOX",
        )
        is None
    )


@pytest.mark.asyncio
async def test_sync_cleans_object_store_when_projection_validation_fails() -> None:
    class TrackingObjectStore(InMemoryObjectStore):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list[str] = []

        async def delete(self, *, tenant_id: str, subject_id: str, object_ref: str) -> None:
            self.deleted.append(object_ref)
            await super().delete(tenant_id=tenant_id, subject_id=subject_id, object_ref=object_ref)

    body = "will be cleaned"
    connector = RepeatedThreadConnector(
        (
            ProviderMessage(
                provider_message_ref="invalid-message",
                provider_thread_ref="invalid-thread",
                internet_message_id=None,
                sender_address="sender@example.test",
                recipient_addresses=("not-an-email",),
                subject="Invalid projection",
                received_at=datetime.now(UTC),
                body_text=body,
                body_object_ref=None,
                content_sha256=digest_text(body),
            ),
        )
    )
    store = TrackingObjectStore()
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: connector},
        object_store=store,
    )
    connection = await service.create_connection(
        tenant_id="tenant-cleanup",
        subject_id="user-cleanup",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="fixture",
    )
    with pytest.raises(ValueError, match="recipient_addresses_invalid"):
        await service.sync_connection(
            tenant_id="tenant-cleanup",
            subject_id="user-cleanup",
            connection_id=connection.connection_id,
        )
    assert len(store.deleted) == 1


@pytest.mark.asyncio
async def test_l3b_send_uses_idempotent_outbox_and_sandbox_provider() -> None:
    repository = InMemoryMailRepository()
    connector = SandboxConnector()
    events = RecordingEventPublisher()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        credential_broker=StaticCredentialBroker({"cred-1": {"mode": "sandbox"}}),
        approval_port=InMemoryApprovalPort(),
        event_publisher=events,
        outbound_enabled=True,
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="agent-1",
        provider=ProviderName.SANDBOX,
        email_address="agent@example.test",
        credential_ref="cred-1",
    )
    thread_id = uuid4()
    await repository.save_thread(
        MailThread(
            thread_id=thread_id,
            tenant_id="tenant-1",
            connection_id=connection.connection_id,
            provider_thread_ref="thread-1",
            normalized_subject="rfq",
            participant_addresses=(
                "buyer@example.test",
                "observer@example.test",
                "agent@example.test",
            ),
            latest_at=datetime.now(UTC),
            message_count=1,
        )
    )
    draft = await service.create_draft(
        tenant_id="tenant-1",
        subject_id="agent-1",
        connection_id=connection.connection_id,
        thread_id=thread_id,
        recipient_addresses=("buyer@example.test",),
        cc_addresses=("observer@example.test",),
        subject="Re: RFQ",
        body_text="We will review this by Friday.",
    )
    policy = MailAgentPolicy(
        policy_id=uuid4(),
        tenant_id="tenant-1",
        owner_subject_id="agent-1",
        allowed_connection_ids=frozenset({connection.connection_id}),
        allowed_actions=frozenset({ActionType.SEND_REPLY}),
        allowed_domains=frozenset({"example.test"}),
        allowed_automation_level=AutomationLevel.L3B_BOUNDED_REPLY,
    )
    await service.create_policy(policy)
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-1",
        policy_id=policy.policy_id,
        agent_subject_id="agent-1",
        granted_by_subject_id="agent-1",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
    )
    await service.create_grant(grant)

    with pytest.raises(Exception, match="draft_precondition_failed"):
        await service.queue_draft_send(
            tenant_id="tenant-1",
            subject_id="agent-1",
            draft_id=draft.draft_id,
            expected_revision=draft.revision + 1,
            expected_content_sha256=draft.content_sha256,
            expected_recipient_digest=digest_recipient_headers(
                draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
            ),
            policy_id=policy.policy_id,
            grant_id=grant.grant_id,
            agent_subject_id="agent-1",
        )

    queued = await service.queue_draft_send(
        tenant_id="tenant-1",
        subject_id="agent-1",
        draft_id=draft.draft_id,
        expected_revision=draft.revision,
        expected_content_sha256=draft.content_sha256,
        expected_recipient_digest=digest_recipient_headers(
            draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
        ),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        agent_subject_id="agent-1",
    )
    replay = await service.queue_draft_send(
        tenant_id="tenant-1",
        subject_id="agent-1",
        draft_id=draft.draft_id,
        expected_revision=draft.revision,
        expected_content_sha256=draft.content_sha256,
        expected_recipient_digest=digest_recipient_headers(
            draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
        ),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        agent_subject_id="agent-1",
    )
    assert replay["operation_id"] == queued["operation_id"]
    assert replay["created"] is False
    result = await service.send_queued(
        tenant_id="tenant-1",
        operation_id=UUID(cast(str, queued["operation_id"])),
        worker_id="worker-1",
    )

    assert result.status.value == "succeeded"
    assert len(connector.sent) == 1
    assert result.provider_message_ref is not None
    assert connector.sent[0].recipient_addresses == ("buyer@example.test",)
    assert connector.sent[0].cc_addresses == ("observer@example.test",)
    assert connector.sent[0].bcc_addresses == ()
    event_types = {str(event["event_type"]) for event in events.events}
    assert {
        "mail.connection.authorized",
        "mail.draft.created",
        "mail.delegation.granted",
        "mail.outbox.queued",
        "mail.outbox.sent",
    }.issubset(event_types)


@pytest.mark.asyncio
async def test_provider_accept_then_worker_crash_reconciles_without_duplicate_send() -> None:
    """A lost provider response is replayed with the same idempotency key."""

    repository = InMemoryMailRepository()
    connector = AcceptThenDisconnectConnector()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        credential_broker=StaticCredentialBroker({"cred-crash": {"mode": "sandbox"}}),
        outbound_enabled=True,
    )
    connection = await service.create_connection(
        tenant_id="tenant-crash",
        subject_id="agent-crash",
        provider=ProviderName.SANDBOX,
        email_address="agent@example.test",
        credential_ref="cred-crash",
    )
    thread_id = uuid4()
    await repository.save_thread(
        MailThread(
            thread_id=thread_id,
            tenant_id="tenant-crash",
            connection_id=connection.connection_id,
            provider_thread_ref="thread-crash",
            normalized_subject="rfq",
            participant_addresses=("buyer@example.test", "agent@example.test"),
            latest_at=datetime.now(UTC),
            message_count=1,
        )
    )
    draft = await service.create_draft(
        tenant_id="tenant-crash",
        subject_id="agent-crash",
        connection_id=connection.connection_id,
        thread_id=thread_id,
        recipient_addresses=("buyer@example.test",),
        subject="Re: RFQ",
        body_text="The worker may lose its response.",
    )
    policy = MailAgentPolicy(
        policy_id=uuid4(),
        tenant_id="tenant-crash",
        owner_subject_id="agent-crash",
        allowed_connection_ids=frozenset({connection.connection_id}),
        allowed_actions=frozenset({ActionType.SEND_REPLY}),
        allowed_domains=frozenset({"example.test"}),
        allowed_automation_level=AutomationLevel.L3B_BOUNDED_REPLY,
    )
    await service.create_policy(policy)
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-crash",
        policy_id=policy.policy_id,
        agent_subject_id="agent-crash",
        granted_by_subject_id="agent-crash",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
    )
    await service.create_grant(grant)
    queued = await service.queue_draft_send(
        tenant_id="tenant-crash",
        subject_id="agent-crash",
        draft_id=draft.draft_id,
        expected_revision=draft.revision,
        expected_content_sha256=draft.content_sha256,
        expected_recipient_digest=digest_addresses(draft.recipient_addresses),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        agent_subject_id="agent-crash",
    )
    operation_id = UUID(cast(str, queued["operation_id"]))

    first = await service.send_queued(
        tenant_id="tenant-crash", operation_id=operation_id, worker_id="worker-crash-1"
    )
    assert first.status is DeliveryStatus.OUTCOME_UNKNOWN
    assert first.error_code == "provider_outcome_unknown"
    assert len(connector.sent) == 1

    reconciled = await service.reconcile_outcome_unknown(
        tenant_id="tenant-crash",
        subject_id="agent-crash",
        operation_id=operation_id,
        status=DeliveryStatus.RETRY_WAIT,
        error_code="provider_lookup_pending",
        next_attempt_at=datetime.now(UTC),
    )
    assert reconciled.status is DeliveryStatus.RETRY_WAIT

    replay = await service.send_queued(
        tenant_id="tenant-crash", operation_id=operation_id, worker_id="worker-crash-2"
    )
    assert replay.status is DeliveryStatus.SUCCEEDED
    assert replay.provider_message_ref is not None
    assert len(connector.sent) == 1
    assert any(
        event.get("event_type") == "mail.outbox.reconciled"
        and event.get("target_ref") == str(operation_id)
        for event in repository.audits
    )


@pytest.mark.asyncio
async def test_bcc_requires_confirmation_and_attachment_capability_fails_closed() -> None:
    repository = InMemoryMailRepository()
    connector = SandboxConnector()
    approval = InMemoryApprovalPort()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        credential_broker=StaticCredentialBroker({"cred-bcc": {"mode": "sandbox"}}),
        approval_port=approval,
        outbound_enabled=True,
    )
    connection = await service.create_connection(
        tenant_id="tenant-bcc",
        subject_id="agent-bcc",
        provider=ProviderName.SANDBOX,
        email_address="agent@example.test",
        credential_ref="cred-bcc",
    )
    thread_id = uuid4()
    await repository.save_thread(
        MailThread(
            thread_id=thread_id,
            tenant_id="tenant-bcc",
            connection_id=connection.connection_id,
            provider_thread_ref="thread-bcc",
            normalized_subject="rfq",
            participant_addresses=(
                "buyer@example.test",
                "audit@example.test",
                "agent@example.test",
            ),
            latest_at=datetime.now(UTC),
            message_count=1,
        )
    )
    draft = await service.create_draft(
        tenant_id="tenant-bcc",
        subject_id="agent-bcc",
        connection_id=connection.connection_id,
        thread_id=thread_id,
        recipient_addresses=("buyer@example.test",),
        bcc_addresses=("audit@example.test",),
        attachment_refs=("obj:attachment-1",),
        subject="Re: RFQ",
        body_text="Please review the attached quote.",
    )
    policy = MailAgentPolicy(
        policy_id=uuid4(),
        tenant_id="tenant-bcc",
        owner_subject_id="agent-bcc",
        allowed_connection_ids=frozenset({connection.connection_id}),
        allowed_actions=frozenset({ActionType.SEND_REPLY}),
        allowed_domains=frozenset({"example.test"}),
        allowed_automation_level=AutomationLevel.L3B_BOUNDED_REPLY,
    )
    await service.create_policy(policy)
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-bcc",
        policy_id=policy.policy_id,
        agent_subject_id="agent-bcc",
        granted_by_subject_id="agent-bcc",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
    )
    await service.create_grant(grant)
    digest = digest_recipient_headers(
        draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
    )
    with pytest.raises(ApprovalRequiredError, match="explicit_confirmation_required"):
        await service.queue_draft_send(
            tenant_id="tenant-bcc",
            subject_id="agent-bcc",
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
            expected_content_sha256=draft.content_sha256,
            expected_recipient_digest=digest,
            policy_id=policy.policy_id,
            grant_id=grant.grant_id,
            agent_subject_id="agent-bcc",
        )
    approval.add_confirmation("bcc-approved")
    queued = await service.queue_draft_send(
        tenant_id="tenant-bcc",
        subject_id="agent-bcc",
        draft_id=draft.draft_id,
        expected_revision=draft.revision,
        expected_content_sha256=draft.content_sha256,
        expected_recipient_digest=digest,
        confirmation_ref="bcc-approved",
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        agent_subject_id="agent-bcc",
    )
    result = await service.send_queued(
        tenant_id="tenant-bcc",
        operation_id=UUID(cast(str, queued["operation_id"])),
        worker_id="worker-bcc",
    )
    assert result.status is DeliveryStatus.DEAD_LETTER
    assert result.error_code == "provider_attachment_capability_disabled"
    assert connector.sent == []


@pytest.mark.asyncio
async def test_revoked_connection_blocks_queued_delivery() -> None:
    repository = InMemoryMailRepository()
    connector = SandboxConnector()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        credential_broker=StaticCredentialBroker({"cred-1": {"mode": "sandbox"}}),
        outbound_enabled=True,
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="agent-1",
        provider=ProviderName.SANDBOX,
        email_address="agent@example.test",
        credential_ref="cred-1",
    )
    thread_id = uuid4()
    await repository.save_thread(
        MailThread(
            thread_id=thread_id,
            tenant_id="tenant-1",
            connection_id=connection.connection_id,
            provider_thread_ref="thread-1",
            normalized_subject="rfq",
            participant_addresses=("buyer@example.test", "agent@example.test"),
            latest_at=datetime.now(UTC),
            message_count=1,
        )
    )
    draft = await service.create_draft(
        tenant_id="tenant-1",
        subject_id="agent-1",
        connection_id=connection.connection_id,
        thread_id=thread_id,
        recipient_addresses=("buyer@example.test",),
        subject="Re: RFQ",
        body_text="Will review.",
    )
    policy = MailAgentPolicy(
        policy_id=uuid4(),
        tenant_id="tenant-1",
        owner_subject_id="agent-1",
        allowed_connection_ids=frozenset({connection.connection_id}),
        allowed_actions=frozenset({ActionType.SEND_REPLY}),
        allowed_domains=frozenset({"example.test"}),
        allowed_automation_level=AutomationLevel.L3B_BOUNDED_REPLY,
    )
    await service.create_policy(policy)
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-1",
        policy_id=policy.policy_id,
        agent_subject_id="agent-1",
        granted_by_subject_id="agent-1",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
    )
    await service.create_grant(grant)
    queued = await service.queue_draft_send(
        tenant_id="tenant-1",
        subject_id="agent-1",
        draft_id=draft.draft_id,
        expected_revision=draft.revision,
        expected_content_sha256=draft.content_sha256,
        expected_recipient_digest=digest_addresses(draft.recipient_addresses),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        agent_subject_id="agent-1",
    )
    await service.revoke_connection(
        tenant_id="tenant-1",
        subject_id="agent-1",
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
    )
    result = await service.send_queued(
        tenant_id="tenant-1",
        operation_id=UUID(cast(str, queued["operation_id"])),
        worker_id="worker-1",
    )
    assert result.status is DeliveryStatus.DEAD_LETTER
    assert result.error_code == "connection_not_active"
    assert connector.sent == []


@pytest.mark.asyncio
async def test_revoked_delegation_blocks_queued_delivery_before_provider_io() -> None:
    repository = InMemoryMailRepository()
    connector = SandboxConnector()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        credential_broker=StaticCredentialBroker({"cred-1": {"mode": "sandbox"}}),
        approval_port=InMemoryApprovalPort(),
        outbound_enabled=True,
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="agent-1",
        provider=ProviderName.SANDBOX,
        email_address="agent@example.test",
        credential_ref="cred-1",
    )
    thread_id = uuid4()
    await repository.save_thread(
        MailThread(
            thread_id=thread_id,
            tenant_id="tenant-1",
            connection_id=connection.connection_id,
            provider_thread_ref="thread-revocation",
            normalized_subject="rfq",
            participant_addresses=("buyer@example.test", "agent@example.test"),
            latest_at=datetime.now(UTC),
            message_count=1,
        )
    )
    draft = await service.create_draft(
        tenant_id="tenant-1",
        subject_id="agent-1",
        connection_id=connection.connection_id,
        thread_id=thread_id,
        recipient_addresses=("buyer@example.test",),
        subject="Re: RFQ",
        body_text="Will review.",
    )
    policy = MailAgentPolicy(
        policy_id=uuid4(),
        tenant_id="tenant-1",
        owner_subject_id="agent-1",
        allowed_connection_ids=frozenset({connection.connection_id}),
        allowed_actions=frozenset({ActionType.SEND_REPLY}),
        allowed_domains=frozenset({"example.test"}),
        allowed_automation_level=AutomationLevel.L3B_BOUNDED_REPLY,
    )
    await service.create_policy(policy)
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-1",
        policy_id=policy.policy_id,
        agent_subject_id="agent-1",
        granted_by_subject_id="agent-1",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
    )
    await service.create_grant(grant)
    queued = await service.queue_draft_send(
        tenant_id="tenant-1",
        subject_id="agent-1",
        draft_id=draft.draft_id,
        expected_revision=draft.revision,
        expected_content_sha256=draft.content_sha256,
        expected_recipient_digest=digest_addresses(draft.recipient_addresses),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        agent_subject_id="agent-1",
    )
    await repository.save_grant(
        replace(grant, revoked_at=datetime.now(UTC), revision=grant.revision + 1)
    )
    result = await service.send_queued(
        tenant_id="tenant-1",
        operation_id=UUID(cast(str, queued["operation_id"])),
        worker_id="worker-1",
    )
    assert result.status is DeliveryStatus.DEAD_LETTER
    assert result.error_code == "grant_revision_changed"
    assert connector.sent == []
