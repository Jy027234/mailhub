from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from caplatform_bff.mailhub_host_actions import (
    EncryptedSQLiteMailHostActionBroker,
    MailHubHostActionError,
)


def _action(
    *,
    tenant_id: str = "tenant-1",
    subject_id: str = "user-1",
    candidate_id: str,
    candidate_revision: int = 2,
) -> dict[str, object]:
    return {
        "action_id": str(uuid4()),
        "action_type": "create_task",
        "tenant_id": tenant_id,
        "agent_subject_id": subject_id,
        "connection_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "recipient_addresses": [],
        "risk_flags": {},
        "input_digest": "a" * 64,
        "source_message_ids": [str(uuid4())],
        "parameters": {
            "candidate_id": candidate_id,
            "candidate_revision": candidate_revision,
            "candidate_type": "task",
            "candidate_payload": {
                "project_refs": ["project-1"],
                "summary": "Create a review task",
            },
        },
    }


@pytest.mark.asyncio
async def test_approval_is_scope_revision_and_action_bound(tmp_path: Path) -> None:
    broker = EncryptedSQLiteMailHostActionBroker(
        database_path=str(tmp_path / "state.sqlite3"),
        encryption_secret="host-action-secret-" * 3,
    )
    await broker.initialize()
    candidate_id = uuid4()
    reference = await broker.issue_candidate_approval(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=candidate_id,
        candidate_revision=2,
    )
    action = _action(candidate_id=str(candidate_id))

    assert await broker.verify_confirmation(confirmation_ref=reference, action=action)
    assert await broker.verify_confirmation(confirmation_ref=reference, action=action)
    assert not await broker.verify_confirmation(
        confirmation_ref=reference,
        action={**action, "agent_subject_id": "user-2"},
    )
    assert not await broker.verify_confirmation(
        confirmation_ref=reference,
        action={**action, "action_id": str(uuid4())},
    )


@pytest.mark.asyncio
async def test_action_ledger_replays_completed_result_and_rejects_digest_conflict(
    tmp_path: Path,
) -> None:
    broker = EncryptedSQLiteMailHostActionBroker(
        database_path=str(tmp_path / "state.sqlite3"),
        encryption_secret="host-action-secret-" * 3,
    )
    await broker.initialize()
    candidate_id = uuid4()
    reference = await broker.issue_candidate_approval(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=candidate_id,
        candidate_revision=2,
    )
    action = _action(candidate_id=str(candidate_id))
    assert await broker.verify_confirmation(confirmation_ref=reference, action=action)

    claimed = await broker.claim_execution(confirmation_ref=reference, action=action)
    assert not claimed.replayed
    result: dict[str, object] = {"status": "applied", "task_ref": "task-1"}
    await broker.complete_execution(binding=claimed.binding, result=result)

    with sqlite3.connect(tmp_path / "state.sqlite3") as database:
        database.execute("UPDATE mailhub_host_approvals SET expires_at=0")

    replayed = await broker.claim_execution(confirmation_ref=reference, action=action)
    assert replayed.replayed
    assert replayed.result == result

    conflicting = {**action, "input_digest": "b" * 64}
    with pytest.raises(MailHubHostActionError, match="mailhub_host_approval_invalid"):
        await broker.claim_execution(confirmation_ref=reference, action=conflicting)


@pytest.mark.asyncio
async def test_unverified_reference_cannot_execute(tmp_path: Path) -> None:
    broker = EncryptedSQLiteMailHostActionBroker(
        database_path=str(tmp_path / "state.sqlite3"),
        encryption_secret="host-action-secret-" * 3,
    )
    await broker.initialize()
    candidate_id = uuid4()
    reference = await broker.issue_candidate_approval(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=candidate_id,
        candidate_revision=2,
    )

    with pytest.raises(MailHubHostActionError, match="mailhub_host_approval_invalid"):
        await broker.claim_execution(
            confirmation_ref=reference,
            action=_action(candidate_id=str(candidate_id)),
        )


@pytest.mark.asyncio
async def test_knowledge_candidate_intake_is_encrypted_and_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    broker = EncryptedSQLiteMailHostActionBroker(
        database_path=str(database_path),
        encryption_secret="host-action-secret-" * 3,
    )
    await broker.initialize()
    candidate_id = uuid4()
    payload: dict[str, object] = {
        "candidate": {"title": "Sensitive bulletin", "suggested_scope": "personal"},
        "source_message_id": str(uuid4()),
    }

    created = await broker.submit_knowledge_candidate(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=candidate_id,
        content_sha256="c" * 64,
        payload=payload,
    )
    replayed = await broker.submit_knowledge_candidate(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=candidate_id,
        content_sha256="c" * 64,
        payload=payload,
    )

    assert created["status"] == "approved"
    assert created["idempotency_replayed"] is False
    assert replayed["idempotency_replayed"] is True
    assert b"Sensitive bulletin" not in database_path.read_bytes()
