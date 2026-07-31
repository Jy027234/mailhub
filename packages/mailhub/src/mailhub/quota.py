"""Provider-neutral quota and concurrency contract for MailHub workers.

The durable production implementation belongs to the deployment (typically a
transactional database or a bounded distributed lease service).  This module
provides the typed contract and a deterministic in-memory implementation for
tests/sandbox.  Quotas are checked at tenant, subject and provider-account
scope; a lease is always released in a worker ``finally`` block.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from mailhub.errors import RateLimitedError


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    """Bounded limits for one worker operation scope.

    ``max_concurrent`` is enforced per provider account, ``max_per_hour`` per
    subject, and ``max_per_day`` per tenant.  A zero value means unlimited for
    that dimension; deployments should choose explicit non-zero defaults.
    """

    max_concurrent: int = 0
    max_per_hour: int = 0
    max_per_day: int = 0

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or value < 0
            for value in (self.max_concurrent, self.max_per_hour, self.max_per_day)
        ):
            raise ValueError("quota_limits_invalid")
        if self.max_concurrent > 10_000 or self.max_per_hour > 1_000_000:
            raise ValueError("quota_limits_too_large")
        if self.max_per_day > 10_000_000:
            raise ValueError("quota_limits_too_large")


@dataclass(frozen=True, slots=True)
class QuotaLease:
    lease_id: UUID
    tenant_id: str
    subject_id: str
    account_id: UUID | None
    operation: str
    acquired_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "tenant_id"),
            (self.subject_id, "subject_id"),
            (self.operation, "operation"),
        ):
            if not value.strip() or len(value) > 200:
                raise ValueError(f"quota_{name}_invalid")
        if self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None:
            raise ValueError("quota_timestamp_not_aware")
        object.__setattr__(self, "acquired_at", self.acquired_at.astimezone(UTC))


class QuotaPort(Protocol):
    """Structural async contract implemented by host/deployment quota stores."""

    async def acquire(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        account_id: UUID | None,
        operation: str,
        limits: QuotaLimits,
    ) -> QuotaLease: ...

    async def release(self, lease: QuotaLease, *, consume: bool = True) -> None: ...


@dataclass(frozen=True, slots=True)
class _Usage:
    lease: QuotaLease
    released: bool = False


class InMemoryQuotaPort(QuotaPort):
    """Atomic bounded quota implementation for local tests only."""

    def __init__(
        self, *, limits: QuotaLimits = QuotaLimits(), lease_ttl: timedelta = timedelta(minutes=15)
    ) -> None:
        if lease_ttl <= timedelta(0) or lease_ttl > timedelta(hours=24):
            raise ValueError("quota_lease_ttl_invalid")
        self.limits = limits
        self.lease_ttl = lease_ttl
        self.leases: dict[UUID, _Usage] = {}
        self.history: list[tuple[QuotaLease, datetime]] = []
        self._lock = Lock()

    async def acquire(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        account_id: UUID | None,
        operation: str,
        limits: QuotaLimits,
    ) -> QuotaLease:
        now = datetime.now(UTC)
        lease = QuotaLease(
            lease_id=uuid4(),
            tenant_id=tenant_id,
            subject_id=subject_id,
            account_id=account_id,
            operation=operation,
            acquired_at=now,
        )
        with self._lock:
            self._prune(now)
            effective = limits if limits != QuotaLimits() else self.limits
            active = tuple(item.lease for item in self.leases.values() if not item.released)
            if effective.max_concurrent:
                account_active = sum(
                    1
                    for item in active
                    if item.tenant_id == tenant_id and item.account_id == account_id
                )
                if account_active >= effective.max_concurrent:
                    raise RateLimitedError("quota_concurrency_limit")
            if effective.max_per_hour:
                hour_start = now - timedelta(hours=1)
                count = sum(
                    1
                    for item, at in self.history
                    if at >= hour_start
                    and item.tenant_id == tenant_id
                    and item.subject_id == subject_id
                )
                if count >= effective.max_per_hour:
                    raise RateLimitedError("quota_hourly_limit")
            if effective.max_per_day:
                day_start = now - timedelta(days=1)
                count = sum(
                    1
                    for item, at in self.history
                    if at >= day_start and item.tenant_id == tenant_id
                )
                if count >= effective.max_per_day:
                    raise RateLimitedError("quota_daily_limit")
            self.leases[lease.lease_id] = _Usage(lease=lease)
        return lease

    async def release(self, lease: QuotaLease, *, consume: bool = True) -> None:
        now = datetime.now(UTC)
        with self._lock:
            usage = self.leases.get(lease.lease_id)
            if usage is None:
                return
            if not usage.released:
                self.leases[lease.lease_id] = _Usage(lease=usage.lease, released=True)
                if consume:
                    self.history.append((lease, now))
            self._prune(now)

    def snapshot(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "active": sum(not item.released for item in self.leases.values()),
                "history": len(self.history),
                "lease_ids": tuple(str(item.lease.lease_id) for item in self.leases.values()),
            }

    def _prune(self, now: datetime) -> None:
        expiry = now - self.lease_ttl
        expired = [
            lease_id for lease_id, usage in self.leases.items() if usage.lease.acquired_at < expiry
        ]
        for lease_id in expired:
            self.leases.pop(lease_id, None)
        history_expiry = now - timedelta(days=2)
        self.history[:] = [(item, at) for item, at in self.history if at >= history_expiry]
