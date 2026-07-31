"""Host-side migration helpers for CAACTRAINING and AeroLink cutovers.

These helpers carry IDs and shadow evidence only.  They do not import either
legacy repository, so the MailHub core remains portable and each host can keep
its own business state machine and rollback authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5


class MigrationPhase(StrEnum):
    SHADOW_READ = "shadow_read"
    MAILHUB_READ_AUTHORITY = "mailhub_read_authority"
    MAILHUB_SEND_AUTHORITY = "mailhub_send_authority"
    LEGACY_CLEANUP = "legacy_cleanup"


@dataclass(frozen=True, slots=True)
class LegacyReference:
    tenant_id: str
    legacy_system: str
    legacy_id: str
    mailhub_ref: str
    mapped_at: datetime
    state: str = "shadow"
    migration_batch_id: UUID | None = None
    owner_subject_id: str | None = None


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    tenant_id: str
    legacy_system: str
    legacy_ref: str
    mailhub_ref: str
    identity_match: bool
    cursor_match: bool
    content_hash_match: bool
    attachment_match: bool
    business_link_match: bool
    failure_state_match: bool
    compared_at: datetime

    @property
    def authority_safe(self) -> bool:
        return all(
            (
                self.identity_match,
                self.cursor_match,
                self.content_hash_match,
                self.attachment_match,
                self.business_link_match,
                self.failure_state_match,
            )
        )


class LegacyReferenceMap:
    """Tenant-scoped old-ID to MailHub reference map with no cross-tenant lookup."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], LegacyReference] = {}

    def put(
        self,
        *,
        tenant_id: str,
        legacy_system: str,
        legacy_id: str,
        mailhub_ref: str,
        state: str = "shadow",
        migration_batch_id: UUID | None = None,
        owner_subject_id: str | None = None,
    ) -> LegacyReference:
        _migration_text(tenant_id, "tenant_id", 200)
        _migration_text(legacy_system, "legacy_system", 100)
        _migration_text(legacy_id, "legacy_id", 512)
        _migration_text(mailhub_ref, "mailhub_ref", 512)
        if owner_subject_id is not None:
            _migration_text(owner_subject_id, "owner_subject_id", 200)
        key = (tenant_id, legacy_system, legacy_id)
        existing = self._items.get(key)
        if existing is not None:
            # A replay may advance migration state, but it must never silently
            # retarget an old identifier to a different MailHub fact.
            if existing.mailhub_ref != mailhub_ref:
                raise ValueError("legacy_reference_conflict")
            if (
                owner_subject_id is not None
                and existing.owner_subject_id is not None
                and owner_subject_id != existing.owner_subject_id
            ):
                raise ValueError("legacy_reference_owner_conflict")
            if (
                migration_batch_id is not None
                and existing.migration_batch_id is not None
                and migration_batch_id != existing.migration_batch_id
            ):
                raise ValueError("legacy_reference_batch_conflict")
        value = LegacyReference(
            tenant_id=tenant_id,
            legacy_system=legacy_system,
            legacy_id=legacy_id,
            mailhub_ref=mailhub_ref,
            mapped_at=existing.mapped_at if existing is not None else datetime.now(UTC),
            state=state,
            migration_batch_id=(
                migration_batch_id
                if migration_batch_id is not None
                else existing.migration_batch_id
                if existing is not None
                else None
            ),
            owner_subject_id=(
                owner_subject_id
                if owner_subject_id is not None
                else existing.owner_subject_id
                if existing is not None
                else None
            ),
        )
        self._items[key] = value
        return value

    def get(self, *, tenant_id: str, legacy_system: str, legacy_id: str) -> LegacyReference | None:
        return self._items.get((tenant_id, legacy_system, legacy_id))

    def list_for_tenant(self, *, tenant_id: str) -> tuple[LegacyReference, ...]:
        """Return deterministic, tenant-scoped mappings for migration evidence."""

        _migration_text(tenant_id, "tenant_id", 200)
        return tuple(
            sorted(
                (
                    value
                    for (mapped_tenant, _, _), value in self._items.items()
                    if mapped_tenant == tenant_id
                ),
                key=lambda value: (value.legacy_system, value.legacy_id),
            )
        )


class NoDualSenderInterlock:
    """Process-local test interlock; production replaces it with a DB/lease authority."""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._owners: dict[tuple[str, str, str], str] = {}
        self._guard = asyncio.Lock()

    async def claim(
        self, *, tenant_id: str, account_ref: str, owner: str, purpose: str = "email"
    ) -> bool:
        key = (tenant_id, account_ref, purpose)
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
            if lock.locked():
                return False
            await lock.acquire()
            self._owners[key] = owner
            return True

    async def release(
        self, *, tenant_id: str, account_ref: str, owner: str, purpose: str = "email"
    ) -> None:
        key = (tenant_id, account_ref, purpose)
        if self._owners.get(key) != owner:
            raise RuntimeError("sender_interlock_owner_mismatch")
        self._owners.pop(key, None)
        lock = self._locks.get(key)
        if lock is not None and lock.locked():
            lock.release()


class SenderInterlockPort(Protocol):
    """Durable host-owned sender lease contract for migration cutovers."""

    async def claim(
        self, *, tenant_id: str, account_ref: str, owner: str, purpose: str = "email"
    ) -> bool: ...

    async def release(
        self, *, tenant_id: str, account_ref: str, owner: str, purpose: str = "email"
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MigrationFeatureFlags:
    shadow_read: bool = True
    mailhub_read_authority: bool = False
    mailhub_send_authority: bool = False
    legacy_cleanup: bool = False

    def validate(self) -> None:
        if self.legacy_cleanup and not self.mailhub_send_authority:
            raise ValueError("legacy_cleanup_requires_mailhub_send_authority")
        if self.mailhub_send_authority and not self.mailhub_read_authority:
            raise ValueError("mailhub_send_requires_mailhub_read_authority")

    @property
    def phase(self) -> MigrationPhase:
        self.validate()
        if self.legacy_cleanup:
            return MigrationPhase.LEGACY_CLEANUP
        if self.mailhub_send_authority:
            return MigrationPhase.MAILHUB_SEND_AUTHORITY
        if self.mailhub_read_authority:
            return MigrationPhase.MAILHUB_READ_AUTHORITY
        return MigrationPhase.SHADOW_READ


@dataclass(frozen=True, slots=True)
class MigrationBatch:
    batch_id: UUID
    tenant_id: str
    legacy_system: str
    account_ref: str
    flags: MigrationFeatureFlags
    owner: str
    created_at: datetime
    state: str = "planned"
    purpose: str = "email"


@dataclass(frozen=True, slots=True)
class MigrationAuditEvent:
    """Safe migration evidence; never contains body, token or attachment bytes."""

    event_type: str
    batch_id: UUID
    tenant_id: str
    account_ref: str
    purpose: str
    owner: str
    phase: MigrationPhase
    occurred_at: datetime
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "batch_id": str(self.batch_id),
            "tenant_id": self.tenant_id,
            "account_ref": self.account_ref,
            "purpose": self.purpose,
            "owner": self.owner,
            "phase": self.phase.value,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "reason": self.reason,
        }


class MigrationController:
    """Small host-side state machine; production interlock is DB-backed."""

    def __init__(self, interlock: SenderInterlockPort | None = None) -> None:
        self.interlock = interlock or NoDualSenderInterlock()
        self.batches: dict[UUID, MigrationBatch] = {}
        self.events: list[MigrationAuditEvent] = []

    async def start(
        self,
        *,
        tenant_id: str,
        legacy_system: str,
        account_ref: str,
        owner: str,
        purpose: str = "email",
        flags: MigrationFeatureFlags | None = None,
        idempotency_key: str | None = None,
    ) -> MigrationBatch:
        _migration_text(tenant_id, "tenant_id", 200)
        _migration_text(legacy_system, "legacy_system", 100)
        _migration_text(account_ref, "account_ref", 512)
        _migration_text(owner, "owner", 200)
        _migration_text(purpose, "purpose", 100)
        if idempotency_key is not None:
            _migration_text(idempotency_key, "idempotency_key", 300)
        selected = flags or MigrationFeatureFlags()
        selected.validate()
        batch_id = (
            uuid5(
                NAMESPACE_URL,
                f"mailhub:migration:{tenant_id}:{legacy_system}:{account_ref}:{purpose}:{idempotency_key}",
            )
            if idempotency_key is not None
            else uuid4()
        )
        existing = self.batches.get(batch_id)
        if existing is not None:
            if (
                existing.tenant_id != tenant_id
                or existing.legacy_system != legacy_system
                or existing.account_ref != account_ref
                or existing.owner != owner
                or existing.purpose != purpose
                or existing.flags != selected
            ):
                raise ValueError("migration_batch_idempotency_conflict")
            return existing
        batch = MigrationBatch(
            batch_id=batch_id,
            tenant_id=tenant_id,
            legacy_system=legacy_system,
            account_ref=account_ref,
            flags=selected,
            owner=owner,
            created_at=datetime.now(UTC),
            purpose=purpose,
        )
        self.batches[batch.batch_id] = batch
        self.events.append(
            MigrationAuditEvent(
                event_type="mail.migration.shadow_compared",
                batch_id=batch.batch_id,
                tenant_id=batch.tenant_id,
                account_ref=batch.account_ref,
                purpose=batch.purpose,
                owner=batch.owner,
                phase=batch.flags.phase,
                occurred_at=batch.created_at,
                reason="migration_batch_started",
            )
        )
        return batch

    async def claim_send_authority(self, *, batch_id: UUID) -> bool:
        batch = self._batch(batch_id)
        if not batch.flags.mailhub_send_authority:
            raise ValueError("mailhub_send_authority_flag_required")
        if batch.state == "send_claimed":
            return True
        if batch.state == "rolled_back":
            raise ValueError("migration_batch_not_claimable")
        claimed = await self.interlock.claim(
            tenant_id=batch.tenant_id,
            account_ref=batch.account_ref,
            owner=batch.owner,
            purpose=batch.purpose,
        )
        if claimed:
            self.batches[batch_id] = replace(batch, state="send_claimed")
            self.events.append(
                MigrationAuditEvent(
                    event_type="mail.migration.authority_changed",
                    batch_id=batch.batch_id,
                    tenant_id=batch.tenant_id,
                    account_ref=batch.account_ref,
                    purpose=batch.purpose,
                    owner=batch.owner,
                    phase=MigrationPhase.MAILHUB_SEND_AUTHORITY,
                    occurred_at=datetime.now(UTC),
                    reason="mailhub_sender_lease_claimed",
                )
            )
        return claimed

    async def release_send_authority(self, *, batch_id: UUID) -> None:
        batch = self._batch(batch_id)
        if batch.state == "rolled_back":
            return
        await self.interlock.release(
            tenant_id=batch.tenant_id,
            account_ref=batch.account_ref,
            owner=batch.owner,
            purpose=batch.purpose,
        )
        self.batches[batch_id] = replace(batch, state="rolled_back")
        self.events.append(
            MigrationAuditEvent(
                event_type="mail.migration.rollback_completed",
                batch_id=batch.batch_id,
                tenant_id=batch.tenant_id,
                account_ref=batch.account_ref,
                purpose=batch.purpose,
                owner=batch.owner,
                phase=batch.flags.phase,
                occurred_at=datetime.now(UTC),
                reason="sender_authority_released",
            )
        )

    def _batch(self, batch_id: UUID) -> MigrationBatch:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise KeyError("migration_batch_not_found")
        return batch


def compare_shadow(
    *,
    tenant_id: str,
    legacy_system: str,
    legacy_ref: str,
    mailhub_ref: str,
    legacy: Mapping[str, object],
    mailhub: Mapping[str, object],
) -> ShadowComparison:
    """Compare normalized facts; raw bodies are never returned in the report."""

    def same(field: str) -> bool:
        return legacy.get(field) == mailhub.get(field)

    return ShadowComparison(
        tenant_id=tenant_id,
        legacy_system=legacy_system,
        legacy_ref=legacy_ref,
        mailhub_ref=mailhub_ref,
        identity_match=same("identity"),
        cursor_match=same("cursor"),
        content_hash_match=same("content_sha256"),
        attachment_match=same("attachments"),
        business_link_match=same("business_link"),
        failure_state_match=same("failure_state"),
        compared_at=datetime.now(UTC),
    )


def _migration_text(value: str, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"migration_{field}_invalid")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"migration_{field}_invalid")
