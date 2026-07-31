from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mailhub.hosts.legacy import (
    AeroLinkDeliverySnapshot,
    AeroLinkMailAdapter,
    ApprovedCampaignSnapshot,
    CAACTRAININGMailAdapter,
    LegacyDeliveryKind,
    MailHubDeliveryResult,
    MailHubInboundMessage,
    project_delivery_result,
)
from mailhub.hosts.migration import (
    LegacyReferenceMap,
    MigrationController,
    MigrationFeatureFlags,
)


def test_caactraining_adapter_preserves_approved_snapshot_and_refs() -> None:
    snapshot = ApprovedCampaignSnapshot(
        tenant_id="tenant-1",
        owner_subject_id="user-1",
        migration_batch_id=uuid4(),
        connection_id=uuid4(),
        thread_id=None,
        recipient_addresses=("buyer@example.test",),
        subject="Approved outreach",
        body_object_ref="object://body-1",
        content_sha256="a" * 64,
        approved_revision=3,
        approval_ref="approval-3",
    )
    command = CAACTRAININGMailAdapter().to_draft_command(snapshot)
    assert command["body_object_ref"] == "object://body-1"
    assert command["approved_revision"] == 3
    assert "body_text" not in command
    assert command["attachment_refs"] == []
    assert str(command["idempotency_key"]).startswith("caactraining:tenant-1:")


def test_aerolink_adapter_only_maps_email_and_never_marks_unknown_sent() -> None:
    def snapshot(kind: LegacyDeliveryKind) -> AeroLinkDeliverySnapshot:
        return AeroLinkDeliverySnapshot(
            tenant_id="tenant-1",
            owner_subject_id="user-1",
            migration_batch_id=uuid4(),
            delivery_id="delivery-1",
            kind=kind,
            connection_id=uuid4(),
            thread_id=None,
            recipient_addresses=("buyer@example.test",),
            subject="Quotation",
            body_object_ref="object://quote-1",
            content_sha256="b" * 64,
            approved_revision=1,
            approval_ref="approval-1",
        )

    assert AeroLinkMailAdapter().to_send_command(snapshot(LegacyDeliveryKind.WEBHOOK)) is None
    command = AeroLinkMailAdapter().to_send_command(snapshot(LegacyDeliveryKind.EMAIL))
    assert command is not None
    assert command["idempotency_key"] == "aerolink:tenant-1:delivery-1:1"
    assert project_delivery_result("outcome_unknown") == "outcome_unknown"


def test_caactraining_result_projection_preserves_outcome_unknown() -> None:
    result = MailHubDeliveryResult(
        tenant_id="tenant-1",
        migration_batch_id=uuid4(),
        delivery_id="delivery-1",
        operation_id=uuid4(),
        status="outcome_unknown",
        content_sha256="a" * 64,
        occurred_at=datetime.now(UTC),
        provider_request_id="request-1",
        error_code="smtp_outcome_unknown",
    )

    projected = CAACTRAININGMailAdapter().project_delivery_result(result)

    assert projected["message_status"] == "SENDING"
    assert projected["delivery_attempt_status"] == "OUTCOME_UNKNOWN"
    assert projected["provider_message_ref"] is None
    assert projected["error_code"] == "smtp_outcome_unknown"


def test_legacy_delivery_snapshot_rejects_duplicate_recipients_and_attachments() -> None:
    snapshot = ApprovedCampaignSnapshot(
        tenant_id="tenant-1",
        owner_subject_id="user-1",
        migration_batch_id=uuid4(),
        connection_id=uuid4(),
        thread_id=None,
        recipient_addresses=("buyer@example.test",),
        cc_addresses=("buyer@example.test",),
        subject="Approved outreach",
        body_object_ref="object://body-1",
        content_sha256="a" * 64,
        approved_revision=1,
        approval_ref="approval-1",
        attachment_refs=("object://attachment-1", "object://attachment-1"),
    )
    with pytest.raises(ValueError, match="legacy_snapshot_recipients_invalid"):
        CAACTRAININGMailAdapter().to_draft_command(snapshot)


def test_aerolink_inbound_projection_is_metadata_only_and_deduplicable() -> None:
    message = MailHubInboundMessage(
        tenant_id="tenant-1",
        owner_subject_id="user-1",
        migration_batch_id=uuid4(),
        provider_message_ref="imap:10:41",
        provider_thread_ref="thread-1",
        sender_address="supplier@example.test",
        recipient_addresses=("ops@example.test",),
        subject="RFQ",
        content_sha256="b" * 64,
        body_object_ref="object://body-1",
        observed_at=datetime.now(UTC),
        business_link_ref="rfq-intake:pending",
    )

    projected = AeroLinkMailAdapter().project_inbound_message(message)

    assert projected["action"] == "aerolink.rfq_intake.propose"
    assert projected["body_object_ref"] == "object://body-1"
    assert "body_text" not in projected
    assert projected["dedupe_key"] == "aerolink:tenant-1:imap:10:41:" + "b" * 64


def test_legacy_reference_map_is_idempotent_but_rejects_retargeting() -> None:
    mapping = LegacyReferenceMap()
    first = mapping.put(
        tenant_id="tenant-1",
        legacy_system="caactraining",
        legacy_id="message-1",
        mailhub_ref="messagehub-1",
        owner_subject_id="user-1",
    )
    replay = mapping.put(
        tenant_id="tenant-1",
        legacy_system="caactraining",
        legacy_id="message-1",
        mailhub_ref="messagehub-1",
        state="cutover",
    )
    assert replay.mapped_at == first.mapped_at
    assert replay.state == "cutover"
    assert replay.owner_subject_id == "user-1"
    assert mapping.list_for_tenant(tenant_id="tenant-1") == (replay,)
    with pytest.raises(ValueError, match="legacy_reference_conflict"):
        mapping.put(
            tenant_id="tenant-1",
            legacy_system="caactraining",
            legacy_id="message-1",
            mailhub_ref="messagehub-2",
        )
    assert (
        mapping.get(tenant_id="tenant-2", legacy_system="caactraining", legacy_id="message-1")
        is None
    )


@pytest.mark.asyncio
async def test_migration_batch_start_and_sender_claim_are_idempotent() -> None:
    controller = MigrationController()
    flags = MigrationFeatureFlags(mailhub_read_authority=True, mailhub_send_authority=True)
    first = await controller.start(
        tenant_id="tenant-1",
        legacy_system="aerolink",
        account_ref="account-1",
        owner="mailhub",
        flags=flags,
        idempotency_key="migration-1",
    )
    replay = await controller.start(
        tenant_id="tenant-1",
        legacy_system="aerolink",
        account_ref="account-1",
        owner="mailhub",
        flags=flags,
        idempotency_key="migration-1",
    )
    assert replay == first
    assert await controller.claim_send_authority(batch_id=first.batch_id)
    assert await controller.claim_send_authority(batch_id=first.batch_id)
    assert (
        len(
            [
                event
                for event in controller.events
                if event.event_type == "mail.migration.authority_changed"
            ]
        )
        == 1
    )
    await controller.release_send_authority(batch_id=first.batch_id)
    await controller.release_send_authority(batch_id=first.batch_id)
    assert controller.batches[first.batch_id].state == "rolled_back"
