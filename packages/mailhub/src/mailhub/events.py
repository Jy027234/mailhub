"""Typed, redacted event builders for the versioned MailHub event schemas."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from mailhub.domain import MailMessageProjection, MailOutboxOperation
from mailhub.observability import redact_event


class MailHubEventType(StrEnum):
    """Versionless business event names carried by the v1 envelope.

    The envelope remains versioned independently from these names so a host
    can route lifecycle events without coupling itself to MailHub's internal
    Python types.  Every value is intentionally metadata-only; body/MIME and
    credential-shaped fields are rejected/redacted before publication.
    """

    CONNECTION_AUTHORIZED = "mail.connection.authorized"
    CONNECTION_REAUTHORIZATION_REQUIRED = "mail.connection.reauthorization_required"
    CONNECTION_REVOKED = "mail.connection.revoked"
    CONNECTION_DELETED = "mail.connection.deleted"
    SUBSCRIPTION_CREATED = "mail.subscription.created"
    SUBSCRIPTION_RENEWED = "mail.subscription.renewed"
    SUBSCRIPTION_EXPIRED = "mail.subscription.expired"
    SUBSCRIPTION_FAILED = "mail.subscription.failed"
    SUBSCRIPTION_CANCELLED = "mail.subscription.cancelled"
    PROVIDER_NOTIFICATION_RECEIVED = "mail.provider.notification.received"
    SYNC_STARTED = "mail.sync.started"
    SYNC_PROGRESSED = "mail.sync.progressed"
    SYNC_COMPLETED = "mail.sync.completed"
    SYNC_FAILED = "mail.sync.failed"
    SYNC_RECONCILE_REQUIRED = "mail.sync.reconcile_required"
    MESSAGE_OBSERVED = "mail.message.observed"
    MESSAGE_CHANGED = "mail.message.changed"
    MESSAGE_DELETED = "mail.message.deleted"
    THREAD_CHANGED = "mail.thread.changed"
    ATTACHMENT_QUARANTINED = "mail.attachment.quarantined"
    ATTACHMENT_CLEARED = "mail.attachment.cleared"
    CANDIDATE_PROJECT_PROPOSED = "mail.candidate.project.proposed"
    CANDIDATE_KNOWLEDGE_PROPOSED = "mail.candidate.knowledge.proposed"
    CANDIDATE_TASK_PROPOSED = "mail.candidate.task.proposed"
    CANDIDATE_REVIEWED = "mail.candidate.reviewed"
    CANDIDATE_APPLIED = "mail.candidate.applied"
    CANDIDATE_REVOKED = "mail.candidate.revoked"
    DRAFT_CREATED = "mail.draft.created"
    DRAFT_REVISED = "mail.draft.revised"
    DRAFT_APPROVED = "mail.draft.approved"
    OUTBOX_QUEUED = "mail.outbox.queued"
    OUTBOX_SENT = "mail.outbox.sent"
    OUTBOX_FAILED = "mail.outbox.failed"
    OUTBOX_OUTCOME_UNKNOWN = "mail.outbox.outcome_unknown"
    OUTBOX_RECONCILED = "mail.outbox.reconciled"
    OUTBOX_DEAD_LETTERED = "mail.outbox.dead_lettered"
    RULE_PUBLISHED = "mail.rule.published"
    RULE_PAUSED = "mail.rule.paused"
    RULE_EXECUTED = "mail.rule.executed"
    RULE_BLOCKED = "mail.rule.blocked"
    DELEGATION_GRANTED = "mail.delegation.granted"
    DELEGATION_REVOKED = "mail.delegation.revoked"
    DELEGATION_EXPIRED = "mail.delegation.expired"
    AGENT_ACTION_PROPOSED = "mail.agent_action.proposed"
    AGENT_ACTION_ALLOWED = "mail.agent_action.allowed"
    AGENT_ACTION_BLOCKED = "mail.agent_action.blocked"
    AGENT_ACTION_EXECUTED = "mail.agent_action.executed"
    MIGRATION_SHADOW_COMPARED = "mail.migration.shadow_compared"
    MIGRATION_AUTHORITY_CHANGED = "mail.migration.authority_changed"
    MIGRATION_ROLLBACK_COMPLETED = "mail.migration.rollback_completed"


# These are the two already-published schema-specific events.  They are kept
# in the same registry so a host can validate one routing table for both the
# generic business events and the older strongly typed event schemas.
_SCHEMA_EVENT_TYPES = frozenset(
    {
        "mailhub.message_observed.v1",
        "mailhub.action_operation.v1",
        "mailhub.provider_event.v1",
    }
)
SUPPORTED_EVENT_TYPES = frozenset(item.value for item in MailHubEventType) | _SCHEMA_EVENT_TYPES


def _bounded(value: str, field_name: str, limit: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise ValueError(f"event_{field_name}_invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Common envelope shared by at-least-once MailHub domain events."""

    event_type: str
    tenant_id: str
    trace_id: str
    data: Mapping[str, object]
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    subject_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        _bounded(self.event_type, "type", 200)
        _bounded(self.tenant_id, "tenant", 200)
        _bounded(self.trace_id, "trace", 200)
        if self.subject_id is not None:
            _bounded(self.subject_id, "subject", 200)
        if self.idempotency_key is not None:
            _bounded(self.idempotency_key, "idempotency_key", 512)
        if not isinstance(self.data, Mapping):
            raise ValueError("event_data_invalid")
        if len(self.data) > 50:
            raise ValueError("event_data_too_large")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("event_occurred_at_naive")
        occurred_at = self.occurred_at.astimezone(UTC)
        object.__setattr__(self, "occurred_at", occurred_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "mailhub.event_envelope.v1",
            "event_id": str(self.event_id),
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "trace_id": self.trace_id,
            "tenant_id": self.tenant_id,
            "subject_id": self.subject_id,
            "idempotency_key": self.idempotency_key,
            "data": redact_event(self.data),
        }


def build_event(
    event_type: str | MailHubEventType,
    *,
    tenant_id: str,
    trace_id: str,
    data: Mapping[str, object],
    subject_id: str | None = None,
    idempotency_key: str | None = None,
    event_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, object]:
    """Build and validate a routable metadata-only MailHub business event.

    Product events must come from the registry.  This prevents a typo from
    silently creating an unrouteable side channel while still retaining the
    published ``mailhub.*.v1`` compatibility events.
    """

    normalized_type = str(event_type).strip()
    if normalized_type not in SUPPORTED_EVENT_TYPES:
        raise ValueError("event_type_not_registered")
    return EventEnvelope(
        event_type=normalized_type,
        tenant_id=tenant_id,
        subject_id=subject_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        data=data,
        event_id=event_id or uuid4(),
        occurred_at=occurred_at or datetime.now(UTC),
    ).to_dict()


def validate_event_payload(payload: Mapping[str, object]) -> None:
    """Validate the common event invariants before a host publishes an event."""

    required = {
        "schema_version",
        "event_id",
        "event_type",
        "occurred_at",
        "trace_id",
        "tenant_id",
        "data",
    }
    if not isinstance(payload, Mapping) or not required.issubset(payload):
        raise ValueError("event_payload_invalid")
    if payload.get("schema_version") != "mailhub.event_envelope.v1":
        raise ValueError("event_schema_version_invalid")
    try:
        UUID(str(payload["event_id"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("event_id_invalid") from exc
    event_type = payload.get("event_type")
    if not isinstance(event_type, str) or event_type not in SUPPORTED_EVENT_TYPES:
        raise ValueError("event_type_not_registered")
    tenant_id = payload.get("tenant_id")
    trace_id = payload.get("trace_id")
    if not isinstance(tenant_id, str) or not tenant_id.strip() or len(tenant_id) > 200:
        raise ValueError("event_tenant_invalid")
    if not isinstance(trace_id, str) or not trace_id.strip() or len(trace_id) > 200:
        raise ValueError("event_trace_invalid")
    occurred_at = payload.get("occurred_at")
    if not isinstance(occurred_at, str):
        raise ValueError("event_occurred_at_invalid")
    try:
        parsed_occurred_at = datetime.fromisoformat(occurred_at)
    except ValueError as exc:
        raise ValueError("event_occurred_at_invalid") from exc
    if parsed_occurred_at.tzinfo is None or parsed_occurred_at.utcoffset() is None:
        raise ValueError("event_occurred_at_naive")
    subject_id = payload.get("subject_id")
    if subject_id is not None and (
        not isinstance(subject_id, str) or not subject_id.strip() or len(subject_id) > 200
    ):
        raise ValueError("event_subject_invalid")
    idempotency_key = payload.get("idempotency_key")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > 512
    ):
        raise ValueError("event_idempotency_key_invalid")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("event_data_invalid")
    if len(data) > 50:
        raise ValueError("event_data_too_large")
    # ``build_event`` emits the redacted copy.  A host adapter must reject a
    # caller that tries to bypass that builder, rather than silently trusting
    # a raw body/token-shaped field at the transport boundary.
    if redact_event(data) != {str(key): value for key, value in data.items()}:
        raise ValueError("event_data_not_redacted")


def message_observed_event(
    message: MailMessageProjection,
    *,
    trace_id: str,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    envelope = EventEnvelope(
        event_type="mailhub.message_observed.v1",
        tenant_id=message.tenant_id,
        subject_id=None,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        occurred_at=message.received_at,
        data={
            "message_id": str(message.message_id),
            "connection_id": str(message.connection_id),
            "thread_id": str(message.thread_id),
            "provider_message_ref": message.provider_message_ref,
            "content_sha256": message.content_sha256,
            "body_object_ref": message.body_object_ref,
            "source_locator": [{"kind": "provider_message", "ref": message.provider_message_ref}],
        },
    )
    return envelope.to_dict()


def action_operation_event(
    operation: MailOutboxOperation,
    *,
    trace_id: str,
) -> dict[str, object]:
    envelope = EventEnvelope(
        event_type="mailhub.action_operation.v1",
        tenant_id=operation.tenant_id,
        subject_id=operation.subject_id,
        trace_id=trace_id,
        idempotency_key=operation.idempotency_key,
        occurred_at=operation.updated_at,
        data={
            "operation_id": str(operation.operation_id),
            "status": operation.status.value,
            "idempotency_key": operation.idempotency_key,
            "provider_message_ref": operation.provider_message_ref,
            "provider_request_id": operation.provider_request_id,
            "error_code": operation.error_code,
            "outcome_unknown": operation.status.value == "outcome_unknown",
        },
    )
    return envelope.to_dict()
