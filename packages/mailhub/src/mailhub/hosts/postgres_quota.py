"""Transactional PostgreSQL quota leases for independent MailHub deployments."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mailhub.errors import RateLimitedError
from mailhub.persistence.sqlalchemy import _set_rls_scope
from mailhub.quota import QuotaLease, QuotaLimits, QuotaPort


class PostgresQuotaPort(QuotaPort):
    """Durable quota admission with transaction-scoped tenant serialization.

    Every acquire takes a PostgreSQL advisory lock for the tenant before
    counting active leases and consumed windows.  This makes the three quota
    dimensions atomic across workers and avoids relying on process-local
    counters.  The table is tenant-RLS protected and stores only bounded
    identifiers/timestamps.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        lease_ttl: timedelta = timedelta(minutes=15),
        limits: QuotaLimits = QuotaLimits(),
    ) -> None:
        if lease_ttl <= timedelta(0) or lease_ttl > timedelta(hours=24):
            raise ValueError("quota_lease_ttl_invalid")
        self.engine = engine
        self.lease_ttl = lease_ttl
        self.limits = limits

    async def acquire(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        account_id: UUID | None,
        operation: str,
        limits: QuotaLimits,
    ) -> QuotaLease:
        _validate_scope(tenant_id, subject_id, operation)
        now = datetime.now(UTC)
        lease = QuotaLease(
            lease_id=uuid4(),
            tenant_id=tenant_id,
            subject_id=subject_id,
            account_id=account_id,
            operation=operation,
            acquired_at=now,
        )
        effective = limits if limits != QuotaLimits() else self.limits
        async with self.engine.begin() as connection:
            await _set_rls_scope(connection, tenant_id=tenant_id, subject_id=subject_id)
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"mailhub:quota:{tenant_id}"},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM mail_quota_leases
                    WHERE tenant_id = :tenant_id
                      AND released_at IS NULL
                      AND expires_at <= :now
                    """
                ),
                {"tenant_id": tenant_id, "now": now},
            )

            if effective.max_concurrent:
                active = await connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM mail_quota_leases
                        WHERE tenant_id = :tenant_id
                          AND account_id IS NOT DISTINCT FROM :account_id
                          AND released_at IS NULL
                          AND expires_at > :now
                        """
                    ),
                    {"tenant_id": tenant_id, "account_id": account_id, "now": now},
                )
                if int(active.scalar_one()) >= effective.max_concurrent:
                    raise RateLimitedError("quota_concurrency_limit")

            if effective.max_per_hour:
                hourly = await connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM mail_quota_leases
                        WHERE tenant_id = :tenant_id
                          AND subject_id = :subject_id
                          AND consumed_at >= :window_start
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "subject_id": subject_id,
                        "window_start": now - timedelta(hours=1),
                    },
                )
                if int(hourly.scalar_one()) >= effective.max_per_hour:
                    raise RateLimitedError("quota_hourly_limit")

            if effective.max_per_day:
                daily = await connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM mail_quota_leases
                        WHERE tenant_id = :tenant_id
                          AND consumed_at >= :window_start
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "window_start": now - timedelta(days=1),
                    },
                )
                if int(daily.scalar_one()) >= effective.max_per_day:
                    raise RateLimitedError("quota_daily_limit")

            await connection.execute(
                text(
                    """
                    INSERT INTO mail_quota_leases
                        (lease_id, tenant_id, subject_id, account_id, operation,
                         acquired_at, expires_at, created_at)
                    VALUES
                        (:lease_id, :tenant_id, :subject_id, :account_id, :operation,
                         :acquired_at, :expires_at, :created_at)
                    """
                ),
                {
                    "lease_id": lease.lease_id,
                    "tenant_id": tenant_id,
                    "subject_id": subject_id,
                    "account_id": account_id,
                    "operation": operation,
                    "acquired_at": now,
                    "expires_at": now + self.lease_ttl,
                    "created_at": now,
                },
            )
        return lease

    async def release(self, lease: QuotaLease, *, consume: bool = True) -> None:
        _validate_scope(lease.tenant_id, lease.subject_id, lease.operation)
        now = datetime.now(UTC)
        async with self.engine.begin() as connection:
            await _set_rls_scope(connection, tenant_id=lease.tenant_id, subject_id=lease.subject_id)
            result = await connection.execute(
                text(
                    """
                    UPDATE mail_quota_leases
                    SET released_at = :released_at,
                        consumed_at = CASE WHEN :consume THEN :consumed_at ELSE NULL END
                    WHERE lease_id = :lease_id
                      AND tenant_id = :tenant_id
                      AND subject_id = :subject_id
                      AND account_id IS NOT DISTINCT FROM :account_id
                      AND operation = :operation
                      AND released_at IS NULL
                    """
                ),
                {
                    "released_at": now,
                    "consumed_at": now,
                    "consume": consume,
                    "lease_id": lease.lease_id,
                    "tenant_id": lease.tenant_id,
                    "subject_id": lease.subject_id,
                    "account_id": lease.account_id,
                    "operation": lease.operation,
                },
            )
            if getattr(result, "rowcount", 0):
                return
            current = await connection.execute(
                text(
                    "SELECT tenant_id, subject_id, operation, released_at "
                    "FROM mail_quota_leases WHERE lease_id = :lease_id"
                ),
                {"lease_id": lease.lease_id},
            )
            row = current.first()
            if row is None or row.released_at is not None:
                return
            raise RuntimeError("quota_lease_scope_mismatch")


def _validate_scope(tenant_id: str, subject_id: str, operation: str) -> None:
    for value, name, maximum in (
        (tenant_id, "tenant_id", 200),
        (subject_id, "subject_id", 200),
        (operation, "operation", 200),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"quota_{name}_invalid")


__all__ = ["PostgresQuotaPort"]
