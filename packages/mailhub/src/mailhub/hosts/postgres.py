"""PostgreSQL-backed host leases for migration sender authority.

The lease table is deliberately separate from MailHub outbox state so the
legacy sender and MailHub sender can share one authority during a cutover. The
adapter stores only bounded identifiers and timestamps; it never receives mail
body bytes or provider credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mailhub.hosts.migration import SenderInterlockPort
from mailhub.persistence.sqlalchemy import _set_rls_scope


class PostgresSenderInterlockAdapter(SenderInterlockPort):
    """Transactional single-sender lease authority.

    ``claim`` is atomic and returns ``False`` when another owner holds an
    unexpired lease. A repeated claim by the same owner renews the lease and
    fences the previous attempt by incrementing ``fencing_token``. The
    process-local interlock remains available for contract tests only.
    """

    def __init__(self, engine: AsyncEngine, *, lease_seconds: int = 300) -> None:
        if lease_seconds < 30 or lease_seconds > 86_400:
            raise ValueError("sender_interlock_lease_seconds_invalid")
        self.engine = engine
        self.lease_seconds = lease_seconds

    async def claim(
        self, *, tenant_id: str, account_ref: str, owner: str, purpose: str = "email"
    ) -> bool:
        _validate_scope(tenant_id, account_ref, owner, purpose)
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=self.lease_seconds)
        async with self.engine.begin() as connection:
            await _set_rls_scope(connection, tenant_id=tenant_id, subject_id=owner)
            result = await connection.execute(
                text(
                    """
                    INSERT INTO mail_sender_leases
                        (tenant_id, account_ref, purpose, owner, fencing_token,
                         lease_until, created_at, updated_at)
                    VALUES
                        (:tenant_id, :account_ref, :purpose, :owner, 1,
                         :lease_until, :now, :now)
                    ON CONFLICT (tenant_id, account_ref, purpose) DO UPDATE
                    SET owner = EXCLUDED.owner,
                        fencing_token = mail_sender_leases.fencing_token + 1,
                        lease_until = EXCLUDED.lease_until,
                        updated_at = EXCLUDED.updated_at
                    WHERE mail_sender_leases.owner = EXCLUDED.owner
                       OR mail_sender_leases.lease_until <= EXCLUDED.updated_at
                    RETURNING fencing_token
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "account_ref": account_ref,
                    "purpose": purpose,
                    "owner": owner,
                    "lease_until": lease_until,
                    "now": now,
                },
            )
            return result.first() is not None

    async def release(
        self, *, tenant_id: str, account_ref: str, owner: str, purpose: str = "email"
    ) -> None:
        _validate_scope(tenant_id, account_ref, owner, purpose)
        async with self.engine.begin() as connection:
            await _set_rls_scope(connection, tenant_id=tenant_id, subject_id=owner)
            deleted = await connection.execute(
                text(
                    """
                    DELETE FROM mail_sender_leases
                    WHERE tenant_id = :tenant_id
                      AND account_ref = :account_ref
                      AND purpose = :purpose
                      AND owner = :owner
                    RETURNING fencing_token
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "account_ref": account_ref,
                    "purpose": purpose,
                    "owner": owner,
                },
            )
            if deleted.first() is not None:
                return
            current = await connection.execute(
                text(
                    """
                    SELECT owner
                    FROM mail_sender_leases
                    WHERE tenant_id = :tenant_id
                      AND account_ref = :account_ref
                      AND purpose = :purpose
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "account_ref": account_ref,
                    "purpose": purpose,
                },
            )
            row = current.first()
            if row is not None:
                raise RuntimeError("sender_interlock_owner_mismatch")

    async def check(self, *, tenant_id: str, owner: str) -> None:
        """Startup self-check: prove the lease table is reachable in tenant scope."""

        _validate_scope(tenant_id, "startup-check", owner, "email")
        async with self.engine.connect() as connection:
            await _set_rls_scope(connection, tenant_id=tenant_id, subject_id=owner)
            await connection.execute(text("SELECT 1 FROM mail_sender_leases LIMIT 1"))


def _validate_scope(tenant_id: str, account_ref: str, owner: str, purpose: str) -> None:
    for value, name, maximum in (
        (tenant_id, "tenant_id", 200),
        (account_ref, "account_ref", 512),
        (owner, "owner", 200),
        (purpose, "purpose", 100),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"sender_interlock_{name}_invalid")


__all__ = ["PostgresSenderInterlockAdapter"]
