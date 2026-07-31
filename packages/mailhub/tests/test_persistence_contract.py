from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from mailhub.domain import (
    MailboxConnection,
    MailboxSyncState,
    MailMessageProjection,
    MailSyncJob,
    ProviderName,
    SubscriptionStatus,
    SyncJobStatus,
    digest_text,
)
from mailhub.persistence.sqlalchemy import (
    _connection_from_row,
    _connection_values,
    _message_from_row,
    _message_values,
    _set_rls_scope,
    _sync_job_from_row,
    _sync_job_values,
    _sync_state_from_row,
    _sync_state_values,
)


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def execute(self, statement: object, parameters: dict[str, str]) -> None:
        self.calls.append((str(statement), parameters))


@pytest.mark.asyncio
async def test_postgres_rls_scope_is_set_for_each_transaction() -> None:
    connection = _RecordingConnection()
    await _set_rls_scope(connection, tenant_id="tenant-1", subject_id="user-1")
    assert len(connection.calls) == 2
    assert all("set_config" in statement for statement, _ in connection.calls)
    assert connection.calls[0][1] == {"tenant_id": "tenant-1"}
    assert connection.calls[1][1] == {"subject_id": "user-1"}


def test_safety_rollback_preserves_core_columns() -> None:
    migration = Path(__file__).parents[1] / "migrations" / "0002_mailhub_safety_lifecycle.down.sql"
    source = migration.read_text(encoding="utf-8")
    assert "DROP COLUMN" not in source
    assert "DROP TABLE IF EXISTS mail_content_objects" in source


def test_provider_metadata_round_trips_through_sqlalchemy_values() -> None:
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-1",
        connection_id=uuid4(),
        thread_id=uuid4(),
        provider_message_ref="provider-1",
        internet_message_id="<provider-1@example.test>",
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        cc_addresses=("team@example.test",),
        bcc_addresses=("audit@example.test",),
        reply_to_addresses=("replies@example.test",),
        subject="Update",
        received_at=datetime.now(UTC),
        body_object_ref="object://message-1",
        content_sha256=digest_text("body"),
        provider_metadata={"body_content_type": "text", "change_key": "change-1"},
    )
    values = _message_values(message)
    restored = _message_from_row(values)
    assert restored.provider_metadata == message.provider_metadata
    assert restored.cc_addresses == message.cc_addresses
    assert restored.bcc_addresses == message.bcc_addresses
    assert restored.reply_to_addresses == message.reply_to_addresses


def test_sync_job_deleted_count_round_trips_through_sqlalchemy_values() -> None:
    job = MailSyncJob(
        job_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="subject-1",
        connection_id=uuid4(),
        idempotency_key="sync-deleted-count",
        fetched_count=4,
        deleted_count=3,
        status=SyncJobStatus.RETRY_WAIT,
        attempt_count=2,
        next_attempt_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    values = _sync_job_values(job)
    restored = _sync_job_from_row(values)
    assert restored.deleted_count == 3
    assert restored.status is SyncJobStatus.RETRY_WAIT
    assert restored.attempt_count == 2
    assert restored.next_attempt_at == job.next_attempt_at


def test_sync_job_cancelling_status_round_trips_through_sqlalchemy_values() -> None:
    job = MailSyncJob(
        job_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="subject-1",
        connection_id=uuid4(),
        idempotency_key="sync-cancelling",
        status=SyncJobStatus.CANCELLING,
        error_code="sync_cancel_requested",
        lease_owner="worker-1",
    )
    values = _sync_job_values(job)
    restored = _sync_job_from_row(values)
    assert restored.status is SyncJobStatus.CANCELLING
    assert restored.error_code == "sync_cancel_requested"
    assert restored.lease_owner == "worker-1"


def test_connection_provider_identity_round_trips_through_sqlalchemy_values() -> None:
    connection = MailboxConnection(
        connection_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="subject-1",
        provider=ProviderName.MICROSOFT_GRAPH,
        email_address="user@example.test",
        credential_ref="credential-ref-1",
        granted_scopes=("Mail.Read",),
        provider_account_id="graph-account-1",
        provider_tenant_id="tenant-guid-1",
        credential_version=4,
    )
    values = _connection_values(connection)
    restored = _connection_from_row(values)
    assert restored == connection


@pytest.mark.parametrize(
    ("field", "value"),
    [("provider_account_id", "bad\nidentity"), ("provider_tenant_id", " ")],
)
def test_connection_provider_identity_rejects_control_or_empty_values(
    field: str, value: str
) -> None:
    with pytest.raises(ValueError, match=f"{field}_invalid"):
        MailboxConnection(
            connection_id=uuid4(),
            tenant_id="tenant-1",
            subject_id="subject-1",
            provider=ProviderName.GMAIL,
            email_address="user@example.test",
            credential_ref="credential-ref-1",
            provider_account_id=value if field == "provider_account_id" else None,
            provider_tenant_id=value if field == "provider_tenant_id" else None,
        )


def test_subscription_sync_state_round_trips_through_sqlalchemy_values() -> None:
    now = datetime.now(UTC)
    state = MailboxSyncState(
        tenant_id="tenant-1",
        connection_id=uuid4(),
        folder_ref="INBOX",
        cursor_kind="history_id",
        cursor_value="123",
        subscription_ref="watch-1",
        subscription_status=SubscriptionStatus.ACTIVE,
        subscription_expires_at=now,
        subscription_callback_endpoint="https://host.example.test/callback",
        subscription_client_state_ref="secret-ref-1",
        subscription_provider_request_id="provider-request-1",
        lease_owner="worker-1",
        fencing_token=4,
        status="idle",
        watermark=now,
        updated_at=now,
    )
    values = _sync_state_values(state)
    restored = _sync_state_from_row(values)
    assert restored == state
