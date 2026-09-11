"""Ports separating MailHub policy/core from providers and host products."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from mailhub.domain import (
    AgentActionRequest,
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
    PolicyDecision,
    ProviderName,
    SyncJobStatus,
)
from mailhub.memory import ApprovedKnowledgeReference
from mailhub.notifications import ProviderNotification
from mailhub.rules import MailRule, RuleExecution


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider: ProviderName
    authorization_modes: tuple[str, ...]
    supports_push: bool
    supports_incremental: bool
    supports_backfill: bool
    supports_draft: bool
    supports_send: bool
    supports_labels: bool
    supports_attachments: bool
    supports_search: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    provider_message_ref: str
    provider_thread_ref: str
    internet_message_id: str | None
    sender_address: str
    recipient_addresses: tuple[str, ...]
    subject: str
    received_at: datetime
    body_text: str | None
    body_object_ref: str | None
    content_sha256: str
    labels: tuple[str, ...] = ()
    folder_ref: str = "INBOX"
    # Provider metadata is deliberately bounded and never carries raw MIME.
    # These fields power safe inbox filters without hydrating message bodies.
    is_read: bool = False
    attachment_count: int = 0
    # Bounded provider facts only; raw headers/MIME/body and credentials are
    # rejected when the application materializes the durable projection.
    provider_metadata: Mapping[str, str] = field(default_factory=dict)
    # Header recipients are normalized address projections.  They intentionally
    # do not preserve display names or raw header syntax.  Bcc is only present
    # when the provider exposes it to the connected mailbox.
    cc_addresses: tuple[str, ...] = ()
    bcc_addresses: tuple[str, ...] = ()
    reply_to_addresses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderSendRequest:
    operation_id: UUID
    connection_id: UUID
    thread_ref: str | None
    recipient_addresses: tuple[str, ...]
    subject: str
    body_text: str
    content_sha256: str
    idempotency_key: str
    # Keep the pre-v1 optional positional slot stable; new recipient classes
    # are additive keyword fields after it.
    in_reply_to_message_ref: str | None = None
    cc_addresses: tuple[str, ...] = ()
    bcc_addresses: tuple[str, ...] = ()
    attachment_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderSendReceipt:
    provider_message_ref: str
    provider_thread_ref: str | None
    accepted_at: datetime
    provider_request_id: str
    idempotency_key: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ProviderSyncPage:
    messages: tuple[ProviderMessage, ...]
    next_cursor: str | None
    provider_cursor_kind: str
    provider_request_id: str
    reset_required: bool = False
    # Incremental APIs can report a message deletion without a fetchable
    # message body.  Keep those refs separate from normalized messages so the
    # application can remove its governed projection before committing the
    # provider cursor.
    deleted_message_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.deleted_message_refs) > 1000:
            raise ValueError("provider_deleted_message_refs_too_many")
        normalized: list[str] = []
        for value in self.deleted_message_refs:
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 1000
                or any(ord(char) < 33 or ord(char) == 127 for char in value)
            ):
                raise ValueError("provider_deleted_message_ref_invalid")
            ref = value.strip()
            if ref not in normalized:
                normalized.append(ref)
        object.__setattr__(self, "deleted_message_refs", tuple(normalized))


@dataclass(frozen=True, slots=True)
class ProviderSyncFilter:
    """Bounded, provider-neutral filter for a finite backfill.

    The filter is intentionally narrower than a provider search language.  It
    can be persisted with a durable job and translated by each connector
    without allowing arbitrary Gmail query or Graph OData fragments to cross
    the application boundary.
    """

    folder_ref: str = "INBOX"
    label_refs: tuple[str, ...] = ()
    received_after: datetime | None = None
    received_before: datetime | None = None

    def __post_init__(self) -> None:
        folder = _bounded_sync_filter_text(self.folder_ref, "folder_ref", 200)
        object.__setattr__(self, "folder_ref", folder)
        if len(self.label_refs) > 20:
            raise ValueError("sync_filter_label_limit")
        labels: list[str] = []
        for value in self.label_refs:
            label = _bounded_sync_filter_text(value, "label_ref", 200)
            if label not in labels:
                labels.append(label)
        object.__setattr__(self, "label_refs", tuple(labels))
        after = _sync_filter_datetime(self.received_after, "received_after")
        before = _sync_filter_datetime(self.received_before, "received_before")
        if after is not None and before is not None and after >= before:
            raise ValueError("sync_filter_date_range_invalid")
        object.__setattr__(self, "received_after", after)
        object.__setattr__(self, "received_before", before)

    @property
    def is_bounded(self) -> bool:
        return bool(
            self.label_refs
            or self.received_after is not None
            or self.received_before is not None
            or self.folder_ref.casefold() != "inbox"
        )


def _bounded_sync_filter_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"sync_filter_{name}_invalid")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError(f"sync_filter_{name}_invalid")
    return value.strip()


def _sync_filter_datetime(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"sync_filter_{name}_timezone_required")
    return value.astimezone(UTC)


class CredentialBrokerPort(Protocol):
    async def resolve(
        self, *, credential_ref: str, tenant_id: str, subject_id: str
    ) -> Mapping[str, str]:
        """Return short-lived provider material; never persist or log it."""


class CredentialRefreshPort(Protocol):
    """Host-owned refresh contract returning metadata only.

    The host may refresh an OAuth grant or rotate its Secret Manager version,
    but MailHub receives no access/refresh token.  Returned identity/version
    metadata is used to fence the existing connection.
    """

    async def refresh(
        self,
        *,
        credential_ref: str,
        tenant_id: str,
        subject_id: str,
        reason: str,
    ) -> Mapping[str, str]: ...


class CredentialRevocationPort(Protocol):
    """Host-owned credential deletion/revocation contract.

    MailHub never receives or stores the secret.  A production implementation
    must revoke the provider grant and delete the Secret Manager version before
    the connection is marked deleted.
    """

    async def revoke(
        self, *, credential_ref: str, tenant_id: str, subject_id: str
    ) -> Mapping[str, object]: ...


class ProviderConnector(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def sync(
        self,
        connection: MailboxConnection,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage: ...

    async def send(
        self, request: ProviderSendRequest, *, credential: Mapping[str, str]
    ) -> ProviderSendReceipt: ...

    async def health_check(self, connection: MailboxConnection) -> Mapping[str, object]: ...


class HostIdentityPort(Protocol):
    async def authorize(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        capability: str,
        connection_id: UUID | None = None,
    ) -> bool: ...


class KillSwitchPort(Protocol):
    """Host-owned runtime kill-switch decision service.

    The host is the authority for global, tenant and provider switches and for
    the four-eyes approval/audit workflow that changes them.  MailHub only
    evaluates the bounded decision immediately before a side effect; it never
    stores switch state or accepts a switch mutation from mail content.
    """

    async def check(
        self,
        *,
        tenant_id: str,
        subject_id: str | None,
        provider: ProviderName | None,
        operation: str,
    ) -> Mapping[str, object]: ...


class ApprovalPort(Protocol):
    async def require_confirmation(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        action: AgentActionRequest,
        decision: PolicyDecision,
    ) -> str: ...

    async def verify_confirmation(
        self,
        *,
        confirmation_ref: str,
        action: AgentActionRequest,
        approver_subject_id: str | None = None,
        revalidation: bool = False,
    ) -> bool:
        """Answer whether ``action`` was approved under ``confirmation_ref``.

        ``require_confirmation`` records the principal that asked for the
        confirmation.  ``approver_subject_id`` is the principal presenting it
        now, so a host that promises separation of duties can refuse when the
        two are the same.  It is optional: hosts that have not adopted the
        stricter policy keep working unchanged, and the conformance kit reports
        their separation of duties as unproven rather than as passed.

        ``revalidation=True`` marks the pre-send re-check of an approval that
        was already exercised, as opposed to presenting a fresh one.  The
        approver identity is still supplied so the host can re-assert
        separation of duties on the same principal rather than lose track of who
        approved once the confirmation has been consumed.
        """
        ...


@dataclass(frozen=True, slots=True)
class OutboundObservation:
    """What a reconciliation probe could establish about one outbound message.

    ``found`` is deliberately three-state.  ``None`` means the probe could not
    answer -- an unreachable mailbox, a folder the provider does not expose, a
    search that errored.  A probe that cannot answer must never be read as "not
    sent": the caller keeps the operation in OUTCOME_UNKNOWN instead of
    retrying a message that may already be on its way.
    """

    found: bool | None
    provider_message_ref: str | None = None
    mailbox: str | None = None
    detail: str = ""


class OutboundReconciliationPort(Protocol):
    """Answers whether an outbound message reached the mailbox.

    Only consulted for operations whose outcome is unknown, so an
    implementation may assume the send was already attempted.
    """

    async def observe_outbound(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        internet_message_id: str,
    ) -> OutboundObservation: ...


class HostActionPort(Protocol):
    async def discover(
        self, *, tenant_id: str, subject_id: str, query: str | None = None
    ) -> Sequence[Mapping[str, object]]: ...

    async def propose(self, *, action: AgentActionRequest) -> Mapping[str, object]: ...

    async def execute(
        self, *, action: AgentActionRequest, approval_ref: str
    ) -> Mapping[str, object]: ...


class KnowledgeSinkPort(Protocol):
    async def submit_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        message: MailMessageProjection,
        candidate: MailActionCandidate,
    ) -> Mapping[str, object]: ...


class KnowledgeLifecyclePort(Protocol):
    """Host-owned revoke/review/reindex contract for source lifecycle changes.

    The request contains only governed identifiers and digests.  A host must
    revoke or quarantine downstream knowledge before MailHub reports a source
    deletion as complete; raw message content never crosses this boundary.
    """

    async def revoke_source(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        message_ids: Sequence[UUID],
        reason: str,
        request_id: str,
    ) -> Mapping[str, object]: ...


class AgentMemoryPort(Protocol):
    """Host-owned memory index accepting approved references only."""

    async def store_approved_reference(
        self, *, reference: ApprovedKnowledgeReference
    ) -> Mapping[str, object]: ...


class KnowledgeSafetyPort(Protocol):
    """Host-owned AV/DLP/rights gate for governed knowledge candidates."""

    async def evaluate_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        message: MailMessageProjection,
        candidate: MailActionCandidate,
    ) -> Mapping[str, object]: ...


class ObjectStorePort(Protocol):
    """Tenant-scoped encrypted storage for bounded message and draft content."""

    async def put_text(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        purpose: str,
        content: str,
        content_sha256: str,
        expires_at: datetime | None = None,
    ) -> str: ...

    async def get_text(self, *, tenant_id: str, subject_id: str, object_ref: str) -> str: ...

    async def delete(self, *, tenant_id: str, subject_id: str, object_ref: str) -> None: ...


class ObjectStoreLifecyclePort(Protocol):
    """Optional lifecycle contract implemented by production object stores."""

    async def purge_expired(
        self, *, now: datetime | None = None, limit: int = 500
    ) -> tuple[Mapping[str, object], ...]: ...


class AiExecutionPort(Protocol):
    async def structure(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        operation: str,
        source: Mapping[str, object],
        schema: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class AuditPort(Protocol):
    async def append_audit(self, event: Mapping[str, object]) -> None: ...


class NotificationPort(Protocol):
    """Host-owned notification sink for bounded status/approval notices."""

    async def notify(self, notification: Mapping[str, object]) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderSubscriptionLease:
    """Non-secret provider subscription state returned by a host adapter."""

    provider: ProviderName
    connection_id: UUID
    subscription_ref: str
    status: str
    expires_at: datetime
    callback_endpoint: str
    provider_request_id: str | None = None
    client_state_ref: str | None = None


class ProviderSubscriptionPort(Protocol):
    """Host-owned watch/subscription lifecycle boundary.

    The host owns provider credentials, Pub/Sub/OIDC validation, Graph
    clientState/certificate validation and durable renewal. MailHub receives
    only bounded subscription metadata and an opaque client-state reference.
    """

    async def ensure_subscription(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        provider: ProviderName,
        callback_endpoint: str,
        desired_expiry: datetime,
        client_state_ref: str | None = None,
        idempotency_key: str,
    ) -> ProviderSubscriptionLease: ...

    async def cancel_subscription(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        provider: ProviderName,
        subscription_ref: str,
        request_id: str,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ProviderNotificationDelivery:
    """A host-verified, body-free route for one provider notification.

    The host remains responsible for provider authentication (Gmail Pub/Sub
    OIDC or Graph clientState/certificate), subscription/account lookup and
    tenant ownership.  MailHub receives only this bounded route plus the
    normalized notification metadata, so a public callback can never choose
    a tenant or connection by sending arbitrary headers.
    """

    tenant_id: str
    subject_id: str
    connection_id: UUID
    folder_ref: str
    notification: ProviderNotification

    def __post_init__(self) -> None:
        for value, field_name, maximum in (
            (self.tenant_id, "tenant_id", 200),
            (self.subject_id, "subject_id", 200),
            (self.folder_ref, "folder_ref", 512),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > maximum
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError(f"provider_notification_{field_name}_invalid")


class EventPublisherPort(Protocol):
    """Host-owned durable event bus boundary.

    MailHub builds and validates the metadata-only envelope; the host owns
    persistence, delivery, replay and subscriber authorization.
    """

    async def publish(self, event: Mapping[str, object]) -> None: ...


class ProviderNotificationVerifierPort(Protocol):
    """Host-owned verification and connection routing boundary.

    Implementations must validate the provider request before returning.  A
    returned delivery contains no raw body, bearer token, Graph clientState,
    Pub/Sub OIDC claims or other secret.  MailHub uses it only to persist a
    receipt, advance the matching subscription watermark and enqueue a
    bounded incremental sync job.
    """

    async def verify_and_route(
        self,
        *,
        provider: ProviderName,
        body: bytes,
        headers: Mapping[str, str],
    ) -> tuple[ProviderNotificationDelivery, ...]: ...


class WebhookReceiptStorePort(Protocol):
    """Durable append-only storage for verified and quarantined receipts."""

    async def save_webhook_receipt(self, receipt: Mapping[str, object]) -> bool: ...

    async def list_webhook_receipts(
        self, *, tenant_id: str, limit: int = 100
    ) -> tuple[Mapping[str, object], ...]: ...

    async def get_webhook_receipt(
        self, *, tenant_id: str, provider: str, event_id: str
    ) -> Mapping[str, object] | None: ...


class MailRepository(AuditPort, Protocol):
    async def save_connection(self, connection: MailboxConnection) -> MailboxConnection: ...

    async def list_connections(
        self, *, tenant_id: str, subject_id: str
    ) -> tuple[MailboxConnection, ...]: ...

    async def save_thread(self, thread: MailThread) -> MailThread: ...

    async def save_message(
        self, message: MailMessageProjection
    ) -> tuple[MailMessageProjection, bool]: ...

    async def delete_message_by_provider_ref(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        provider_message_ref: str,
    ) -> MailMessageProjection | None: ...

    async def get_connection(
        self, *, tenant_id: str, connection_id: UUID
    ) -> MailboxConnection | None: ...

    async def get_cursor(
        self, *, tenant_id: str, connection_id: UUID, folder_ref: str
    ) -> str | None: ...

    async def commit_cursor(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        folder_ref: str,
        expected_cursor: str | None,
        next_cursor: str | None,
    ) -> None: ...

    async def get_sync_state(
        self, *, tenant_id: str, connection_id: UUID, folder_ref: str
    ) -> MailboxSyncState | None: ...

    async def list_sync_states(
        self, *, tenant_id: str, connection_id: UUID
    ) -> tuple[MailboxSyncState, ...]: ...

    async def save_sync_state(
        self,
        state: MailboxSyncState,
        *,
        expected_subscription_ref: str | None = None,
    ) -> MailboxSyncState: ...

    async def list_messages(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        limit: int,
    ) -> tuple[MailMessageProjection, ...]: ...

    async def list_messages_for_connection(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID, limit: int = 5000
    ) -> tuple[MailMessageProjection, ...]: ...

    async def list_connection_cleanup_refs(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID
    ) -> ConnectionCleanupRefs: ...

    async def search_messages(
        self, *, tenant_id: str, subject_id: str, query: str, limit: int
    ) -> tuple[MailMessageProjection, ...]: ...

    async def list_threads(
        self, *, tenant_id: str, subject_id: str, limit: int
    ) -> tuple[MailThread, ...]: ...

    async def get_thread(self, *, tenant_id: str, thread_id: UUID) -> MailThread | None: ...

    async def list_thread_messages(
        self, *, tenant_id: str, subject_id: str, thread_id: UUID, limit: int = 200
    ) -> tuple[MailMessageProjection, ...]: ...

    async def get_message(
        self, *, tenant_id: str, message_id: UUID
    ) -> MailMessageProjection | None: ...

    async def save_candidate(self, candidate: MailActionCandidate) -> MailActionCandidate: ...

    async def list_candidates(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        status: CandidateState | None = None,
        candidate_type: CandidateType | None = None,
        limit: int = 50,
    ) -> tuple[MailActionCandidate, ...]: ...

    async def get_candidate(
        self, *, tenant_id: str, candidate_id: UUID
    ) -> MailActionCandidate | None: ...

    async def save_policy(self, policy: MailAgentPolicy) -> MailAgentPolicy: ...

    async def list_policies(
        self, *, tenant_id: str, owner_subject_id: str, limit: int = 50
    ) -> tuple[MailAgentPolicy, ...]: ...

    async def get_policy(self, *, tenant_id: str, policy_id: UUID) -> MailAgentPolicy | None: ...

    async def save_grant(self, grant: DelegationGrant) -> DelegationGrant: ...

    async def list_grants(
        self, *, tenant_id: str, granted_by_subject_id: str, limit: int = 50
    ) -> tuple[DelegationGrant, ...]: ...

    async def get_grant(self, *, tenant_id: str, grant_id: UUID) -> DelegationGrant | None: ...

    async def append_audit(self, event: Mapping[str, object]) -> None: ...

    async def list_audit_events(
        self, *, tenant_id: str, subject_id: str, limit: int = 100
    ) -> tuple[Mapping[str, object], ...]: ...

    async def save_draft(self, draft: MailDraft) -> MailDraft: ...

    async def get_draft(self, *, tenant_id: str, draft_id: UUID) -> MailDraft | None: ...

    async def list_drafts(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 5000,
    ) -> tuple[MailDraft, ...]: ...

    async def list_operations(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 5000,
    ) -> tuple[MailOutboxOperation, ...]: ...

    async def delete_connection_data(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID
    ) -> Mapping[str, object]: ...

    async def create_or_get_operation(
        self,
        operation: MailOutboxOperation,
    ) -> tuple[MailOutboxOperation, bool]: ...

    async def get_operation(
        self, *, tenant_id: str, operation_id: UUID
    ) -> MailOutboxOperation | None: ...

    async def count_operations_since(
        self, *, tenant_id: str, subject_id: str, since: datetime
    ) -> int: ...

    async def lease_operation(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> MailOutboxOperation: ...

    async def mark_sending(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
        fencing_token: int,
    ) -> MailOutboxOperation: ...

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
    ) -> MailOutboxOperation: ...

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
    ) -> MailOutboxOperation: ...

    async def create_or_get_sync_job(self, job: MailSyncJob) -> tuple[MailSyncJob, bool]: ...

    async def get_sync_job(self, *, tenant_id: str, job_id: UUID) -> MailSyncJob | None: ...

    async def list_sync_jobs(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID | None = None, limit: int = 50
    ) -> tuple[MailSyncJob, ...]: ...

    async def claim_sync_job(
        self,
        *,
        tenant_id: str,
        job_id: UUID,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> MailSyncJob: ...

    async def cancel_sync_job(self, *, tenant_id: str, job_id: UUID) -> MailSyncJob: ...

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
    ) -> MailSyncJob: ...

    async def create_or_get_autonomy_run(
        self, run: MailAutonomyRun
    ) -> tuple[MailAutonomyRun, bool]: ...

    async def get_autonomy_run(self, *, tenant_id: str, run_id: UUID) -> MailAutonomyRun | None: ...

    async def list_autonomy_runs(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MailAutonomyRun, ...]: ...

    async def claim_autonomy_run(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        worker_id: str,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> MailAutonomyRun: ...

    async def pause_autonomy_run(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        reason: str,
        worker_id: str | None = None,
        fencing_token: int | None = None,
    ) -> MailAutonomyRun: ...

    async def resume_autonomy_run(self, *, tenant_id: str, run_id: UUID) -> MailAutonomyRun: ...

    async def cancel_autonomy_run(self, *, tenant_id: str, run_id: UUID) -> MailAutonomyRun: ...

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
    ) -> MailAutonomyRun: ...

    async def save_rule(self, rule: MailRule) -> MailRule: ...

    async def get_rule(self, *, tenant_id: str, rule_id: UUID) -> MailRule | None: ...

    async def list_rules(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> tuple[MailRule, ...]: ...

    async def create_or_get_rule_execution(
        self, execution: RuleExecution
    ) -> tuple[RuleExecution, bool]: ...

    async def save_rule_execution(self, execution: RuleExecution) -> RuleExecution: ...

    async def list_rule_executions(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[RuleExecution, ...]: ...

    async def count_rule_executions_since(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID,
        since: datetime,
    ) -> int: ...
