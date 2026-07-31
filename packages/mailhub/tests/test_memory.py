from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mailhub.hosts.inmemory import RecordingAgentMemory
from mailhub.memory import ApprovedKnowledgeReference, reference_payload


def _reference() -> ApprovedKnowledgeReference:
    return ApprovedKnowledgeReference(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=uuid4(),
        source_message_id=uuid4(),
        knowledge_ref="knowledge:1",
        approval_ref="approval:1",
        content_sha256="a" * 64,
        scope="project",
        rights_state="approved",
        security_state="cleared",
        approved_at=datetime.now(UTC),
    )


def test_memory_payload_is_body_free_and_requires_approved_states() -> None:
    reference = _reference()
    payload = reference_payload(reference)

    assert payload["knowledge_ref"] == "knowledge:1"
    assert "body_text" not in payload
    assert "content" not in payload
    with pytest.raises(ValueError, match="memory_rights_not_approved"):
        ApprovedKnowledgeReference(
            tenant_id=reference.tenant_id,
            subject_id=reference.subject_id,
            candidate_id=reference.candidate_id,
            source_message_id=reference.source_message_id,
            knowledge_ref=reference.knowledge_ref,
            approval_ref=reference.approval_ref,
            content_sha256=reference.content_sha256,
            scope=reference.scope,
            rights_state="awaiting_review",
            security_state=reference.security_state,
            approved_at=reference.approved_at,
        )


@pytest.mark.asyncio
async def test_agent_memory_is_idempotent_by_candidate_reference() -> None:
    memory = RecordingAgentMemory()
    reference = _reference()

    first = await memory.store_approved_reference(reference=reference)
    replay = await memory.store_approved_reference(reference=reference)

    assert replay == first
    assert len(memory.references) == 1
