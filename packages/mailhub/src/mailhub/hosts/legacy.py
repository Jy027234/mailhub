"""Clean-room anti-corruption mappings for the two legacy host families.

The adapters accept approved snapshots and return MailHub command-shaped data;
they never read legacy repositories, choose recipients, or copy raw bodies.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from mailhub.domain import ensure_addresses


class LegacyDeliveryKind(StrEnum):
    EMAIL = "email"
    WEBHOOK = "webhook"
    SOCKET = "socket"


@dataclass(frozen=True, slots=True)
class MailHubDeliveryResult:
    """Bounded result envelope used when projecting MailHub into a host.

    The envelope carries operation/provider identity and digests only.  Host
    adapters must persist the result in their own delivery/message tables;
    MailHub never writes those facts directly.
    """

    tenant_id: str
    migration_batch_id: UUID
    delivery_id: str
    operation_id: UUID
    status: str
    content_sha256: str
    occurred_at: datetime
    provider_message_ref: str | None = None
    provider_request_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.delivery_id.strip():
            raise ValueError("mailhub_delivery_result_identity_missing")
        if len(self.delivery_id) > 256:
            raise ValueError("mailhub_delivery_result_identity_too_large")
        if len(self.content_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.content_sha256.casefold()
        ):
            raise ValueError("mailhub_delivery_result_digest_invalid")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("mailhub_delivery_result_timestamp_not_aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class MailHubInboundMessage:
    """Metadata-only inbound event for AeroLink/CAA host projection."""

    tenant_id: str
    owner_subject_id: str
    migration_batch_id: UUID
    provider_message_ref: str
    provider_thread_ref: str
    sender_address: str
    recipient_addresses: tuple[str, ...]
    subject: str
    content_sha256: str
    body_object_ref: str | None
    observed_at: datetime
    business_link_ref: str | None = None

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.tenant_id, "tenant_id", 200),
            (self.owner_subject_id, "owner_subject_id", 200),
            (self.provider_message_ref, "provider_message_ref", 512),
            (self.provider_thread_ref, "provider_thread_ref", 512),
            (self.sender_address, "sender_address", 320),
            (self.subject, "subject", 1000),
        ):
            if not value.strip() or len(value) > maximum:
                raise ValueError(f"mailhub_inbound_{name}_invalid")
        if not self.recipient_addresses or any(
            not item.strip() or len(item) > 320 for item in self.recipient_addresses
        ):
            raise ValueError("mailhub_inbound_recipients_invalid")
        if len(self.content_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.content_sha256.casefold()
        ):
            raise ValueError("mailhub_inbound_digest_invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("mailhub_inbound_timestamp_not_aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class ApprovedCampaignSnapshot:
    tenant_id: str
    owner_subject_id: str
    migration_batch_id: UUID
    connection_id: UUID
    thread_id: UUID | None
    recipient_addresses: tuple[str, ...]
    subject: str
    body_object_ref: str
    content_sha256: str
    approved_revision: int
    approval_ref: str
    cc_addresses: tuple[str, ...] = ()
    bcc_addresses: tuple[str, ...] = ()
    attachment_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AeroLinkDeliverySnapshot:
    tenant_id: str
    owner_subject_id: str
    migration_batch_id: UUID
    delivery_id: str
    kind: LegacyDeliveryKind
    connection_id: UUID | None
    thread_id: UUID | None
    recipient_addresses: tuple[str, ...]
    subject: str
    body_object_ref: str | None
    content_sha256: str | None
    approved_revision: int
    approval_ref: str
    cc_addresses: tuple[str, ...] = ()
    bcc_addresses: tuple[str, ...] = ()
    attachment_refs: tuple[str, ...] = ()


class CAACTRAININGMailAdapter:
    """Map an approved outreach snapshot to a MailHub draft command."""

    def to_draft_command(self, snapshot: ApprovedCampaignSnapshot) -> dict[str, object]:
        _validate_snapshot(snapshot.tenant_id, snapshot.approved_revision, snapshot.approval_ref)
        if not snapshot.body_object_ref or len(snapshot.content_sha256) != 64:
            raise ValueError("caactraining_body_ref_or_digest_missing")
        _validate_delivery_fields(
            recipient_addresses=snapshot.recipient_addresses,
            cc_addresses=snapshot.cc_addresses,
            bcc_addresses=snapshot.bcc_addresses,
            subject=snapshot.subject,
            body_object_ref=snapshot.body_object_ref,
            content_sha256=snapshot.content_sha256,
            attachment_refs=snapshot.attachment_refs,
        )
        return {
            "tenant_id": snapshot.tenant_id,
            "subject_id": snapshot.owner_subject_id,
            "connection_id": str(snapshot.connection_id),
            "thread_id": str(snapshot.thread_id) if snapshot.thread_id else None,
            "recipient_addresses": list(snapshot.recipient_addresses),
            "cc_addresses": list(snapshot.cc_addresses),
            "bcc_addresses": list(snapshot.bcc_addresses),
            "attachment_refs": list(snapshot.attachment_refs),
            "subject": snapshot.subject,
            "body_object_ref": snapshot.body_object_ref,
            "content_sha256": snapshot.content_sha256,
            "approved_revision": snapshot.approved_revision,
            "source": "caactraining.approved_campaign_snapshot",
            "migration_batch_id": str(snapshot.migration_batch_id),
            "approval_ref": snapshot.approval_ref,
            "idempotency_key": _snapshot_idempotency_key(
                "caactraining",
                snapshot.tenant_id,
                snapshot.migration_batch_id,
                snapshot.connection_id,
                snapshot.thread_id,
                snapshot.approved_revision,
                snapshot.content_sha256,
                snapshot.recipient_addresses,
                snapshot.cc_addresses,
                snapshot.bcc_addresses,
            ),
        }

    def project_delivery_result(self, result: MailHubDeliveryResult) -> dict[str, object]:
        """Project a MailHub result without ever upgrading uncertainty to sent."""

        status = project_delivery_result(result.status)
        return {
            "tenant_id": result.tenant_id,
            "migration_batch_id": str(result.migration_batch_id),
            "delivery_id": result.delivery_id,
            "delivery_attempt_id": str(
                uuid5(
                    NAMESPACE_URL,
                    f"caactraining:delivery-attempt:{result.delivery_id}:{result.operation_id}",
                )
            ),
            "operation_id": str(result.operation_id),
            "message_status": {
                "sent": "SENT",
                "failed": "FAILED",
                "outcome_unknown": "SENDING",
                "pending": "PENDING",
            }[status],
            "delivery_attempt_status": {
                "sent": "SENT",
                "failed": "FAILED",
                "outcome_unknown": "OUTCOME_UNKNOWN",
                "pending": "PENDING",
            }[status],
            "provider_message_ref": result.provider_message_ref,
            "provider_request_id": result.provider_request_id,
            "content_sha256": result.content_sha256,
            "error_code": result.error_code,
            "occurred_at": result.occurred_at.isoformat(),
        }


class AeroLinkMailAdapter:
    """Map only AeroLink EMAIL delivery; leave WEBHOOK/SOCKET host-owned."""

    def to_send_command(self, snapshot: AeroLinkDeliverySnapshot) -> dict[str, object] | None:
        if snapshot.kind is not LegacyDeliveryKind.EMAIL:
            return None
        _validate_snapshot(snapshot.tenant_id, snapshot.approved_revision, snapshot.approval_ref)
        if (
            snapshot.connection_id is None
            or snapshot.body_object_ref is None
            or snapshot.content_sha256 is None
            or len(snapshot.content_sha256) != 64
        ):
            raise ValueError("aerolink_email_snapshot_incomplete")
        _validate_delivery_fields(
            recipient_addresses=snapshot.recipient_addresses,
            cc_addresses=snapshot.cc_addresses,
            bcc_addresses=snapshot.bcc_addresses,
            subject=snapshot.subject,
            body_object_ref=snapshot.body_object_ref,
            content_sha256=snapshot.content_sha256,
            attachment_refs=snapshot.attachment_refs,
        )
        return {
            "tenant_id": snapshot.tenant_id,
            "subject_id": snapshot.owner_subject_id,
            "connection_id": str(snapshot.connection_id),
            "thread_id": str(snapshot.thread_id) if snapshot.thread_id else None,
            "recipient_addresses": list(snapshot.recipient_addresses),
            "cc_addresses": list(snapshot.cc_addresses),
            "bcc_addresses": list(snapshot.bcc_addresses),
            "attachment_refs": list(snapshot.attachment_refs),
            "subject": snapshot.subject,
            "body_object_ref": snapshot.body_object_ref,
            "content_sha256": snapshot.content_sha256,
            "approved_revision": snapshot.approved_revision,
            "idempotency_key": (
                f"aerolink:{snapshot.tenant_id}:{snapshot.delivery_id}:{snapshot.approved_revision}"
            ),
            "source": "aerolink.email_delivery_snapshot",
            "migration_batch_id": str(snapshot.migration_batch_id),
            "approval_ref": snapshot.approval_ref,
        }

    def project_inbound_message(self, message: MailHubInboundMessage) -> dict[str, object]:
        """Create an AeroLink RFQ-intake proposal from metadata-only mail facts."""

        return {
            "tenant_id": message.tenant_id,
            "subject_id": message.owner_subject_id,
            "migration_batch_id": str(message.migration_batch_id),
            "provider_message_ref": message.provider_message_ref,
            "provider_thread_ref": message.provider_thread_ref,
            "sender_address": message.sender_address,
            "recipient_addresses": list(message.recipient_addresses),
            "subject": message.subject,
            "content_sha256": message.content_sha256,
            "body_object_ref": message.body_object_ref,
            "business_link_ref": message.business_link_ref,
            "observed_at": message.observed_at.isoformat(),
            "action": "aerolink.rfq_intake.propose",
            "dedupe_key": (
                f"aerolink:{message.tenant_id}:{message.provider_message_ref}:"
                f"{message.content_sha256}"
            ),
            "source": "mailhub.message_observed.v1",
        }


def project_delivery_result(status: str) -> str:
    """Map MailHub result without ever turning uncertainty into sent."""

    normalized = status.strip().casefold()
    if normalized in {"succeeded", "reconciled_succeeded"}:
        return "sent"
    if normalized in {"outcome_unknown", "manual_resolution"}:
        return "outcome_unknown"
    if normalized in {"dead_letter", "failed"}:
        return "failed"
    return "pending"


def _validate_snapshot(tenant_id: str, revision: int, approval_ref: str) -> None:
    if not tenant_id.strip() or len(tenant_id) > 200 or revision < 1 or not approval_ref.strip():
        raise ValueError("legacy_snapshot_approval_missing")


def _validate_delivery_fields(
    *,
    recipient_addresses: tuple[str, ...],
    cc_addresses: tuple[str, ...],
    bcc_addresses: tuple[str, ...],
    subject: str,
    body_object_ref: str | None,
    content_sha256: str,
    attachment_refs: tuple[str, ...],
) -> None:
    try:
        normalized_cc = ensure_addresses(cc_addresses) if cc_addresses else ()
        normalized_bcc = ensure_addresses(bcc_addresses) if bcc_addresses else ()
        all_recipients = (
            *ensure_addresses(recipient_addresses),
            *normalized_cc,
            *normalized_bcc,
        )
    except ValueError as exc:
        raise ValueError("legacy_snapshot_recipients_invalid") from exc
    if not all_recipients or len(set(all_recipients)) != len(all_recipients):
        raise ValueError("legacy_snapshot_recipients_invalid")
    if not subject.strip() or len(subject) > 1000:
        raise ValueError("legacy_snapshot_subject_invalid")
    if body_object_ref is None or not body_object_ref.strip() or len(body_object_ref) > 1000:
        raise ValueError("legacy_snapshot_body_ref_invalid")
    if len(content_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in content_sha256.casefold()
    ):
        raise ValueError("legacy_snapshot_digest_invalid")
    if len(attachment_refs) > 20 or any(
        not ref.strip() or len(ref) > 1000 for ref in attachment_refs
    ):
        raise ValueError("legacy_snapshot_attachments_invalid")
    if len(set(attachment_refs)) != len(attachment_refs):
        raise ValueError("legacy_snapshot_attachments_invalid")


def _snapshot_idempotency_key(
    kind: str,
    tenant_id: str,
    migration_batch_id: UUID,
    connection_id: UUID,
    thread_id: UUID | None,
    revision: int,
    content_sha256: str,
    recipients: tuple[str, ...],
    cc_addresses: tuple[str, ...],
    bcc_addresses: tuple[str, ...],
) -> str:
    envelope = "|".join(
        (
            kind,
            tenant_id,
            str(migration_batch_id),
            str(connection_id),
            str(thread_id) if thread_id else "",
            str(revision),
            content_sha256,
            ",".join(recipients),
            ",".join(cc_addresses),
            ",".join(bcc_addresses),
        )
    )
    digest = hashlib.sha256(envelope.encode("utf-8")).hexdigest()
    return f"{kind}:{tenant_id}:{migration_batch_id}:{revision}:{digest}"
