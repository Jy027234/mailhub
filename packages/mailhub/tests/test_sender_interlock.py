from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from mailhub.hosts.postgres import PostgresSenderInterlockAdapter


class _Result:
    def __init__(self, row: object | None) -> None:
        self.row = row

    def first(self) -> object | None:
        return self.row


class _Connection:
    def __init__(self) -> None:
        self.claim_row: object | None = (1,)
        self.delete_row: object | None = (1,)
        self.current_row: object | None = None
        self.statements: list[str] = []

    async def execute(
        self, statement: object, params: Mapping[str, object] | None = None
    ) -> _Result:
        del params
        sql = str(statement)
        self.statements.append(sql)
        if "INSERT INTO mail_sender_leases" in sql:
            return _Result(self.claim_row)
        if "DELETE FROM mail_sender_leases" in sql:
            return _Result(self.delete_row)
        if "SELECT owner" in sql:
            return _Result(self.current_row)
        return _Result(None)


class _Context:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    def begin(self) -> _Context:
        return _Context(self.connection)

    def connect(self) -> _Context:
        return _Context(self.connection)


@pytest.mark.asyncio
async def test_postgres_sender_interlock_claims_with_purpose_and_fences() -> None:
    engine = _Engine()
    adapter = PostgresSenderInterlockAdapter(cast(AsyncEngine, engine), lease_seconds=60)

    assert await adapter.claim(
        tenant_id="tenant-1", account_ref="account-1", owner="mailhub", purpose="outreach"
    )
    assert "ON CONFLICT (tenant_id, account_ref, purpose)" in engine.connection.statements[-1]

    engine.connection.claim_row = None
    assert not await adapter.claim(
        tenant_id="tenant-1", account_ref="account-1", owner="legacy", purpose="outreach"
    )


@pytest.mark.asyncio
async def test_postgres_sender_interlock_release_is_owner_bound() -> None:
    engine = _Engine()
    adapter = PostgresSenderInterlockAdapter(cast(AsyncEngine, engine))
    await adapter.release(
        tenant_id="tenant-1", account_ref="account-1", owner="mailhub", purpose="email"
    )

    engine.connection.delete_row = None
    engine.connection.current_row = ("legacy",)
    with pytest.raises(RuntimeError, match="sender_interlock_owner_mismatch"):
        await adapter.release(
            tenant_id="tenant-1", account_ref="account-1", owner="mailhub", purpose="email"
        )


def test_postgres_sender_interlock_rejects_unbounded_lease() -> None:
    with pytest.raises(ValueError, match="sender_interlock_lease_seconds_invalid"):
        PostgresSenderInterlockAdapter(cast(AsyncEngine, _Engine()), lease_seconds=1)
