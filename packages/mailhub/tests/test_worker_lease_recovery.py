from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from mailhub.domain import (
    AutonomyRunStatus,
    MailAutonomyRun,
    MailSyncJob,
    SyncJobStatus,
)
from mailhub.storage import InMemoryMailRepository, RepositoryConflictError


@pytest.mark.asyncio
async def test_expired_sync_and_autonomy_leases_requeue_with_new_fencing_tokens() -> None:
    repository = InMemoryMailRepository()
    started = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)

    sync_job = MailSyncJob(
        job_id=uuid4(),
        tenant_id="tenant-worker-lease",
        subject_id="subject-worker-lease",
        connection_id=uuid4(),
        idempotency_key="sync-worker-lease",
    )
    await repository.create_or_get_sync_job(sync_job)
    first_sync = await repository.claim_sync_job(
        tenant_id=sync_job.tenant_id,
        job_id=sync_job.job_id,
        worker_id="worker-a",
        now=started,
        lease_seconds=10,
    )
    assert first_sync.lease_expires_at == started + timedelta(seconds=10)
    recovered_sync = await repository.claim_sync_job(
        tenant_id=sync_job.tenant_id,
        job_id=sync_job.job_id,
        worker_id="worker-b",
        now=started + timedelta(seconds=11),
        lease_seconds=10,
    )
    assert recovered_sync.status is SyncJobStatus.RUNNING
    assert recovered_sync.lease_owner == "worker-b"
    assert recovered_sync.fencing_token == first_sync.fencing_token + 1
    assert recovered_sync.error_code is None
    with pytest.raises(RepositoryConflictError, match="sync_job_lease_fenced"):
        await repository.complete_sync_job(
            tenant_id=sync_job.tenant_id,
            job_id=sync_job.job_id,
            worker_id="worker-a",
            fencing_token=first_sync.fencing_token,
            status=SyncJobStatus.SUCCEEDED,
        )
    assert any(
        event.get("event_type") == "mail.sync_job.lease_expired"
        and event.get("target_ref") == str(sync_job.job_id)
        for event in repository.audits
    )

    autonomy_run = MailAutonomyRun(
        run_id=uuid4(),
        tenant_id=sync_job.tenant_id,
        subject_id=sync_job.subject_id,
        connection_id=sync_job.connection_id,
        replay_key="autonomy-worker-lease",
    )
    await repository.create_or_get_autonomy_run(autonomy_run)
    first_run = await repository.claim_autonomy_run(
        tenant_id=autonomy_run.tenant_id,
        run_id=autonomy_run.run_id,
        worker_id="worker-a",
        now=started,
        lease_seconds=10,
    )
    recovered_run = await repository.claim_autonomy_run(
        tenant_id=autonomy_run.tenant_id,
        run_id=autonomy_run.run_id,
        worker_id="worker-b",
        now=started + timedelta(seconds=11),
        lease_seconds=10,
    )
    assert recovered_run.status is AutonomyRunStatus.RUNNING
    assert recovered_run.lease_owner == "worker-b"
    assert recovered_run.fencing_token == first_run.fencing_token + 1
    assert recovered_run.error_code is None
    with pytest.raises(RepositoryConflictError, match="autonomy_run_lease_fenced"):
        await repository.complete_autonomy_run(
            tenant_id=autonomy_run.tenant_id,
            run_id=autonomy_run.run_id,
            worker_id="worker-a",
            fencing_token=first_run.fencing_token,
            status=AutonomyRunStatus.COMPLETED,
        )
    assert any(
        event.get("event_type") == "mail.agent.autonomy.lease_expired"
        and event.get("target_ref") == str(autonomy_run.run_id)
        for event in repository.audits
    )


@pytest.mark.asyncio
async def test_expired_cancelling_sync_lease_finalizes_without_requeue() -> None:
    repository = InMemoryMailRepository()
    started = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)
    sync_job = MailSyncJob(
        job_id=uuid4(),
        tenant_id="tenant-worker-cancel",
        subject_id="subject-worker-cancel",
        connection_id=uuid4(),
        idempotency_key="sync-worker-cancel",
    )
    await repository.create_or_get_sync_job(sync_job)
    claimed = await repository.claim_sync_job(
        tenant_id=sync_job.tenant_id,
        job_id=sync_job.job_id,
        worker_id="worker-a",
        now=started,
        lease_seconds=10,
    )
    cancelling = await repository.cancel_sync_job(
        tenant_id=sync_job.tenant_id,
        job_id=sync_job.job_id,
    )
    assert cancelling.status is SyncJobStatus.CANCELLING
    with pytest.raises(RepositoryConflictError, match="sync_job_not_claimable"):
        await repository.claim_sync_job(
            tenant_id=sync_job.tenant_id,
            job_id=sync_job.job_id,
            worker_id="worker-b",
            now=started + timedelta(seconds=11),
            lease_seconds=10,
        )
    finalized = await repository.get_sync_job(
        tenant_id=sync_job.tenant_id,
        job_id=sync_job.job_id,
    )
    assert finalized is not None
    assert finalized.status is SyncJobStatus.CANCELLED
    assert finalized.lease_owner is None
    assert finalized.fencing_token == claimed.fencing_token
    assert any(
        event.get("event_type") == "mail.sync_job.cancelled_after_lease_expiry"
        and event.get("target_ref") == str(sync_job.job_id)
        for event in repository.audits
    )
