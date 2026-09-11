"""Tests for automatic OUTCOME_UNKNOWN reconciliation.

The manual entry point records a conclusion somebody else reached; this one
reaches it, by looking for the deterministic Message-ID in the mailbox.  The
tests therefore concentrate on the cases where deciding would be wrong -- an
unreachable mailbox, a probe that throws, a probe that answers "cannot tell".
Every one of those must leave the operation exactly where it was, because
reading "I could not check" as "it was not sent" would resend a message that may
already be delivered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from mailhub.domain import (
    DeliveryStatus,
    MailboxConnection,
    MailOutboxOperation,
    ProviderName,
    outbound_internet_message_id,
)
from mailhub.errors import ConflictError, NotFoundError, ProviderFailureError
from mailhub.ports import OutboundObservation
from mailhub.service import MailService
from mailhub.storage import InMemoryMailRepository


class FakeProbe:
    """A reconciliation probe under test control."""

    def __init__(
        self,
        observation: OutboundObservation | None = None,
        error: Exception | None = None,
    ) -> None:
        self.observation = observation
        self.error = error
        self.calls: list[str] = []

    async def observe_outbound(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        internet_message_id: str,
    ) -> OutboundObservation:
        del tenant_id, subject_id, connection
        self.calls.append(internet_message_id)
        if self.error is not None:
            raise self.error
        assert self.observation is not None
        return self.observation


async def _unknown_operation(
    repository: InMemoryMailRepository, *, with_connection: bool = True
) -> MailOutboxOperation:
    connection_id = uuid4()
    if with_connection:
        await repository.save_connection(
            MailboxConnection(
                connection_id=connection_id,
                tenant_id="tenant-1",
                subject_id="user-1",
                provider=ProviderName.IMAP_SMTP,
                email_address="user@example.test",
                credential_ref="credential-1",
            )
        )
    operation = MailOutboxOperation(
        operation_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="user-1",
        draft_id=uuid4(),
        connection_id=connection_id,
        idempotency_key="mail-send:reconcile",
        status=DeliveryStatus.OUTCOME_UNKNOWN,
    )
    await repository.create_or_get_operation(operation)
    return operation


def _service(repository: InMemoryMailRepository, probe: FakeProbe | None) -> MailService:
    return MailService(repository, connectors={}, outbound_reconciliation_port=probe)


async def _status(
    repository: InMemoryMailRepository, operation: MailOutboxOperation
) -> MailOutboxOperation:
    stored = await repository.get_operation(
        tenant_id=operation.tenant_id, operation_id=operation.operation_id
    )
    assert stored is not None
    return stored


@pytest.mark.asyncio
async def test_a_present_message_is_reconciled_as_sent() -> None:
    repository = InMemoryMailRepository()
    operation = await _unknown_operation(repository)
    probe = FakeProbe(
        OutboundObservation(
            found=True,
            provider_message_ref="<read-back@example.test>",
            mailbox="INBOX",
            detail="header search hit",
        )
    )

    reconciled = await _service(repository, probe).reconcile_outbound_outcome(
        tenant_id="tenant-1", subject_id="user-1", operation_id=operation.operation_id
    )

    assert reconciled.status is DeliveryStatus.RECONCILED_SUCCEEDED
    assert reconciled.provider_message_ref == "<read-back@example.test>"
    # The probe is asked about the identifier the send actually carried.
    assert probe.calls == [outbound_internet_message_id(operation.operation_id)]
    assert any(
        event.get("event_type") == "mail.outbox.reconciled"
        and event.get("target_ref") == str(operation.operation_id)
        for event in repository.audits
    )


@pytest.mark.asyncio
async def test_an_absent_message_is_scheduled_for_retry() -> None:
    repository = InMemoryMailRepository()
    operation = await _unknown_operation(repository)
    probe = FakeProbe(OutboundObservation(found=False, mailbox="INBOX"))
    retry_at = datetime.now(UTC) + timedelta(minutes=9)

    reconciled = await _service(repository, probe).reconcile_outbound_outcome(
        tenant_id="tenant-1",
        subject_id="user-1",
        operation_id=operation.operation_id,
        next_attempt_at=retry_at,
    )

    assert reconciled.status is DeliveryStatus.RETRY_WAIT
    assert reconciled.error_code == "outbound_absent_after_unknown"
    assert reconciled.next_attempt_at == retry_at


@pytest.mark.asyncio
async def test_an_indeterminate_probe_leaves_the_operation_untouched() -> None:
    repository = InMemoryMailRepository()
    operation = await _unknown_operation(repository)
    probe = FakeProbe(OutboundObservation(found=None, detail="mailbox_unreachable"))

    returned = await _service(repository, probe).reconcile_outbound_outcome(
        tenant_id="tenant-1", subject_id="user-1", operation_id=operation.operation_id
    )

    assert returned.status is DeliveryStatus.OUTCOME_UNKNOWN
    assert (await _status(repository, operation)).status is DeliveryStatus.OUTCOME_UNKNOWN
    assert any(
        event.get("event_type") == "mail.outbox.reconciliation_indeterminate"
        for event in repository.audits
    )


@pytest.mark.asyncio
async def test_a_failing_probe_is_not_read_as_absent() -> None:
    repository = InMemoryMailRepository()
    operation = await _unknown_operation(repository)
    probe = FakeProbe(error=RuntimeError("socket exploded"))

    returned = await _service(repository, probe).reconcile_outbound_outcome(
        tenant_id="tenant-1", subject_id="user-1", operation_id=operation.operation_id
    )

    assert returned.status is DeliveryStatus.OUTCOME_UNKNOWN
    indeterminate = [
        event
        for event in repository.audits
        if event.get("event_type") == "mail.outbox.reconciliation_indeterminate"
    ]
    assert indeterminate, "a probe failure must be recorded, not silently swallowed"
    assert indeterminate[0].get("detail") == "RuntimeError"


@pytest.mark.asyncio
async def test_a_provider_failure_from_the_probe_is_recorded_by_code() -> None:
    repository = InMemoryMailRepository()
    operation = await _unknown_operation(repository)
    probe = FakeProbe(error=ProviderFailureError("imap_search_failed"))

    returned = await _service(repository, probe).reconcile_outbound_outcome(
        tenant_id="tenant-1", subject_id="user-1", operation_id=operation.operation_id
    )

    assert returned.status is DeliveryStatus.OUTCOME_UNKNOWN
    assert any(
        event.get("event_type") == "mail.outbox.reconciliation_indeterminate"
        and event.get("detail") == "imap_search_failed"
        for event in repository.audits
    )


@pytest.mark.asyncio
async def test_reconciliation_refuses_an_operation_whose_outcome_is_known() -> None:
    repository = InMemoryMailRepository()
    operation = await _unknown_operation(repository)
    # Settle the outcome first, the way a manual decision would.
    await repository.reconcile_outcome_unknown(
        tenant_id=operation.tenant_id,
        operation_id=operation.operation_id,
        status=DeliveryStatus.RECONCILED_SUCCEEDED,
        provider_message_ref="<settled@example.test>",
    )

    with pytest.raises(ConflictError, match="outcome_reconciliation_not_applicable"):
        await _service(
            repository, FakeProbe(OutboundObservation(found=True))
        ).reconcile_outbound_outcome(
            tenant_id="tenant-1", subject_id="user-1", operation_id=operation.operation_id
        )


@pytest.mark.asyncio
async def test_reconciliation_requires_a_probe_rather_than_guessing() -> None:
    repository = InMemoryMailRepository()
    operation = await _unknown_operation(repository)

    with pytest.raises(ProviderFailureError, match="outbound_reconciliation_unavailable"):
        await _service(repository, None).reconcile_outbound_outcome(
            tenant_id="tenant-1", subject_id="user-1", operation_id=operation.operation_id
        )


@pytest.mark.asyncio
async def test_reconciliation_reports_a_missing_connection() -> None:
    repository = InMemoryMailRepository()
    operation = await _unknown_operation(repository, with_connection=False)

    with pytest.raises(NotFoundError, match="connection_not_found"):
        await _service(
            repository, FakeProbe(OutboundObservation(found=True))
        ).reconcile_outbound_outcome(
            tenant_id="tenant-1", subject_id="user-1", operation_id=operation.operation_id
        )
