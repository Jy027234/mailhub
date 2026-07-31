from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from mailhub.connectors.sandbox import SandboxConnector
from mailhub.domain import (
    CandidateType,
    ConnectionStatus,
    MailActionCandidate,
    MailboxConnection,
    MailDraft,
    MailMessageProjection,
    MailThread,
    ProviderName,
    digest_text,
)
from mailhub.errors import ProviderFailureError
from mailhub.hosts.inmemory import RecordingKnowledgeLifecycle
from mailhub.intelligence import AnalysisPolicy
from mailhub.service import MailService, StaticCredentialBroker
from mailhub.storage import InMemoryMailRepository, InMemoryObjectStore


class _RecordingObjectStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.deleted_refs: list[str] = []

    async def delete(self, *, tenant_id: str, subject_id: str, object_ref: str) -> None:
        self.deleted_refs.append(object_ref)
        await super().delete(tenant_id=tenant_id, subject_id=subject_id, object_ref=object_ref)


class _RecordingCredentialRevocation:
    def __init__(self, result: dict[str, object] | None = None) -> None:
        self.calls = 0
        self.result = result or {"status": "revoked"}

    async def revoke(
        self, *, credential_ref: str, tenant_id: str, subject_id: str
    ) -> dict[str, object]:
        del credential_ref, tenant_id, subject_id
        self.calls += 1
        return dict(self.result)


def _thread(connection_id: UUID, *, tenant_id: str, subject_id: str) -> MailThread:
    del subject_id
    return MailThread(
        thread_id=uuid4(),
        tenant_id=tenant_id,
        connection_id=connection_id,
        provider_thread_ref="thread-1",
        normalized_subject="subject",
        participant_addresses=("sender@example.test", "user@example.test"),
        latest_at=datetime.now(UTC),
        message_count=1,
    )


@pytest.mark.asyncio
async def test_delete_connection_revokes_objects_and_retains_tombstone() -> None:
    repository = InMemoryMailRepository()
    object_store = InMemoryObjectStore()
    credentials = StaticCredentialBroker({"credential-ref": {"mode": "test"}})
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        object_store=object_store,
        credential_broker=credentials,
        credential_revocation=credentials,
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="credential-ref",
    )
    thread = _thread(connection.connection_id, tenant_id="tenant-1", subject_id="user-1")
    await repository.save_thread(thread)
    body = "private body"
    object_ref = await object_store.put_text(
        tenant_id="tenant-1",
        subject_id="user-1",
        purpose="mail_message",
        content=body,
        content_sha256=digest_text(body),
    )
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-1",
        connection_id=connection.connection_id,
        thread_id=thread.thread_id,
        provider_message_ref="provider-message-1",
        internet_message_id="<message-1@example.test>",
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject="subject",
        received_at=datetime.now(UTC),
        body_object_ref=object_ref,
        content_sha256=digest_text(body),
    )
    await repository.save_message(message)
    draft_body = "draft body"
    draft_ref = await object_store.put_text(
        tenant_id="tenant-1",
        subject_id="user-1",
        purpose="mail_draft",
        content=draft_body,
        content_sha256=digest_text(draft_body),
    )
    draft = MailDraft(
        draft_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="user-1",
        connection_id=connection.connection_id,
        thread_id=thread.thread_id,
        recipient_addresses=("sender@example.test",),
        subject="reply",
        body_text=None,
        body_object_ref=draft_ref,
        content_sha256=digest_text(draft_body),
    )
    await repository.save_draft(draft)

    deleted = await service.delete_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
    )

    assert deleted.status is ConnectionStatus.DELETED
    assert deleted.credential_ref.startswith("deleted:")
    assert deleted.granted_scopes == ()
    assert await repository.get_message(tenant_id="tenant-1", message_id=message.message_id) is None
    assert await repository.get_draft(tenant_id="tenant-1", draft_id=draft.draft_id) is None
    with pytest.raises(KeyError):
        await object_store.get_text(
            tenant_id="tenant-1", subject_id="user-1", object_ref=object_ref
        )
    with pytest.raises(KeyError):
        await object_store.get_text(tenant_id="tenant-1", subject_id="user-1", object_ref=draft_ref)
    assert repository.audits[-1]["event_type"] == "mail.connection.deleted"
    assert "credential-ref" not in str(repository.audits[-1])

    # A network retry may still carry the pre-delete revision.  A completed
    # tombstone is safe to return idempotently instead of turning that retry
    # into a revision conflict.
    retried = await service.delete_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
    )
    assert retried.status is ConnectionStatus.DELETED


@pytest.mark.asyncio
async def test_real_provider_revoke_calls_host_and_delete_is_not_double_revoke() -> None:
    repository = InMemoryMailRepository()
    revocation = _RecordingCredentialRevocation()
    service = MailService(
        repository,
        connectors={ProviderName.GMAIL: SandboxConnector()},
        credential_revocation=revocation,
    )
    pending = await service.create_connection(
        tenant_id="tenant-real-revoke",
        subject_id="user-real-revoke",
        provider=ProviderName.GMAIL,
        email_address="user@example.test",
        credential_ref="host-credential-ref",
        granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        provider_account_id="gmail-account-1",
        credential_version=1,
    )
    active = await service.activate_connection(
        tenant_id=pending.tenant_id,
        subject_id=pending.subject_id,
        connection_id=pending.connection_id,
        expected_revision=pending.revision,
    )

    revoked = await service.revoke_connection(
        tenant_id=active.tenant_id,
        subject_id=active.subject_id,
        connection_id=active.connection_id,
        expected_revision=active.revision,
    )

    assert revoked.status is ConnectionStatus.REVOKED
    assert revocation.calls == 1
    assert repository.audits[-1]["event_type"] == "mail.connection.revoked"
    assert repository.audits[-1]["provider_grant_status"] == "revoked"

    repeated = await service.revoke_connection(
        tenant_id=revoked.tenant_id,
        subject_id=revoked.subject_id,
        connection_id=revoked.connection_id,
        expected_revision=revoked.revision,
    )
    assert repeated.status is ConnectionStatus.REVOKED
    assert repeated.revision == revoked.revision
    assert revocation.calls == 1

    deleted = await service.delete_connection(
        tenant_id=revoked.tenant_id,
        subject_id=revoked.subject_id,
        connection_id=revoked.connection_id,
        expected_revision=revoked.revision,
    )
    assert deleted.status is ConnectionStatus.DELETED
    assert revocation.calls == 1


@pytest.mark.asyncio
async def test_real_provider_revoke_requires_host_and_keeps_active_state() -> None:
    repository = InMemoryMailRepository()
    service = MailService(
        repository,
        connectors={ProviderName.GMAIL: SandboxConnector()},
    )
    pending = await service.create_connection(
        tenant_id="tenant-revoke-gate",
        subject_id="user-revoke-gate",
        provider=ProviderName.GMAIL,
        email_address="user@example.test",
        credential_ref="host-credential-ref",
        granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    active = await service.activate_connection(
        tenant_id=pending.tenant_id,
        subject_id=pending.subject_id,
        connection_id=pending.connection_id,
        expected_revision=pending.revision,
    )

    with pytest.raises(ProviderFailureError, match="credential_revocation_unconfigured"):
        await service.revoke_connection(
            tenant_id=active.tenant_id,
            subject_id=active.subject_id,
            connection_id=active.connection_id,
            expected_revision=active.revision,
        )

    current = await repository.get_connection(
        tenant_id=active.tenant_id, connection_id=active.connection_id
    )
    assert current is not None and current.status is ConnectionStatus.ACTIVE


@pytest.mark.asyncio
async def test_real_provider_revoke_rejects_secret_shaped_host_proof() -> None:
    repository = InMemoryMailRepository()
    revocation = _RecordingCredentialRevocation(
        {"status": "revoked", "access_token": "must-not-cross"}
    )
    service = MailService(
        repository,
        connectors={ProviderName.GMAIL: SandboxConnector()},
        credential_revocation=revocation,
    )
    pending = await service.create_connection(
        tenant_id="tenant-revoke-secret",
        subject_id="user-revoke-secret",
        provider=ProviderName.GMAIL,
        email_address="user@example.test",
        credential_ref="host-credential-ref",
        granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    active = await service.activate_connection(
        tenant_id=pending.tenant_id,
        subject_id=pending.subject_id,
        connection_id=pending.connection_id,
        expected_revision=pending.revision,
    )

    with pytest.raises(ProviderFailureError, match="credential_revoke"):
        await service.revoke_connection(
            tenant_id=active.tenant_id,
            subject_id=active.subject_id,
            connection_id=active.connection_id,
            expected_revision=active.revision,
        )
    current = await repository.get_connection(
        tenant_id=active.tenant_id, connection_id=active.connection_id
    )
    assert current is not None and current.status is ConnectionStatus.ACTIVE


@pytest.mark.asyncio
async def test_non_sandbox_delete_fails_closed_without_credential_revocation() -> None:
    repository = InMemoryMailRepository()
    service = MailService(
        repository,
        connectors={ProviderName.GMAIL: SandboxConnector()},
    )
    connection = await repository.save_connection(
        # Sandbox connector is only a deterministic test transport; the domain
        # provider remains Gmail so production deletion cannot bypass revocation.
        MailboxConnection(
            connection_id=uuid4(),
            tenant_id="tenant-1",
            subject_id="user-1",
            provider=ProviderName.GMAIL,
            email_address="user@example.test",
            credential_ref="credential-ref",
            status=ConnectionStatus.ACTIVE,
        )
    )

    with pytest.raises(ProviderFailureError, match="credential_revocation_unconfigured"):
        await service.delete_connection(
            tenant_id="tenant-1",
            subject_id="user-1",
            connection_id=connection.connection_id,
            expected_revision=connection.revision,
        )
    current = await repository.get_connection(
        tenant_id="tenant-1", connection_id=connection.connection_id
    )
    assert current is not None and current.status is ConnectionStatus.DELETING

    # The failed attempt is resumable at the fenced DELETING revision once
    # the host credential authority becomes available.
    service.credential_revocation = StaticCredentialBroker({})
    resumed = await service.delete_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        connection_id=connection.connection_id,
        expected_revision=current.revision,
    )
    assert resumed.status is ConnectionStatus.DELETED


@pytest.mark.asyncio
async def test_delete_connection_revokes_downstream_knowledge_by_source_refs() -> None:
    repository = InMemoryMailRepository()
    lifecycle = RecordingKnowledgeLifecycle()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        knowledge_lifecycle=lifecycle,
        intelligence_policy=AnalysisPolicy(knowledge_min_confidence=0.5),
    )
    connection = await service.create_connection(
        tenant_id="tenant-knowledge",
        subject_id="user-knowledge",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="credential-ref",
    )
    thread = _thread(
        connection.connection_id,
        tenant_id="tenant-knowledge",
        subject_id="user-knowledge",
    )
    await repository.save_thread(thread)
    body = "Project: AL-42\nDecision: retain the approved maintenance record.\n" + "Context " * 40
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-knowledge",
        connection_id=connection.connection_id,
        thread_id=thread.thread_id,
        provider_message_ref="provider-message-knowledge",
        internet_message_id=None,
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject="Project knowledge",
        received_at=datetime.now(UTC),
        body_text=body,
        content_sha256=digest_text(body),
    )
    await repository.save_message(message)
    result = await service.analyze_message(
        tenant_id="tenant-knowledge",
        subject_id="user-knowledge",
        message_id=message.message_id,
    )
    assert result.knowledge_candidate is not None

    revoked = await service.revoke_connection(
        tenant_id="tenant-knowledge",
        subject_id="user-knowledge",
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
    )
    assert revoked.status is ConnectionStatus.REVOKED
    assert len(lifecycle.requests) == 1

    deleted = await service.delete_connection(
        tenant_id="tenant-knowledge",
        subject_id="user-knowledge",
        connection_id=connection.connection_id,
        expected_revision=revoked.revision,
    )

    assert deleted.status is ConnectionStatus.DELETED
    assert len(lifecycle.requests) == 2
    request = lifecycle.requests[0]
    assert request["message_ids"] == (str(message.message_id),)
    assert "body_text" not in str(request)
    proof = repository.audits[-1]["deletion_proof"]
    assert isinstance(proof, dict)
    assert proof["knowledge_lifecycle"]["status"] == "revoke_requested"

    retried = await service.delete_connection(
        tenant_id="tenant-knowledge",
        subject_id="user-knowledge",
        connection_id=connection.connection_id,
        expected_revision=deleted.revision,
    )
    assert retried.status is ConnectionStatus.DELETED
    assert len(lifecycle.requests) == 2


@pytest.mark.asyncio
async def test_delete_connection_blocks_if_knowledge_exists_without_lifecycle_port() -> None:
    repository = InMemoryMailRepository()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        intelligence_policy=AnalysisPolicy(knowledge_min_confidence=0.5),
    )
    connection = await service.create_connection(
        tenant_id="tenant-knowledge-blocked",
        subject_id="user-knowledge-blocked",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="credential-ref",
    )
    thread = _thread(
        connection.connection_id,
        tenant_id="tenant-knowledge-blocked",
        subject_id="user-knowledge-blocked",
    )
    await repository.save_thread(thread)
    body = "Project: AL-42\nDecision: keep this record.\n" + "Context " * 40
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-knowledge-blocked",
        connection_id=connection.connection_id,
        thread_id=thread.thread_id,
        provider_message_ref="provider-message-knowledge-blocked",
        internet_message_id=None,
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject="Project knowledge",
        received_at=datetime.now(UTC),
        body_text=body,
        content_sha256=digest_text(body),
    )
    await repository.save_message(message)
    await service.analyze_message(
        tenant_id="tenant-knowledge-blocked",
        subject_id="user-knowledge-blocked",
        message_id=message.message_id,
    )

    with pytest.raises(ProviderFailureError, match="knowledge_lifecycle_unconfigured"):
        await service.delete_connection(
            tenant_id="tenant-knowledge-blocked",
            subject_id="user-knowledge-blocked",
            connection_id=connection.connection_id,
            expected_revision=connection.revision,
        )


@pytest.mark.asyncio
async def test_export_contains_content_only_when_requested_and_never_credentials() -> None:
    repository = InMemoryMailRepository()
    object_store = InMemoryObjectStore()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        object_store=object_store,
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="credential-ref",
    )
    thread = _thread(connection.connection_id, tenant_id="tenant-1", subject_id="user-1")
    await repository.save_thread(thread)
    body = "exportable body"
    ref = await object_store.put_text(
        tenant_id="tenant-1",
        subject_id="user-1",
        purpose="mail_message",
        content=body,
        content_sha256=digest_text(body),
    )
    await repository.save_message(
        MailMessageProjection(
            message_id=uuid4(),
            tenant_id="tenant-1",
            connection_id=connection.connection_id,
            thread_id=thread.thread_id,
            provider_message_ref="provider-message-1",
            internet_message_id=None,
            sender_address="sender@example.test",
            recipient_addresses=("user@example.test",),
            subject="subject",
            received_at=datetime.now(UTC),
            body_object_ref=ref,
            content_sha256=digest_text(body),
        )
    )

    metadata_export = await service.export_subject_data(
        tenant_id="tenant-1", subject_id="user-1", include_content=False
    )
    assert metadata_export["schema_version"] == "mailhub.data_export.v1"
    assert "credential_ref" not in str(metadata_export)
    assert "body_text" not in str(metadata_export["messages"])
    assert "attachment_refs" not in str(metadata_export["drafts"])
    assert metadata_export["rule_executions"] == []

    content_export = await service.export_subject_data(
        tenant_id="tenant-1", subject_id="user-1", include_content=True
    )
    exported_messages = cast(list[dict[str, object]], content_export["messages"])
    assert exported_messages[0]["body_text"] == body
    assert "body_object_ref" not in str(content_export)
    assert repository.audits[-1]["event_type"] == "mail.data.exported"


@pytest.mark.asyncio
async def test_connection_cleanup_refs_cover_rows_beyond_user_page_limits() -> None:
    repository = InMemoryMailRepository()
    object_store = _RecordingObjectStore()
    lifecycle = RecordingKnowledgeLifecycle()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        object_store=object_store,
        knowledge_lifecycle=lifecycle,
    )
    connection = await service.create_connection(
        tenant_id="tenant-large",
        subject_id="user-large",
        provider=ProviderName.SANDBOX,
        email_address="large@example.test",
        credential_ref="credential-ref",
    )
    thread_id = uuid4()
    body_digest = digest_text("bounded body")
    for index in range(201):
        message_id = uuid4()
        await repository.save_message(
            MailMessageProjection(
                message_id=message_id,
                tenant_id="tenant-large",
                connection_id=connection.connection_id,
                thread_id=thread_id,
                provider_message_ref=f"provider-message-{index}",
                internet_message_id=None,
                sender_address="sender@example.test",
                recipient_addresses=("large@example.test",),
                subject=f"subject-{index}",
                received_at=datetime.now(UTC),
                body_object_ref=f"memory://body/{index}",
                content_sha256=body_digest,
            )
        )
        await repository.save_candidate(
            MailActionCandidate(
                candidate_id=uuid4(),
                tenant_id="tenant-large",
                subject_id="user-large",
                message_id=message_id,
                candidate_type=CandidateType.KNOWLEDGE,
                payload={},
                evidence=(),
                confidence=0.9,
            )
        )

    refs = await repository.list_connection_cleanup_refs(
        tenant_id="tenant-large",
        subject_id="user-large",
        connection_id=connection.connection_id,
    )
    assert len(refs.message_ids) == 201
    assert len(refs.knowledge_message_ids) == 201
    assert len(refs.object_refs) == 201

    preview = await service.connection_impact_preview(
        tenant_id="tenant-large",
        subject_id="user-large",
        connection_id=connection.connection_id,
    )
    preview_counts = cast(dict[str, object], preview["counts"])
    assert preview_counts["messages"] == 201
    assert preview_counts["governed_object_refs_estimate"] == 201
    preview_effects = cast(dict[str, object], preview["deletion_effects"])
    assert preview_effects["knowledge_lifecycle_proof_required"] is True

    deleted = await service.delete_connection(
        tenant_id="tenant-large",
        subject_id="user-large",
        connection_id=connection.connection_id,
        expected_revision=connection.revision,
    )
    assert deleted.status is ConnectionStatus.DELETED
    assert len(object_store.deleted_refs) == 201
    assert len(lifecycle.requests) == 1
    assert len(cast(tuple[str, ...], lifecycle.requests[0]["message_ids"])) == 201
