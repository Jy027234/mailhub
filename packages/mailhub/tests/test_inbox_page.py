from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from mailhub.connectors.sandbox import SandboxConnector
from mailhub.domain import (
    CandidateType,
    MailActionCandidate,
    MailMessageProjection,
    MailThread,
    ProviderName,
    digest_text,
)
from mailhub.service import MailService
from mailhub.storage import InMemoryMailRepository


@pytest.mark.asyncio
async def test_unified_thread_page_is_scope_bound_stable_and_filterable() -> None:
    repository = InMemoryMailRepository()
    service = MailService(repository, connectors={ProviderName.SANDBOX: SandboxConnector()})
    first_connection = await service.create_connection(
        tenant_id="tenant-inbox",
        subject_id="owner-inbox",
        provider=ProviderName.SANDBOX,
        email_address="first@example.test",
        credential_ref="fixture-first",
    )
    second_connection = await service.create_connection(
        tenant_id="tenant-inbox",
        subject_id="owner-inbox",
        provider=ProviderName.SANDBOX,
        email_address="second@example.test",
        credential_ref="fixture-second",
    )
    now = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)
    first_thread = MailThread(
        thread_id=uuid4(),
        tenant_id="tenant-inbox",
        connection_id=first_connection.connection_id,
        provider_thread_ref="thread-first",
        normalized_subject="Project RFQ",
        participant_addresses=("buyer@example.test", "first@example.test"),
        latest_at=now,
        message_count=1,
    )
    second_thread = MailThread(
        thread_id=uuid4(),
        tenant_id="tenant-inbox",
        connection_id=second_connection.connection_id,
        provider_thread_ref="thread-second",
        normalized_subject="Routine notice",
        participant_addresses=("notice@example.test", "second@example.test"),
        latest_at=now - timedelta(minutes=1),
        message_count=1,
    )
    await repository.save_thread(first_thread)
    await repository.save_thread(second_thread)
    first_body = "RFQ with attachment"
    second_body = "Routine"
    first_message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-inbox",
        connection_id=first_connection.connection_id,
        thread_id=first_thread.thread_id,
        provider_message_ref="message-first",
        internet_message_id=None,
        sender_address="buyer@example.test",
        recipient_addresses=("first@example.test",),
        subject="Project RFQ",
        received_at=now,
        body_text=first_body,
        content_sha256=digest_text(first_body),
        labels=("important", "project"),
        attachment_count=2,
        is_read=False,
    )
    second_message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-inbox",
        connection_id=second_connection.connection_id,
        thread_id=second_thread.thread_id,
        provider_message_ref="message-second",
        internet_message_id=None,
        sender_address="notice@example.test",
        recipient_addresses=("second@example.test",),
        subject="Routine notice",
        received_at=now - timedelta(minutes=1),
        body_text=second_body,
        content_sha256=digest_text(second_body),
        is_read=True,
    )
    await repository.save_message(first_message)
    await repository.save_message(second_message)
    candidate = MailActionCandidate(
        candidate_id=uuid4(),
        tenant_id="tenant-inbox",
        subject_id="owner-inbox",
        message_id=first_message.message_id,
        candidate_type=CandidateType.PROJECT,
        payload={"project_hint": "航材采购"},
        evidence=({"locator": "body:1"},),
        confidence=0.9,
    )
    await repository.save_candidate(candidate)

    page = await service.list_thread_page(
        tenant_id="tenant-inbox", subject_id="owner-inbox", limit=1
    )
    assert len(page.items) == 1
    assert page.items[0].account_email == "first@example.test"
    assert page.items[0].unread is True
    assert page.items[0].important is True
    assert page.items[0].has_attachment is True
    assert page.items[0].candidate_count == 1
    assert page.items[0].project_hint == "航材采购"
    assert page.has_more is True
    assert page.next_cursor is not None

    next_page = await service.list_thread_page(
        tenant_id="tenant-inbox",
        subject_id="owner-inbox",
        limit=1,
        cursor=page.next_cursor,
    )
    assert [item.thread_id for item in next_page.items] == [second_thread.thread_id]
    assert next_page.next_cursor is None

    assert [
        item.thread_id
        for item in (
            await service.list_thread_page(
                tenant_id="tenant-inbox",
                subject_id="owner-inbox",
                has_attachment=True,
            )
        ).items
    ] == [first_thread.thread_id]
    assert [
        item.thread_id
        for item in (
            await service.list_thread_page(
                tenant_id="tenant-inbox",
                subject_id="owner-inbox",
                candidate=True,
            )
        ).items
    ] == [first_thread.thread_id]

    with pytest.raises(ValueError, match="thread_cursor_scope_mismatch"):
        await service.list_thread_page(
            tenant_id="tenant-inbox",
            subject_id="owner-inbox",
            cursor=page.next_cursor,
            unread=True,
        )
