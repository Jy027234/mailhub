"""PostgreSQL repository implementing MailHub's durable state contract.

The adapter intentionally uses SQLAlchemy Core rather than ORM entities.  This
keeps the domain independent from persistence and makes the transaction/lease
boundaries visible.  Run the checked-in SQL migrations before using it; the
adapter does not silently create or alter production schema.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BIGINT,
    BOOLEAN,
    CHAR,
    INTEGER,
    NUMERIC,
    TEXT,
    Column,
    DateTime,
    MetaData,
    Table,
    and_,
    delete,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mailhub.domain import (
    ActionType,
    AutomationLevel,
    AutonomyRunStatus,
    CandidateState,
    CandidateType,
    ConnectionCleanupRefs,
    ConnectionStatus,
    ContentMode,
    DelegationGrant,
    DeliveryStatus,
    MailActionCandidate,
    MailAgentPolicy,
    MailAutonomyRun,
    MailboxConnection,
    MailboxSyncState,
    MailDraft,
    MailMessageProjection,
    MailOutboxOperation,
    MailSyncJob,
    MailThread,
    ProviderName,
    SubscriptionStatus,
    SyncJobStatus,
    transition_autonomy_run,
    transition_delivery,
    transition_sync_job,
    utc_now,
)
from mailhub.observability import redact_event
from mailhub.rules import (
    MailRule,
    RuleCondition,
    RuleExecution,
    RuleExecutionStatus,
    RuleField,
    RuleOperator,
)
from mailhub.storage import RepositoryConflictError
from mailhub.webhook import normalize_receipt_route_metadata

UTC_DATETIME = DateTime(timezone=True)
_metadata = MetaData()


async def _set_rls_scope(connection: Any, *, tenant_id: str, subject_id: str | None) -> None:
    if not tenant_id.strip():
        raise ValueError("tenant_id_required")
    await connection.execute(
        text("SELECT set_config('mailhub.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )
    if subject_id is not None:
        if not subject_id.strip():
            raise ValueError("subject_id_required")
        await connection.execute(
            text("SELECT set_config('mailhub.subject_id', :subject_id, true)"),
            {"subject_id": subject_id},
        )


def _table(name: str, *columns: Column[Any], **kwargs: Any) -> Table:
    return Table(name, _metadata, *columns, **kwargs)


connections = _table(
    "mail_connections",
    Column("connection_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("subject_id", TEXT, nullable=False),
    Column("provider", TEXT, nullable=False),
    Column("email_address", TEXT, nullable=False),
    Column("credential_ref", TEXT, nullable=False),
    Column("granted_scopes", ARRAY(TEXT), nullable=False),
    Column("provider_account_id", TEXT),
    Column("provider_tenant_id", TEXT),
    Column("credential_version", BIGINT, nullable=False),
    Column("content_mode", TEXT, nullable=False),
    Column("status", TEXT, nullable=False),
    Column("revision", BIGINT, nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("updated_at", UTC_DATETIME, nullable=False),
)
threads = _table(
    "mail_threads",
    Column("thread_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("connection_id", PG_UUID(as_uuid=True), nullable=False),
    Column("provider_thread_ref", TEXT, nullable=False),
    Column("normalized_subject", TEXT, nullable=False),
    Column("participant_addresses", ARRAY(TEXT), nullable=False),
    Column("latest_at", UTC_DATETIME, nullable=False),
    Column("message_count", INTEGER, nullable=False),
    Column("revision", BIGINT, nullable=False),
)
messages = _table(
    "mail_messages",
    Column("message_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("connection_id", PG_UUID(as_uuid=True), nullable=False),
    Column("thread_id", PG_UUID(as_uuid=True), nullable=False),
    Column("provider_message_ref", TEXT, nullable=False),
    Column("internet_message_id", TEXT),
    Column("sender_address", TEXT, nullable=False),
    Column("recipient_addresses", ARRAY(TEXT), nullable=False),
    Column("cc_addresses", ARRAY(TEXT), nullable=False),
    Column("bcc_addresses", ARRAY(TEXT), nullable=False),
    Column("reply_to_addresses", ARRAY(TEXT), nullable=False),
    Column("subject", TEXT, nullable=False),
    Column("received_at", UTC_DATETIME, nullable=False),
    Column("body_object_ref", TEXT),
    Column("content_sha256", CHAR(64), nullable=False),
    Column("labels", ARRAY(TEXT), nullable=False),
    Column("attachment_count", INTEGER, nullable=False),
    Column("is_read", BOOLEAN, nullable=False),
    Column("provider_metadata", JSONB, nullable=False),
    Column("revision", BIGINT, nullable=False),
)
cursors = _table(
    "mail_sync_cursors",
    Column("connection_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("folder_ref", TEXT, primary_key=True),
    Column("cursor_kind", TEXT, nullable=False),
    Column("cursor_value", TEXT),
    Column("subscription_ref", TEXT),
    Column("subscription_status", TEXT, nullable=False),
    Column("subscription_expires_at", UTC_DATETIME),
    Column("subscription_callback_endpoint", TEXT),
    Column("subscription_client_state_ref", TEXT),
    Column("subscription_provider_request_id", TEXT),
    Column("lease_owner", TEXT),
    Column("fencing_token", BIGINT, nullable=False),
    Column("status", TEXT, nullable=False),
    Column("watermark", UTC_DATETIME),
    Column("updated_at", UTC_DATETIME, nullable=False),
)
sync_jobs = _table(
    "mail_sync_jobs",
    Column("job_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("subject_id", TEXT, nullable=False),
    Column("connection_id", PG_UUID(as_uuid=True), nullable=False),
    Column("mode", TEXT, nullable=False),
    Column("requested_limit", INTEGER, nullable=False),
    Column("folder_ref", TEXT, nullable=False),
    Column("label_refs", ARRAY(TEXT), nullable=False),
    Column("received_after", UTC_DATETIME),
    Column("received_before", UTC_DATETIME),
    Column("idempotency_key", TEXT, nullable=False),
    Column("status", TEXT, nullable=False),
    Column("fetched_count", INTEGER, nullable=False),
    Column("saved_count", INTEGER, nullable=False),
    Column("duplicate_count", INTEGER, nullable=False),
    Column("deleted_count", INTEGER, nullable=False),
    Column("cursor_before", TEXT),
    Column("cursor_after", TEXT),
    Column("provider_request_id", TEXT),
    Column("error_code", TEXT),
    Column("trace_id", TEXT),
    Column("lease_owner", TEXT),
    Column("lease_expires_at", UTC_DATETIME),
    Column("fencing_token", BIGINT, nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("started_at", UTC_DATETIME),
    Column("finished_at", UTC_DATETIME),
    Column("updated_at", UTC_DATETIME, nullable=False),
    Column("attempt_count", INTEGER, nullable=False, server_default="0"),
    Column("next_attempt_at", UTC_DATETIME),
)
autonomy_runs = _table(
    "mail_autonomy_runs",
    Column("run_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("subject_id", TEXT, nullable=False),
    Column("connection_id", PG_UUID(as_uuid=True), nullable=False),
    Column("replay_key", TEXT, nullable=False),
    Column("mode", TEXT, nullable=False),
    Column("requested_limit", INTEGER, nullable=False),
    Column("message_limit", INTEGER, nullable=False),
    Column("status", TEXT, nullable=False),
    Column("message_ids", ARRAY(PG_UUID(as_uuid=True)), nullable=False),
    Column("analyzed_message_ids", ARRAY(PG_UUID(as_uuid=True)), nullable=False),
    Column("candidate_ids", ARRAY(PG_UUID(as_uuid=True)), nullable=False),
    Column("sync_result", JSONB, nullable=False),
    Column("sync_job_id", PG_UUID(as_uuid=True)),
    Column("error_code", TEXT),
    Column("pause_reason", TEXT),
    Column("lease_owner", TEXT),
    Column("lease_expires_at", UTC_DATETIME),
    Column("fencing_token", BIGINT, nullable=False),
    Column("revision", BIGINT, nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("started_at", UTC_DATETIME),
    Column("finished_at", UTC_DATETIME),
    Column("updated_at", UTC_DATETIME, nullable=False),
)
webhook_receipts = _table(
    "mail_webhook_receipts",
    Column("receipt_id", BIGINT, primary_key=True),
    Column("provider", TEXT, nullable=False),
    Column("tenant_id", TEXT, nullable=False),
    Column("event_id", TEXT, nullable=False),
    Column("body_sha256", CHAR(64), nullable=False),
    Column("received_at", UTC_DATETIME, nullable=False),
    Column("verified", BOOLEAN, nullable=False),
    Column("duplicate", BOOLEAN, nullable=False),
    Column("status", TEXT, nullable=False),
    Column("trace_id", TEXT, nullable=False),
    Column("route_metadata", JSONB, nullable=False),
)
rules = _table(
    "mail_rules",
    Column("rule_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("owner_subject_id", TEXT, nullable=False),
    Column("name", TEXT, nullable=False),
    Column("conditions", JSONB, nullable=False),
    Column("action_type", TEXT, nullable=False),
    Column("action_params", JSONB, nullable=False),
    Column("automation_level", TEXT, nullable=False),
    Column("version", BIGINT, nullable=False),
    Column("enabled", BOOLEAN, nullable=False),
    Column("max_per_hour", INTEGER, nullable=False),
    Column("max_per_day", INTEGER, nullable=False),
    Column("valid_from", UTC_DATETIME, nullable=False),
    Column("valid_until", UTC_DATETIME, nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("updated_at", UTC_DATETIME, nullable=False),
)
rule_executions = _table(
    "mail_rule_executions",
    Column("execution_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("subject_id", TEXT, nullable=False),
    Column("rule_id", PG_UUID(as_uuid=True), nullable=False),
    Column("rule_version", BIGINT, nullable=False),
    Column("message_id", PG_UUID(as_uuid=True), nullable=False),
    Column("action_id", PG_UUID(as_uuid=True), nullable=False),
    Column("input_digest", CHAR(64), nullable=False),
    Column("status", TEXT, nullable=False),
    Column("reason", TEXT, nullable=False),
    Column("result", JSONB, nullable=False),
    Column("revision", BIGINT, nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("updated_at", UTC_DATETIME, nullable=False),
)
policies = _table(
    "mail_agent_policies",
    Column("policy_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("owner_subject_id", TEXT, nullable=False),
    Column("allowed_connection_ids", ARRAY(PG_UUID(as_uuid=True)), nullable=False),
    Column("allowed_folder_refs", ARRAY(TEXT), nullable=False),
    Column("allowed_actions", ARRAY(TEXT), nullable=False),
    Column("allowed_domains", ARRAY(TEXT), nullable=False),
    Column("allowed_data_classes", ARRAY(TEXT), nullable=False),
    Column("thread_only", BOOLEAN, nullable=False),
    Column("allowed_automation_level", TEXT, nullable=False),
    Column("max_per_hour", INTEGER, nullable=False),
    Column("max_per_day", INTEGER, nullable=False),
    Column("valid_from", UTC_DATETIME, nullable=False),
    Column("valid_until", UTC_DATETIME, nullable=False),
    Column("revision", BIGINT, nullable=False),
    Column("enabled", BOOLEAN, nullable=False),
)
grants = _table(
    "mail_delegation_grants",
    Column("grant_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("policy_id", PG_UUID(as_uuid=True), nullable=False),
    Column("agent_subject_id", TEXT, nullable=False),
    Column("granted_by_subject_id", TEXT, nullable=False),
    Column("capability_ids", ARRAY(TEXT), nullable=False),
    Column("granted_at", UTC_DATETIME, nullable=False),
    Column("expires_at", UTC_DATETIME, nullable=False),
    Column("revoked_at", UTC_DATETIME),
    Column("revision", BIGINT, nullable=False),
)
drafts = _table(
    "mail_drafts",
    Column("draft_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("subject_id", TEXT, nullable=False),
    Column("connection_id", PG_UUID(as_uuid=True), nullable=False),
    Column("thread_id", PG_UUID(as_uuid=True)),
    Column("recipient_addresses", ARRAY(TEXT), nullable=False),
    Column("subject", TEXT, nullable=False),
    Column("cc_addresses", ARRAY(TEXT), nullable=False, server_default="{}"),
    Column("bcc_addresses", ARRAY(TEXT), nullable=False, server_default="{}"),
    Column("attachment_refs", ARRAY(TEXT), nullable=False, server_default="{}"),
    Column("body_object_ref", TEXT),
    Column("content_sha256", CHAR(64), nullable=False),
    Column("revision", BIGINT, nullable=False),
    Column("status", TEXT, nullable=False),
    Column("provider_draft_ref", TEXT),
    Column("expires_at", UTC_DATETIME, nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("updated_at", UTC_DATETIME, nullable=False),
)
operations = _table(
    "mail_outbox_operations",
    Column("operation_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("subject_id", TEXT, nullable=False),
    Column("draft_id", PG_UUID(as_uuid=True), nullable=False),
    Column("connection_id", PG_UUID(as_uuid=True), nullable=False),
    Column("idempotency_key", TEXT, nullable=False),
    Column("status", TEXT, nullable=False),
    Column("action_id", PG_UUID(as_uuid=True)),
    Column("input_digest", CHAR(64)),
    Column("agent_subject_id", TEXT),
    Column("policy_id", PG_UUID(as_uuid=True)),
    Column("grant_id", PG_UUID(as_uuid=True)),
    Column("policy_revision", BIGINT),
    Column("grant_revision", BIGINT),
    Column("approval_ref", TEXT),
    Column("approver_subject_id", TEXT),
    Column("attempt_count", INTEGER, nullable=False),
    Column("lease_owner", TEXT),
    Column("lease_expires_at", UTC_DATETIME),
    Column("fencing_token", BIGINT, nullable=False),
    Column("provider_message_ref", TEXT),
    Column("provider_request_id", TEXT),
    Column("error_code", TEXT),
    Column("next_attempt_at", UTC_DATETIME),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("updated_at", UTC_DATETIME, nullable=False),
)
audits = _table(
    "mail_audit_events",
    Column("audit_id", BIGINT, primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("subject_id", TEXT, nullable=False),
    Column("event_type", TEXT, nullable=False),
    Column("target_ref", TEXT, nullable=False),
    Column("occurred_at", UTC_DATETIME, nullable=False),
    Column("metadata", JSONB, nullable=False),
)
candidates = _table(
    "mail_action_candidates",
    Column("candidate_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", TEXT, nullable=False),
    Column("subject_id", TEXT, nullable=False),
    Column("message_id", PG_UUID(as_uuid=True), nullable=False),
    Column("candidate_type", TEXT, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("evidence", JSONB, nullable=False),
    Column("confidence", NUMERIC(5, 4), nullable=False),
    Column("requires_review", BOOLEAN, nullable=False),
    Column("status", TEXT, nullable=False),
    Column("revision", BIGINT, nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("updated_at", UTC_DATETIME, nullable=False),
)
sender_leases = _table(
    "mail_sender_leases",
    Column("tenant_id", TEXT, primary_key=True),
    Column("account_ref", TEXT, primary_key=True),
    Column("purpose", TEXT, primary_key=True),
    Column("owner", TEXT, nullable=False),
    Column("fencing_token", BIGINT, nullable=False),
    Column("lease_until", UTC_DATETIME, nullable=False),
    Column("created_at", UTC_DATETIME, nullable=False),
    Column("updated_at", UTC_DATETIME, nullable=False),
)


class SqlAlchemyMailRepository:
    """Transactional PostgreSQL repository; credentials/body bytes never enter it."""

    metadata = _metadata

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def _begin(self, *, tenant_id: str, subject_id: str | None = None) -> AsyncIterator[Any]:
        """Open a transaction with the PostgreSQL RLS tenant context set.

        ``0002_mailhub_safety_lifecycle.sql`` enables FORCE RLS.  Setting the
        context in the same transaction as every read/write prevents a pooled
        connection from inheriting another tenant's scope.  Repository WHERE
        predicates remain a second, application-level gate.
        """

        async with self.engine.begin() as conn:
            await _set_rls_scope(conn, tenant_id=tenant_id, subject_id=subject_id)
            yield conn

    @asynccontextmanager
    async def _connect(
        self, *, tenant_id: str, subject_id: str | None = None
    ) -> AsyncIterator[Any]:
        """Open a read connection with a transaction-local RLS context."""

        async with self.engine.connect() as conn:
            await _set_rls_scope(conn, tenant_id=tenant_id, subject_id=subject_id)
            yield conn

    @classmethod
    def from_url(cls, database_url: str, **kwargs: Any) -> SqlAlchemyMailRepository:
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("mailhub_requires_postgresql_asyncpg_url")
        return cls(create_async_engine(database_url, **kwargs))

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def save_connection(self, connection: MailboxConnection) -> MailboxConnection:
        values = _connection_values(connection)
        try:
            async with self._begin(
                tenant_id=connection.tenant_id, subject_id=connection.subject_id
            ) as conn:
                current = await conn.execute(
                    select(connections.c.revision).where(
                        connections.c.connection_id == connection.connection_id
                    )
                )
                row = current.first()
                if row is not None and int(row.revision) >= connection.revision:
                    raise RepositoryConflictError("connection_revision_conflict")
                if row is None:
                    await conn.execute(insert(connections).values(**values))
                else:
                    await conn.execute(
                        update(connections)
                        .where(connections.c.connection_id == connection.connection_id)
                        .values(**values)
                    )
        except IntegrityError as exc:
            raise RepositoryConflictError("connection_identity_conflict") from exc
        return connection

    async def list_connections(
        self, *, tenant_id: str, subject_id: str
    ) -> tuple[MailboxConnection, ...]:
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(connections)
                .where(
                    and_(
                        connections.c.tenant_id == tenant_id, connections.c.subject_id == subject_id
                    )
                )
                .order_by(connections.c.updated_at.desc())
            )
            return tuple(_connection_from_row(row) for row in result.mappings())

    async def get_connection(
        self, *, tenant_id: str, connection_id: UUID
    ) -> MailboxConnection | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(connections).where(
                            and_(
                                connections.c.connection_id == connection_id,
                                connections.c.tenant_id == tenant_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _connection_from_row(row) if row else None

    async def save_thread(self, thread: MailThread) -> MailThread:
        values = _thread_values(thread)
        statement = (
            pg_insert(threads)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[threads.c.connection_id, threads.c.provider_thread_ref],
                set_={
                    "normalized_subject": thread.normalized_subject,
                    "participant_addresses": list(thread.participant_addresses),
                    "latest_at": thread.latest_at,
                    "message_count": thread.message_count,
                    "revision": thread.revision,
                },
            )
        )
        try:
            async with self._begin(tenant_id=thread.tenant_id) as conn:
                await conn.execute(statement)
        except IntegrityError as exc:
            raise RepositoryConflictError("thread_conflict") from exc
        return thread

    async def save_message(
        self, message: MailMessageProjection
    ) -> tuple[MailMessageProjection, bool]:
        if message.body_text is not None and message.body_object_ref is None:
            raise RepositoryConflictError("message_object_ref_required")
        values = _message_values(message)
        statement = (
            pg_insert(messages)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[messages.c.connection_id, messages.c.provider_message_ref]
            )
        )
        try:
            async with self._begin(tenant_id=message.tenant_id) as conn:
                result = await conn.execute(statement)
                created = result.rowcount == 1
                row = (
                    (
                        await conn.execute(
                            select(messages).where(
                                and_(
                                    messages.c.connection_id == message.connection_id,
                                    messages.c.provider_message_ref == message.provider_message_ref,
                                )
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
        except IntegrityError as exc:
            raise RepositoryConflictError("message_conflict") from exc
        if row is None:
            raise RepositoryConflictError("message_not_saved")
        return _message_from_row(row), created

    async def delete_message_by_provider_ref(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        provider_message_ref: str,
    ) -> MailMessageProjection | None:
        """Idempotently remove a provider-deleted message and its candidates."""

        if not provider_message_ref.strip():
            raise ValueError("provider_message_ref_required")
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(messages)
                        .where(
                            and_(
                                messages.c.tenant_id == tenant_id,
                                messages.c.connection_id == connection_id,
                                messages.c.provider_message_ref == provider_message_ref,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            message = _message_from_row(row)
            await conn.execute(
                delete(candidates).where(
                    and_(
                        candidates.c.tenant_id == tenant_id,
                        candidates.c.message_id == message.message_id,
                    )
                )
            )
            await conn.execute(
                delete(rule_executions).where(
                    and_(
                        rule_executions.c.tenant_id == tenant_id,
                        rule_executions.c.message_id == message.message_id,
                    )
                )
            )
            await conn.execute(
                delete(messages).where(
                    and_(
                        messages.c.tenant_id == tenant_id,
                        messages.c.message_id == message.message_id,
                    )
                )
            )
            thread_row = (
                (
                    await conn.execute(
                        select(threads)
                        .where(
                            and_(
                                threads.c.tenant_id == tenant_id,
                                threads.c.thread_id == message.thread_id,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if thread_row is not None:
                remaining_rows = (
                    await conn.execute(
                        select(
                            messages.c.sender_address,
                            messages.c.recipient_addresses,
                            messages.c.cc_addresses,
                            messages.c.bcc_addresses,
                            messages.c.reply_to_addresses,
                            messages.c.received_at,
                        ).where(
                            and_(
                                messages.c.tenant_id == tenant_id,
                                messages.c.thread_id == message.thread_id,
                            )
                        )
                    )
                ).mappings()
                remaining = tuple(remaining_rows)
                participants = tuple(
                    dict.fromkeys(
                        address
                        for item in remaining
                        for address in (
                            str(item["sender_address"]),
                            *tuple(str(value) for value in _list(item["recipient_addresses"])),
                            *tuple(str(value) for value in _list(item["cc_addresses"])),
                            *tuple(str(value) for value in _list(item["bcc_addresses"])),
                            *tuple(str(value) for value in _list(item["reply_to_addresses"])),
                        )
                    )
                )
                latest_at = max(
                    (_parse_datetime(item["received_at"]) for item in remaining),
                    default=_parse_datetime(thread_row["latest_at"]),
                )
                await conn.execute(
                    update(threads)
                    .where(
                        and_(
                            threads.c.tenant_id == tenant_id,
                            threads.c.thread_id == message.thread_id,
                        )
                    )
                    .values(
                        participant_addresses=list(participants),
                        latest_at=latest_at,
                        message_count=len(remaining),
                        revision=int(thread_row["revision"]) + 1,
                    )
                )
            return message

    async def get_message(
        self, *, tenant_id: str, message_id: UUID
    ) -> MailMessageProjection | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(messages).where(
                            and_(
                                messages.c.message_id == message_id,
                                messages.c.tenant_id == tenant_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _message_from_row(row) if row else None

    async def list_messages(
        self, *, tenant_id: str, subject_id: str, limit: int
    ) -> tuple[MailMessageProjection, ...]:
        _check_limit(limit)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(messages)
                .join(connections, connections.c.connection_id == messages.c.connection_id)
                .where(
                    and_(
                        messages.c.tenant_id == tenant_id,
                        connections.c.subject_id == subject_id,
                    )
                )
                .order_by(messages.c.received_at.desc())
                .limit(limit)
            )
            return tuple(_message_from_row(row) for row in result.mappings())

    async def list_messages_for_connection(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID, limit: int = 5000
    ) -> tuple[MailMessageProjection, ...]:
        _check_lifecycle_limit(limit)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(messages)
                .join(connections, connections.c.connection_id == messages.c.connection_id)
                .where(
                    and_(
                        messages.c.tenant_id == tenant_id,
                        connections.c.subject_id == subject_id,
                        messages.c.connection_id == connection_id,
                    )
                )
                .order_by(messages.c.received_at.desc())
                .limit(limit)
            )
            return tuple(_message_from_row(row) for row in result.mappings())

    async def list_connection_cleanup_refs(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID
    ) -> ConnectionCleanupRefs:
        """Return complete opaque cleanup references in one RLS-scoped read."""

        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            exists = (
                await conn.execute(
                    select(connections.c.connection_id).where(
                        and_(
                            connections.c.connection_id == connection_id,
                            connections.c.tenant_id == tenant_id,
                            connections.c.subject_id == subject_id,
                        )
                    )
                )
            ).first()
            if exists is None:
                raise RepositoryConflictError("connection_not_found")

            message_rows = (
                await conn.execute(
                    select(messages.c.message_id, messages.c.body_object_ref).where(
                        and_(
                            messages.c.tenant_id == tenant_id,
                            messages.c.connection_id == connection_id,
                        )
                    )
                )
            ).all()
            message_ids = tuple(row[0] for row in message_rows)
            object_refs = {row[1] for row in message_rows if row[1]}

            draft_rows = (
                await conn.execute(
                    select(drafts.c.body_object_ref, drafts.c.attachment_refs).where(
                        and_(
                            drafts.c.tenant_id == tenant_id,
                            drafts.c.connection_id == connection_id,
                        )
                    )
                )
            ).all()
            for body_ref, attachment_refs in draft_rows:
                if body_ref:
                    object_refs.add(body_ref)
                object_refs.update(ref for ref in (attachment_refs or ()) if ref)

            knowledge_query = select(candidates.c.message_id).where(
                and_(
                    candidates.c.tenant_id == tenant_id,
                    candidates.c.candidate_type == CandidateType.KNOWLEDGE.value,
                    candidates.c.message_id.in_(message_ids),
                )
            )
            knowledge_message_ids = tuple(
                row[0] for row in (await conn.execute(knowledge_query)).all()
            )
            return ConnectionCleanupRefs(
                message_ids=message_ids,
                knowledge_message_ids=knowledge_message_ids,
                object_refs=tuple(object_refs),
            )

    async def search_messages(
        self, *, tenant_id: str, subject_id: str, query: str, limit: int
    ) -> tuple[MailMessageProjection, ...]:
        _check_limit(limit)
        if not query.strip():
            raise ValueError("search_query_invalid")
        pattern = f"%{query.strip()}%"
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(messages)
                .join(connections, connections.c.connection_id == messages.c.connection_id)
                .where(
                    and_(
                        messages.c.tenant_id == tenant_id,
                        connections.c.subject_id == subject_id,
                        (
                            messages.c.subject.ilike(pattern)
                            | messages.c.sender_address.ilike(pattern)
                            | sql_cast(messages.c.recipient_addresses, TEXT).ilike(pattern)
                            | sql_cast(messages.c.cc_addresses, TEXT).ilike(pattern)
                            | sql_cast(messages.c.bcc_addresses, TEXT).ilike(pattern)
                            | sql_cast(messages.c.reply_to_addresses, TEXT).ilike(pattern)
                            | messages.c.provider_message_ref.ilike(pattern)
                        ),
                    )
                )
                .order_by(messages.c.received_at.desc())
                .limit(limit)
            )
            return tuple(_message_from_row(row) for row in result.mappings())

    async def list_threads(
        self, *, tenant_id: str, subject_id: str, limit: int
    ) -> tuple[MailThread, ...]:
        _check_limit(limit)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(threads)
                .join(connections, connections.c.connection_id == threads.c.connection_id)
                .where(
                    and_(threads.c.tenant_id == tenant_id, connections.c.subject_id == subject_id)
                )
                .order_by(threads.c.latest_at.desc())
                .limit(limit)
            )
            return tuple(_thread_from_row(row) for row in result.mappings())

    async def get_thread(self, *, tenant_id: str, thread_id: UUID) -> MailThread | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(threads).where(
                            and_(threads.c.thread_id == thread_id, threads.c.tenant_id == tenant_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _thread_from_row(row) if row else None

    async def list_thread_messages(
        self, *, tenant_id: str, subject_id: str, thread_id: UUID, limit: int = 200
    ) -> tuple[MailMessageProjection, ...]:
        _check_limit(min(limit, 200))
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(messages)
                .join(connections, connections.c.connection_id == messages.c.connection_id)
                .where(
                    and_(
                        messages.c.tenant_id == tenant_id,
                        messages.c.thread_id == thread_id,
                        connections.c.subject_id == subject_id,
                    )
                )
                .order_by(messages.c.received_at.asc())
                .limit(limit)
            )
            return tuple(_message_from_row(row) for row in result.mappings())

    async def get_cursor(
        self, *, tenant_id: str, connection_id: UUID, folder_ref: str
    ) -> str | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                await conn.execute(
                    select(cursors.c.cursor_value).where(
                        and_(
                            cursors.c.connection_id == connection_id,
                            cursors.c.tenant_id == tenant_id,
                            cursors.c.folder_ref == folder_ref,
                        )
                    )
                )
            ).first()
            return cast(str | None, row[0]) if row else None

    async def commit_cursor(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        folder_ref: str,
        expected_cursor: str | None,
        next_cursor: str | None,
    ) -> None:
        async with self._begin(tenant_id=tenant_id) as conn:
            current = (
                (
                    await conn.execute(
                        select(cursors)
                        .where(
                            and_(
                                cursors.c.connection_id == connection_id,
                                cursors.c.tenant_id == tenant_id,
                                cursors.c.folder_ref == folder_ref,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            current_value = cast(str | None, current["cursor_value"]) if current else None
            if current_value != expected_cursor:
                raise RepositoryConflictError("cursor_conflict")
            if current is None:
                await conn.execute(
                    insert(cursors).values(
                        connection_id=connection_id,
                        tenant_id=tenant_id,
                        folder_ref=folder_ref,
                        cursor_kind="provider",
                        cursor_value=next_cursor,
                        subscription_status=SubscriptionStatus.NONE.value,
                        fencing_token=0,
                        status="idle",
                        updated_at=utc_now(),
                    )
                )
            else:
                await conn.execute(
                    update(cursors)
                    .where(
                        and_(
                            cursors.c.connection_id == connection_id,
                            cursors.c.tenant_id == tenant_id,
                            cursors.c.folder_ref == folder_ref,
                        )
                    )
                    .values(cursor_value=next_cursor, status="idle", updated_at=utc_now())
                )

    async def get_sync_state(
        self, *, tenant_id: str, connection_id: UUID, folder_ref: str
    ) -> MailboxSyncState | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(cursors).where(
                            and_(
                                cursors.c.connection_id == connection_id,
                                cursors.c.tenant_id == tenant_id,
                                cursors.c.folder_ref == folder_ref,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _sync_state_from_row(row) if row else None

    async def list_sync_states(
        self, *, tenant_id: str, connection_id: UUID
    ) -> tuple[MailboxSyncState, ...]:
        async with self._connect(tenant_id=tenant_id) as conn:
            result = await conn.execute(
                select(cursors)
                .where(
                    and_(
                        cursors.c.connection_id == connection_id,
                        cursors.c.tenant_id == tenant_id,
                    )
                )
                .order_by(cursors.c.folder_ref.asc())
            )
            return tuple(_sync_state_from_row(row) for row in result.mappings())

    async def save_sync_state(
        self,
        state: MailboxSyncState,
        *,
        expected_subscription_ref: str | None = None,
    ) -> MailboxSyncState:
        values = _sync_state_values(state)
        async with self._begin(tenant_id=state.tenant_id) as conn:
            current = (
                (
                    await conn.execute(
                        select(cursors)
                        .where(
                            and_(
                                cursors.c.connection_id == state.connection_id,
                                cursors.c.tenant_id == state.tenant_id,
                                cursors.c.folder_ref == state.folder_ref,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if current is not None and current.get("subscription_ref") != expected_subscription_ref:
                raise RepositoryConflictError("subscription_state_conflict")
            if current is None:
                await conn.execute(insert(cursors).values(**values))
            else:
                await conn.execute(
                    update(cursors)
                    .where(
                        and_(
                            cursors.c.connection_id == state.connection_id,
                            cursors.c.tenant_id == state.tenant_id,
                            cursors.c.folder_ref == state.folder_ref,
                        )
                    )
                    .values(
                        cursor_kind=values["cursor_kind"],
                        cursor_value=values["cursor_value"],
                        subscription_ref=values["subscription_ref"],
                        subscription_status=values["subscription_status"],
                        subscription_expires_at=values["subscription_expires_at"],
                        subscription_callback_endpoint=values["subscription_callback_endpoint"],
                        subscription_client_state_ref=values["subscription_client_state_ref"],
                        subscription_provider_request_id=values["subscription_provider_request_id"],
                        lease_owner=values["lease_owner"],
                        fencing_token=values["fencing_token"],
                        status=values["status"],
                        watermark=values["watermark"],
                        updated_at=values["updated_at"],
                    )
                )
        return state

    async def save_policy(self, policy: MailAgentPolicy) -> MailAgentPolicy:
        values = _policy_values(policy)
        async with self._begin(
            tenant_id=policy.tenant_id, subject_id=policy.owner_subject_id
        ) as conn:
            row = (
                await conn.execute(
                    select(policies.c.revision).where(policies.c.policy_id == policy.policy_id)
                )
            ).first()
            if row and int(row[0]) >= policy.revision:
                raise RepositoryConflictError("policy_revision_conflict")
            if row:
                await conn.execute(
                    update(policies)
                    .where(policies.c.policy_id == policy.policy_id)
                    .values(**values)
                )
            else:
                await conn.execute(insert(policies).values(**values))
        return policy

    async def get_policy(self, *, tenant_id: str, policy_id: UUID) -> MailAgentPolicy | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(policies).where(
                            and_(
                                policies.c.policy_id == policy_id, policies.c.tenant_id == tenant_id
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _policy_from_row(row) if row else None

    async def list_policies(
        self, *, tenant_id: str, owner_subject_id: str, limit: int = 50
    ) -> tuple[MailAgentPolicy, ...]:
        _check_limit(limit)
        async with self._connect(tenant_id=tenant_id, subject_id=owner_subject_id) as conn:
            result = await conn.execute(
                select(policies)
                .where(
                    and_(
                        policies.c.tenant_id == tenant_id,
                        policies.c.owner_subject_id == owner_subject_id,
                    )
                )
                .order_by(policies.c.valid_from.desc())
                .limit(limit)
            )
            return tuple(_policy_from_row(row) for row in result.mappings())

    async def save_grant(self, grant: DelegationGrant) -> DelegationGrant:
        values = _grant_values(grant)
        async with self._begin(
            tenant_id=grant.tenant_id, subject_id=grant.granted_by_subject_id
        ) as conn:
            row = (
                await conn.execute(
                    select(grants.c.revision).where(grants.c.grant_id == grant.grant_id)
                )
            ).first()
            if row and int(row[0]) >= grant.revision:
                raise RepositoryConflictError("grant_revision_conflict")
            if row:
                await conn.execute(
                    update(grants).where(grants.c.grant_id == grant.grant_id).values(**values)
                )
            else:
                await conn.execute(insert(grants).values(**values))
        return grant

    async def get_grant(self, *, tenant_id: str, grant_id: UUID) -> DelegationGrant | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(grants).where(
                            and_(grants.c.grant_id == grant_id, grants.c.tenant_id == tenant_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _grant_from_row(row) if row else None

    async def list_grants(
        self, *, tenant_id: str, granted_by_subject_id: str, limit: int = 50
    ) -> tuple[DelegationGrant, ...]:
        _check_limit(limit)
        async with self._connect(tenant_id=tenant_id, subject_id=granted_by_subject_id) as conn:
            result = await conn.execute(
                select(grants)
                .where(
                    and_(
                        grants.c.tenant_id == tenant_id,
                        grants.c.granted_by_subject_id == granted_by_subject_id,
                    )
                )
                .order_by(grants.c.granted_at.desc())
                .limit(limit)
            )
            return tuple(_grant_from_row(row) for row in result.mappings())

    async def save_draft(self, draft: MailDraft) -> MailDraft:
        if draft.body_object_ref is None:
            raise RepositoryConflictError("draft_object_ref_required")
        values = _draft_values(draft)
        async with self._begin(tenant_id=draft.tenant_id, subject_id=draft.subject_id) as conn:
            row = (
                await conn.execute(
                    select(drafts.c.revision).where(drafts.c.draft_id == draft.draft_id)
                )
            ).first()
            if row and int(row[0]) >= draft.revision:
                raise RepositoryConflictError("draft_revision_conflict")
            if row:
                await conn.execute(
                    update(drafts).where(drafts.c.draft_id == draft.draft_id).values(**values)
                )
            else:
                await conn.execute(insert(drafts).values(**values))
        return draft

    async def get_draft(self, *, tenant_id: str, draft_id: UUID) -> MailDraft | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(drafts).where(
                            and_(drafts.c.draft_id == draft_id, drafts.c.tenant_id == tenant_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _draft_from_row(row) if row else None

    async def list_drafts(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 5000,
    ) -> tuple[MailDraft, ...]:
        _check_lifecycle_limit(limit)
        conditions = [
            drafts.c.tenant_id == tenant_id,
            drafts.c.subject_id == subject_id,
        ]
        if connection_id is not None:
            conditions.append(drafts.c.connection_id == connection_id)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(drafts)
                .where(and_(*conditions))
                .order_by(drafts.c.updated_at.desc())
                .limit(limit)
            )
            return tuple(_draft_from_row(row) for row in result.mappings())

    async def save_candidate(self, candidate: MailActionCandidate) -> MailActionCandidate:
        values = _candidate_values(candidate)
        async with self._begin(
            tenant_id=candidate.tenant_id, subject_id=candidate.subject_id
        ) as conn:
            current_row = (
                await conn.execute(
                    select(candidates).where(candidates.c.candidate_id == candidate.candidate_id)
                )
            ).first()
            row = current_row._mapping if current_row is not None else None
            if row is not None and _candidate_from_row(row) == candidate:
                return candidate
            if row is not None and int(row["revision"]) >= candidate.revision:
                raise RepositoryConflictError("candidate_revision_conflict")
            if row is not None:
                await conn.execute(
                    update(candidates)
                    .where(candidates.c.candidate_id == candidate.candidate_id)
                    .values(**values)
                )
            else:
                await conn.execute(insert(candidates).values(**values))
        return candidate

    async def list_candidates(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        status: CandidateState | None = None,
        candidate_type: CandidateType | None = None,
        limit: int = 50,
    ) -> tuple[MailActionCandidate, ...]:
        _check_limit(limit)
        query = (
            select(candidates)
            .where(
                and_(
                    candidates.c.tenant_id == tenant_id,
                    candidates.c.subject_id == subject_id,
                )
            )
            .order_by(candidates.c.created_at.desc())
            .limit(limit)
        )
        if status is not None:
            query = query.where(candidates.c.status == status.value)
        if candidate_type is not None:
            query = query.where(candidates.c.candidate_type == candidate_type.value)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(query)
            return tuple(_candidate_from_row(row) for row in result.mappings())

    async def get_candidate(
        self, *, tenant_id: str, candidate_id: UUID
    ) -> MailActionCandidate | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(candidates).where(
                            and_(
                                candidates.c.candidate_id == candidate_id,
                                candidates.c.tenant_id == tenant_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _candidate_from_row(row) if row else None

    async def append_audit(self, event: Mapping[str, object]) -> None:
        safe_event = redact_event(event)
        async with self._begin(
            tenant_id=str(safe_event["tenant_id"]), subject_id=str(safe_event["subject_id"])
        ) as conn:
            await conn.execute(
                insert(audits).values(
                    tenant_id=str(safe_event["tenant_id"]),
                    subject_id=str(safe_event["subject_id"]),
                    event_type=str(safe_event["event_type"]),
                    target_ref=str(safe_event["target_ref"]),
                    occurred_at=_parse_datetime(safe_event.get("occurred_at")),
                    metadata=safe_event,
                )
            )

    async def list_audit_events(
        self, *, tenant_id: str, subject_id: str, limit: int = 100
    ) -> tuple[Mapping[str, object], ...]:
        _check_limit(min(limit, 200))
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(
                    audits.c.event_type,
                    audits.c.target_ref,
                    audits.c.occurred_at,
                    audits.c.metadata,
                )
                .where(
                    and_(
                        audits.c.tenant_id == tenant_id,
                        audits.c.subject_id == subject_id,
                    )
                )
                .order_by(audits.c.occurred_at.desc())
                .limit(limit)
            )
            events: list[Mapping[str, object]] = []
            for row in result.mappings():
                raw_metadata = row.get("metadata")
                event = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
                # Keep the SQL projection shape identical to the in-memory
                # repository.  The JSON column is the allowlisted event
                # payload, while these indexed columns are authoritative for
                # ordering and routing.
                event["event_type"] = str(row["event_type"])
                event["target_ref"] = str(row["target_ref"])
                occurred_at = row["occurred_at"]
                if isinstance(occurred_at, datetime):
                    event["occurred_at"] = occurred_at.astimezone(UTC).isoformat()
                else:
                    event["occurred_at"] = str(occurred_at)
                event.setdefault("tenant_id", tenant_id)
                event.setdefault("subject_id", subject_id)
                events.append(event)
            return tuple(events)

    async def create_or_get_operation(
        self, operation: MailOutboxOperation
    ) -> tuple[MailOutboxOperation, bool]:
        values = _operation_values(operation)
        async with self._begin(
            tenant_id=operation.tenant_id, subject_id=operation.subject_id
        ) as conn:
            statement = (
                pg_insert(operations)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[operations.c.connection_id, operations.c.idempotency_key]
                )
            )
            result = await conn.execute(statement)
            row = (
                (
                    await conn.execute(
                        select(operations).where(
                            and_(
                                operations.c.connection_id == operation.connection_id,
                                operations.c.idempotency_key == operation.idempotency_key,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("operation_not_saved")
            return _operation_from_row(row), result.rowcount == 1

    async def get_operation(
        self, *, tenant_id: str, operation_id: UUID
    ) -> MailOutboxOperation | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(operations).where(
                            and_(
                                operations.c.operation_id == operation_id,
                                operations.c.tenant_id == tenant_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _operation_from_row(row) if row else None

    async def list_operations(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 5000,
    ) -> tuple[MailOutboxOperation, ...]:
        _check_lifecycle_limit(limit)
        conditions = [
            operations.c.tenant_id == tenant_id,
            operations.c.subject_id == subject_id,
        ]
        if connection_id is not None:
            conditions.append(operations.c.connection_id == connection_id)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(operations)
                .where(and_(*conditions))
                .order_by(operations.c.updated_at.desc())
                .limit(limit)
            )
            return tuple(_operation_from_row(row) for row in result.mappings())

    async def count_operations_since(
        self, *, tenant_id: str, subject_id: str, since: datetime
    ) -> int:
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            value = (
                await conn.execute(
                    select(func.count())
                    .select_from(operations)
                    .where(
                        and_(
                            operations.c.tenant_id == tenant_id,
                            operations.c.subject_id == subject_id,
                            operations.c.created_at >= since,
                        )
                    )
                )
            ).scalar_one()
            return int(value)

    async def lease_operation(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> MailOutboxOperation:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("operation_lease_seconds_invalid")
        current_time = (now or utc_now()).astimezone(UTC)
        recovery_required = False
        leased_result: MailOutboxOperation | None = None
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(operations)
                        .where(
                            and_(
                                operations.c.operation_id == operation_id,
                                operations.c.tenant_id == tenant_id,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("operation_not_found")
            operation = _operation_from_row(row)
            if operation.status in {DeliveryStatus.LEASED, DeliveryStatus.SENDING} and (
                operation.lease_expires_at is None or operation.lease_expires_at <= current_time
            ):
                transition_delivery(operation.status, DeliveryStatus.OUTCOME_UNKNOWN)
                await conn.execute(
                    update(operations)
                    .where(operations.c.operation_id == operation_id)
                    .values(
                        status=DeliveryStatus.OUTCOME_UNKNOWN.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        error_code="lease_expired_outcome_unknown",
                        updated_at=current_time,
                    )
                )
                await conn.execute(
                    insert(audits).values(
                        tenant_id=tenant_id,
                        subject_id=operation.subject_id,
                        event_type="mail.outbox.lease_expired",
                        target_ref=str(operation_id),
                        occurred_at=current_time,
                        metadata={
                            "previous_status": operation.status.value,
                            "reason": "lease_expired_outcome_unknown",
                        },
                    )
                )
                recovery_required = True
            else:
                if operation.status not in {DeliveryStatus.QUEUED, DeliveryStatus.RETRY_WAIT}:
                    raise RepositoryConflictError("operation_not_leaseable")
                if operation.next_attempt_at and operation.next_attempt_at > current_time:
                    raise RepositoryConflictError("operation_not_due")
                lease_expires_at = current_time + timedelta(seconds=lease_seconds)
                await conn.execute(
                    update(operations)
                    .where(operations.c.operation_id == operation_id)
                    .values(
                        status=DeliveryStatus.LEASED.value,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        fencing_token=operation.fencing_token + 1,
                        updated_at=current_time,
                    )
                )
                leased_result = _replace_operation(
                    operation,
                    status=DeliveryStatus.LEASED,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    fencing_token=operation.fencing_token + 1,
                    updated_at=current_time,
                )
        if recovery_required:
            raise RepositoryConflictError("operation_outcome_unknown_reconciliation_required")
        if leased_result is None:  # pragma: no cover - defensive invariant
            raise RepositoryConflictError("operation_not_leased")
        return leased_result

    async def mark_sending(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
        fencing_token: int,
    ) -> MailOutboxOperation:
        return await self._transition_operation(
            tenant_id=tenant_id,
            operation_id=operation_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            status=DeliveryStatus.SENDING,
        )

    async def complete_operation(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
        fencing_token: int,
        status: DeliveryStatus,
        provider_message_ref: str | None = None,
        provider_request_id: str | None = None,
        error_code: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> MailOutboxOperation:
        return await self._transition_operation(
            tenant_id=tenant_id,
            operation_id=operation_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            status=status,
            provider_message_ref=provider_message_ref,
            provider_request_id=provider_request_id,
            error_code=error_code,
            next_attempt_at=next_attempt_at,
            increment_attempt=True,
            clear_lease=True,
        )

    async def reconcile_outcome_unknown(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        status: DeliveryStatus,
        provider_message_ref: str | None = None,
        provider_request_id: str | None = None,
        error_code: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> MailOutboxOperation:
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(operations)
                        .where(
                            and_(
                                operations.c.operation_id == operation_id,
                                operations.c.tenant_id == tenant_id,
                                operations.c.status == DeliveryStatus.OUTCOME_UNKNOWN.value,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("operation_not_reconcilable")
            operation = _operation_from_row(row)
            transition_delivery(operation.status, status)
            values: dict[str, object] = {
                "status": status.value,
                "error_code": error_code,
                "next_attempt_at": next_attempt_at,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": utc_now(),
            }
            if provider_message_ref is not None:
                values["provider_message_ref"] = provider_message_ref
            if provider_request_id is not None:
                values["provider_request_id"] = provider_request_id
            await conn.execute(
                update(operations).where(operations.c.operation_id == operation_id).values(**values)
            )
            return _replace_operation(
                operation,
                status=status,
                provider_message_ref=provider_message_ref or operation.provider_message_ref,
                provider_request_id=provider_request_id or operation.provider_request_id,
                error_code=error_code,
                next_attempt_at=next_attempt_at,
                updated_at=cast(datetime, values["updated_at"]),
            )

    async def create_or_get_sync_job(self, job: MailSyncJob) -> tuple[MailSyncJob, bool]:
        values = _sync_job_values(job)
        async with self._begin(tenant_id=job.tenant_id, subject_id=job.subject_id) as conn:
            statement = (
                pg_insert(sync_jobs)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[sync_jobs.c.connection_id, sync_jobs.c.idempotency_key]
                )
            )
            result = await conn.execute(statement)
            row = (
                (
                    await conn.execute(
                        select(sync_jobs).where(
                            and_(
                                sync_jobs.c.connection_id == job.connection_id,
                                sync_jobs.c.idempotency_key == job.idempotency_key,
                                sync_jobs.c.tenant_id == job.tenant_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("sync_job_not_saved")
            return _sync_job_from_row(row), result.rowcount == 1

    async def get_sync_job(self, *, tenant_id: str, job_id: UUID) -> MailSyncJob | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(sync_jobs).where(
                            and_(
                                sync_jobs.c.job_id == job_id,
                                sync_jobs.c.tenant_id == tenant_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _sync_job_from_row(row) if row else None

    async def list_sync_jobs(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MailSyncJob, ...]:
        _check_limit(limit)
        predicate = and_(
            sync_jobs.c.tenant_id == tenant_id,
            sync_jobs.c.subject_id == subject_id,
        )
        if connection_id is not None:
            predicate = and_(predicate, sync_jobs.c.connection_id == connection_id)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(sync_jobs)
                .where(predicate)
                .order_by(sync_jobs.c.created_at.desc())
                .limit(limit)
            )
            return tuple(_sync_job_from_row(row) for row in result.mappings())

    async def claim_sync_job(
        self,
        *,
        tenant_id: str,
        job_id: UUID,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> MailSyncJob:
        _validate_worker_lease_seconds(lease_seconds)
        current_time = (now or utc_now()).astimezone(UTC)
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(sync_jobs)
                        .where(
                            and_(
                                sync_jobs.c.job_id == job_id,
                                sync_jobs.c.tenant_id == tenant_id,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("sync_job_not_found")
            job = _sync_job_from_row(row)
            if job.status is SyncJobStatus.RUNNING and (
                job.lease_expires_at is None or job.lease_expires_at <= current_time
            ):
                transition_sync_job(job.status, SyncJobStatus.QUEUED)
                await conn.execute(
                    update(sync_jobs)
                    .where(sync_jobs.c.job_id == job_id)
                    .values(
                        status=SyncJobStatus.QUEUED.value,
                        error_code="lease_expired_requeued",
                        lease_owner=None,
                        lease_expires_at=None,
                        finished_at=None,
                        updated_at=current_time,
                    )
                )
                await conn.execute(
                    insert(audits).values(
                        tenant_id=tenant_id,
                        subject_id=job.subject_id,
                        event_type="mail.sync_job.lease_expired",
                        target_ref=str(job_id),
                        occurred_at=current_time,
                        metadata={
                            "previous_status": SyncJobStatus.RUNNING.value,
                            "reason": "lease_expired_requeued",
                        },
                    )
                )
                job = replace(
                    job,
                    status=SyncJobStatus.QUEUED,
                    error_code="lease_expired_requeued",
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=None,
                    updated_at=current_time,
                )
            if job.status is SyncJobStatus.CANCELLING and (
                job.lease_expires_at is None or job.lease_expires_at <= current_time
            ):
                transition_sync_job(job.status, SyncJobStatus.CANCELLED)
                await conn.execute(
                    update(sync_jobs)
                    .where(sync_jobs.c.job_id == job_id)
                    .values(
                        status=SyncJobStatus.CANCELLED.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        finished_at=current_time,
                        updated_at=current_time,
                    )
                )
                await conn.execute(
                    insert(audits).values(
                        tenant_id=tenant_id,
                        subject_id=job.subject_id,
                        event_type="mail.sync_job.cancelled_after_lease_expiry",
                        target_ref=str(job_id),
                        occurred_at=current_time,
                        metadata={
                            "previous_status": SyncJobStatus.CANCELLING.value,
                            "reason": "cancel_request_finalized_after_worker_lease_expiry",
                        },
                    )
                )
                job = replace(
                    job,
                    status=SyncJobStatus.CANCELLED,
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=current_time,
                    updated_at=current_time,
                )
            if job.status is SyncJobStatus.RETRY_WAIT:
                if job.next_attempt_at is not None and job.next_attempt_at > current_time:
                    raise RepositoryConflictError("sync_job_not_due")
                transition_sync_job(job.status, SyncJobStatus.QUEUED)
                await conn.execute(
                    update(sync_jobs)
                    .where(sync_jobs.c.job_id == job_id)
                    .values(
                        status=SyncJobStatus.QUEUED.value,
                        next_attempt_at=None,
                        finished_at=None,
                        updated_at=current_time,
                    )
                )
                await conn.execute(
                    insert(audits).values(
                        tenant_id=tenant_id,
                        subject_id=job.subject_id,
                        event_type="mail.sync_job.retry_due",
                        target_ref=str(job_id),
                        occurred_at=current_time,
                        metadata={"attempt_count": job.attempt_count},
                    )
                )
                job = replace(
                    job,
                    status=SyncJobStatus.QUEUED,
                    next_attempt_at=None,
                    finished_at=None,
                    updated_at=current_time,
                )
            if job.status is not SyncJobStatus.QUEUED:
                raise RepositoryConflictError("sync_job_not_claimable")
            transition_sync_job(job.status, SyncJobStatus.RUNNING)
            lease_expires_at = current_time + timedelta(seconds=lease_seconds)
            await conn.execute(
                update(sync_jobs)
                .where(sync_jobs.c.job_id == job_id)
                .values(
                    status=SyncJobStatus.RUNNING.value,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    fencing_token=job.fencing_token + 1,
                    error_code=None,
                    next_attempt_at=None,
                    started_at=current_time,
                    updated_at=current_time,
                )
            )
            return replace(
                job,
                status=SyncJobStatus.RUNNING,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                fencing_token=job.fencing_token + 1,
                error_code=None,
                next_attempt_at=None,
                started_at=current_time,
                updated_at=current_time,
            )

    async def cancel_sync_job(self, *, tenant_id: str, job_id: UUID) -> MailSyncJob:
        now = utc_now()
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(sync_jobs)
                        .where(
                            and_(
                                sync_jobs.c.job_id == job_id,
                                sync_jobs.c.tenant_id == tenant_id,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("sync_job_not_found")
            job = _sync_job_from_row(row)
            if job.status is SyncJobStatus.CANCELLING:
                return job
            if job.status is SyncJobStatus.RETRY_WAIT:
                transition_sync_job(job.status, SyncJobStatus.CANCELLED)
                await conn.execute(
                    update(sync_jobs)
                    .where(sync_jobs.c.job_id == job_id)
                    .values(
                        status=SyncJobStatus.CANCELLED.value,
                        next_attempt_at=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                return replace(
                    job,
                    status=SyncJobStatus.CANCELLED,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=now,
                    updated_at=now,
                )
            target_status = (
                SyncJobStatus.CANCELLING
                if job.status is SyncJobStatus.RUNNING
                else SyncJobStatus.CANCELLED
            )
            transition_sync_job(job.status, target_status)
            await conn.execute(
                update(sync_jobs)
                .where(sync_jobs.c.job_id == job_id)
                .values(
                    status=target_status.value,
                    error_code=(
                        "sync_cancel_requested"
                        if job.status is SyncJobStatus.RUNNING
                        else job.error_code
                    ),
                    lease_owner=(job.lease_owner if job.status is SyncJobStatus.RUNNING else None),
                    lease_expires_at=(
                        job.lease_expires_at if job.status is SyncJobStatus.RUNNING else None
                    ),
                    finished_at=(None if job.status is SyncJobStatus.RUNNING else now),
                    updated_at=now,
                )
            )
            return replace(
                job,
                status=target_status,
                error_code=(
                    "sync_cancel_requested"
                    if job.status is SyncJobStatus.RUNNING
                    else job.error_code
                ),
                lease_owner=(job.lease_owner if job.status is SyncJobStatus.RUNNING else None),
                lease_expires_at=(
                    job.lease_expires_at if job.status is SyncJobStatus.RUNNING else None
                ),
                finished_at=(None if job.status is SyncJobStatus.RUNNING else now),
                updated_at=now,
            )

    async def complete_sync_job(
        self,
        *,
        tenant_id: str,
        job_id: UUID,
        worker_id: str,
        fencing_token: int,
        status: SyncJobStatus,
        fetched_count: int = 0,
        saved_count: int = 0,
        duplicate_count: int = 0,
        deleted_count: int = 0,
        cursor_before: str | None = None,
        cursor_after: str | None = None,
        provider_request_id: str | None = None,
        error_code: str | None = None,
        next_attempt_at: datetime | None = None,
        increment_attempt: bool = False,
    ) -> MailSyncJob:
        now = utc_now()
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(sync_jobs)
                        .where(
                            and_(
                                sync_jobs.c.job_id == job_id,
                                sync_jobs.c.tenant_id == tenant_id,
                                sync_jobs.c.lease_owner == worker_id,
                                sync_jobs.c.fencing_token == fencing_token,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("sync_job_lease_fenced")
            job = _sync_job_from_row(row)
            transition_sync_job(job.status, status)
            retrying = status is SyncJobStatus.RETRY_WAIT
            next_attempt_value = next_attempt_at if retrying else None
            attempt_count = job.attempt_count + int(increment_attempt)
            await conn.execute(
                update(sync_jobs)
                .where(sync_jobs.c.job_id == job_id)
                .values(
                    status=status.value,
                    fetched_count=fetched_count,
                    saved_count=saved_count,
                    duplicate_count=duplicate_count,
                    deleted_count=deleted_count,
                    cursor_before=cursor_before,
                    cursor_after=cursor_after,
                    provider_request_id=provider_request_id,
                    error_code=error_code,
                    attempt_count=attempt_count,
                    next_attempt_at=next_attempt_value,
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=None if retrying else now,
                    updated_at=now,
                )
            )
            return replace(
                job,
                status=status,
                fetched_count=fetched_count,
                saved_count=saved_count,
                duplicate_count=duplicate_count,
                deleted_count=deleted_count,
                cursor_before=cursor_before,
                cursor_after=cursor_after,
                provider_request_id=provider_request_id,
                error_code=error_code,
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_value,
                lease_owner=None,
                lease_expires_at=None,
                finished_at=None if retrying else now,
                updated_at=now,
            )

    async def create_or_get_autonomy_run(
        self, run: MailAutonomyRun
    ) -> tuple[MailAutonomyRun, bool]:
        values = _autonomy_run_values(run)
        async with self._begin(tenant_id=run.tenant_id, subject_id=run.subject_id) as conn:
            statement = (
                pg_insert(autonomy_runs)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        autonomy_runs.c.tenant_id,
                        autonomy_runs.c.subject_id,
                        autonomy_runs.c.connection_id,
                        autonomy_runs.c.replay_key,
                    ]
                )
            )
            result = await conn.execute(statement)
            row = (
                (
                    await conn.execute(
                        select(autonomy_runs).where(
                            and_(
                                autonomy_runs.c.tenant_id == run.tenant_id,
                                autonomy_runs.c.subject_id == run.subject_id,
                                autonomy_runs.c.connection_id == run.connection_id,
                                autonomy_runs.c.replay_key == run.replay_key,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("autonomy_run_not_saved")
            return _autonomy_run_from_row(row), result.rowcount == 1

    async def get_autonomy_run(self, *, tenant_id: str, run_id: UUID) -> MailAutonomyRun | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(autonomy_runs).where(
                            and_(
                                autonomy_runs.c.run_id == run_id,
                                autonomy_runs.c.tenant_id == tenant_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _autonomy_run_from_row(row) if row else None

    async def list_autonomy_runs(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MailAutonomyRun, ...]:
        _check_limit(limit)
        predicate = and_(
            autonomy_runs.c.tenant_id == tenant_id,
            autonomy_runs.c.subject_id == subject_id,
        )
        if connection_id is not None:
            predicate = and_(predicate, autonomy_runs.c.connection_id == connection_id)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(autonomy_runs)
                .where(predicate)
                .order_by(autonomy_runs.c.created_at.desc())
                .limit(limit)
            )
            return tuple(_autonomy_run_from_row(row) for row in result.mappings())

    async def claim_autonomy_run(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> MailAutonomyRun:
        _validate_worker_lease_seconds(lease_seconds)
        current_time = (now or utc_now()).astimezone(UTC)
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(autonomy_runs)
                        .where(
                            and_(
                                autonomy_runs.c.run_id == run_id,
                                autonomy_runs.c.tenant_id == tenant_id,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("autonomy_run_not_found")
            run = _autonomy_run_from_row(row)
            if run.status is AutonomyRunStatus.RUNNING and (
                run.lease_expires_at is None or run.lease_expires_at <= current_time
            ):
                transition_autonomy_run(run.status, AutonomyRunStatus.QUEUED)
                await conn.execute(
                    update(autonomy_runs)
                    .where(autonomy_runs.c.run_id == run_id)
                    .values(
                        status=AutonomyRunStatus.QUEUED.value,
                        error_code="lease_expired_requeued",
                        lease_owner=None,
                        lease_expires_at=None,
                        finished_at=None,
                        revision=run.revision + 1,
                        updated_at=current_time,
                    )
                )
                await conn.execute(
                    insert(audits).values(
                        tenant_id=tenant_id,
                        subject_id=run.subject_id,
                        event_type="mail.agent.autonomy.lease_expired",
                        target_ref=str(run_id),
                        occurred_at=current_time,
                        metadata={
                            "previous_status": AutonomyRunStatus.RUNNING.value,
                            "reason": "lease_expired_requeued",
                        },
                    )
                )
                run = replace(
                    run,
                    status=AutonomyRunStatus.QUEUED,
                    error_code="lease_expired_requeued",
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=None,
                    revision=run.revision + 1,
                    updated_at=current_time,
                )
            if run.status is not AutonomyRunStatus.QUEUED:
                raise RepositoryConflictError("autonomy_run_not_claimable")
            transition_autonomy_run(run.status, AutonomyRunStatus.RUNNING)
            lease_expires_at = current_time + timedelta(seconds=lease_seconds)
            await conn.execute(
                update(autonomy_runs)
                .where(autonomy_runs.c.run_id == run_id)
                .values(
                    status=AutonomyRunStatus.RUNNING.value,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    fencing_token=run.fencing_token + 1,
                    error_code=None,
                    started_at=current_time,
                    pause_reason=None,
                    revision=run.revision + 1,
                    updated_at=current_time,
                )
            )
            return replace(
                run,
                status=AutonomyRunStatus.RUNNING,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                fencing_token=run.fencing_token + 1,
                error_code=None,
                started_at=current_time,
                pause_reason=None,
                revision=run.revision + 1,
                updated_at=current_time,
            )

    async def pause_autonomy_run(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        reason: str,
        worker_id: str | None = None,
        fencing_token: int | None = None,
    ) -> MailAutonomyRun:
        if not reason.strip():
            raise ValueError("autonomy_pause_reason_required")
        if (worker_id is None) != (fencing_token is None):
            raise ValueError("autonomy_pause_fence_incomplete")
        now = utc_now()
        async with self._begin(tenant_id=tenant_id) as conn:
            predicates = [
                autonomy_runs.c.run_id == run_id,
                autonomy_runs.c.tenant_id == tenant_id,
            ]
            if worker_id is not None and fencing_token is not None:
                predicates.extend(
                    [
                        autonomy_runs.c.status == AutonomyRunStatus.RUNNING.value,
                        autonomy_runs.c.lease_owner == worker_id,
                        autonomy_runs.c.fencing_token == fencing_token,
                    ]
                )
            row = (
                (
                    await conn.execute(
                        select(autonomy_runs).where(and_(*predicates)).with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("autonomy_run_not_found")
            run = _autonomy_run_from_row(row)
            transition_autonomy_run(run.status, AutonomyRunStatus.PAUSED)
            await conn.execute(
                update(autonomy_runs)
                .where(autonomy_runs.c.run_id == run_id)
                .values(
                    status=AutonomyRunStatus.PAUSED.value,
                    pause_reason=reason.strip(),
                    lease_owner=None,
                    lease_expires_at=None,
                    revision=run.revision + 1,
                    updated_at=now,
                )
            )
            return replace(
                run,
                status=AutonomyRunStatus.PAUSED,
                pause_reason=reason.strip(),
                lease_owner=None,
                lease_expires_at=None,
                revision=run.revision + 1,
                updated_at=now,
            )

    async def resume_autonomy_run(self, *, tenant_id: str, run_id: UUID) -> MailAutonomyRun:
        now = utc_now()
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(autonomy_runs)
                        .where(
                            and_(
                                autonomy_runs.c.run_id == run_id,
                                autonomy_runs.c.tenant_id == tenant_id,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("autonomy_run_not_found")
            run = _autonomy_run_from_row(row)
            transition_autonomy_run(run.status, AutonomyRunStatus.QUEUED)
            await conn.execute(
                update(autonomy_runs)
                .where(autonomy_runs.c.run_id == run_id)
                .values(
                    status=AutonomyRunStatus.QUEUED.value,
                    pause_reason=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    revision=run.revision + 1,
                    updated_at=now,
                )
            )
            return replace(
                run,
                status=AutonomyRunStatus.QUEUED,
                pause_reason=None,
                lease_owner=None,
                lease_expires_at=None,
                revision=run.revision + 1,
                updated_at=now,
            )

    async def cancel_autonomy_run(self, *, tenant_id: str, run_id: UUID) -> MailAutonomyRun:
        now = utc_now()
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(autonomy_runs)
                        .where(
                            and_(
                                autonomy_runs.c.run_id == run_id,
                                autonomy_runs.c.tenant_id == tenant_id,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("autonomy_run_not_found")
            run = _autonomy_run_from_row(row)
            transition_autonomy_run(run.status, AutonomyRunStatus.CANCELLED)
            await conn.execute(
                update(autonomy_runs)
                .where(autonomy_runs.c.run_id == run_id)
                .values(
                    status=AutonomyRunStatus.CANCELLED.value,
                    finished_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    revision=run.revision + 1,
                    updated_at=now,
                )
            )
            return replace(
                run,
                status=AutonomyRunStatus.CANCELLED,
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
                revision=run.revision + 1,
                updated_at=now,
            )

    async def complete_autonomy_run(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        worker_id: str,
        fencing_token: int,
        status: AutonomyRunStatus,
        message_ids: tuple[UUID, ...] = (),
        analyzed_message_ids: tuple[UUID, ...] = (),
        candidate_ids: tuple[UUID, ...] = (),
        sync_result: Mapping[str, object] | None = None,
        sync_job_id: UUID | None = None,
        error_code: str | None = None,
    ) -> MailAutonomyRun:
        if status not in {AutonomyRunStatus.COMPLETED, AutonomyRunStatus.FAILED}:
            raise ValueError("autonomy_completion_status_invalid")
        now = utc_now()
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(autonomy_runs)
                        .where(
                            and_(
                                autonomy_runs.c.run_id == run_id,
                                autonomy_runs.c.tenant_id == tenant_id,
                                autonomy_runs.c.lease_owner == worker_id,
                                autonomy_runs.c.fencing_token == fencing_token,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("autonomy_run_lease_fenced")
            run = _autonomy_run_from_row(row)
            transition_autonomy_run(run.status, status)
            await conn.execute(
                update(autonomy_runs)
                .where(autonomy_runs.c.run_id == run_id)
                .values(
                    status=status.value,
                    message_ids=list(message_ids),
                    analyzed_message_ids=list(analyzed_message_ids),
                    candidate_ids=list(candidate_ids),
                    sync_result=dict(sync_result or {}),
                    sync_job_id=sync_job_id,
                    error_code=error_code,
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=now,
                    revision=run.revision + 1,
                    updated_at=now,
                )
            )
            return replace(
                run,
                status=status,
                message_ids=message_ids,
                analyzed_message_ids=analyzed_message_ids,
                candidate_ids=candidate_ids,
                sync_result=dict(sync_result or {}),
                sync_job_id=sync_job_id,
                error_code=error_code,
                lease_owner=None,
                lease_expires_at=None,
                finished_at=now,
                revision=run.revision + 1,
                updated_at=now,
            )

    async def save_webhook_receipt(self, receipt: Mapping[str, object]) -> bool:
        required = (
            "provider",
            "tenant_id",
            "event_id",
            "body_sha256",
            "received_at",
            "verified",
            "duplicate",
            "status",
            "trace_id",
        )
        if any(
            not str(receipt.get(key, "")).strip()
            for key in required
            if key not in {"verified", "duplicate"}
        ):
            raise ValueError("webhook_receipt_identity_missing")
        values = {key: receipt[key] for key in required}
        route_metadata = receipt.get("route_metadata")
        if route_metadata is not None and not isinstance(route_metadata, Mapping):
            raise ValueError("webhook_receipt_route_metadata_invalid")
        values["route_metadata"] = normalize_receipt_route_metadata(route_metadata)
        async with self._begin(tenant_id=str(values["tenant_id"])) as conn:
            result = await conn.execute(
                pg_insert(webhook_receipts)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        webhook_receipts.c.provider,
                        webhook_receipts.c.tenant_id,
                        webhook_receipts.c.event_id,
                    ]
                )
            )
            return cast(int, result.rowcount) == 1

    async def list_webhook_receipts(
        self, *, tenant_id: str, limit: int = 100
    ) -> tuple[Mapping[str, object], ...]:
        _check_limit(min(limit, 200))
        async with self._connect(tenant_id=tenant_id) as conn:
            result = await conn.execute(
                select(webhook_receipts)
                .where(webhook_receipts.c.tenant_id == tenant_id)
                .order_by(webhook_receipts.c.received_at.desc())
                .limit(limit)
            )
            return tuple(dict(row) for row in result.mappings())

    async def get_webhook_receipt(
        self, *, tenant_id: str, provider: str, event_id: str
    ) -> Mapping[str, object] | None:
        if not tenant_id.strip() or not provider.strip() or not event_id.strip():
            raise ValueError("webhook_receipt_identity_missing")
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(webhook_receipts).where(
                            and_(
                                webhook_receipts.c.tenant_id == tenant_id,
                                webhook_receipts.c.provider == provider,
                                webhook_receipts.c.event_id == event_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row is not None else None

    async def save_rule(self, rule: MailRule) -> MailRule:
        values = _rule_values(rule)
        async with self._begin(tenant_id=rule.tenant_id, subject_id=rule.owner_subject_id) as conn:
            row = (
                await conn.execute(select(rules.c.version).where(rules.c.rule_id == rule.rule_id))
            ).first()
            if row is not None and int(row[0]) >= rule.version:
                raise RepositoryConflictError("rule_revision_conflict")
            if row is None:
                await conn.execute(insert(rules).values(**values))
            else:
                await conn.execute(
                    update(rules).where(rules.c.rule_id == rule.rule_id).values(**values)
                )
        return rule

    async def get_rule(self, *, tenant_id: str, rule_id: UUID) -> MailRule | None:
        async with self._connect(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(rules).where(
                            and_(rules.c.rule_id == rule_id, rules.c.tenant_id == tenant_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _rule_from_row(row) if row else None

    async def list_rules(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> tuple[MailRule, ...]:
        _check_limit(limit)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(rules)
                .where(
                    and_(
                        rules.c.tenant_id == tenant_id,
                        rules.c.owner_subject_id == subject_id,
                    )
                )
                .order_by(rules.c.updated_at.desc())
                .limit(limit)
            )
            return tuple(_rule_from_row(row) for row in result.mappings())

    async def create_or_get_rule_execution(
        self, execution: RuleExecution
    ) -> tuple[RuleExecution, bool]:
        values = _rule_execution_values(execution)
        async with self._begin(
            tenant_id=execution.tenant_id, subject_id=execution.subject_id
        ) as conn:
            result = await conn.execute(
                pg_insert(rule_executions)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[rule_executions.c.execution_id])
            )
            row = (
                (
                    await conn.execute(
                        select(rule_executions).where(
                            rule_executions.c.execution_id == execution.execution_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("rule_execution_not_saved")
            existing = _rule_execution_from_row(row)
            if existing.tenant_id != execution.tenant_id:
                raise RepositoryConflictError("rule_execution_scope_conflict")
            return existing, cast(int, result.rowcount) == 1

    async def save_rule_execution(self, execution: RuleExecution) -> RuleExecution:
        values = _rule_execution_values(execution)
        async with self._begin(
            tenant_id=execution.tenant_id, subject_id=execution.subject_id
        ) as conn:
            row = (
                (
                    await conn.execute(
                        select(rule_executions).where(
                            rule_executions.c.execution_id == execution.execution_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                current = _rule_execution_from_row(row)
                if (
                    current.tenant_id != execution.tenant_id
                    or current.subject_id != execution.subject_id
                    or current.revision >= execution.revision
                ):
                    if current == execution:
                        return current
                    raise RepositoryConflictError("rule_execution_revision_conflict")
                await conn.execute(
                    update(rule_executions)
                    .where(rule_executions.c.execution_id == execution.execution_id)
                    .values(**values)
                )
            else:
                await conn.execute(insert(rule_executions).values(**values))
        return execution

    async def list_rule_executions(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[RuleExecution, ...]:
        _check_rule_execution_limit(limit)
        conditions = [
            rule_executions.c.tenant_id == tenant_id,
            rule_executions.c.subject_id == subject_id,
        ]
        if rule_id is not None:
            conditions.append(rule_executions.c.rule_id == rule_id)
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(rule_executions)
                .where(and_(*conditions))
                .order_by(rule_executions.c.updated_at.desc())
                .limit(limit)
            )
            return tuple(_rule_execution_from_row(row) for row in result.mappings())

    async def count_rule_executions_since(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID,
        since: datetime,
    ) -> int:
        async with self._connect(tenant_id=tenant_id, subject_id=subject_id) as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(rule_executions)
                .where(
                    and_(
                        rule_executions.c.tenant_id == tenant_id,
                        rule_executions.c.subject_id == subject_id,
                        rule_executions.c.rule_id == rule_id,
                        rule_executions.c.status == RuleExecutionStatus.EXECUTED.value,
                        rule_executions.c.updated_at >= since,
                    )
                )
            )
            return int(result.scalar_one())

    async def delete_connection_data(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID
    ) -> Mapping[str, object]:
        """Delete connection projections while retaining the connection tombstone and audit."""

        async with self._begin(tenant_id=tenant_id, subject_id=subject_id) as conn:
            exists = (
                await conn.execute(
                    select(connections.c.connection_id).where(
                        and_(
                            connections.c.connection_id == connection_id,
                            connections.c.tenant_id == tenant_id,
                            connections.c.subject_id == subject_id,
                        )
                    )
                )
            ).first()
            if exists is None:
                raise RepositoryConflictError("connection_not_found")
            message_ids = [
                row[0]
                for row in (
                    await conn.execute(
                        select(messages.c.message_id).where(
                            and_(
                                messages.c.tenant_id == tenant_id,
                                messages.c.connection_id == connection_id,
                            )
                        )
                    )
                ).all()
            ]
            counts: dict[str, int] = {}
            for key, table in (
                ("operations_deleted", operations),
                ("drafts_deleted", drafts),
                ("sync_jobs_deleted", sync_jobs),
                ("cursors_deleted", cursors),
            ):
                result = await conn.execute(
                    delete(table).where(
                        and_(table.c.tenant_id == tenant_id, table.c.connection_id == connection_id)
                    )
                )
                counts[key] = int(result.rowcount or 0)
            if message_ids:
                execution_result = await conn.execute(
                    delete(rule_executions).where(
                        and_(
                            rule_executions.c.tenant_id == tenant_id,
                            rule_executions.c.message_id.in_(message_ids),
                        )
                    )
                )
                candidate_result = await conn.execute(
                    delete(candidates).where(
                        and_(
                            candidates.c.tenant_id == tenant_id,
                            candidates.c.message_id.in_(message_ids),
                        )
                    )
                )
                message_result = await conn.execute(
                    delete(messages).where(
                        and_(
                            messages.c.tenant_id == tenant_id,
                            messages.c.connection_id == connection_id,
                        )
                    )
                )
                counts["candidates_deleted"] = int(candidate_result.rowcount or 0)
                counts["messages_deleted"] = int(message_result.rowcount or 0)
                counts["rule_executions_deleted"] = int(execution_result.rowcount or 0)
            else:
                counts["candidates_deleted"] = 0
                counts["messages_deleted"] = 0
                counts["rule_executions_deleted"] = 0
            thread_result = await conn.execute(
                delete(threads).where(
                    and_(threads.c.tenant_id == tenant_id, threads.c.connection_id == connection_id)
                )
            )
            counts["threads_deleted"] = int(thread_result.rowcount or 0)
            return {"connection_id": str(connection_id), **counts}

    async def _transition_operation(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
        fencing_token: int,
        status: DeliveryStatus,
        provider_message_ref: str | None = None,
        provider_request_id: str | None = None,
        error_code: str | None = None,
        next_attempt_at: datetime | None = None,
        increment_attempt: bool = False,
        clear_lease: bool = False,
    ) -> MailOutboxOperation:
        async with self._begin(tenant_id=tenant_id) as conn:
            row = (
                (
                    await conn.execute(
                        select(operations)
                        .where(
                            and_(
                                operations.c.operation_id == operation_id,
                                operations.c.tenant_id == tenant_id,
                                operations.c.lease_owner == worker_id,
                                operations.c.fencing_token == fencing_token,
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise RepositoryConflictError("stale_fencing_token")
            operation = _operation_from_row(row)
            values: dict[str, object] = {
                "status": status.value,
                "updated_at": utc_now(),
            }
            if increment_attempt:
                values["attempt_count"] = operation.attempt_count + 1
            if clear_lease:
                values["lease_owner"] = None
                values["lease_expires_at"] = None
            if provider_message_ref is not None:
                values["provider_message_ref"] = provider_message_ref
            if provider_request_id is not None:
                values["provider_request_id"] = provider_request_id
            if error_code is not None:
                values["error_code"] = error_code
            values["next_attempt_at"] = next_attempt_at
            await conn.execute(
                update(operations).where(operations.c.operation_id == operation_id).values(**values)
            )
            return _replace_operation(
                operation,
                status=status,
                attempt_count=operation.attempt_count + int(increment_attempt),
                lease_owner=None if clear_lease else operation.lease_owner,
                lease_expires_at=None if clear_lease else operation.lease_expires_at,
                provider_message_ref=provider_message_ref or operation.provider_message_ref,
                provider_request_id=provider_request_id or operation.provider_request_id,
                error_code=error_code,
                next_attempt_at=next_attempt_at,
                updated_at=cast(datetime, values["updated_at"]),
            )


def _validate_worker_lease_seconds(lease_seconds: int) -> None:
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("worker_lease_seconds_invalid")


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= 200:
        raise ValueError("limit_invalid")


def _check_lifecycle_limit(limit: int) -> None:
    if not 1 <= limit <= 10_000:
        raise ValueError("limit_invalid")


def _check_rule_execution_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("limit_invalid")


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return utc_now()


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set, frozenset)) else []


def _connection_values(value: MailboxConnection) -> dict[str, object]:
    return {
        "connection_id": value.connection_id,
        "tenant_id": value.tenant_id,
        "subject_id": value.subject_id,
        "provider": value.provider.value,
        "email_address": value.email_address,
        "credential_ref": value.credential_ref,
        "granted_scopes": list(value.granted_scopes),
        "provider_account_id": value.provider_account_id,
        "provider_tenant_id": value.provider_tenant_id,
        "credential_version": value.credential_version,
        "content_mode": value.content_mode.value,
        "status": value.status.value,
        "revision": value.revision,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _connection_from_row(row: Mapping[Any, Any]) -> MailboxConnection:
    return MailboxConnection(
        connection_id=cast(UUID, row["connection_id"]),
        tenant_id=str(row["tenant_id"]),
        subject_id=str(row["subject_id"]),
        provider=ProviderName(str(row["provider"])),
        email_address=str(row["email_address"]),
        credential_ref=str(row["credential_ref"]),
        granted_scopes=tuple(str(item) for item in _list(row["granted_scopes"])),
        provider_account_id=(
            str(row["provider_account_id"]) if row.get("provider_account_id") is not None else None
        ),
        provider_tenant_id=(
            str(row["provider_tenant_id"]) if row.get("provider_tenant_id") is not None else None
        ),
        credential_version=int(row.get("credential_version") or 1),
        content_mode=ContentMode(str(row["content_mode"])),
        status=ConnectionStatus(str(row["status"])),
        revision=int(row["revision"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _thread_values(value: MailThread) -> dict[str, object]:
    return {
        "thread_id": value.thread_id,
        "tenant_id": value.tenant_id,
        "connection_id": value.connection_id,
        "provider_thread_ref": value.provider_thread_ref,
        "normalized_subject": value.normalized_subject,
        "participant_addresses": list(value.participant_addresses),
        "latest_at": value.latest_at,
        "message_count": value.message_count,
        "revision": value.revision,
    }


def _thread_from_row(row: Mapping[Any, Any]) -> MailThread:
    return MailThread(
        thread_id=cast(UUID, row["thread_id"]),
        tenant_id=str(row["tenant_id"]),
        connection_id=cast(UUID, row["connection_id"]),
        provider_thread_ref=str(row["provider_thread_ref"]),
        normalized_subject=str(row["normalized_subject"]),
        participant_addresses=tuple(str(item) for item in _list(row["participant_addresses"])),
        latest_at=_parse_datetime(row["latest_at"]),
        message_count=int(row["message_count"]),
        revision=int(row["revision"]),
    )


def _message_values(value: MailMessageProjection) -> dict[str, object]:
    return {
        "message_id": value.message_id,
        "tenant_id": value.tenant_id,
        "connection_id": value.connection_id,
        "thread_id": value.thread_id,
        "provider_message_ref": value.provider_message_ref,
        "internet_message_id": value.internet_message_id,
        "sender_address": value.sender_address,
        "recipient_addresses": list(value.recipient_addresses),
        "cc_addresses": list(value.cc_addresses),
        "bcc_addresses": list(value.bcc_addresses),
        "reply_to_addresses": list(value.reply_to_addresses),
        "subject": value.subject,
        "received_at": value.received_at,
        "body_object_ref": value.body_object_ref,
        "content_sha256": value.content_sha256,
        "labels": list(value.labels),
        "attachment_count": value.attachment_count,
        "is_read": value.is_read,
        "provider_metadata": dict(value.provider_metadata),
        "revision": value.revision,
    }


def _message_from_row(row: Mapping[Any, Any]) -> MailMessageProjection:
    return MailMessageProjection(
        message_id=cast(UUID, row["message_id"]),
        tenant_id=str(row["tenant_id"]),
        connection_id=cast(UUID, row["connection_id"]),
        thread_id=cast(UUID, row["thread_id"]),
        provider_message_ref=str(row["provider_message_ref"]),
        internet_message_id=cast(str | None, row["internet_message_id"]),
        sender_address=str(row["sender_address"]),
        recipient_addresses=tuple(str(item) for item in _list(row["recipient_addresses"])),
        cc_addresses=tuple(str(item) for item in _list(row.get("cc_addresses", []))),
        bcc_addresses=tuple(str(item) for item in _list(row.get("bcc_addresses", []))),
        reply_to_addresses=tuple(str(item) for item in _list(row.get("reply_to_addresses", []))),
        subject=str(row["subject"]),
        received_at=_parse_datetime(row["received_at"]),
        body_text=None,
        body_object_ref=cast(str | None, row["body_object_ref"]),
        content_sha256=str(row["content_sha256"]),
        labels=tuple(str(item) for item in _list(row["labels"])),
        attachment_count=int(row.get("attachment_count", 0)),
        is_read=bool(row["is_read"]),
        revision=int(row["revision"]),
        provider_metadata={
            str(key): str(value)
            for key, value in cast(Mapping[Any, Any], row.get("provider_metadata", {})).items()
        },
    )


def _sync_state_values(value: MailboxSyncState) -> dict[str, object]:
    return {
        "connection_id": value.connection_id,
        "tenant_id": value.tenant_id,
        "folder_ref": value.folder_ref,
        "cursor_kind": value.cursor_kind,
        "cursor_value": value.cursor_value,
        "subscription_ref": value.subscription_ref,
        "subscription_status": value.subscription_status.value,
        "subscription_expires_at": value.subscription_expires_at,
        "subscription_callback_endpoint": value.subscription_callback_endpoint,
        "subscription_client_state_ref": value.subscription_client_state_ref,
        "subscription_provider_request_id": value.subscription_provider_request_id,
        "lease_owner": value.lease_owner,
        "fencing_token": value.fencing_token,
        "status": value.status,
        "watermark": value.watermark,
        "updated_at": value.updated_at,
    }


def _sync_state_from_row(row: Mapping[Any, Any]) -> MailboxSyncState:
    raw_status = row.get("subscription_status", SubscriptionStatus.NONE.value)
    return MailboxSyncState(
        tenant_id=str(row["tenant_id"]),
        connection_id=cast(UUID, row["connection_id"]),
        folder_ref=str(row["folder_ref"]),
        cursor_kind=str(row.get("cursor_kind", "provider")),
        cursor_value=cast(str | None, row.get("cursor_value")),
        subscription_ref=cast(str | None, row.get("subscription_ref")),
        subscription_status=SubscriptionStatus(str(raw_status)),
        subscription_expires_at=(
            _parse_datetime(row["subscription_expires_at"])
            if row.get("subscription_expires_at") is not None
            else None
        ),
        subscription_callback_endpoint=cast(str | None, row.get("subscription_callback_endpoint")),
        subscription_client_state_ref=cast(str | None, row.get("subscription_client_state_ref")),
        subscription_provider_request_id=cast(
            str | None, row.get("subscription_provider_request_id")
        ),
        lease_owner=cast(str | None, row.get("lease_owner")),
        fencing_token=int(row.get("fencing_token", 0)),
        status=str(row.get("status", "idle")),
        watermark=(_parse_datetime(row["watermark"]) if row.get("watermark") is not None else None),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _policy_values(value: MailAgentPolicy) -> dict[str, object]:
    return {
        "policy_id": value.policy_id,
        "tenant_id": value.tenant_id,
        "owner_subject_id": value.owner_subject_id,
        "allowed_connection_ids": list(value.allowed_connection_ids),
        "allowed_folder_refs": list(value.allowed_folder_refs),
        "allowed_actions": [item.value for item in value.allowed_actions],
        "allowed_domains": list(value.allowed_domains),
        "allowed_data_classes": list(value.allowed_data_classes),
        "thread_only": value.thread_only,
        "allowed_automation_level": value.allowed_automation_level.value,
        "max_per_hour": value.max_per_hour,
        "max_per_day": value.max_per_day,
        "valid_from": value.valid_from,
        "valid_until": value.valid_until,
        "revision": value.revision,
        "enabled": value.enabled,
    }


def _policy_from_row(row: Mapping[Any, Any]) -> MailAgentPolicy:
    return MailAgentPolicy(
        policy_id=cast(UUID, row["policy_id"]),
        tenant_id=str(row["tenant_id"]),
        owner_subject_id=str(row["owner_subject_id"]),
        allowed_connection_ids=frozenset(cast(Sequence[UUID], row["allowed_connection_ids"])),
        allowed_folder_refs=frozenset(str(item) for item in _list(row["allowed_folder_refs"])),
        allowed_actions=frozenset(ActionType(str(item)) for item in _list(row["allowed_actions"])),
        allowed_domains=frozenset(str(item) for item in _list(row["allowed_domains"])),
        allowed_data_classes=frozenset(str(item) for item in _list(row["allowed_data_classes"])),
        thread_only=bool(row["thread_only"]),
        allowed_automation_level=AutomationLevel(str(row["allowed_automation_level"])),
        max_per_hour=int(row["max_per_hour"]),
        max_per_day=int(row["max_per_day"]),
        valid_from=_parse_datetime(row["valid_from"]),
        valid_until=_parse_datetime(row["valid_until"]),
        revision=int(row["revision"]),
        enabled=bool(row["enabled"]),
    )


def _grant_values(value: DelegationGrant) -> dict[str, object]:
    return {
        "grant_id": value.grant_id,
        "tenant_id": value.tenant_id,
        "policy_id": value.policy_id,
        "agent_subject_id": value.agent_subject_id,
        "granted_by_subject_id": value.granted_by_subject_id,
        "capability_ids": [item.value for item in value.capability_ids],
        "granted_at": value.granted_at,
        "expires_at": value.expires_at,
        "revoked_at": value.revoked_at,
        "revision": value.revision,
    }


def _grant_from_row(row: Mapping[Any, Any]) -> DelegationGrant:
    return DelegationGrant(
        grant_id=cast(UUID, row["grant_id"]),
        tenant_id=str(row["tenant_id"]),
        policy_id=cast(UUID, row["policy_id"]),
        agent_subject_id=str(row["agent_subject_id"]),
        granted_by_subject_id=str(row["granted_by_subject_id"]),
        capability_ids=frozenset(ActionType(str(item)) for item in _list(row["capability_ids"])),
        granted_at=_parse_datetime(row["granted_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        revoked_at=(_parse_datetime(row["revoked_at"]) if row["revoked_at"] is not None else None),
        revision=int(row["revision"]),
    )


def _draft_values(value: MailDraft) -> dict[str, object]:
    return {
        "draft_id": value.draft_id,
        "tenant_id": value.tenant_id,
        "subject_id": value.subject_id,
        "connection_id": value.connection_id,
        "thread_id": value.thread_id,
        "recipient_addresses": list(value.recipient_addresses),
        "subject": value.subject,
        "cc_addresses": list(value.cc_addresses),
        "bcc_addresses": list(value.bcc_addresses),
        "attachment_refs": list(value.attachment_refs),
        "body_object_ref": value.body_object_ref,
        "content_sha256": value.content_sha256,
        "revision": value.revision,
        "status": value.status.value,
        "provider_draft_ref": value.provider_draft_ref,
        "expires_at": value.expires_at,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _draft_from_row(row: Mapping[Any, Any]) -> MailDraft:
    return MailDraft(
        draft_id=cast(UUID, row["draft_id"]),
        tenant_id=str(row["tenant_id"]),
        subject_id=str(row["subject_id"]),
        connection_id=cast(UUID, row["connection_id"]),
        thread_id=cast(UUID | None, row["thread_id"]),
        recipient_addresses=tuple(str(item) for item in _list(row["recipient_addresses"])),
        subject=str(row["subject"]),
        cc_addresses=tuple(str(item) for item in _list(row.get("cc_addresses", []))),
        bcc_addresses=tuple(str(item) for item in _list(row.get("bcc_addresses", []))),
        attachment_refs=tuple(str(item) for item in _list(row.get("attachment_refs", []))),
        body_text=None,
        content_sha256=str(row["content_sha256"]),
        body_object_ref=cast(str | None, row["body_object_ref"]),
        revision=int(row["revision"]),
        status=DeliveryStatus(str(row["status"])),
        provider_draft_ref=cast(str | None, row["provider_draft_ref"]),
        expires_at=_parse_datetime(row["expires_at"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _candidate_values(value: MailActionCandidate) -> dict[str, object]:
    return {
        "candidate_id": value.candidate_id,
        "tenant_id": value.tenant_id,
        "subject_id": value.subject_id,
        "message_id": value.message_id,
        "candidate_type": value.candidate_type.value,
        "payload": dict(value.payload),
        "evidence": [dict(item) for item in value.evidence],
        "confidence": value.confidence,
        "requires_review": value.requires_review,
        "status": value.status.value,
        "revision": value.revision,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _candidate_from_row(row: Mapping[Any, Any]) -> MailActionCandidate:
    return MailActionCandidate(
        candidate_id=cast(UUID, row["candidate_id"]),
        tenant_id=str(row["tenant_id"]),
        subject_id=str(row["subject_id"]),
        message_id=cast(UUID, row["message_id"]),
        candidate_type=CandidateType(str(row["candidate_type"])),
        payload=cast(Mapping[str, object], row["payload"]),
        evidence=tuple(cast(Sequence[Mapping[str, object]], row["evidence"])),
        confidence=float(row["confidence"]),
        requires_review=bool(row["requires_review"]),
        status=CandidateState(str(row["status"])),
        revision=int(row["revision"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _operation_values(value: MailOutboxOperation) -> dict[str, object]:
    return {
        "operation_id": value.operation_id,
        "tenant_id": value.tenant_id,
        "subject_id": value.subject_id,
        "draft_id": value.draft_id,
        "connection_id": value.connection_id,
        "idempotency_key": value.idempotency_key,
        "status": value.status.value,
        "action_id": value.action_id,
        "input_digest": value.input_digest,
        "agent_subject_id": value.agent_subject_id,
        "policy_id": value.policy_id,
        "grant_id": value.grant_id,
        "policy_revision": value.policy_revision,
        "grant_revision": value.grant_revision,
        "approval_ref": value.approval_ref,
        "approver_subject_id": value.approver_subject_id,
        "attempt_count": value.attempt_count,
        "lease_owner": value.lease_owner,
        "lease_expires_at": value.lease_expires_at,
        "fencing_token": value.fencing_token,
        "provider_message_ref": value.provider_message_ref,
        "provider_request_id": value.provider_request_id,
        "error_code": value.error_code,
        "next_attempt_at": value.next_attempt_at,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _operation_from_row(row: Mapping[Any, Any]) -> MailOutboxOperation:
    return MailOutboxOperation(
        operation_id=cast(UUID, row["operation_id"]),
        tenant_id=str(row["tenant_id"]),
        subject_id=str(row["subject_id"]),
        draft_id=cast(UUID, row["draft_id"]),
        connection_id=cast(UUID, row["connection_id"]),
        idempotency_key=str(row["idempotency_key"]),
        status=DeliveryStatus(str(row["status"])),
        action_id=cast(UUID | None, row["action_id"]),
        input_digest=cast(str | None, row["input_digest"]),
        agent_subject_id=cast(str | None, row["agent_subject_id"]),
        policy_id=cast(UUID | None, row["policy_id"]),
        grant_id=cast(UUID | None, row["grant_id"]),
        policy_revision=(
            int(row["policy_revision"]) if row["policy_revision"] is not None else None
        ),
        grant_revision=(int(row["grant_revision"]) if row["grant_revision"] is not None else None),
        approval_ref=cast(str | None, row["approval_ref"]),
        approver_subject_id=cast(str | None, row["approver_subject_id"]),
        attempt_count=int(row["attempt_count"]),
        lease_owner=cast(str | None, row["lease_owner"]),
        lease_expires_at=(
            _parse_datetime(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        fencing_token=int(row["fencing_token"]),
        provider_message_ref=cast(str | None, row["provider_message_ref"]),
        provider_request_id=cast(str | None, row["provider_request_id"]),
        error_code=cast(str | None, row["error_code"]),
        next_attempt_at=(
            _parse_datetime(row["next_attempt_at"]) if row["next_attempt_at"] is not None else None
        ),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _sync_job_values(value: MailSyncJob) -> dict[str, object]:
    return {
        "job_id": value.job_id,
        "tenant_id": value.tenant_id,
        "subject_id": value.subject_id,
        "connection_id": value.connection_id,
        "mode": value.mode,
        "requested_limit": value.requested_limit,
        "folder_ref": value.folder_ref,
        "label_refs": list(value.label_refs),
        "received_after": value.received_after,
        "received_before": value.received_before,
        "idempotency_key": value.idempotency_key,
        "status": value.status.value,
        "fetched_count": value.fetched_count,
        "saved_count": value.saved_count,
        "duplicate_count": value.duplicate_count,
        "deleted_count": value.deleted_count,
        "cursor_before": value.cursor_before,
        "cursor_after": value.cursor_after,
        "provider_request_id": value.provider_request_id,
        "error_code": value.error_code,
        "trace_id": value.trace_id,
        "lease_owner": value.lease_owner,
        "lease_expires_at": value.lease_expires_at,
        "fencing_token": value.fencing_token,
        "created_at": value.created_at,
        "started_at": value.started_at,
        "finished_at": value.finished_at,
        "updated_at": value.updated_at,
        "attempt_count": value.attempt_count,
        "next_attempt_at": value.next_attempt_at,
    }


def _sync_job_from_row(row: Mapping[Any, Any]) -> MailSyncJob:
    return MailSyncJob(
        job_id=cast(UUID, row["job_id"]),
        tenant_id=str(row["tenant_id"]),
        subject_id=str(row["subject_id"]),
        connection_id=cast(UUID, row["connection_id"]),
        mode=str(row["mode"]),
        requested_limit=int(row["requested_limit"]),
        folder_ref=str(row.get("folder_ref") or "INBOX"),
        label_refs=tuple(str(item) for item in _list(row.get("label_refs", []))),
        received_after=(
            _parse_datetime(row["received_after"])
            if row.get("received_after") is not None
            else None
        ),
        received_before=(
            _parse_datetime(row["received_before"])
            if row.get("received_before") is not None
            else None
        ),
        idempotency_key=str(row["idempotency_key"]),
        status=SyncJobStatus(str(row["status"])),
        fetched_count=int(row["fetched_count"]),
        saved_count=int(row["saved_count"]),
        duplicate_count=int(row["duplicate_count"]),
        deleted_count=int(row.get("deleted_count", 0)),
        cursor_before=cast(str | None, row["cursor_before"]),
        cursor_after=cast(str | None, row["cursor_after"]),
        provider_request_id=cast(str | None, row["provider_request_id"]),
        error_code=cast(str | None, row["error_code"]),
        trace_id=cast(str | None, row["trace_id"]),
        lease_owner=cast(str | None, row["lease_owner"]),
        lease_expires_at=(
            _parse_datetime(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        fencing_token=int(row["fencing_token"]),
        created_at=_parse_datetime(row["created_at"]),
        started_at=(_parse_datetime(row["started_at"]) if row["started_at"] is not None else None),
        finished_at=(
            _parse_datetime(row["finished_at"]) if row["finished_at"] is not None else None
        ),
        updated_at=_parse_datetime(row["updated_at"]),
        attempt_count=int(row.get("attempt_count", 0)),
        next_attempt_at=(
            _parse_datetime(row["next_attempt_at"])
            if row.get("next_attempt_at") is not None
            else None
        ),
    )


def _autonomy_run_values(value: MailAutonomyRun) -> dict[str, object]:
    return {
        "run_id": value.run_id,
        "tenant_id": value.tenant_id,
        "subject_id": value.subject_id,
        "connection_id": value.connection_id,
        "replay_key": value.replay_key,
        "mode": value.mode,
        "requested_limit": value.requested_limit,
        "message_limit": value.message_limit,
        "status": value.status.value,
        "message_ids": list(value.message_ids),
        "analyzed_message_ids": list(value.analyzed_message_ids),
        "candidate_ids": list(value.candidate_ids),
        "sync_result": dict(value.sync_result),
        "sync_job_id": value.sync_job_id,
        "error_code": value.error_code,
        "pause_reason": value.pause_reason,
        "lease_owner": value.lease_owner,
        "lease_expires_at": value.lease_expires_at,
        "fencing_token": value.fencing_token,
        "revision": value.revision,
        "created_at": value.created_at,
        "started_at": value.started_at,
        "finished_at": value.finished_at,
        "updated_at": value.updated_at,
    }


def _autonomy_run_from_row(row: Mapping[Any, Any]) -> MailAutonomyRun:
    return MailAutonomyRun(
        run_id=cast(UUID, row["run_id"]),
        tenant_id=str(row["tenant_id"]),
        subject_id=str(row["subject_id"]),
        connection_id=cast(UUID, row["connection_id"]),
        replay_key=str(row["replay_key"]),
        mode=str(row["mode"]),
        requested_limit=int(row["requested_limit"]),
        message_limit=int(row["message_limit"]),
        status=AutonomyRunStatus(str(row["status"])),
        message_ids=tuple(cast(Sequence[UUID], row["message_ids"])),
        analyzed_message_ids=tuple(cast(Sequence[UUID], row["analyzed_message_ids"])),
        candidate_ids=tuple(cast(Sequence[UUID], row["candidate_ids"])),
        sync_result=cast(Mapping[str, object], row["sync_result"]),
        sync_job_id=cast(UUID | None, row["sync_job_id"]),
        error_code=cast(str | None, row["error_code"]),
        pause_reason=cast(str | None, row["pause_reason"]),
        lease_owner=cast(str | None, row["lease_owner"]),
        lease_expires_at=(
            _parse_datetime(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        fencing_token=int(row["fencing_token"]),
        revision=int(row["revision"]),
        created_at=_parse_datetime(row["created_at"]),
        started_at=(_parse_datetime(row["started_at"]) if row["started_at"] is not None else None),
        finished_at=(
            _parse_datetime(row["finished_at"]) if row["finished_at"] is not None else None
        ),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _rule_values(value: MailRule) -> dict[str, object]:
    return {
        "rule_id": value.rule_id,
        "tenant_id": value.tenant_id,
        "owner_subject_id": value.owner_subject_id,
        "name": value.name,
        "conditions": [
            {"field": item.field.value, "operator": item.operator.value, "value": item.value}
            for item in value.conditions
        ],
        "action_type": value.action_type.value,
        "action_params": dict(value.action_params),
        "automation_level": value.automation_level.value,
        "version": value.version,
        "enabled": value.enabled,
        "max_per_hour": value.max_per_hour,
        "max_per_day": value.max_per_day,
        "valid_from": value.valid_from,
        "valid_until": value.valid_until,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _rule_execution_values(value: RuleExecution) -> dict[str, object]:
    return {
        "execution_id": value.execution_id,
        "tenant_id": value.tenant_id,
        "subject_id": value.subject_id,
        "rule_id": value.rule_id,
        "rule_version": value.rule_version,
        "message_id": value.message_id,
        "action_id": value.action_id,
        "input_digest": value.input_digest,
        "status": value.status.value,
        "reason": value.reason,
        "result": dict(value.result),
        "revision": value.revision,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _rule_execution_from_row(row: Mapping[Any, Any]) -> RuleExecution:
    return RuleExecution(
        execution_id=cast(UUID, row["execution_id"]),
        tenant_id=str(row["tenant_id"]),
        subject_id=str(row["subject_id"]),
        rule_id=cast(UUID, row["rule_id"]),
        rule_version=int(row["rule_version"]),
        message_id=cast(UUID, row["message_id"]),
        action_id=cast(UUID, row["action_id"]),
        input_digest=str(row["input_digest"]),
        status=RuleExecutionStatus(str(row["status"])),
        reason=str(row["reason"]),
        result=cast(Mapping[str, object], row["result"]),
        revision=int(row["revision"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _rule_from_row(row: Mapping[Any, Any]) -> MailRule:
    raw_conditions = row["conditions"]
    if not isinstance(raw_conditions, list):
        raise RepositoryConflictError("rule_conditions_corrupt")
    conditions = tuple(
        RuleCondition(
            field=RuleField(str(item["field"])),
            operator=RuleOperator(str(item["operator"])),
            value=(
                tuple(str(value) for value in item["value"])
                if isinstance(item["value"], list)
                else item["value"]
            ),
        )
        for item in raw_conditions
        if isinstance(item, Mapping)
    )
    return MailRule(
        rule_id=cast(UUID, row["rule_id"]),
        tenant_id=str(row["tenant_id"]),
        owner_subject_id=str(row["owner_subject_id"]),
        name=str(row["name"]),
        conditions=conditions,
        action_type=ActionType(str(row["action_type"])),
        action_params=cast(Mapping[str, object], row["action_params"]),
        automation_level=AutomationLevel(str(row["automation_level"])),
        version=int(row["version"]),
        enabled=bool(row["enabled"]),
        max_per_hour=int(row["max_per_hour"]),
        max_per_day=int(row["max_per_day"]),
        valid_from=_parse_datetime(row["valid_from"]),
        valid_until=_parse_datetime(row["valid_until"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _replace_operation(operation: MailOutboxOperation, **values: object) -> MailOutboxOperation:
    return replace(operation, **cast(Any, values))
