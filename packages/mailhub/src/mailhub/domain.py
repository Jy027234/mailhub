"""Framework-free MailHub domain objects and state transitions.

The domain deliberately contains no FastAPI, database, provider SDK or host
imports.  Provider and host facts enter through typed application ports.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROVIDER_METADATA_SECRET_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "raw_headers",
        "raw_message",
        "raw_mime",
        "refresh_token",
        "secret",
        "token",
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_addresses(addresses: Iterable[str]) -> str:
    """Stable digest used to bind an approval to the exact recipient set."""

    return digest_text("\n".join(addresses))


def digest_recipient_headers(
    recipient_addresses: Iterable[str],
    cc_addresses: Iterable[str] = (),
    bcc_addresses: Iterable[str] = (),
) -> str:
    """Digest To/Cc/Bcc with a backwards-compatible To-only representation."""

    to = tuple(recipient_addresses)
    cc = tuple(cc_addresses)
    bcc = tuple(bcc_addresses)
    if not cc and not bcc:
        return digest_addresses(to)
    return digest_text(
        "\n".join(
            [
                *(f"to:{address}" for address in to),
                *(f"cc:{address}" for address in cc),
                *(f"bcc:{address}" for address in bcc),
            ]
        )
    )


def _non_empty(value: str, field_name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name}_invalid")
    return value.strip()


def _external_identity(value: str, field_name: str, maximum: int = 512) -> str:
    normalized = _non_empty(value, field_name, maximum)
    if any(ord(char) < 33 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{field_name}_invalid")
    return normalized


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(UTC)


def normalize_provider_metadata(value: Mapping[str, str] | None) -> dict[str, str]:
    """Validate the bounded, non-secret provider metadata projection.

    Provider adapters may preserve facts such as a body content type,
    category/folder identifier, history id or change key.  The projection is
    intentionally string-only and small: raw headers/MIME/body, credentials,
    cookies and client-state material cannot cross into the core model.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 20:
        raise ValueError("provider_metadata_invalid")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or _PROVIDER_METADATA_KEY_RE.fullmatch(raw_key) is None:
            raise ValueError("provider_metadata_key_invalid")
        key = raw_key.casefold()
        if key in _PROVIDER_METADATA_SECRET_KEYS:
            raise ValueError("provider_metadata_secret_or_raw")
        if not isinstance(raw_value, str) or not raw_value.strip() or len(raw_value) > 2_000:
            raise ValueError("provider_metadata_value_invalid")
        if any(ord(char) < 32 or ord(char) == 127 for char in raw_value):
            raise ValueError("provider_metadata_value_invalid")
        normalized[key] = raw_value.strip()
    return dict(sorted(normalized.items()))


class ProviderName(StrEnum):
    SANDBOX = "sandbox"
    GMAIL = "gmail"
    MICROSOFT_GRAPH = "microsoft_graph"
    IMAP_SMTP = "imap_smtp"


class ConnectionStatus(StrEnum):
    PENDING_AUTHORIZATION = "pending_authorization"
    ACTIVE = "active"
    DEGRADED = "degraded"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    REVOKED = "revoked"
    DELETING = "deleting"
    DELETED = "deleted"


class ContentMode(StrEnum):
    METADATA_ONLY = "metadata_only"
    BOUNDED_PROCESSING = "bounded_processing"
    ENCRYPTED_CACHE = "encrypted_cache"
    ARCHIVE = "archive"


class ActionType(StrEnum):
    LABEL = "label"
    ARCHIVE = "archive"
    MARK_READ = "mark_read"
    CREATE_TASK = "create_task"
    UPDATE_TASK = "update_task"
    KNOWLEDGE_CANDIDATE = "knowledge_candidate"
    DRAFT_REPLY = "draft_reply"
    SEND_REPLY = "send_reply"
    FORWARD = "forward"
    BULK_SEND = "bulk_send"


class CandidateType(StrEnum):
    PROJECT = "project"
    TASK = "task"
    KNOWLEDGE = "knowledge"


class CandidateState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    REVOKED = "revoked"
    FAILED = "failed"


class AutomationLevel(StrEnum):
    L0_OBSERVE = "l0_observe"
    L1_RECOMMEND = "l1_recommend"
    L2_REVIEW_QUEUE = "l2_review_queue"
    L3A_BOUNDED_ORGANIZE = "l3a_bounded_organize"
    L3B_BOUNDED_REPLY = "l3b_bounded_reply"
    L4_HIGH_RISK = "l4_high_risk"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REVOKED = "revoked"


class DeliveryStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    QUEUED = "queued"
    LEASED = "leased"
    SENDING = "sending"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    DEAD_LETTER = "dead_letter"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILED_SUCCEEDED = "reconciled_succeeded"
    MANUAL_RESOLUTION = "manual_resolution"


class SyncJobStatus(StrEnum):
    """Durable synchronization job lifecycle.

    A job is deliberately separate from the provider cursor.  The cursor is
    committed only after the provider page has been normalized and persisted,
    while the job records the attempt, progress and failure evidence.
    """

    QUEUED = "queued"
    RUNNING = "running"
    # A transient Provider/network failure is durable rather than terminal.
    # The scheduler may claim it again once ``next_attempt_at`` is due.
    RETRY_WAIT = "retry_wait"
    # A running worker keeps its lease while it reaches a safe cancellation
    # checkpoint.  This avoids clearing the fence underneath the worker and
    # turning an operator cancellation into an unhandled lease conflict.
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubscriptionStatus(StrEnum):
    """Durable provider watch/subscription lifecycle.

    The sync cursor status and the provider subscription status are kept
    separate: a mailbox can be idle while its watch is due for renewal, and a
    failed watch must not make an already committed cursor look corrupt.
    """

    NONE = "none"
    ACTIVE = "active"
    RENEWAL_REQUIRED = "renewal_required"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AutonomyRunStatus(StrEnum):
    """Durable lifecycle for one bounded L0/L1 recommendation run."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MailboxSyncState:
    """Durable cursor plus non-secret Provider subscription metadata.

    Provider OAuth material and raw notification bodies are intentionally not
    represented.  ``subscription_client_state_ref`` is an opaque host-owned
    reference; the actual client state/secret never crosses this boundary.
    """

    tenant_id: str
    connection_id: UUID
    folder_ref: str = "INBOX"
    cursor_kind: str = "provider"
    cursor_value: str | None = None
    subscription_ref: str | None = None
    subscription_status: SubscriptionStatus = SubscriptionStatus.NONE
    subscription_expires_at: datetime | None = None
    subscription_callback_endpoint: str | None = None
    subscription_client_state_ref: str | None = None
    subscription_provider_request_id: str | None = None
    lease_owner: str | None = None
    fencing_token: int = 0
    status: str = "idle"
    watermark: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.folder_ref, "folder_ref", 512)
        _non_empty(self.cursor_kind, "cursor_kind", 64)
        status = _non_empty(self.status, "sync_state_status", 64).casefold()
        if status not in {"idle", "syncing", "retrying", "blocked"}:
            raise ValueError("sync_state_status_invalid")
        object.__setattr__(self, "status", status)
        if self.fencing_token < 0:
            raise ValueError("sync_state_fencing_token_invalid")
        if not isinstance(self.subscription_status, SubscriptionStatus):
            object.__setattr__(
                self,
                "subscription_status",
                SubscriptionStatus(str(self.subscription_status)),
            )
        for field_name in ("updated_at", "watermark", "subscription_expires_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _aware(value, field_name))
        if self.subscription_ref is not None:
            _non_empty(self.subscription_ref, "subscription_ref", 1000)
        if self.subscription_callback_endpoint is not None:
            _non_empty(self.subscription_callback_endpoint, "subscription_callback_endpoint", 2000)
        if self.subscription_client_state_ref is not None:
            _non_empty(self.subscription_client_state_ref, "subscription_client_state_ref", 512)
        if self.subscription_provider_request_id is not None:
            _non_empty(
                self.subscription_provider_request_id, "subscription_provider_request_id", 512
            )


@dataclass(frozen=True, slots=True)
class MailSyncJob:
    job_id: UUID
    tenant_id: str
    subject_id: str
    connection_id: UUID
    mode: str = "incremental"
    requested_limit: int = 50
    folder_ref: str = "INBOX"
    label_refs: tuple[str, ...] = ()
    received_after: datetime | None = None
    received_before: datetime | None = None
    idempotency_key: str = ""
    status: SyncJobStatus = SyncJobStatus.QUEUED
    fetched_count: int = 0
    saved_count: int = 0
    duplicate_count: int = 0
    cursor_before: str | None = None
    cursor_after: str | None = None
    provider_request_id: str | None = None
    error_code: str | None = None
    trace_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    fencing_token: int = 0
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)
    # Appended for v1 compatibility: existing positional constructors keep
    # their field order while durable sync results gain deletion accounting.
    deleted_count: int = 0
    # Retry metadata is appended so older positional constructors remain
    # source-compatible.  It is only populated for transient failures.
    attempt_count: int = 0
    next_attempt_at: datetime | None = None

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.subject_id, "subject_id")
        mode = _non_empty(self.mode, "sync_mode", 64).casefold()
        if mode not in {"incremental", "backfill", "reconcile"}:
            raise ValueError("sync_mode_invalid")
        object.__setattr__(self, "mode", mode)
        if not 1 <= self.requested_limit <= 500:
            raise ValueError("sync_requested_limit_invalid")
        folder_ref = _non_empty(self.folder_ref, "sync_folder_ref", 200)
        if any(ord(char) < 33 or ord(char) == 127 for char in folder_ref):
            raise ValueError("sync_folder_ref_invalid")
        object.__setattr__(self, "folder_ref", folder_ref)
        if len(self.label_refs) > 20:
            raise ValueError("sync_label_limit")
        labels: list[str] = []
        for label in self.label_refs:
            normalized_label = _non_empty(label, "sync_label_ref", 200)
            if any(ord(char) < 33 or ord(char) == 127 for char in normalized_label):
                raise ValueError("sync_label_ref_invalid")
            if normalized_label not in labels:
                labels.append(normalized_label)
        object.__setattr__(self, "label_refs", tuple(labels))
        for field_name in ("received_after", "received_before"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _aware(value, field_name))
        if (
            self.received_after is not None
            and self.received_before is not None
            and self.received_after >= self.received_before
        ):
            raise ValueError("sync_date_range_invalid")
        if not self.idempotency_key:
            object.__setattr__(
                self,
                "idempotency_key",
                f"mail-sync:{self.connection_id}:{self.mode}:{self.requested_limit}",
            )
        _non_empty(self.idempotency_key, "sync_idempotency_key", 300)
        if (
            min(
                self.fetched_count,
                self.saved_count,
                self.duplicate_count,
                self.deleted_count,
            )
            < 0
        ):
            raise ValueError("sync_counter_invalid")
        if self.attempt_count < 0:
            raise ValueError("sync_attempt_count_invalid")
        if self.fencing_token < 0:
            raise ValueError("sync_fencing_token_invalid")
        if self.lease_expires_at is not None:
            object.__setattr__(
                self, "lease_expires_at", _aware(self.lease_expires_at, "lease_expires_at")
            )
        if self.next_attempt_at is not None:
            object.__setattr__(
                self, "next_attempt_at", _aware(self.next_attempt_at, "next_attempt_at")
            )
        for field_name in ("created_at", "updated_at"):
            object.__setattr__(self, field_name, _aware(getattr(self, field_name), field_name))
        for field_name in ("started_at", "finished_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _aware(value, field_name))


def transition_sync_job(current: SyncJobStatus, target: SyncJobStatus) -> None:
    allowed: dict[SyncJobStatus, set[SyncJobStatus]] = {
        SyncJobStatus.QUEUED: {SyncJobStatus.RUNNING, SyncJobStatus.CANCELLED},
        SyncJobStatus.RUNNING: {
            SyncJobStatus.QUEUED,
            SyncJobStatus.RETRY_WAIT,
            SyncJobStatus.CANCELLING,
            SyncJobStatus.SUCCEEDED,
            SyncJobStatus.FAILED,
            SyncJobStatus.CANCELLED,
        },
        SyncJobStatus.RETRY_WAIT: {SyncJobStatus.QUEUED, SyncJobStatus.CANCELLED},
        SyncJobStatus.CANCELLING: {SyncJobStatus.CANCELLED},
        SyncJobStatus.SUCCEEDED: set(),
        SyncJobStatus.FAILED: set(),
        SyncJobStatus.CANCELLED: set(),
    }
    if target not in allowed[current]:
        raise ValueError(f"sync_job_transition_invalid:{current}->{target}")


@dataclass(frozen=True, slots=True)
class MailAutonomyRun:
    """Persisted, replayable recommendation-cycle evidence.

    A run is intentionally limited to metadata reads and reviewable candidate
    proposals.  Message/candidate identifiers are persisted as references only;
    raw bodies and credentials never enter this record.
    """

    run_id: UUID
    tenant_id: str
    subject_id: str
    connection_id: UUID
    replay_key: str
    mode: str = "recommend_only"
    requested_limit: int = 50
    message_limit: int = 50
    status: AutonomyRunStatus = AutonomyRunStatus.QUEUED
    message_ids: tuple[UUID, ...] = ()
    analyzed_message_ids: tuple[UUID, ...] = ()
    candidate_ids: tuple[UUID, ...] = ()
    sync_result: Mapping[str, object] = field(default_factory=dict)
    sync_job_id: UUID | None = None
    error_code: str | None = None
    pause_reason: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    fencing_token: int = 0
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.subject_id, "subject_id")
        _non_empty(self.replay_key, "autonomy_replay_key", 200)
        mode = _non_empty(self.mode, "autonomy_mode", 64).casefold()
        if mode != "recommend_only":
            raise ValueError("autonomy_mode_invalid")
        object.__setattr__(self, "mode", mode)
        if not 1 <= self.requested_limit <= 500:
            raise ValueError("autonomy_sync_limit_invalid")
        if not 1 <= self.message_limit <= 200:
            raise ValueError("autonomy_message_limit_invalid")
        for name, values in (
            ("message_ids", self.message_ids),
            ("analyzed_message_ids", self.analyzed_message_ids),
            ("candidate_ids", self.candidate_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"autonomy_{name}_duplicate")
        if len(self.message_ids) > self.message_limit:
            raise ValueError("autonomy_message_count_exceeded")
        if len(self.analyzed_message_ids) > len(self.message_ids):
            raise ValueError("autonomy_analyzed_count_exceeded")
        if self.fencing_token < 0 or self.revision < 1:
            raise ValueError("autonomy_counter_invalid")
        if self.error_code is not None:
            _non_empty(self.error_code, "autonomy_error_code", 256)
        if self.pause_reason is not None:
            _non_empty(self.pause_reason, "autonomy_pause_reason", 500)
        if self.lease_expires_at is not None:
            object.__setattr__(
                self, "lease_expires_at", _aware(self.lease_expires_at, "lease_expires_at")
            )
        for field_name in ("created_at", "updated_at"):
            object.__setattr__(self, field_name, _aware(getattr(self, field_name), field_name))
        for field_name in ("started_at", "finished_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _aware(value, field_name))


def transition_autonomy_run(current: AutonomyRunStatus, target: AutonomyRunStatus) -> None:
    allowed: dict[AutonomyRunStatus, set[AutonomyRunStatus]] = {
        AutonomyRunStatus.QUEUED: {
            AutonomyRunStatus.RUNNING,
            AutonomyRunStatus.PAUSED,
            AutonomyRunStatus.CANCELLED,
        },
        AutonomyRunStatus.RUNNING: {
            AutonomyRunStatus.QUEUED,
            AutonomyRunStatus.COMPLETED,
            AutonomyRunStatus.FAILED,
            AutonomyRunStatus.PAUSED,
            AutonomyRunStatus.CANCELLED,
        },
        AutonomyRunStatus.PAUSED: {
            AutonomyRunStatus.QUEUED,
            AutonomyRunStatus.RUNNING,
            AutonomyRunStatus.CANCELLED,
        },
        AutonomyRunStatus.COMPLETED: set(),
        AutonomyRunStatus.FAILED: set(),
        AutonomyRunStatus.CANCELLED: set(),
    }
    if target not in allowed[current]:
        raise ValueError(f"autonomy_run_transition_invalid:{current}->{target}")


@dataclass(frozen=True, slots=True)
class MailDraft:
    draft_id: UUID
    tenant_id: str
    subject_id: str
    connection_id: UUID
    thread_id: UUID | None
    recipient_addresses: tuple[str, ...]
    subject: str
    body_text: str | None
    content_sha256: str
    cc_addresses: tuple[str, ...] = ()
    bcc_addresses: tuple[str, ...] = ()
    attachment_refs: tuple[str, ...] = ()
    body_object_ref: str | None = None
    revision: int = 1
    status: DeliveryStatus = DeliveryStatus.DRAFT
    provider_draft_ref: str | None = None
    expires_at: datetime = field(default_factory=lambda: utc_now() + timedelta(hours=24))
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.subject_id, "subject_id")
        _non_empty(self.subject, "subject", 1000)
        if self.body_text is None:
            if not self.body_object_ref:
                raise ValueError("draft_body_reference_missing")
        else:
            _non_empty(self.body_text, "body_text", 200_000)
        normalized_to = ensure_addresses(self.recipient_addresses)
        normalized_cc = _optional_addresses(self.cc_addresses, "cc_addresses")
        normalized_bcc = _optional_addresses(self.bcc_addresses, "bcc_addresses")
        all_recipients = (*normalized_to, *normalized_cc, *normalized_bcc)
        if not all_recipients:
            raise ValueError("draft_recipients_missing")
        if len(set(all_recipients)) != len(all_recipients):
            raise ValueError("draft_recipients_duplicate")
        if len(all_recipients) > 50:
            raise ValueError("draft_recipients_too_many")
        object.__setattr__(self, "recipient_addresses", normalized_to)
        object.__setattr__(self, "cc_addresses", normalized_cc)
        object.__setattr__(self, "bcc_addresses", normalized_bcc)
        normalized_attachments = tuple(
            _non_empty(ref, "attachment_ref", 1000) for ref in self.attachment_refs
        )
        if len(normalized_attachments) > 20:
            raise ValueError("draft_attachments_too_many")
        if len(set(normalized_attachments)) != len(normalized_attachments):
            raise ValueError("draft_attachments_duplicate")
        object.__setattr__(self, "attachment_refs", normalized_attachments)
        if not _DIGEST_RE.fullmatch(self.content_sha256):
            raise ValueError("draft_content_digest_mismatch")
        if self.body_text is not None and digest_text(self.body_text) != self.content_sha256:
            raise ValueError("draft_content_digest_mismatch")
        if self.revision < 1:
            raise ValueError("revision_invalid")
        object.__setattr__(self, "expires_at", _aware(self.expires_at, "expires_at"))
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))


def outbound_internet_message_id(operation_id: UUID) -> str:
    """The deterministic Message-ID every MailHub send carries.

    Deterministic on purpose.  It is what lets an OUTCOME_UNKNOWN send be
    reconciled later without persisting anything extra -- the message can be
    looked up by the identifier it was sent with -- and what makes a duplicate
    detectable if a retry does go out after all.
    """

    return f"<mailhub-{operation_id}@mailhub.invalid>"


@dataclass(frozen=True, slots=True)
class MailOutboxOperation:
    operation_id: UUID
    tenant_id: str
    subject_id: str
    draft_id: UUID
    connection_id: UUID
    idempotency_key: str
    status: DeliveryStatus = DeliveryStatus.DRAFT
    action_id: UUID | None = None
    input_digest: str | None = None
    agent_subject_id: str | None = None
    policy_id: UUID | None = None
    grant_id: UUID | None = None
    policy_revision: int | None = None
    grant_revision: int | None = None
    approval_ref: str | None = None
    # Who approved the send, once the host verified the confirmation.  Persisted
    # so the pre-I/O re-check can re-assert separation of duties instead of
    # re-validating a confirmation whose approver is no longer known.
    approver_subject_id: str | None = None
    attempt_count: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    fencing_token: int = 0
    provider_message_ref: str | None = None
    provider_request_id: str | None = None
    error_code: str | None = None
    next_attempt_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.subject_id, "subject_id")
        _non_empty(self.idempotency_key, "idempotency_key", 300)
        if self.attempt_count < 0 or self.fencing_token < 0:
            raise ValueError("operation_counter_invalid")
        if self.policy_revision is not None and self.policy_revision < 1:
            raise ValueError("policy_revision_invalid")
        if self.grant_revision is not None and self.grant_revision < 1:
            raise ValueError("grant_revision_invalid")
        if self.input_digest is not None and not _DIGEST_RE.fullmatch(self.input_digest):
            raise ValueError("operation_input_digest_invalid")
        if self.agent_subject_id is not None:
            _non_empty(self.agent_subject_id, "agent_subject_id")
        if self.approval_ref is not None:
            _non_empty(self.approval_ref, "approval_ref", 512)
        if self.lease_expires_at is not None:
            object.__setattr__(
                self, "lease_expires_at", _aware(self.lease_expires_at, "lease_expires_at")
            )
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))
        if self.next_attempt_at is not None:
            object.__setattr__(
                self, "next_attempt_at", _aware(self.next_attempt_at, "next_attempt_at")
            )


@dataclass(frozen=True, slots=True)
class MailboxConnection:
    connection_id: UUID
    tenant_id: str
    subject_id: str
    provider: ProviderName
    email_address: str
    credential_ref: str
    granted_scopes: tuple[str, ...] = ()
    # Provider account/tenant identities are non-secret OAuth facts.  They
    # let the host fence a refreshed credential to the same mailbox instead
    # of trusting only a mutable display email address.  Older sandbox and
    # manually-created connections may omit them until the host supplies the
    # metadata during reauthorization.
    provider_account_id: str | None = None
    provider_tenant_id: str | None = None
    credential_version: int = 1
    content_mode: ContentMode = ContentMode.BOUNDED_PROCESSING
    status: ConnectionStatus = ConnectionStatus.PENDING_AUTHORIZATION
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.subject_id, "subject_id")
        _non_empty(self.credential_ref, "credential_ref")
        if self.provider_account_id is not None:
            object.__setattr__(
                self,
                "provider_account_id",
                _external_identity(self.provider_account_id, "provider_account_id"),
            )
        if self.provider_tenant_id is not None:
            object.__setattr__(
                self,
                "provider_tenant_id",
                _external_identity(self.provider_tenant_id, "provider_tenant_id"),
            )
        if len(self.granted_scopes) > 40 or any(
            not isinstance(scope, str)
            or not scope.strip()
            or len(scope.strip()) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in scope)
            for scope in self.granted_scopes
        ):
            raise ValueError("granted_scopes_invalid")
        object.__setattr__(
            self,
            "granted_scopes",
            tuple(dict.fromkeys(scope.strip() for scope in self.granted_scopes)),
        )
        if not 1 <= self.credential_version <= 2_147_483_647:
            raise ValueError("credential_version_invalid")
        email = _non_empty(self.email_address, "email_address", 320).lower()
        if not _EMAIL_RE.fullmatch(email):
            raise ValueError("email_address_invalid")
        object.__setattr__(self, "email_address", email)
        if self.revision < 1:
            raise ValueError("revision_invalid")
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class ConnectionCleanupRefs:
    """Complete, content-free references needed before connection cleanup.

    Cleanup must not rely on a user-facing page limit: a mailbox can contain
    more messages than any bounded API response.  Repositories return only
    opaque IDs/object refs, never message bodies or credentials.
    """

    message_ids: tuple[UUID, ...] = ()
    knowledge_message_ids: tuple[UUID, ...] = ()
    object_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("message_ids", "knowledge_message_ids"):
            values = getattr(self, field_name)
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name}_duplicate")
            object.__setattr__(self, field_name, tuple(sorted(values, key=str)))
        normalized_refs = tuple(sorted({ref.strip() for ref in self.object_refs if ref.strip()}))
        if any(len(ref) > 2000 for ref in normalized_refs):
            raise ValueError("cleanup_object_ref_invalid")
        object.__setattr__(self, "object_refs", normalized_refs)


@dataclass(frozen=True, slots=True)
class MailThread:
    thread_id: UUID
    tenant_id: str
    connection_id: UUID
    provider_thread_ref: str
    normalized_subject: str
    participant_addresses: tuple[str, ...]
    latest_at: datetime
    message_count: int = 0
    revision: int = 1

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.provider_thread_ref, "provider_thread_ref", 1000)
        _non_empty(self.normalized_subject, "normalized_subject", 1000)
        if self.message_count < 0 or self.revision < 1:
            raise ValueError("thread_count_or_revision_invalid")
        object.__setattr__(self, "latest_at", _aware(self.latest_at, "latest_at"))


@dataclass(frozen=True, slots=True)
class MailThreadSummary:
    """Metadata-only unified-inbox row derived from a governed thread.

    The summary intentionally contains account identity and bounded message
    metadata only.  It never carries body text, object references, MIME or
    attachment bytes.
    """

    thread_id: UUID
    tenant_id: str
    connection_id: UUID
    provider: ProviderName
    account_email: str
    normalized_subject: str
    participant_addresses: tuple[str, ...]
    latest_at: datetime
    message_count: int
    revision: int
    labels: tuple[str, ...] = ()
    unread: bool = False
    important: bool = False
    has_attachment: bool = False
    project_hint: str | None = None
    candidate_count: int = 0

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.account_email, "account_email", 320)
        if not _EMAIL_RE.fullmatch(self.account_email.strip().lower()):
            raise ValueError("account_email_invalid")
        if self.message_count < 0 or self.revision < 1 or self.candidate_count < 0:
            raise ValueError("thread_summary_count_invalid")
        object.__setattr__(self, "account_email", self.account_email.strip().lower())
        object.__setattr__(self, "latest_at", _aware(self.latest_at, "latest_at"))
        if self.project_hint is not None:
            object.__setattr__(self, "project_hint", self.project_hint.strip() or None)


@dataclass(frozen=True, slots=True)
class MailThreadPage:
    """Stable cursor page for the metadata-only unified inbox."""

    items: tuple[MailThreadSummary, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class MailMessageProjection:
    message_id: UUID
    tenant_id: str
    connection_id: UUID
    thread_id: UUID
    provider_message_ref: str
    internet_message_id: str | None
    sender_address: str
    recipient_addresses: tuple[str, ...]
    subject: str
    received_at: datetime
    body_text: str | None = None
    body_object_ref: str | None = None
    content_sha256: str = ""
    labels: tuple[str, ...] = ()
    attachment_count: int = 0
    is_read: bool = False
    revision: int = 1
    provider_metadata: Mapping[str, str] = field(default_factory=dict)
    # Normalized, bounded header projections.  Raw RFC 5322 headers never
    # cross this model; providers may omit Bcc when it is not visible.
    cc_addresses: tuple[str, ...] = ()
    bcc_addresses: tuple[str, ...] = ()
    reply_to_addresses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.provider_message_ref, "provider_message_ref", 1000)
        sender = _non_empty(self.sender_address, "sender_address", 320).lower()
        if not _EMAIL_RE.fullmatch(sender):
            raise ValueError("sender_address_invalid")
        object.__setattr__(self, "sender_address", sender)
        object.__setattr__(self, "recipient_addresses", ensure_addresses(self.recipient_addresses))
        object.__setattr__(
            self, "cc_addresses", _optional_addresses(self.cc_addresses, "cc_addresses")
        )
        object.__setattr__(
            self, "bcc_addresses", _optional_addresses(self.bcc_addresses, "bcc_addresses")
        )
        object.__setattr__(
            self,
            "reply_to_addresses",
            _optional_addresses(self.reply_to_addresses, "reply_to_addresses"),
        )
        subject = _non_empty(self.subject, "subject", 1000)
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "received_at", _aware(self.received_at, "received_at"))
        if self.body_text is not None and len(self.body_text) > 200_000:
            raise ValueError("body_text_too_large")
        if self.body_object_ref is None and self.body_text is None:
            raise ValueError("body_reference_missing")
        if not isinstance(self.attachment_count, int) or not 0 <= self.attachment_count <= 100:
            raise ValueError("attachment_count_invalid")
        calculated = (
            digest_text(self.body_text) if self.body_text is not None else self.content_sha256
        )
        if not _DIGEST_RE.fullmatch(self.content_sha256) or not calculated:
            raise ValueError("content_digest_mismatch")
        if calculated != self.content_sha256:
            raise ValueError("content_digest_mismatch")
        if self.revision < 1:
            raise ValueError("revision_invalid")
        object.__setattr__(
            self, "provider_metadata", normalize_provider_metadata(self.provider_metadata)
        )


@dataclass(frozen=True, slots=True)
class MailActionCandidate:
    candidate_id: UUID
    tenant_id: str
    subject_id: str
    message_id: UUID
    candidate_type: CandidateType
    payload: Mapping[str, object]
    evidence: tuple[Mapping[str, object], ...]
    confidence: float
    requires_review: bool = True
    status: CandidateState = CandidateState.PROPOSED
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.subject_id, "subject_id")
        if not 0 <= self.confidence <= 1:
            raise ValueError("candidate_confidence_invalid")
        if self.revision < 1:
            raise ValueError("revision_invalid")
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class MailAgentPolicy:
    policy_id: UUID
    tenant_id: str
    owner_subject_id: str
    allowed_connection_ids: frozenset[UUID] = frozenset()
    allowed_folder_refs: frozenset[str] = frozenset()
    allowed_actions: frozenset[ActionType] = frozenset()
    allowed_domains: frozenset[str] = frozenset()
    allowed_data_classes: frozenset[str] = frozenset({"public", "internal"})
    thread_only: bool = True
    allowed_automation_level: AutomationLevel = AutomationLevel.L1_RECOMMEND
    max_per_hour: int = 0
    max_per_day: int = 0
    valid_from: datetime = field(default_factory=utc_now)
    valid_until: datetime = field(default_factory=lambda: utc_now() + timedelta(days=30))
    revision: int = 1
    enabled: bool = True

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.owner_subject_id, "owner_subject_id")
        if self.max_per_hour < 0 or self.max_per_day < 0:
            raise ValueError("policy_rate_limit_invalid")
        if self.revision < 1:
            raise ValueError("revision_invalid")
        valid_from = _aware(self.valid_from, "valid_from")
        valid_until = _aware(self.valid_until, "valid_until")
        if valid_until <= valid_from:
            raise ValueError("policy_validity_invalid")
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(
            self,
            "allowed_domains",
            frozenset(domain.lower().strip() for domain in self.allowed_domains if domain.strip()),
        )


@dataclass(frozen=True, slots=True)
class DelegationGrant:
    grant_id: UUID
    tenant_id: str
    policy_id: UUID
    agent_subject_id: str
    granted_by_subject_id: str
    capability_ids: frozenset[ActionType]
    granted_at: datetime = field(default_factory=utc_now)
    expires_at: datetime = field(default_factory=lambda: utc_now() + timedelta(days=7))
    revoked_at: datetime | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        _non_empty(self.tenant_id, "tenant_id")
        _non_empty(self.agent_subject_id, "agent_subject_id")
        _non_empty(self.granted_by_subject_id, "granted_by_subject_id")
        if not self.capability_ids:
            raise ValueError("grant_capabilities_missing")
        granted_at = _aware(self.granted_at, "granted_at")
        expires_at = _aware(self.expires_at, "expires_at")
        if expires_at <= granted_at:
            raise ValueError("grant_validity_invalid")
        object.__setattr__(self, "granted_at", granted_at)
        object.__setattr__(self, "expires_at", expires_at)
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _aware(self.revoked_at, "revoked_at"))


@dataclass(frozen=True, slots=True)
class AgentActionContext:
    tenant_id: str
    agent_subject_id: str
    connection_id: UUID | None
    folder_ref: str | None
    thread_id: UUID | None
    recipient_addresses: tuple[str, ...] = ()
    data_classes: frozenset[str] = frozenset({"internal"})
    has_new_recipient: bool = False
    has_attachment: bool = False
    has_bcc: bool = False
    has_group_recipient: bool = False
    has_external_recipient: bool = False
    has_large_recipient_set: bool = False
    contains_high_risk_terms: bool = False
    request_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason_code: str
    policy_id: UUID | None = None
    grant_id: UUID | None = None
    required_approval: bool = True
    automation_level: AutomationLevel = AutomationLevel.L4_HIGH_RISK


@dataclass(frozen=True, slots=True)
class AgentActionRequest:
    action_id: UUID
    action_type: ActionType
    context: AgentActionContext
    input_digest: str
    policy_revision: int | None = None
    grant_revision: int | None = None
    source_message_ids: tuple[UUID, ...] = ()
    parameters: Mapping[str, object] = field(default_factory=dict)
    requested_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not _DIGEST_RE.fullmatch(self.input_digest):
            raise ValueError("input_digest_invalid")
        if len(self.parameters) > 20:
            raise ValueError("action_parameters_too_many")
        object.__setattr__(self, "requested_at", _aware(self.requested_at, "requested_at"))


def evaluate_policy(
    policy: MailAgentPolicy | None,
    grant: DelegationGrant | None,
    action: AgentActionRequest,
    *,
    now: datetime | None = None,
) -> PolicyDecision:
    """Fail-closed policy and delegation intersection evaluation."""

    current = (now or utc_now()).astimezone(UTC)
    if policy is None:
        return PolicyDecision(False, "policy_missing")
    if not policy.enabled:
        return PolicyDecision(False, "policy_disabled", policy.policy_id)
    if policy.tenant_id != action.context.tenant_id:
        return PolicyDecision(False, "tenant_mismatch", policy.policy_id)
    if policy.owner_subject_id != action.context.agent_subject_id and not (
        grant is not None
        and grant.policy_id == policy.policy_id
        and grant.agent_subject_id == action.context.agent_subject_id
    ):
        return PolicyDecision(False, "policy_owner_mismatch", policy.policy_id)
    if not policy.valid_from <= current < policy.valid_until:
        return PolicyDecision(False, "policy_expired", policy.policy_id)
    if action.action_type not in policy.allowed_actions:
        return PolicyDecision(False, "action_not_allowed", policy.policy_id)
    if (
        policy.allowed_connection_ids
        and action.context.connection_id not in policy.allowed_connection_ids
    ):
        return PolicyDecision(False, "connection_not_allowed", policy.policy_id)
    if policy.allowed_folder_refs and action.context.folder_ref not in policy.allowed_folder_refs:
        return PolicyDecision(False, "folder_not_allowed", policy.policy_id)
    if not action.context.data_classes.issubset(policy.allowed_data_classes):
        return PolicyDecision(False, "data_class_not_allowed", policy.policy_id)
    if policy.allowed_domains:
        domains = {
            address.rsplit("@", 1)[-1].lower() for address in action.context.recipient_addresses
        }
        if not domains.issubset(policy.allowed_domains):
            return PolicyDecision(False, "recipient_domain_not_allowed", policy.policy_id)
    if policy.thread_only and action.context.thread_id is None:
        return PolicyDecision(False, "thread_required", policy.policy_id)

    high_risk = (
        action.action_type
        in {
            ActionType.FORWARD,
            ActionType.BULK_SEND,
            ActionType.CREATE_TASK,
            ActionType.UPDATE_TASK,
            ActionType.KNOWLEDGE_CANDIDATE,
        }
        or action.context.has_new_recipient
        or action.context.contains_high_risk_terms
        or (action.context.has_attachment and action.action_type is ActionType.SEND_REPLY)
        or (action.context.has_bcc and action.action_type is ActionType.SEND_REPLY)
        or (action.context.has_group_recipient and action.action_type is ActionType.SEND_REPLY)
        or (action.context.has_external_recipient and action.action_type is ActionType.SEND_REPLY)
        or (action.context.has_large_recipient_set and action.action_type is ActionType.SEND_REPLY)
    )
    if action.action_type is ActionType.SEND_REPLY:
        if action.context.has_new_recipient or action.context.contains_high_risk_terms:
            high_risk = True
        if policy.allowed_automation_level is not AutomationLevel.L3B_BOUNDED_REPLY:
            high_risk = True
    if high_risk:
        return PolicyDecision(
            False,
            "approval_required",
            policy.policy_id,
            grant.grant_id if grant else None,
            required_approval=True,
            automation_level=AutomationLevel.L4_HIGH_RISK,
        )
    if grant is None:
        return PolicyDecision(False, "delegation_missing", policy.policy_id)
    if grant.tenant_id != policy.tenant_id or grant.policy_id != policy.policy_id:
        return PolicyDecision(False, "delegation_scope_mismatch", policy.policy_id, grant.grant_id)
    if grant.agent_subject_id != action.context.agent_subject_id:
        return PolicyDecision(False, "delegation_agent_mismatch", policy.policy_id, grant.grant_id)
    if grant.revoked_at is not None or not grant.granted_at <= current < grant.expires_at:
        return PolicyDecision(False, "delegation_expired", policy.policy_id, grant.grant_id)
    if action.action_type not in grant.capability_ids:
        return PolicyDecision(
            False, "delegation_action_not_allowed", policy.policy_id, grant.grant_id
        )

    level = policy.allowed_automation_level
    return PolicyDecision(
        True,
        "delegation_allowed",
        policy.policy_id,
        grant.grant_id,
        required_approval=False,
        automation_level=level,
    )


def transition_delivery(current: DeliveryStatus, target: DeliveryStatus) -> None:
    allowed: dict[DeliveryStatus, set[DeliveryStatus]] = {
        DeliveryStatus.DRAFT: {DeliveryStatus.AWAITING_CONFIRMATION, DeliveryStatus.APPROVED},
        DeliveryStatus.AWAITING_CONFIRMATION: {DeliveryStatus.APPROVED, DeliveryStatus.DEAD_LETTER},
        DeliveryStatus.APPROVED: {DeliveryStatus.QUEUED},
        DeliveryStatus.QUEUED: {DeliveryStatus.LEASED, DeliveryStatus.DEAD_LETTER},
        DeliveryStatus.LEASED: {
            DeliveryStatus.SENDING,
            DeliveryStatus.QUEUED,
            DeliveryStatus.OUTCOME_UNKNOWN,
        },
        DeliveryStatus.SENDING: {
            DeliveryStatus.SUCCEEDED,
            DeliveryStatus.RETRY_WAIT,
            DeliveryStatus.OUTCOME_UNKNOWN,
            DeliveryStatus.DEAD_LETTER,
        },
        DeliveryStatus.RETRY_WAIT: {
            DeliveryStatus.QUEUED,
            DeliveryStatus.LEASED,
            DeliveryStatus.DEAD_LETTER,
        },
        DeliveryStatus.OUTCOME_UNKNOWN: {
            DeliveryStatus.RECONCILED_SUCCEEDED,
            DeliveryStatus.RETRY_WAIT,
            DeliveryStatus.MANUAL_RESOLUTION,
        },
        DeliveryStatus.RECONCILED_SUCCEEDED: set(),
        DeliveryStatus.SUCCEEDED: set(),
        DeliveryStatus.DEAD_LETTER: set(),
        DeliveryStatus.MANUAL_RESOLUTION: set(),
    }
    if target not in allowed[current]:
        raise ValueError(f"delivery_transition_invalid:{current}->{target}")


def ensure_addresses(addresses: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(
        _non_empty(address, "recipient_address", 320).lower() for address in addresses
    )
    if not normalized or any(not _EMAIL_RE.fullmatch(address) for address in normalized):
        raise ValueError("recipient_addresses_invalid")
    if len(set(normalized)) != len(normalized):
        raise ValueError("recipient_addresses_duplicate")
    return normalized


def _optional_addresses(addresses: Iterable[str], field_name: str) -> tuple[str, ...]:
    values = tuple(addresses)
    if not values:
        return ()
    try:
        return ensure_addresses(values)
    except ValueError as exc:
        raise ValueError(f"{field_name}_invalid") from exc
