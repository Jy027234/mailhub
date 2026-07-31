from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from mailhub.domain import ConnectionStatus, MailboxConnection, ProviderName, SubscriptionStatus
from mailhub.errors import ProviderFailureError
from mailhub.hosts.inmemory import RecordingEventPublisher
from mailhub.ports import ProviderSubscriptionLease
from mailhub.storage import InMemoryMailRepository, RepositoryConflictError
from mailhub.subscriptions import MailSubscriptionCoordinator


class _FakeSubscriptionPort:
    def __init__(self) -> None:
        self.ensure_calls: list[dict[str, object]] = []
        self.cancel_calls: list[dict[str, object]] = []
        self.subscription_ref = "projects/p/subscriptions/mailhub-test"
        self.expires_at = datetime.now(UTC) + timedelta(hours=2)
        self.fail_ensure = False

    async def ensure_subscription(self, **kwargs: object) -> ProviderSubscriptionLease:
        self.ensure_calls.append(dict(kwargs))
        if self.fail_ensure:
            raise RuntimeError("host_renewal_unavailable")
        return ProviderSubscriptionLease(
            provider=kwargs["provider"],  # type: ignore[arg-type]
            connection_id=kwargs["connection_id"],  # type: ignore[arg-type]
            subscription_ref=self.subscription_ref,
            status="active",
            expires_at=self.expires_at,
            callback_endpoint=kwargs["callback_endpoint"],  # type: ignore[arg-type]
            provider_request_id="provider-request-1",
            client_state_ref=kwargs.get("client_state_ref"),  # type: ignore[arg-type]
        )

    async def cancel_subscription(self, **kwargs: object) -> dict[str, object]:
        self.cancel_calls.append(dict(kwargs))
        return {"status": "cancelled", "provider_request_id": "provider-cancel-1"}


class _FailingEventPublisher:
    async def publish(self, event: object) -> None:
        del event
        raise RuntimeError("event_bus_unavailable")


def _connection() -> MailboxConnection:
    return MailboxConnection(
        connection_id=uuid4(),
        tenant_id="tenant-subscription",
        subject_id="subject-subscription",
        provider=ProviderName.GMAIL,
        email_address="isolated@example.test",
        credential_ref="cred-ref",
        status=ConnectionStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_subscription_state_is_durable_renewable_and_cancellable() -> None:
    repository = InMemoryMailRepository()
    publisher = RecordingEventPublisher()
    port = _FakeSubscriptionPort()
    connection = _connection()
    await repository.save_connection(connection)
    coordinator = MailSubscriptionCoordinator(
        repository, subscription_port=port, event_publisher=publisher
    )
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    port.expires_at = now + timedelta(minutes=30)

    state = await coordinator.ensure(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        callback_endpoint="https://mailhub.example.test/v1/mail/webhooks/gmail",
        desired_expiry=now + timedelta(hours=6),
        client_state_ref="opaque-client-state-ref",
        idempotency_key="subscription-ensure-1",
        now=now,
    )
    assert state.subscription_status is SubscriptionStatus.ACTIVE
    assert state.subscription_ref == port.subscription_ref
    assert state.cursor_value is None
    assert "opaque-client-state-ref" not in str(publisher.events)

    port.expires_at = now + timedelta(days=1)
    renewed = await coordinator.renew_if_due(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        renewal_window=timedelta(hours=1),
        desired_expiry=now + timedelta(days=1),
        now=now,
    )
    assert renewed is not None
    assert len(port.ensure_calls) == 2

    watermarked = await coordinator.record_notification(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        subscription_ref=port.subscription_ref,
        received_at=now + timedelta(minutes=1),
    )
    assert watermarked.watermark == now + timedelta(minutes=1)

    cancelled = await coordinator.cancel(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        request_id="subscription-cancel-1",
        now=now,
    )
    assert cancelled is not None
    assert cancelled.subscription_status is SubscriptionStatus.CANCELLED
    assert len(port.cancel_calls) == 1
    assert publisher.events[-1]["event_type"] == "mail.subscription.cancelled"
    with pytest.raises(ProviderFailureError, match="subscription_notification_inactive"):
        await coordinator.record_notification(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            connection=connection,
            subscription_ref=port.subscription_ref,
            received_at=now + timedelta(minutes=2),
        )


@pytest.mark.asyncio
async def test_subscription_state_fencing_rejects_stale_ref() -> None:
    repository = InMemoryMailRepository()
    connection = _connection()
    await repository.save_connection(connection)
    port = _FakeSubscriptionPort()
    coordinator = MailSubscriptionCoordinator(repository, subscription_port=port)
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    port.expires_at = now + timedelta(hours=2)
    await coordinator.ensure(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        callback_endpoint="https://mailhub.example.test/v1/mail/webhooks/gmail",
        desired_expiry=now + timedelta(hours=6),
        idempotency_key="subscription-ensure-fenced",
        now=now,
    )
    state = await repository.get_sync_state(
        tenant_id=connection.tenant_id,
        connection_id=connection.connection_id,
        folder_ref="INBOX",
    )
    assert state is not None
    with pytest.raises(RepositoryConflictError, match="subscription_state_conflict"):
        await repository.save_sync_state(state, expected_subscription_ref="stale-ref")


@pytest.mark.asyncio
async def test_subscription_failure_and_expiry_are_durable_status_events() -> None:
    repository = InMemoryMailRepository()
    publisher = RecordingEventPublisher()
    port = _FakeSubscriptionPort()
    connection = _connection()
    await repository.save_connection(connection)
    coordinator = MailSubscriptionCoordinator(
        repository, subscription_port=port, event_publisher=publisher
    )
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    port.expires_at = now + timedelta(minutes=30)
    await coordinator.ensure(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        callback_endpoint="https://mailhub.example.test/v1/mail/webhooks/gmail",
        desired_expiry=now + timedelta(hours=6),
        idempotency_key="subscription-status-1",
        now=now,
    )
    port.fail_ensure = True
    with pytest.raises(RuntimeError, match="host_renewal_unavailable"):
        await coordinator.renew_if_due(
            tenant_id=connection.tenant_id,
            subject_id=connection.subject_id,
            connection=connection,
            renewal_window=timedelta(hours=1),
            now=now,
        )
    failed = await repository.get_sync_state(
        tenant_id=connection.tenant_id,
        connection_id=connection.connection_id,
        folder_ref="INBOX",
    )
    assert failed is not None
    assert failed.subscription_status is SubscriptionStatus.FAILED

    expired = await coordinator.mark_expired(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        now=now + timedelta(days=1),
    )
    assert expired is not None
    assert expired.subscription_status is SubscriptionStatus.EXPIRED
    assert publisher.events[-1]["event_type"] == "mail.subscription.expired"


@pytest.mark.asyncio
async def test_provider_lifecycle_fences_subscription_for_immediate_renewal() -> None:
    repository = InMemoryMailRepository()
    publisher = RecordingEventPublisher()
    port = _FakeSubscriptionPort()
    connection = _connection()
    await repository.save_connection(connection)
    coordinator = MailSubscriptionCoordinator(
        repository, subscription_port=port, event_publisher=publisher
    )
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    port.expires_at = now + timedelta(days=7)
    await coordinator.ensure(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        callback_endpoint="https://mailhub.example.test/v1/mail/webhooks/graph",
        desired_expiry=now + timedelta(days=7),
        idempotency_key="lifecycle-renewal-required",
        now=now,
    )

    required = await coordinator.mark_renewal_required(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        error_code="provider_subscription_removed",
        now=now + timedelta(minutes=1),
    )
    assert required is not None
    assert required.subscription_status is SubscriptionStatus.RENEWAL_REQUIRED
    assert required.subscription_expires_at == now + timedelta(minutes=1)

    # A scheduler must not wait for the stale seven-day expiry after a
    # provider lifecycle removal signal.
    port.expires_at = now + timedelta(days=1)
    renewed = await coordinator.renew_if_due(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        renewal_window=timedelta(hours=1),
        now=now + timedelta(minutes=2),
    )
    assert renewed is not None
    assert renewed.subscription_status is SubscriptionStatus.ACTIVE
    assert len(port.ensure_calls) == 2


@pytest.mark.asyncio
async def test_notification_watermark_survives_event_bus_outage() -> None:
    repository = InMemoryMailRepository()
    connection = _connection()
    await repository.save_connection(connection)
    port = _FakeSubscriptionPort()
    publisher = _FailingEventPublisher()
    coordinator = MailSubscriptionCoordinator(
        repository,
        subscription_port=port,
        event_publisher=publisher,
    )
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    port.expires_at = now + timedelta(hours=2)
    await coordinator.ensure(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        callback_endpoint="https://mailhub.example.test/v1/mail/webhooks/gmail",
        desired_expiry=now + timedelta(hours=6),
        idempotency_key="event-outage-ensure",
        now=now,
    )
    state = await coordinator.record_notification(
        tenant_id=connection.tenant_id,
        subject_id=connection.subject_id,
        connection=connection,
        subscription_ref=port.subscription_ref,
        received_at=now + timedelta(minutes=1),
        notification_id="event-outage-notification",
    )
    assert state.watermark == now + timedelta(minutes=1)
    assert any(audit["event_type"] == "mail.event.publish_failed" for audit in repository.audits)
