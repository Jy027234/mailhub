from datetime import UTC, datetime, timedelta

import pytest

from mailhub.connectors.sandbox import SandboxConnector
from mailhub.domain import ProviderName
from mailhub.service import MailService
from mailhub.storage import InMemoryMailRepository, InMemoryObjectStore
from mailhub.worker import MailWorker


@pytest.mark.asyncio
async def test_object_store_rejects_unverifiable_content() -> None:
    store = InMemoryObjectStore()
    expired = datetime.now(UTC) - timedelta(seconds=1)
    # An unverifiable object must be rejected before it can enter the cleanup
    # path.
    with pytest.raises(ValueError, match="object_content_digest_mismatch"):
        await store.put_text(
            tenant_id="tenant-1",
            subject_id="user-1",
            purpose="mail_message",
            content="retained only briefly",
            content_sha256="",
            expires_at=expired,
        )


@pytest.mark.asyncio
async def test_object_store_ttl_cleanup_audits_deleted_objects() -> None:
    from mailhub.domain import digest_text

    store = InMemoryObjectStore()
    ref = await store.put_text(
        tenant_id="tenant-1",
        subject_id="user-1",
        purpose="mail_message",
        content="expire me",
        content_sha256=digest_text("expire me"),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    repository = InMemoryMailRepository()
    service = MailService(repository, connectors={}, object_store=store)
    worker = MailWorker(service, worker_id="cleanup-worker")
    result = await worker.purge_expired_objects()
    assert result[0]["object_ref"] == ref
    assert result[0]["reason"] == "ttl_expired"
    assert repository.audits[0]["event_type"] == "mail.object.deleted"


@pytest.mark.asyncio
async def test_draft_object_uses_the_same_expiry_as_the_draft_revision() -> None:
    store = InMemoryObjectStore()
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        object_store=store,
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="sandbox",
    )
    draft = await service.create_draft(
        tenant_id="tenant-1",
        subject_id="user-1",
        connection_id=connection.connection_id,
        thread_id=None,
        recipient_addresses=("buyer@example.test",),
        subject="Subject",
        body_text="Body",
    )
    assert draft.body_object_ref is not None
    stored = store._objects[draft.body_object_ref]
    assert stored[4] == draft.expires_at


@pytest.mark.asyncio
async def test_draft_update_is_revision_bound_and_replaces_old_object() -> None:
    store = InMemoryObjectStore()
    service = MailService(
        InMemoryMailRepository(),
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        object_store=store,
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="sandbox",
    )
    draft = await service.create_draft(
        tenant_id="tenant-1",
        subject_id="user-1",
        connection_id=connection.connection_id,
        thread_id=None,
        recipient_addresses=("buyer@example.test",),
        subject="Subject",
        body_text="Body",
    )
    assert draft.body_object_ref is not None
    old_ref = draft.body_object_ref
    updated = await service.update_draft(
        tenant_id="tenant-1",
        subject_id="user-1",
        draft_id=draft.draft_id,
        expected_revision=draft.revision,
        connection_id=connection.connection_id,
        thread_id=None,
        recipient_addresses=("buyer@example.test",),
        subject="Updated subject",
        body_text="Updated body",
    )
    assert updated.revision == draft.revision + 1
    assert updated.body_object_ref is not None and updated.body_object_ref != old_ref
    with pytest.raises(KeyError):
        await store.get_text(tenant_id="tenant-1", subject_id="user-1", object_ref=old_ref)
    with pytest.raises(Exception, match="draft_revision_conflict"):
        await service.update_draft(
            tenant_id="tenant-1",
            subject_id="user-1",
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
            connection_id=connection.connection_id,
            thread_id=None,
            recipient_addresses=("buyer@example.test",),
            subject="Stale",
            body_text="Stale",
        )
