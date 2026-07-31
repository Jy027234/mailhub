"""Portable in-memory host assembly used by the second-host contract test."""

from __future__ import annotations

from uuid import UUID

from mailhub.connectors.sandbox import SandboxConnector
from mailhub.domain import ProviderName
from mailhub.hosts.inmemory import (
    RecordingHostActionAdapter,
    RecordingKnowledgeSafetyAdapter,
    RecordingKnowledgeSink,
)
from mailhub.service import InMemoryApprovalPort, MailService, StaticCredentialBroker
from mailhub.storage import InMemoryMailRepository, InMemoryObjectStore


class SecondHostIdentity:
    async def authorize(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        capability: str,
        connection_id: UUID | None = None,
    ) -> bool:
        return bool(tenant_id and subject_id and capability and connection_id is None)


def build_second_host() -> tuple[
    MailService,
    SandboxConnector,
    RecordingHostActionAdapter,
    RecordingKnowledgeSink,
]:
    repository = InMemoryMailRepository()
    connector = SandboxConnector()
    host_actions = RecordingHostActionAdapter()
    knowledge = RecordingKnowledgeSink()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        credential_broker=StaticCredentialBroker(),
        approval_port=InMemoryApprovalPort(),
        host_identity=SecondHostIdentity(),
        host_action_port=host_actions,
        knowledge_sink=knowledge,
        knowledge_safety=RecordingKnowledgeSafetyAdapter(),
        object_store=InMemoryObjectStore(),
    )
    return service, connector, host_actions, knowledge
