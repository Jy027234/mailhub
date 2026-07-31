from datetime import timedelta
from uuid import uuid4

import pytest

from mailhub.errors import RateLimitedError
from mailhub.quota import InMemoryQuotaPort, QuotaLimits


@pytest.mark.asyncio
async def test_quota_enforces_account_concurrency_and_releases() -> None:
    account_id = uuid4()
    quota = InMemoryQuotaPort(limits=QuotaLimits(max_concurrent=1))
    first = await quota.acquire(
        tenant_id="tenant-1",
        subject_id="user-1",
        account_id=account_id,
        operation="mail.sync",
        limits=QuotaLimits(),
    )
    with pytest.raises(RateLimitedError, match="quota_concurrency_limit"):
        await quota.acquire(
            tenant_id="tenant-1",
            subject_id="user-1",
            account_id=account_id,
            operation="mail.sync",
            limits=QuotaLimits(),
        )
    await quota.release(first, consume=False)
    second = await quota.acquire(
        tenant_id="tenant-1",
        subject_id="user-1",
        account_id=account_id,
        operation="mail.sync",
        limits=QuotaLimits(),
    )
    await quota.release(second)
    assert quota.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_quota_counts_subject_and_tenant_windows() -> None:
    quota = InMemoryQuotaPort()
    limits = QuotaLimits(max_per_hour=1, max_per_day=2)
    first = await quota.acquire(
        tenant_id="tenant-1",
        subject_id="user-1",
        account_id=None,
        operation="mail.send",
        limits=limits,
    )
    await quota.release(first)
    with pytest.raises(RateLimitedError, match="quota_hourly_limit"):
        await quota.acquire(
            tenant_id="tenant-1",
            subject_id="user-1",
            account_id=None,
            operation="mail.send",
            limits=limits,
        )
    second = await quota.acquire(
        tenant_id="tenant-1",
        subject_id="user-2",
        account_id=None,
        operation="mail.send",
        limits=limits,
    )
    await quota.release(second)
    with pytest.raises(RateLimitedError, match="quota_daily_limit"):
        await quota.acquire(
            tenant_id="tenant-1",
            subject_id="user-3",
            account_id=None,
            operation="mail.send",
            limits=limits,
        )


def test_quota_rejects_unbounded_lease_ttl() -> None:
    with pytest.raises(ValueError, match="quota_lease_ttl_invalid"):
        InMemoryQuotaPort(lease_ttl=timedelta(days=2))
