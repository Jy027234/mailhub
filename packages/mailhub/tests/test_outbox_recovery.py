from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from mailhub.domain import DeliveryStatus, MailOutboxOperation
from mailhub.storage import InMemoryMailRepository, RepositoryConflictError


@pytest.mark.asyncio
async def test_expired_outbox_lease_becomes_outcome_unknown_before_replay() -> None:
    repository = InMemoryMailRepository()
    operation = MailOutboxOperation(
        operation_id=uuid4(),
        tenant_id="tenant-lease",
        subject_id="user-lease",
        draft_id=uuid4(),
        connection_id=uuid4(),
        idempotency_key="mail-send:lease-recovery",
        status=DeliveryStatus.QUEUED,
    )
    await repository.create_or_get_operation(operation)
    leased_at = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    leased = await repository.lease_operation(
        tenant_id=operation.tenant_id,
        operation_id=operation.operation_id,
        worker_id="worker-1",
        now=leased_at,
        lease_seconds=10,
    )
    assert leased.lease_expires_at == leased_at + timedelta(seconds=10)

    with pytest.raises(
        RepositoryConflictError,
        match="operation_outcome_unknown_reconciliation_required",
    ):
        await repository.lease_operation(
            tenant_id=operation.tenant_id,
            operation_id=operation.operation_id,
            worker_id="worker-2",
            now=leased_at + timedelta(seconds=11),
            lease_seconds=10,
        )

    unknown = await repository.get_operation(
        tenant_id=operation.tenant_id, operation_id=operation.operation_id
    )
    assert unknown is not None
    assert unknown.status is DeliveryStatus.OUTCOME_UNKNOWN
    assert unknown.lease_owner is None
    assert unknown.lease_expires_at is None
    assert unknown.error_code == "lease_expired_outcome_unknown"
    assert any(
        event.get("event_type") == "mail.outbox.lease_expired"
        and event.get("target_ref") == str(operation.operation_id)
        for event in repository.audits
    )

    reconciled = await repository.reconcile_outcome_unknown(
        tenant_id=operation.tenant_id,
        operation_id=operation.operation_id,
        status=DeliveryStatus.RETRY_WAIT,
        error_code="provider_not_found_after_reconciliation",
        next_attempt_at=leased_at + timedelta(seconds=11),
    )
    assert reconciled.status is DeliveryStatus.RETRY_WAIT
    replay = await repository.lease_operation(
        tenant_id=operation.tenant_id,
        operation_id=operation.operation_id,
        worker_id="worker-3",
        now=leased_at + timedelta(seconds=11),
        lease_seconds=10,
    )
    assert replay.fencing_token == 2
    assert replay.status is DeliveryStatus.LEASED
