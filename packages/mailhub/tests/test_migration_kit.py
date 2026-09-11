"""Tests for the generic migration kit's takeover and rollback paths.

The kit exists so a product can move off its own mail module without guessing.
These tests pin the parts where guessing would be expensive: adopting a sync
position the old module reported, and handing authority back when the move has
to be undone.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from mailhub.hosts.migration import (
    MigrationAuditEvent,
    MigrationBatch,
    MigrationController,
    MigrationFeatureFlags,
)
from mailhub.storage import RepositoryConflictError


class FakeCursorSlot:
    """The repository slice a takeover needs, with the same compare-and-set."""

    def __init__(self, existing: str | None = None) -> None:
        self.cursor = existing
        self.calls: list[tuple[str | None, str | None]] = []

    async def get_cursor(
        self, *, tenant_id: str, connection_id: object, folder_ref: str
    ) -> str | None:
        del tenant_id, connection_id, folder_ref
        return self.cursor

    async def commit_cursor(
        self,
        *,
        tenant_id: str,
        connection_id: object,
        folder_ref: str,
        expected_cursor: str | None,
        next_cursor: str | None,
    ) -> None:
        del tenant_id, connection_id, folder_ref
        self.calls.append((expected_cursor, next_cursor))
        if self.cursor != expected_cursor:
            raise RepositoryConflictError("cursor_conflict")
        self.cursor = next_cursor


async def _batch(controller: MigrationController, **flags: bool) -> MigrationBatch:
    return await controller.start(
        tenant_id="tenant-1",
        legacy_system="legacy-mail",
        account_ref="ops@example.test",
        owner="operator-1",
        flags=MigrationFeatureFlags(**flags) if flags else None,
    )


def _events(controller: MigrationController, event_type: str) -> list[MigrationAuditEvent]:
    return [event for event in controller.events if event.event_type == event_type]


@pytest.mark.asyncio
async def test_takeover_adopts_the_legacy_position() -> None:
    controller = MigrationController()
    batch = await _batch(controller)
    slot = FakeCursorSlot()

    takeover = await controller.take_over_cursor(
        batch_id=batch.batch_id,
        cursors=slot,
        connection_id=uuid4(),
        folder_ref="INBOX",
        legacy_cursor="legacy:page-42",
        adopted_cursor="1789113608:327",
    )

    assert slot.cursor == "1789113608:327"
    assert takeover.legacy_cursor == "legacy:page-42"
    assert takeover.adopted_cursor == "1789113608:327"
    # Both cursors survive: the translation has to stay auditable.
    assert takeover.to_dict()["legacy_cursor"] == "legacy:page-42"
    assert controller.batches[batch.batch_id].state == "read_authority"
    assert len(_events(controller, "mail.migration.cursor_taken_over")) == 1


@pytest.mark.asyncio
async def test_takeover_refuses_over_an_existing_cursor_and_says_so() -> None:
    """Adopting over a live cursor would replay or skip mail, not migrate."""

    controller = MigrationController()
    batch = await _batch(controller)
    slot = FakeCursorSlot(existing="1789113608:300")

    with pytest.raises(RepositoryConflictError, match="cursor_conflict"):
        await controller.take_over_cursor(
            batch_id=batch.batch_id,
            cursors=slot,
            connection_id=uuid4(),
            folder_ref="INBOX",
            legacy_cursor="legacy:page-42",
            adopted_cursor="1789113608:327",
        )

    assert slot.cursor == "1789113608:300", "the running cursor must not be replaced"
    assert len(_events(controller, "mail.migration.cursor_takeover_refused")) == 1
    assert controller.batches[batch.batch_id].state == "planned"


@pytest.mark.asyncio
async def test_a_malformed_cursor_is_refused_before_anything_is_written() -> None:
    controller = MigrationController()
    batch = await _batch(controller)
    slot = FakeCursorSlot()

    with pytest.raises(ValueError, match="migration_adopted_cursor_invalid"):
        await controller.take_over_cursor(
            batch_id=batch.batch_id,
            cursors=slot,
            connection_id=uuid4(),
            folder_ref="INBOX",
            legacy_cursor="legacy:page-42",
            adopted_cursor="not-a-cursor",
        )

    assert slot.calls == [], "the repository must not be touched at all"


@pytest.mark.asyncio
async def test_takeover_is_refused_once_sending_has_moved() -> None:
    controller = MigrationController()
    batch = await _batch(controller, mailhub_read_authority=True, mailhub_send_authority=True)
    await controller.claim_send_authority(batch_id=batch.batch_id)

    with pytest.raises(ValueError, match="migration_cursor_takeover_not_allowed"):
        await controller.take_over_cursor(
            batch_id=batch.batch_id,
            cursors=FakeCursorSlot(),
            connection_id=uuid4(),
            folder_ref="INBOX",
            legacy_cursor="legacy:page-42",
            adopted_cursor="1789113608:327",
        )


@pytest.mark.asyncio
async def test_rollback_records_the_reason_and_is_idempotent() -> None:
    controller = MigrationController()
    batch = await _batch(controller)

    rolled_back = await controller.rollback(
        batch_id=batch.batch_id, reason="legacy_cursor_mismatch"
    )
    assert rolled_back.state == "rolled_back"

    again = await controller.rollback(
        batch_id=batch.batch_id, reason="operator_repeated_the_command"
    )
    assert again.state == "rolled_back"
    # Repeating a rollback must not manufacture a second piece of history.
    assert len(_events(controller, "mail.migration.rollback_completed")) == 1

    with pytest.raises(ValueError, match="migration_cursor_takeover_not_allowed"):
        await controller.take_over_cursor(
            batch_id=batch.batch_id,
            cursors=FakeCursorSlot(),
            connection_id=uuid4(),
            folder_ref="INBOX",
            legacy_cursor="legacy:page-42",
            adopted_cursor="1789113608:327",
        )


@pytest.mark.asyncio
async def test_releasing_send_authority_still_records_the_original_reason() -> None:
    """The old entry point keeps working now that it shares one code path."""

    controller = MigrationController()
    batch = await _batch(controller, mailhub_read_authority=True, mailhub_send_authority=True)
    assert await controller.claim_send_authority(batch_id=batch.batch_id) is True

    await controller.release_send_authority(batch_id=batch.batch_id)

    events = _events(controller, "mail.migration.rollback_completed")
    assert len(events) == 1
    assert events[0].reason == "sender_authority_released"
    assert controller.batches[batch.batch_id].state == "rolled_back"
