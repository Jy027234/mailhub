from datetime import timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from mailhub.errors import RateLimitedError
from mailhub.hosts.postgres_quota import PostgresQuotaPort
from mailhub.quota import QuotaLimits


class _Result:
    def __init__(self, value: int = 0, *, rowcount: int = 0) -> None:
        self.value = value
        self.rowcount = rowcount

    def scalar_one(self) -> int:
        return self.value

    def first(self) -> object | None:
        return None


class _Connection:
    def __init__(self, *, count: int = 0, update_rowcount: int = 1) -> None:
        self.count = count
        self.update_rowcount = update_rowcount
        self.statements: list[str] = []

    async def execute(self, statement: object, parameters: object) -> _Result:
        del parameters
        sql = str(statement)
        self.statements.append(sql)
        if "SELECT COUNT(*)" in sql:
            return _Result(self.count)
        if "UPDATE mail_quota_leases" in sql:
            return _Result(rowcount=self.update_rowcount)
        return _Result()


class _Context:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def begin(self) -> _Context:
        return _Context(self.connection)


@pytest.mark.asyncio
async def test_postgres_quota_serializes_tenant_and_releases_idempotently() -> None:
    connection = _Connection()
    quota = PostgresQuotaPort(
        cast(AsyncEngine, _Engine(connection)),
        limits=QuotaLimits(max_concurrent=1),
        lease_ttl=timedelta(minutes=5),
    )
    lease = await quota.acquire(
        tenant_id="tenant-1",
        subject_id="subject-1",
        account_id=UUID("00000000-0000-0000-0000-000000000001"),
        operation="sync",
        limits=QuotaLimits(),
    )
    await quota.release(lease, consume=False)

    assert any("pg_advisory_xact_lock" in sql for sql in connection.statements)
    assert any("INSERT INTO mail_quota_leases" in sql for sql in connection.statements)
    assert any("UPDATE mail_quota_leases" in sql for sql in connection.statements)


@pytest.mark.asyncio
async def test_postgres_quota_rejects_limit_before_insert() -> None:
    connection = _Connection(count=1)
    quota = PostgresQuotaPort(cast(AsyncEngine, _Engine(connection)))

    with pytest.raises(RateLimitedError, match="quota_concurrency_limit"):
        await quota.acquire(
            tenant_id="tenant-1",
            subject_id="subject-1",
            account_id=None,
            operation="sync",
            limits=QuotaLimits(max_concurrent=1),
        )

    assert not any("INSERT INTO mail_quota_leases" in sql for sql in connection.statements)
