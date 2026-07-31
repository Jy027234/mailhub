"""Governed bridge from approved knowledge references to Agent memory.

MailHub never writes raw email content to Agent memory.  The only accepted
input is a host-issued knowledge reference with approval, rights, security and
source-digest provenance.  Hosts remain authoritative for indexing, recall and
deletion of the referenced knowledge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ApprovedKnowledgeReference:
    """Body-free, approval-bound reference eligible for Agent memory."""

    tenant_id: str
    subject_id: str
    candidate_id: UUID
    source_message_id: UUID
    knowledge_ref: str
    approval_ref: str
    content_sha256: str
    scope: str
    rights_state: str
    security_state: str
    approved_at: datetime

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.tenant_id, "tenant_id", 200),
            (self.subject_id, "subject_id", 200),
            (self.knowledge_ref, "knowledge_ref", 500),
            (self.approval_ref, "approval_ref", 500),
            (self.scope, "scope", 100),
            (self.rights_state, "rights_state", 80),
            (self.security_state, "security_state", 80),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"memory_{name}_invalid")
        if not _DIGEST_RE.fullmatch(self.content_sha256):
            raise ValueError("memory_content_digest_invalid")
        if self.rights_state != "approved":
            raise ValueError("memory_rights_not_approved")
        if self.security_state != "cleared":
            raise ValueError("memory_security_not_cleared")
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("memory_approval_timestamp_not_aware")
        object.__setattr__(self, "approved_at", self.approved_at.astimezone(UTC))


def reference_payload(reference: ApprovedKnowledgeReference) -> dict[str, object]:
    """Serialize only governed identifiers; this function cannot emit a body."""

    return {
        "tenant_id": reference.tenant_id,
        "subject_id": reference.subject_id,
        "candidate_id": str(reference.candidate_id),
        "source_message_id": str(reference.source_message_id),
        "knowledge_ref": reference.knowledge_ref,
        "approval_ref": reference.approval_ref,
        "content_sha256": reference.content_sha256,
        "scope": reference.scope,
        "rights_state": reference.rights_state,
        "security_state": reference.security_state,
        "approved_at": reference.approved_at.isoformat(),
    }
