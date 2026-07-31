"""Explicit local host adapters used by portability/contract tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from mailhub.domain import (
    AgentActionRequest,
    MailActionCandidate,
    MailMessageProjection,
    ProviderName,
)
from mailhub.events import validate_event_payload
from mailhub.memory import ApprovedKnowledgeReference


class RecordingHostActionAdapter:
    """A deterministic host fact-owner double; it is not a production adapter."""

    def __init__(self, objects: Sequence[Mapping[str, object]] = ()) -> None:
        self.objects = [dict(item) for item in objects]
        self.proposals: list[AgentActionRequest] = []
        self.executions: list[tuple[AgentActionRequest, str]] = []
        self._execution_results: dict[str, Mapping[str, object]] = {}

    async def discover(
        self, *, tenant_id: str, subject_id: str, query: str | None = None
    ) -> Sequence[Mapping[str, object]]:
        del tenant_id, subject_id
        if not query:
            return tuple(self.objects)
        normalized = query.casefold()
        return tuple(item for item in self.objects if normalized in str(item).casefold())

    async def propose(self, *, action: AgentActionRequest) -> Mapping[str, object]:
        self.proposals.append(action)
        return {
            "proposal_id": f"proposal:{action.action_id}",
            "status": "proposed",
            "action_id": str(action.action_id),
            "input_digest": action.input_digest,
        }

    async def execute(
        self, *, action: AgentActionRequest, approval_ref: str
    ) -> Mapping[str, object]:
        if not approval_ref.strip():
            raise ValueError("approval_ref_required")
        replay = self._execution_results.get(str(action.action_id))
        if replay is not None:
            return dict(replay)
        self.executions.append((action, approval_ref))
        result = {
            "execution_id": f"execution:{action.action_id}",
            "status": "applied",
            "action_id": str(action.action_id),
            "approval_ref": approval_ref,
        }
        self._execution_results[str(action.action_id)] = result
        return result


class RecordingKillSwitch:
    """Deterministic tenant/provider/global kill-switch contract double.

    The double models a host-owned decision endpoint.  It intentionally has no
    mutation API resembling an agent action; tests configure the state directly
    and inspect the bounded checks made by MailHub.
    """

    def __init__(
        self,
        *,
        global_enabled: bool = True,
        blocked_tenants: Sequence[str] = (),
        blocked_providers: Sequence[ProviderName] = (),
        blocked_operations: Sequence[str] = (),
    ) -> None:
        self.global_enabled = global_enabled
        self.blocked_tenants = set(blocked_tenants)
        self.blocked_providers = set(blocked_providers)
        self.blocked_operations = set(blocked_operations)
        self.checks: list[Mapping[str, object]] = []

    async def check(
        self,
        *,
        tenant_id: str,
        subject_id: str | None,
        provider: ProviderName | None,
        operation: str,
    ) -> Mapping[str, object]:
        request = {
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            "provider": provider.value if provider is not None else None,
            "operation": operation,
        }
        self.checks.append(request)
        if not self.global_enabled:
            return {"allowed": False, "reason": "global_kill_switch_active", "scope": "global"}
        if tenant_id in self.blocked_tenants:
            return {"allowed": False, "reason": "tenant_kill_switch_active", "scope": "tenant"}
        if provider is not None and provider in self.blocked_providers:
            return {
                "allowed": False,
                "reason": "provider_kill_switch_active",
                "scope": "provider",
            }
        if operation in self.blocked_operations:
            return {
                "allowed": False,
                "reason": "operation_kill_switch_active",
                "scope": "operation",
            }
        return {"allowed": True, "scope": "none"}


class RecordingKnowledgeSink:
    """Review-only knowledge sink double; publication stays host-owned."""

    def __init__(self) -> None:
        self.submissions: list[tuple[MailMessageProjection, MailActionCandidate]] = []
        self._submission_results: dict[str, Mapping[str, object]] = {}

    async def submit_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        message: MailMessageProjection,
        candidate: MailActionCandidate,
    ) -> Mapping[str, object]:
        if message.tenant_id != tenant_id or candidate.subject_id != subject_id:
            raise ValueError("knowledge_scope_mismatch")
        replay = self._submission_results.get(str(candidate.candidate_id))
        if replay is not None:
            return dict(replay)
        self.submissions.append((message, candidate))
        result = {
            "knowledge_candidate_ref": f"knowledge:{candidate.candidate_id}",
            "status": "awaiting_host_review",
            "source_message_id": str(message.message_id),
        }
        self._submission_results[str(candidate.candidate_id)] = result
        return result


class RecordingKnowledgeLifecycle:
    """Idempotent source-lifecycle double; it never receives message bodies."""

    def __init__(self) -> None:
        self.requests: list[Mapping[str, object]] = []
        self._results: dict[str, Mapping[str, object]] = {}

    async def revoke_source(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        message_ids: Sequence[UUID],
        reason: str,
        request_id: str,
    ) -> Mapping[str, object]:
        if not tenant_id.strip() or not subject_id.strip() or not reason.strip():
            raise ValueError("knowledge_lifecycle_scope_invalid")
        if not request_id.strip() or len(request_id) > 300:
            raise ValueError("knowledge_lifecycle_request_id_invalid")
        replay = self._results.get(request_id)
        if replay is not None:
            return dict(replay)
        request = {
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            "connection_id": str(connection_id),
            "message_ids": tuple(str(message_id) for message_id in message_ids),
            "reason": reason,
            "request_id": request_id,
        }
        self.requests.append(request)
        result = {
            "status": "revoke_requested",
            "request_id": request_id,
            "revoked_count": len(message_ids),
        }
        self._results[request_id] = result
        return dict(result)


class RecordingAgentMemory:
    """Memory double that accepts only body-free approved references."""

    def __init__(self) -> None:
        self.references: list[ApprovedKnowledgeReference] = []
        self._results: dict[str, Mapping[str, object]] = {}

    async def store_approved_reference(
        self, *, reference: ApprovedKnowledgeReference
    ) -> Mapping[str, object]:
        replay = self._results.get(str(reference.candidate_id))
        if replay is not None:
            return dict(replay)
        self.references.append(reference)
        result = {
            "status": "stored",
            "memory_ref": f"memory:{reference.candidate_id}",
            "candidate_id": str(reference.candidate_id),
        }
        self._results[str(reference.candidate_id)] = result
        return dict(result)


class RecordingKnowledgeSafetyAdapter:
    """Deterministic test gate; production must use AV/DLP/rights services."""

    def __init__(self, *, security_state: str = "cleared", rights_state: str = "approved") -> None:
        self.security_state = security_state
        self.rights_state = rights_state
        self.evaluations: list[tuple[MailMessageProjection, MailActionCandidate]] = []

    async def evaluate_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        message: MailMessageProjection,
        candidate: MailActionCandidate,
    ) -> Mapping[str, object]:
        if message.tenant_id != tenant_id or candidate.subject_id != subject_id:
            raise ValueError("knowledge_scope_mismatch")
        self.evaluations.append((message, candidate))
        return {
            "security_state": self.security_state,
            "rights_state": self.rights_state,
            "gate_ref": f"knowledge-gate:{candidate.candidate_id}",
        }


class RecordingEventPublisher:
    """Append-only event bus double with stable replay suppression."""

    def __init__(self) -> None:
        self.events: list[Mapping[str, object]] = []
        self._event_ids: set[str] = set()

    async def publish(self, event: Mapping[str, object]) -> None:
        validate_event_payload(event)
        event_id = str(event["event_id"])
        if event_id in self._event_ids:
            return
        self._event_ids.add(event_id)
        self.events.append(dict(event))
