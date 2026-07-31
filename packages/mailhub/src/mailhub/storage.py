"""Local repository adapter used by the contract/API tests.

The adapter is intentionally explicit about leases and fencing.  It is not a
production persistence implementation; M1's PostgreSQL adapter must implement
the same protocol and transaction invariants before production claims.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from mailhub.domain import (
    AutonomyRunStatus,
    CandidateState,
    CandidateType,
    ConnectionCleanupRefs,
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
    SubscriptionStatus,
    SyncJobStatus,
    transition_autonomy_run,
    transition_delivery,
    transition_sync_job,
    utc_now,
)
from mailhub.observability import redact_event
from mailhub.ports import ObjectStorePort
from mailhub.rules import MailRule, RuleExecution
from mailhub.webhook import normalize_receipt_route_metadata


class RepositoryConflictError(RuntimeError):
    """A revision, identity, lease or tenant invariant failed."""


def _validate_worker_lease_seconds(lease_seconds: int) -> None:
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("worker_lease_seconds_invalid")


class InMemoryObjectStore(ObjectStorePort):
    """Test-only tenant-scoped object store; production must use encrypted storage."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[str, str, str, str, datetime | None, datetime]] = {}

    async def put_text(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        purpose: str,
        content: str,
        content_sha256: str,
        expires_at: datetime | None = None,
    ) -> str:
        from mailhub.domain import digest_text

        if digest_text(content) != content_sha256:
            raise ValueError("object_content_digest_mismatch")
        ref = f"memory://mailhub/{uuid4()}"
        created_at = datetime.now(UTC)
        self._objects[ref] = (
            tenant_id,
            subject_id,
            content,
            purpose,
            expires_at.astimezone(UTC) if expires_at is not None else None,
            created_at,
        )
        return ref

    async def get_text(self, *, tenant_id: str, subject_id: str, object_ref: str) -> str:
        value = self._objects.get(object_ref)
        if value is None or value[:2] != (tenant_id, subject_id):
            raise KeyError("object_not_found")
        if value[4] is not None and value[4] <= datetime.now(UTC):
            del self._objects[object_ref]
            raise KeyError("object_expired")
        return value[2]

    async def delete(self, *, tenant_id: str, subject_id: str, object_ref: str) -> None:
        value = self._objects.get(object_ref)
        if value is not None and value[:2] == (tenant_id, subject_id):
            del self._objects[object_ref]

    async def purge_expired(
        self, *, now: datetime | None = None, limit: int = 500
    ) -> tuple[Mapping[str, object], ...]:
        if not 1 <= limit <= 5000:
            raise ValueError("object_purge_limit_invalid")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        deleted: list[Mapping[str, object]] = []
        for object_ref, value in tuple(self._objects.items()):
            if len(deleted) >= limit:
                break
            expires_at = value[4]
            if expires_at is None or expires_at > current:
                continue
            del self._objects[object_ref]
            deleted.append(
                {
                    "object_ref": object_ref,
                    "tenant_id": value[0],
                    "subject_id": value[1],
                    "purpose": value[3],
                    "content_sha256": hashlib.sha256(value[2].encode("utf-8")).hexdigest(),
                    "deleted_at": current.isoformat(),
                    "reason": "ttl_expired",
                }
            )
        return tuple(deleted)


class InMemoryMailRepository:
    """Deterministic repository for local development and contract tests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.connections: dict[UUID, MailboxConnection] = {}
        self.threads: dict[UUID, MailThread] = {}
        self.messages: dict[UUID, MailMessageProjection] = {}
        self.candidates: dict[UUID, MailActionCandidate] = {}
        self._message_identity: dict[tuple[UUID, str], UUID] = {}
        self.policies: dict[UUID, MailAgentPolicy] = {}
        self.grants: dict[UUID, DelegationGrant] = {}
        self.drafts: dict[UUID, MailDraft] = {}
        self.operations: dict[UUID, MailOutboxOperation] = {}
        self._operation_idempotency: dict[tuple[UUID, str], UUID] = {}
        self.audits: list[dict[str, object]] = []
        self.cursors: dict[tuple[UUID, str], str | None] = {}
        self.sync_states: dict[tuple[UUID, str], MailboxSyncState] = {}
        self.sync_jobs: dict[UUID, MailSyncJob] = {}
        self._sync_job_idempotency: dict[tuple[UUID, str], UUID] = {}
        self.autonomy_runs: dict[UUID, MailAutonomyRun] = {}
        self._autonomy_run_idempotency: dict[tuple[str, str, UUID, str], UUID] = {}
        self.webhook_receipts: dict[tuple[str, str, str], dict[str, object]] = {}
        self.rules: dict[UUID, MailRule] = {}
        self.rule_executions: dict[UUID, RuleExecution] = {}

    async def save_connection(self, connection: MailboxConnection) -> MailboxConnection:
        async with self._lock:
            existing = self.connections.get(connection.connection_id)
            if existing and existing.revision >= connection.revision:
                raise RepositoryConflictError("connection_revision_conflict")
            for item in self.connections.values():
                if item.connection_id != connection.connection_id and (
                    item.tenant_id,
                    item.subject_id,
                    item.email_address,
                ) == (connection.tenant_id, connection.subject_id, connection.email_address):
                    raise RepositoryConflictError("connection_identity_conflict")
            self.connections[connection.connection_id] = connection
            return connection

    async def list_connections(
        self, *, tenant_id: str, subject_id: str
    ) -> tuple[MailboxConnection, ...]:
        async with self._lock:
            return tuple(
                connection
                for connection in self.connections.values()
                if connection.tenant_id == tenant_id and connection.subject_id == subject_id
            )

    async def get_connection(
        self, *, tenant_id: str, connection_id: UUID
    ) -> MailboxConnection | None:
        async with self._lock:
            connection = self.connections.get(connection_id)
            if connection is None or connection.tenant_id != tenant_id:
                return None
            return connection

    async def save_thread(self, thread: MailThread) -> MailThread:
        async with self._lock:
            self.threads[thread.thread_id] = thread
            return thread

    async def save_message(
        self, message: MailMessageProjection
    ) -> tuple[MailMessageProjection, bool]:
        async with self._lock:
            key = (message.connection_id, message.provider_message_ref)
            existing_id = self._message_identity.get(key)
            if existing_id is not None:
                return self.messages[existing_id], False
            if message.message_id in self.messages:
                raise RepositoryConflictError("message_id_conflict")
            self.messages[message.message_id] = message
            self._message_identity[key] = message.message_id
            return message, True

    async def delete_message_by_provider_ref(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        provider_message_ref: str,
    ) -> MailMessageProjection | None:
        """Idempotently remove one provider-deleted message projection.

        Provider history/delta feeds may redeliver a deletion.  A missing
        projection is therefore a successful no-op; when present, dependent
        candidates and rule executions are removed before the message identity
        is released for a future provider reuse.
        """

        if not provider_message_ref.strip():
            raise ValueError("provider_message_ref_required")
        async with self._lock:
            message_id = self._message_identity.get((connection_id, provider_message_ref))
            if message_id is None:
                return None
            message = self.messages.get(message_id)
            if (
                message is None
                or message.tenant_id != tenant_id
                or message.connection_id != connection_id
            ):
                return None
            self.messages.pop(message_id, None)
            self._message_identity.pop((connection_id, provider_message_ref), None)
            self.candidates = {
                candidate_id: candidate
                for candidate_id, candidate in self.candidates.items()
                if candidate.message_id != message_id
            }
            self.rule_executions = {
                execution_id: execution
                for execution_id, execution in self.rule_executions.items()
                if execution.message_id != message_id
            }
            thread = self.threads.get(message.thread_id)
            if thread is not None:
                remaining = tuple(
                    item for item in self.messages.values() if item.thread_id == message.thread_id
                )
                participants = tuple(
                    dict.fromkeys(
                        address
                        for item in remaining
                        for address in (
                            item.sender_address,
                            *item.recipient_addresses,
                            *item.cc_addresses,
                            *item.bcc_addresses,
                            *item.reply_to_addresses,
                        )
                    )
                )
                latest_at = max((item.received_at for item in remaining), default=thread.latest_at)
                self.threads[thread.thread_id] = replace(
                    thread,
                    participant_addresses=participants,
                    latest_at=latest_at,
                    message_count=len(remaining),
                    revision=thread.revision + 1,
                )
            return message

    async def get_message(
        self, *, tenant_id: str, message_id: UUID
    ) -> MailMessageProjection | None:
        async with self._lock:
            message = self.messages.get(message_id)
            if message is None or message.tenant_id != tenant_id:
                return None
            return message

    async def save_candidate(self, candidate: MailActionCandidate) -> MailActionCandidate:
        async with self._lock:
            existing = self.candidates.get(candidate.candidate_id)
            if existing == candidate:
                return existing
            if existing and existing.revision >= candidate.revision:
                raise RepositoryConflictError("candidate_revision_conflict")
            self.candidates[candidate.candidate_id] = candidate
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
        if not 1 <= limit <= 200:
            raise ValueError("limit_invalid")
        async with self._lock:
            allowed_messages = {
                message.message_id
                for message in self.messages.values()
                if message.tenant_id == tenant_id
                and any(
                    connection.connection_id == message.connection_id
                    and connection.subject_id == subject_id
                    for connection in self.connections.values()
                )
            }
            return tuple(
                sorted(
                    (
                        candidate
                        for candidate in self.candidates.values()
                        if candidate.tenant_id == tenant_id
                        and candidate.message_id in allowed_messages
                        and (status is None or candidate.status is status)
                        and (candidate_type is None or candidate.candidate_type is candidate_type)
                    ),
                    key=lambda item: item.created_at,
                    reverse=True,
                )[:limit]
            )

    async def get_candidate(
        self, *, tenant_id: str, candidate_id: UUID
    ) -> MailActionCandidate | None:
        async with self._lock:
            candidate = self.candidates.get(candidate_id)
            return candidate if candidate and candidate.tenant_id == tenant_id else None

    async def list_messages(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        limit: int = 50,
    ) -> tuple[MailMessageProjection, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit_invalid")
        async with self._lock:
            allowed = {
                connection.connection_id
                for connection in self.connections.values()
                if connection.tenant_id == tenant_id and connection.subject_id == subject_id
            }
            return tuple(
                sorted(
                    (
                        message
                        for message in self.messages.values()
                        if message.connection_id in allowed
                    ),
                    key=lambda item: item.received_at,
                    reverse=True,
                )[:limit]
            )

    async def list_messages_for_connection(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID, limit: int = 5000
    ) -> tuple[MailMessageProjection, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("limit_invalid")
        async with self._lock:
            connection = self.connections.get(connection_id)
            if (
                connection is None
                or connection.tenant_id != tenant_id
                or connection.subject_id != subject_id
            ):
                return ()
            return tuple(
                sorted(
                    (
                        message
                        for message in self.messages.values()
                        if message.tenant_id == tenant_id and message.connection_id == connection_id
                    ),
                    key=lambda item: item.received_at,
                    reverse=True,
                )[:limit]
            )

    async def list_connection_cleanup_refs(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID
    ) -> ConnectionCleanupRefs:
        """Return all opaque cleanup references without a page-size cap."""

        async with self._lock:
            connection = self.connections.get(connection_id)
            if (
                connection is None
                or connection.tenant_id != tenant_id
                or connection.subject_id != subject_id
            ):
                raise RepositoryConflictError("connection_not_found")
            messages = tuple(
                message
                for message in self.messages.values()
                if message.tenant_id == tenant_id and message.connection_id == connection_id
            )
            message_ids = {message.message_id for message in messages}
            knowledge_message_ids = {
                candidate.message_id
                for candidate in self.candidates.values()
                if candidate.tenant_id == tenant_id
                and candidate.candidate_type is CandidateType.KNOWLEDGE
                and candidate.message_id in message_ids
            }
            object_refs = {
                message.body_object_ref
                for message in messages
                if message.body_object_ref is not None
            }
            for draft in self.drafts.values():
                if draft.tenant_id != tenant_id or draft.connection_id != connection_id:
                    continue
                if draft.body_object_ref is not None:
                    object_refs.add(draft.body_object_ref)
                object_refs.update(draft.attachment_refs)
            return ConnectionCleanupRefs(
                message_ids=tuple(message_ids),
                knowledge_message_ids=tuple(knowledge_message_ids),
                object_refs=tuple(object_refs),
            )

    async def search_messages(
        self, *, tenant_id: str, subject_id: str, query: str, limit: int
    ) -> tuple[MailMessageProjection, ...]:
        if not 1 <= limit <= 200 or not query.strip():
            raise ValueError("search_query_invalid")
        normalized = query.casefold().strip()
        async with self._lock:
            allowed = {
                connection.connection_id
                for connection in self.connections.values()
                if connection.tenant_id == tenant_id and connection.subject_id == subject_id
            }
            return tuple(
                sorted(
                    (
                        message
                        for message in self.messages.values()
                        if message.connection_id in allowed
                        and (
                            normalized in message.subject.casefold()
                            or normalized in message.sender_address.casefold()
                            or any(
                                normalized in address.casefold()
                                for address in (
                                    *message.recipient_addresses,
                                    *message.cc_addresses,
                                    *message.bcc_addresses,
                                    *message.reply_to_addresses,
                                )
                            )
                            or normalized in message.provider_message_ref.casefold()
                            or any(normalized in label.casefold() for label in message.labels)
                        )
                    ),
                    key=lambda item: item.received_at,
                    reverse=True,
                )[:limit]
            )

    async def list_threads(
        self, *, tenant_id: str, subject_id: str, limit: int
    ) -> tuple[MailThread, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit_invalid")
        async with self._lock:
            allowed = {
                connection.connection_id
                for connection in self.connections.values()
                if connection.tenant_id == tenant_id and connection.subject_id == subject_id
            }
            return tuple(
                sorted(
                    (thread for thread in self.threads.values() if thread.connection_id in allowed),
                    key=lambda item: item.latest_at,
                    reverse=True,
                )[:limit]
            )

    async def get_thread(self, *, tenant_id: str, thread_id: UUID) -> MailThread | None:
        async with self._lock:
            thread = self.threads.get(thread_id)
            return thread if thread and thread.tenant_id == tenant_id else None

    async def list_thread_messages(
        self, *, tenant_id: str, subject_id: str, thread_id: UUID, limit: int = 200
    ) -> tuple[MailMessageProjection, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("limit_invalid")
        async with self._lock:
            allowed_connections = {
                connection.connection_id
                for connection in self.connections.values()
                if connection.tenant_id == tenant_id and connection.subject_id == subject_id
            }
            return tuple(
                sorted(
                    (
                        message
                        for message in self.messages.values()
                        if message.thread_id == thread_id
                        and message.connection_id in allowed_connections
                    ),
                    key=lambda item: item.received_at,
                )[:limit]
            )

    async def get_cursor(
        self, *, tenant_id: str, connection_id: UUID, folder_ref: str
    ) -> str | None:
        async with self._lock:
            connection = self.connections.get(connection_id)
            if connection is None or connection.tenant_id != tenant_id:
                return None
            return self.cursors.get((connection_id, folder_ref))

    async def commit_cursor(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        folder_ref: str,
        expected_cursor: str | None,
        next_cursor: str | None,
    ) -> None:
        async with self._lock:
            connection = self.connections.get(connection_id)
            if connection is None or connection.tenant_id != tenant_id:
                raise RepositoryConflictError("cursor_scope_denied")
            key = (connection_id, folder_ref)
            current = self.cursors.get(key)
            if current != expected_cursor:
                raise RepositoryConflictError("cursor_conflict")
            self.cursors[key] = next_cursor
            existing_state = self.sync_states.get(key)
            self.sync_states[key] = MailboxSyncState(
                tenant_id=tenant_id,
                connection_id=connection_id,
                folder_ref=folder_ref,
                cursor_kind=existing_state.cursor_kind if existing_state else "provider",
                cursor_value=next_cursor,
                subscription_ref=existing_state.subscription_ref if existing_state else None,
                subscription_status=(
                    existing_state.subscription_status
                    if existing_state
                    else SubscriptionStatus.NONE
                ),
                subscription_expires_at=(
                    existing_state.subscription_expires_at if existing_state else None
                ),
                subscription_callback_endpoint=(
                    existing_state.subscription_callback_endpoint if existing_state else None
                ),
                subscription_client_state_ref=(
                    existing_state.subscription_client_state_ref if existing_state else None
                ),
                subscription_provider_request_id=(
                    existing_state.subscription_provider_request_id if existing_state else None
                ),
                lease_owner=existing_state.lease_owner if existing_state else None,
                fencing_token=existing_state.fencing_token if existing_state else 0,
                status="idle",
                watermark=existing_state.watermark if existing_state else None,
                updated_at=utc_now(),
            )

    async def get_sync_state(
        self, *, tenant_id: str, connection_id: UUID, folder_ref: str
    ) -> MailboxSyncState | None:
        async with self._lock:
            connection = self.connections.get(connection_id)
            if connection is None or connection.tenant_id != tenant_id:
                return None
            state = self.sync_states.get((connection_id, folder_ref))
            if state is not None:
                return state
            if (connection_id, folder_ref) not in self.cursors:
                return None
            return MailboxSyncState(
                tenant_id=tenant_id,
                connection_id=connection_id,
                folder_ref=folder_ref,
                cursor_value=self.cursors[(connection_id, folder_ref)],
            )

    async def list_sync_states(
        self, *, tenant_id: str, connection_id: UUID
    ) -> tuple[MailboxSyncState, ...]:
        async with self._lock:
            connection = self.connections.get(connection_id)
            if connection is None or connection.tenant_id != tenant_id:
                return ()
            keys = set(key for key in self.cursors if key[0] == connection_id)
            keys.update(key for key in self.sync_states if key[0] == connection_id)
            states: list[MailboxSyncState] = []
            for key in sorted(keys, key=lambda item: item[1]):
                state = self.sync_states.get(key)
                if state is None:
                    state = MailboxSyncState(
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        folder_ref=key[1],
                        cursor_value=self.cursors[key],
                    )
                states.append(state)
            return tuple(states)

    async def save_sync_state(
        self,
        state: MailboxSyncState,
        *,
        expected_subscription_ref: str | None = None,
    ) -> MailboxSyncState:
        async with self._lock:
            connection = self.connections.get(state.connection_id)
            if connection is None or connection.tenant_id != state.tenant_id:
                raise RepositoryConflictError("sync_state_scope_denied")
            key = (state.connection_id, state.folder_ref)
            current = self.sync_states.get(key)
            if current is not None and current.subscription_ref != expected_subscription_ref:
                raise RepositoryConflictError("subscription_state_conflict")
            self.sync_states[key] = state
            self.cursors[key] = state.cursor_value
            return state

    async def save_policy(self, policy: MailAgentPolicy) -> MailAgentPolicy:
        async with self._lock:
            existing = self.policies.get(policy.policy_id)
            if existing and existing.revision >= policy.revision:
                raise RepositoryConflictError("policy_revision_conflict")
            self.policies[policy.policy_id] = policy
            return policy

    async def get_policy(self, *, tenant_id: str, policy_id: UUID) -> MailAgentPolicy | None:
        async with self._lock:
            policy = self.policies.get(policy_id)
            return policy if policy and policy.tenant_id == tenant_id else None

    async def list_policies(
        self, *, tenant_id: str, owner_subject_id: str, limit: int = 50
    ) -> tuple[MailAgentPolicy, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit_invalid")
        async with self._lock:
            return tuple(
                sorted(
                    (
                        policy
                        for policy in self.policies.values()
                        if policy.tenant_id == tenant_id
                        and policy.owner_subject_id == owner_subject_id
                    ),
                    key=lambda item: item.valid_from,
                    reverse=True,
                )[:limit]
            )

    async def save_grant(self, grant: DelegationGrant) -> DelegationGrant:
        async with self._lock:
            existing = self.grants.get(grant.grant_id)
            if existing and existing.revision >= grant.revision:
                raise RepositoryConflictError("grant_revision_conflict")
            self.grants[grant.grant_id] = grant
            return grant

    async def get_grant(self, *, tenant_id: str, grant_id: UUID) -> DelegationGrant | None:
        async with self._lock:
            grant = self.grants.get(grant_id)
            return grant if grant and grant.tenant_id == tenant_id else None

    async def list_grants(
        self, *, tenant_id: str, granted_by_subject_id: str, limit: int = 50
    ) -> tuple[DelegationGrant, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit_invalid")
        async with self._lock:
            return tuple(
                sorted(
                    (
                        grant
                        for grant in self.grants.values()
                        if grant.tenant_id == tenant_id
                        and grant.granted_by_subject_id == granted_by_subject_id
                    ),
                    key=lambda item: item.granted_at,
                    reverse=True,
                )[:limit]
            )

    async def save_draft(self, draft: MailDraft) -> MailDraft:
        async with self._lock:
            existing = self.drafts.get(draft.draft_id)
            if existing and existing.revision >= draft.revision:
                raise RepositoryConflictError("draft_revision_conflict")
            self.drafts[draft.draft_id] = draft
            return draft

    async def get_draft(self, *, tenant_id: str, draft_id: UUID) -> MailDraft | None:
        async with self._lock:
            draft = self.drafts.get(draft_id)
            return draft if draft and draft.tenant_id == tenant_id else None

    async def list_drafts(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 5000,
    ) -> tuple[MailDraft, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("limit_invalid")
        async with self._lock:
            allowed_connections = {
                item.connection_id
                for item in self.connections.values()
                if item.tenant_id == tenant_id and item.subject_id == subject_id
            }
            if connection_id is not None:
                allowed_connections &= {connection_id}
            return tuple(
                sorted(
                    (
                        draft
                        for draft in self.drafts.values()
                        if draft.tenant_id == tenant_id
                        and draft.connection_id in allowed_connections
                    ),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )[:limit]
            )

    async def list_operations(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 5000,
    ) -> tuple[MailOutboxOperation, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("limit_invalid")
        async with self._lock:
            allowed_connections = {
                item.connection_id
                for item in self.connections.values()
                if item.tenant_id == tenant_id and item.subject_id == subject_id
            }
            if connection_id is not None:
                allowed_connections &= {connection_id}
            return tuple(
                sorted(
                    (
                        operation
                        for operation in self.operations.values()
                        if operation.tenant_id == tenant_id
                        and operation.connection_id in allowed_connections
                    ),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )[:limit]
            )

    async def create_or_get_operation(
        self,
        operation: MailOutboxOperation,
    ) -> tuple[MailOutboxOperation, bool]:
        async with self._lock:
            key = (operation.connection_id, operation.idempotency_key)
            existing_id = self._operation_idempotency.get(key)
            if existing_id is not None:
                return self.operations[existing_id], False
            self.operations[operation.operation_id] = operation
            self._operation_idempotency[key] = operation.operation_id
            return operation, True

    async def get_operation(
        self, *, tenant_id: str, operation_id: UUID
    ) -> MailOutboxOperation | None:
        async with self._lock:
            operation = self.operations.get(operation_id)
            return operation if operation and operation.tenant_id == tenant_id else None

    async def count_operations_since(
        self, *, tenant_id: str, subject_id: str, since: datetime
    ) -> int:
        threshold = since.astimezone(UTC)
        async with self._lock:
            return sum(
                1
                for operation in self.operations.values()
                if operation.tenant_id == tenant_id
                and operation.subject_id == subject_id
                and operation.created_at >= threshold
            )

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
        async with self._lock:
            operation = self.operations.get(operation_id)
            if operation is None or operation.tenant_id != tenant_id:
                raise RepositoryConflictError("operation_not_found")
            if operation.status in {DeliveryStatus.LEASED, DeliveryStatus.SENDING} and (
                operation.lease_expires_at is None or operation.lease_expires_at <= current_time
            ):
                transition_delivery(operation.status, DeliveryStatus.OUTCOME_UNKNOWN)
                self.operations[operation_id] = replace(
                    operation,
                    status=DeliveryStatus.OUTCOME_UNKNOWN,
                    lease_owner=None,
                    lease_expires_at=None,
                    error_code="lease_expired_outcome_unknown",
                    updated_at=current_time,
                )
                self.audits.append(
                    redact_event(
                        {
                            "event_type": "mail.outbox.lease_expired",
                            "tenant_id": tenant_id,
                            "subject_id": operation.subject_id,
                            "target_ref": str(operation_id),
                            "occurred_at": current_time.isoformat(),
                            "previous_status": operation.status.value,
                            "reason": "lease_expired_outcome_unknown",
                        }
                    )
                )
                raise RepositoryConflictError("operation_outcome_unknown_reconciliation_required")
            if operation.status not in {DeliveryStatus.QUEUED, DeliveryStatus.RETRY_WAIT}:
                raise RepositoryConflictError("operation_not_leaseable")
            if operation.next_attempt_at and operation.next_attempt_at > current_time:
                raise RepositoryConflictError("operation_not_due")
            target = DeliveryStatus.LEASED
            transition_delivery(operation.status, target)
            updated = replace(
                operation,
                status=target,
                lease_owner=worker_id,
                lease_expires_at=current_time + timedelta(seconds=lease_seconds),
                fencing_token=operation.fencing_token + 1,
                updated_at=current_time,
            )
            self.operations[operation_id] = updated
            return updated

    async def mark_sending(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
        fencing_token: int,
    ) -> MailOutboxOperation:
        async with self._lock:
            operation = self._owned_operation(tenant_id, operation_id, worker_id, fencing_token)
            transition_delivery(operation.status, DeliveryStatus.SENDING)
            updated = replace(operation, status=DeliveryStatus.SENDING, updated_at=utc_now())
            self.operations[operation_id] = updated
            return updated

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
        async with self._lock:
            operation = self._owned_operation(tenant_id, operation_id, worker_id, fencing_token)
            transition_delivery(operation.status, status)
            updated = replace(
                operation,
                status=status,
                attempt_count=operation.attempt_count + 1,
                lease_owner=None,
                lease_expires_at=None,
                provider_message_ref=provider_message_ref,
                provider_request_id=provider_request_id,
                error_code=error_code,
                next_attempt_at=next_attempt_at,
                updated_at=utc_now(),
            )
            self.operations[operation_id] = updated
            return updated

    async def append_audit(self, event: Mapping[str, object]) -> None:
        async with self._lock:
            self.audits.append(redact_event(event))

    async def list_audit_events(
        self, *, tenant_id: str, subject_id: str, limit: int = 100
    ) -> tuple[Mapping[str, object], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("limit_invalid")
        async with self._lock:
            values = [
                event
                for event in self.audits
                if str(event.get("tenant_id", "")) == tenant_id
                and str(event.get("subject_id", "")) == subject_id
            ]
            return tuple(values[-limit:][::-1])

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
        async with self._lock:
            operation = self.operations.get(operation_id)
            if operation is None or operation.tenant_id != tenant_id:
                raise RepositoryConflictError("operation_not_found")
            transition_delivery(operation.status, status)
            if operation.status is not DeliveryStatus.OUTCOME_UNKNOWN:
                raise RepositoryConflictError("operation_not_reconcilable")
            updated = replace(
                operation,
                status=status,
                lease_owner=None,
                lease_expires_at=None,
                provider_message_ref=provider_message_ref or operation.provider_message_ref,
                provider_request_id=provider_request_id or operation.provider_request_id,
                error_code=error_code,
                next_attempt_at=next_attempt_at,
                updated_at=utc_now(),
            )
            self.operations[operation_id] = updated
            return updated

    async def create_or_get_sync_job(self, job: MailSyncJob) -> tuple[MailSyncJob, bool]:
        async with self._lock:
            key = (job.connection_id, job.idempotency_key)
            existing_id = self._sync_job_idempotency.get(key)
            if existing_id is not None:
                return self.sync_jobs[existing_id], False
            self.sync_jobs[job.job_id] = job
            self._sync_job_idempotency[key] = job.job_id
            return job, True

    async def get_sync_job(self, *, tenant_id: str, job_id: UUID) -> MailSyncJob | None:
        async with self._lock:
            job = self.sync_jobs.get(job_id)
            return job if job and job.tenant_id == tenant_id else None

    async def list_sync_jobs(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MailSyncJob, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit_invalid")
        async with self._lock:
            return tuple(
                sorted(
                    (
                        job
                        for job in self.sync_jobs.values()
                        if job.tenant_id == tenant_id
                        and job.subject_id == subject_id
                        and (connection_id is None or job.connection_id == connection_id)
                    ),
                    key=lambda item: item.created_at,
                    reverse=True,
                )[:limit]
            )

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
        async with self._lock:
            job = self.sync_jobs.get(job_id)
            if job is None or job.tenant_id != tenant_id:
                raise RepositoryConflictError("sync_job_not_found")
            current_time = (now or utc_now()).astimezone(UTC)
            if job.status is SyncJobStatus.RUNNING and (
                job.lease_expires_at is None or job.lease_expires_at <= current_time
            ):
                transition_sync_job(job.status, SyncJobStatus.QUEUED)
                self.sync_jobs[job_id] = job = replace(
                    job,
                    status=SyncJobStatus.QUEUED,
                    error_code="lease_expired_requeued",
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=None,
                    updated_at=current_time,
                )
                self.audits.append(
                    redact_event(
                        {
                            "event_type": "mail.sync_job.lease_expired",
                            "tenant_id": tenant_id,
                            "subject_id": job.subject_id,
                            "target_ref": str(job_id),
                            "occurred_at": current_time.isoformat(),
                            "previous_status": SyncJobStatus.RUNNING.value,
                            "reason": "lease_expired_requeued",
                        }
                    )
                )
            if job.status is SyncJobStatus.CANCELLING and (
                job.lease_expires_at is None or job.lease_expires_at <= current_time
            ):
                transition_sync_job(job.status, SyncJobStatus.CANCELLED)
                self.sync_jobs[job_id] = job = replace(
                    job,
                    status=SyncJobStatus.CANCELLED,
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=current_time,
                    updated_at=current_time,
                )
                self.audits.append(
                    redact_event(
                        {
                            "event_type": "mail.sync_job.cancelled_after_lease_expiry",
                            "tenant_id": tenant_id,
                            "subject_id": job.subject_id,
                            "target_ref": str(job_id),
                            "occurred_at": current_time.isoformat(),
                            "previous_status": SyncJobStatus.CANCELLING.value,
                            "reason": "cancel_request_finalized_after_worker_lease_expiry",
                        }
                    )
                )
            if job.status is SyncJobStatus.RETRY_WAIT:
                if job.next_attempt_at is not None and job.next_attempt_at > current_time:
                    raise RepositoryConflictError("sync_job_not_due")
                transition_sync_job(job.status, SyncJobStatus.QUEUED)
                self.sync_jobs[job_id] = job = replace(
                    job,
                    status=SyncJobStatus.QUEUED,
                    next_attempt_at=None,
                    finished_at=None,
                    updated_at=current_time,
                )
                self.audits.append(
                    redact_event(
                        {
                            "event_type": "mail.sync_job.retry_due",
                            "tenant_id": tenant_id,
                            "subject_id": job.subject_id,
                            "target_ref": str(job_id),
                            "occurred_at": current_time.isoformat(),
                            "attempt_count": job.attempt_count,
                        }
                    )
                )
            if job.status is not SyncJobStatus.QUEUED:
                raise RepositoryConflictError("sync_job_not_claimable")
            transition_sync_job(job.status, SyncJobStatus.RUNNING)
            updated = replace(
                job,
                status=SyncJobStatus.RUNNING,
                lease_owner=worker_id,
                lease_expires_at=current_time + timedelta(seconds=lease_seconds),
                fencing_token=job.fencing_token + 1,
                error_code=None,
                next_attempt_at=None,
                started_at=current_time,
                updated_at=current_time,
            )
            self.sync_jobs[job_id] = updated
            return updated

    async def cancel_sync_job(self, *, tenant_id: str, job_id: UUID) -> MailSyncJob:
        async with self._lock:
            job = self.sync_jobs.get(job_id)
            if job is None or job.tenant_id != tenant_id:
                raise RepositoryConflictError("sync_job_not_found")
            now = utc_now()
            if job.status is SyncJobStatus.CANCELLING:
                return job
            if job.status is SyncJobStatus.RUNNING:
                transition_sync_job(job.status, SyncJobStatus.CANCELLING)
                updated = replace(
                    job,
                    status=SyncJobStatus.CANCELLING,
                    error_code="sync_cancel_requested",
                    updated_at=now,
                )
                self.sync_jobs[job_id] = updated
                return updated
            if job.status is SyncJobStatus.RETRY_WAIT:
                transition_sync_job(job.status, SyncJobStatus.CANCELLED)
                updated = replace(
                    job,
                    status=SyncJobStatus.CANCELLED,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=now,
                    updated_at=now,
                )
                self.sync_jobs[job_id] = updated
                return updated
            transition_sync_job(job.status, SyncJobStatus.CANCELLED)
            updated = replace(
                job,
                status=SyncJobStatus.CANCELLED,
                lease_owner=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            )
            self.sync_jobs[job_id] = updated
            return updated

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
        async with self._lock:
            job = self.sync_jobs.get(job_id)
            if (
                job is None
                or job.tenant_id != tenant_id
                or job.lease_owner != worker_id
                or job.fencing_token != fencing_token
            ):
                raise RepositoryConflictError("sync_job_lease_fenced")
            transition_sync_job(job.status, status)
            now = utc_now()
            retrying = status is SyncJobStatus.RETRY_WAIT
            updated = replace(
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
                attempt_count=job.attempt_count + int(increment_attempt),
                next_attempt_at=next_attempt_at if retrying else None,
                lease_owner=None,
                lease_expires_at=None,
                finished_at=None if retrying else now,
                updated_at=now,
            )
            self.sync_jobs[job_id] = updated
            return updated

    async def create_or_get_autonomy_run(
        self, run: MailAutonomyRun
    ) -> tuple[MailAutonomyRun, bool]:
        async with self._lock:
            key = (run.tenant_id, run.subject_id, run.connection_id, run.replay_key)
            existing_id = self._autonomy_run_idempotency.get(key)
            if existing_id is not None:
                return self.autonomy_runs[existing_id], False
            self.autonomy_runs[run.run_id] = run
            self._autonomy_run_idempotency[key] = run.run_id
            return run, True

    async def get_autonomy_run(self, *, tenant_id: str, run_id: UUID) -> MailAutonomyRun | None:
        async with self._lock:
            run = self.autonomy_runs.get(run_id)
            return run if run is not None and run.tenant_id == tenant_id else None

    async def list_autonomy_runs(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MailAutonomyRun, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit_invalid")
        async with self._lock:
            return tuple(
                sorted(
                    (
                        run
                        for run in self.autonomy_runs.values()
                        if run.tenant_id == tenant_id
                        and run.subject_id == subject_id
                        and (connection_id is None or run.connection_id == connection_id)
                    ),
                    key=lambda item: item.created_at,
                    reverse=True,
                )[:limit]
            )

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
        async with self._lock:
            run = self.autonomy_runs.get(run_id)
            if run is None or run.tenant_id != tenant_id:
                raise RepositoryConflictError("autonomy_run_not_found")
            current_time = (now or utc_now()).astimezone(UTC)
            if run.status is AutonomyRunStatus.RUNNING and (
                run.lease_expires_at is None or run.lease_expires_at <= current_time
            ):
                transition_autonomy_run(run.status, AutonomyRunStatus.QUEUED)
                self.autonomy_runs[run_id] = run = replace(
                    run,
                    status=AutonomyRunStatus.QUEUED,
                    error_code="lease_expired_requeued",
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=None,
                    revision=run.revision + 1,
                    updated_at=current_time,
                )
                self.audits.append(
                    redact_event(
                        {
                            "event_type": "mail.agent.autonomy.lease_expired",
                            "tenant_id": tenant_id,
                            "subject_id": run.subject_id,
                            "target_ref": str(run_id),
                            "occurred_at": current_time.isoformat(),
                            "previous_status": AutonomyRunStatus.RUNNING.value,
                            "reason": "lease_expired_requeued",
                        }
                    )
                )
            if run.status is not AutonomyRunStatus.QUEUED:
                raise RepositoryConflictError("autonomy_run_not_claimable")
            transition_autonomy_run(run.status, AutonomyRunStatus.RUNNING)
            updated = replace(
                run,
                status=AutonomyRunStatus.RUNNING,
                lease_owner=worker_id,
                lease_expires_at=current_time + timedelta(seconds=lease_seconds),
                fencing_token=run.fencing_token + 1,
                error_code=None,
                started_at=current_time,
                pause_reason=None,
                revision=run.revision + 1,
                updated_at=current_time,
            )
            self.autonomy_runs[run_id] = updated
            return updated

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
        async with self._lock:
            run = self.autonomy_runs.get(run_id)
            if run is None or run.tenant_id != tenant_id:
                raise RepositoryConflictError("autonomy_run_not_found")
            if worker_id is not None and (
                run.status is not AutonomyRunStatus.RUNNING
                or run.lease_owner != worker_id
                or run.fencing_token != fencing_token
            ):
                raise RepositoryConflictError("autonomy_run_lease_fenced")
            transition_autonomy_run(run.status, AutonomyRunStatus.PAUSED)
            now = utc_now()
            updated = replace(
                run,
                status=AutonomyRunStatus.PAUSED,
                pause_reason=reason.strip(),
                lease_owner=None,
                lease_expires_at=None,
                revision=run.revision + 1,
                updated_at=now,
            )
            self.autonomy_runs[run_id] = updated
            return updated

    async def resume_autonomy_run(self, *, tenant_id: str, run_id: UUID) -> MailAutonomyRun:
        async with self._lock:
            run = self.autonomy_runs.get(run_id)
            if run is None or run.tenant_id != tenant_id:
                raise RepositoryConflictError("autonomy_run_not_found")
            transition_autonomy_run(run.status, AutonomyRunStatus.QUEUED)
            now = utc_now()
            updated = replace(
                run,
                status=AutonomyRunStatus.QUEUED,
                pause_reason=None,
                lease_owner=None,
                lease_expires_at=None,
                revision=run.revision + 1,
                updated_at=now,
            )
            self.autonomy_runs[run_id] = updated
            return updated

    async def cancel_autonomy_run(self, *, tenant_id: str, run_id: UUID) -> MailAutonomyRun:
        async with self._lock:
            run = self.autonomy_runs.get(run_id)
            if run is None or run.tenant_id != tenant_id:
                raise RepositoryConflictError("autonomy_run_not_found")
            transition_autonomy_run(run.status, AutonomyRunStatus.CANCELLED)
            now = utc_now()
            updated = replace(
                run,
                status=AutonomyRunStatus.CANCELLED,
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
                revision=run.revision + 1,
                updated_at=now,
            )
            self.autonomy_runs[run_id] = updated
            return updated

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
        async with self._lock:
            run = self.autonomy_runs.get(run_id)
            if (
                run is None
                or run.tenant_id != tenant_id
                or run.lease_owner != worker_id
                or run.fencing_token != fencing_token
            ):
                raise RepositoryConflictError("autonomy_run_lease_fenced")
            transition_autonomy_run(run.status, status)
            now = utc_now()
            updated = replace(
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
            self.autonomy_runs[run_id] = updated
            return updated

    async def save_webhook_receipt(self, receipt: Mapping[str, object]) -> bool:
        provider = str(receipt.get("provider", ""))
        event_id = str(receipt.get("event_id", ""))
        tenant_id = str(receipt.get("tenant_id", ""))
        if not provider or not event_id or not tenant_id:
            raise ValueError("webhook_receipt_identity_missing")
        raw_route_metadata = receipt.get("route_metadata")
        if raw_route_metadata is not None and not isinstance(raw_route_metadata, Mapping):
            raise ValueError("webhook_receipt_route_metadata_invalid")
        normalized_route = normalize_receipt_route_metadata(raw_route_metadata)
        async with self._lock:
            key = (provider, tenant_id, event_id)
            if key in self.webhook_receipts:
                return False
            stored = dict(receipt)
            stored["route_metadata"] = normalized_route
            self.webhook_receipts[key] = stored
            return True

    async def list_webhook_receipts(
        self, *, tenant_id: str, limit: int = 100
    ) -> tuple[Mapping[str, object], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("limit_invalid")
        async with self._lock:
            values = [
                receipt
                for (provider, receipt_tenant, _event_id), receipt in self.webhook_receipts.items()
                if receipt_tenant == tenant_id
            ]
            return tuple(values[-limit:][::-1])

    async def get_webhook_receipt(
        self, *, tenant_id: str, provider: str, event_id: str
    ) -> Mapping[str, object] | None:
        if not tenant_id.strip() or not provider.strip() or not event_id.strip():
            raise ValueError("webhook_receipt_identity_missing")
        async with self._lock:
            receipt = self.webhook_receipts.get((provider, tenant_id, event_id))
            return dict(receipt) if receipt is not None else None

    async def save_rule(self, rule: MailRule) -> MailRule:
        async with self._lock:
            existing = self.rules.get(rule.rule_id)
            if existing is not None and (
                existing.tenant_id != rule.tenant_id or existing.version >= rule.version
            ):
                raise RepositoryConflictError("rule_revision_conflict")
            self.rules[rule.rule_id] = rule
            return rule

    async def get_rule(self, *, tenant_id: str, rule_id: UUID) -> MailRule | None:
        async with self._lock:
            rule = self.rules.get(rule_id)
            return rule if rule is not None and rule.tenant_id == tenant_id else None

    async def list_rules(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> tuple[MailRule, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit_invalid")
        async with self._lock:
            return tuple(
                sorted(
                    (
                        rule
                        for rule in self.rules.values()
                        if rule.tenant_id == tenant_id and rule.owner_subject_id == subject_id
                    ),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )[:limit]
            )

    async def delete_connection_data(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID
    ) -> Mapping[str, object]:
        """Delete connection-owned projections while retaining the tombstone/audit trail."""

        async with self._lock:
            connection = self.connections.get(connection_id)
            if (
                connection is None
                or connection.tenant_id != tenant_id
                or connection.subject_id != subject_id
            ):
                raise RepositoryConflictError("connection_not_found")
            message_ids = {
                message.message_id
                for message in self.messages.values()
                if message.connection_id == connection_id
            }
            thread_ids = {
                thread.thread_id
                for thread in self.threads.values()
                if thread.connection_id == connection_id
            }
            candidate_ids = {
                candidate_id
                for candidate_id, candidate in self.candidates.items()
                if candidate.message_id in message_ids
            }
            draft_ids = {
                draft_id
                for draft_id, draft in self.drafts.items()
                if draft.connection_id == connection_id
            }
            operation_ids = {
                operation_id
                for operation_id, operation in self.operations.items()
                if operation.connection_id == connection_id
            }
            job_ids = {
                job_id
                for job_id, job in self.sync_jobs.items()
                if job.connection_id == connection_id
            }
            execution_ids = {
                execution_id
                for execution_id, execution in self.rule_executions.items()
                if execution.message_id in message_ids
            }
            for candidate_id in candidate_ids:
                self.candidates.pop(candidate_id, None)
            for message_id in message_ids:
                self.messages.pop(message_id, None)
            self._message_identity = {
                key: value
                for key, value in self._message_identity.items()
                if value not in message_ids
            }
            for thread_id in thread_ids:
                self.threads.pop(thread_id, None)
            for key in tuple(self.cursors):
                if key[0] == connection_id:
                    self.cursors.pop(key, None)
            for key in tuple(self.sync_states):
                if key[0] == connection_id:
                    self.sync_states.pop(key, None)
            for draft_id in draft_ids:
                self.drafts.pop(draft_id, None)
            for operation_id in operation_ids:
                self.operations.pop(operation_id, None)
            self._operation_idempotency = {
                key: value
                for key, value in self._operation_idempotency.items()
                if value not in operation_ids
            }
            for job_id in job_ids:
                self.sync_jobs.pop(job_id, None)
            self._sync_job_idempotency = {
                key: value
                for key, value in self._sync_job_idempotency.items()
                if value not in job_ids
            }
            for execution_id in execution_ids:
                self.rule_executions.pop(execution_id, None)
            return {
                "connection_id": str(connection_id),
                "candidates_deleted": len(candidate_ids),
                "messages_deleted": len(message_ids),
                "threads_deleted": len(thread_ids),
                "drafts_deleted": len(draft_ids),
                "operations_deleted": len(operation_ids),
                "sync_jobs_deleted": len(job_ids),
                "rule_executions_deleted": len(execution_ids),
            }

    async def create_or_get_rule_execution(
        self, execution: RuleExecution
    ) -> tuple[RuleExecution, bool]:
        async with self._lock:
            existing = self.rule_executions.get(execution.execution_id)
            if existing is not None:
                if existing.tenant_id != execution.tenant_id:
                    raise RepositoryConflictError("rule_execution_scope_conflict")
                return existing, False
            self.rule_executions[execution.execution_id] = execution
            return execution, True

    async def save_rule_execution(self, execution: RuleExecution) -> RuleExecution:
        async with self._lock:
            existing = self.rule_executions.get(execution.execution_id)
            if existing is not None and (
                existing.tenant_id != execution.tenant_id
                or existing.subject_id != execution.subject_id
                or existing.revision >= execution.revision
            ):
                if existing == execution:
                    return existing
                raise RepositoryConflictError("rule_execution_revision_conflict")
            self.rule_executions[execution.execution_id] = execution
            return execution

    async def list_rule_executions(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[RuleExecution, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("limit_invalid")
        async with self._lock:
            return tuple(
                sorted(
                    (
                        execution
                        for execution in self.rule_executions.values()
                        if execution.tenant_id == tenant_id
                        and execution.subject_id == subject_id
                        and (rule_id is None or execution.rule_id == rule_id)
                    ),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )[:limit]
            )

    async def count_rule_executions_since(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID,
        since: datetime,
    ) -> int:
        threshold = since.astimezone(UTC)
        async with self._lock:
            return sum(
                1
                for execution in self.rule_executions.values()
                if execution.tenant_id == tenant_id
                and execution.subject_id == subject_id
                and execution.rule_id == rule_id
                and execution.status.value == "executed"
                and execution.updated_at >= threshold
            )

    def _owned_operation(
        self,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
        fencing_token: int,
    ) -> MailOutboxOperation:
        operation = self.operations.get(operation_id)
        if operation is None or operation.tenant_id != tenant_id:
            raise RepositoryConflictError("operation_not_found")
        if operation.lease_owner != worker_id or operation.fencing_token != fencing_token:
            raise RepositoryConflictError("stale_fencing_token")
        return operation
