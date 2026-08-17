from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mailhub.connectors.sandbox import SandboxConnector
from mailhub.domain import (
    ActionType,
    AgentActionContext,
    AgentActionRequest,
    CandidateState,
    CandidateType,
    MailActionCandidate,
    MailMessageProjection,
    MailThread,
    ProviderName,
    digest_text,
)
from mailhub.hosts.inmemory import (
    RecordingAgentMemory,
    RecordingHostActionAdapter,
    RecordingKnowledgeSafetyAdapter,
    RecordingKnowledgeSink,
)
from mailhub.hosts.migration import (
    MigrationController,
    MigrationFeatureFlags,
    NoDualSenderInterlock,
    compare_shadow,
)
from mailhub.ports import ProviderMessage
from mailhub.service import InMemoryApprovalPort, MailService, _deduplicate_candidates
from mailhub.storage import InMemoryMailRepository, InMemoryObjectStore


@pytest.mark.asyncio
async def test_analysis_persists_evidence_candidates_and_review_submits_knowledge() -> None:
    repository = InMemoryMailRepository()
    connector = SandboxConnector()
    memory = RecordingAgentMemory()

    class ApprovedKnowledgeSink(RecordingKnowledgeSink):
        async def submit_candidate(
            self,
            *,
            tenant_id: str,
            subject_id: str,
            message: MailMessageProjection,
            candidate: MailActionCandidate,
        ) -> Mapping[str, object]:
            result = await super().submit_candidate(
                tenant_id=tenant_id,
                subject_id=subject_id,
                message=message,
                candidate=candidate,
            )
            return {
                **dict(result),
                "status": "approved",
                "knowledge_ref": "knowledge-approved-1",
                "approved_at": datetime.now(UTC).isoformat(),
            }

    knowledge = ApprovedKnowledgeSink()
    knowledge_safety = RecordingKnowledgeSafetyAdapter()
    approval = InMemoryApprovalPort()
    approval.add_confirmation("review:knowledge-candidate")
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        knowledge_sink=knowledge,
        agent_memory=memory,
        knowledge_safety=knowledge_safety,
        object_store=InMemoryObjectStore(),
        approval_port=approval,
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="sandbox",
    )
    body = (
        "Project: AL-42 Task: T-9 is due 2026-08-01. "
        "This decision records a durable supplier qualification outcome and the "
        "follow-up evidence required by the project team. "
    )
    await connector.seed(
        ProviderMessage(
            provider_message_ref="m-1",
            provider_thread_ref="t-1",
            internet_message_id="<m-1@example.test>",
            sender_address="buyer@example.test",
            recipient_addresses=("user@example.test",),
            subject="Project decision",
            received_at=datetime.now(UTC),
            body_text=body,
            body_object_ref=None,
            content_sha256=digest_text(body),
        )
    )
    await service.sync_connection(
        tenant_id="tenant-1", subject_id="user-1", connection_id=connection.connection_id
    )
    message = (await repository.list_messages(tenant_id="tenant-1", subject_id="user-1", limit=10))[
        0
    ]
    assert message.body_text is None
    result = await service.analyze_message(
        tenant_id="tenant-1", subject_id="user-1", message_id=message.message_id
    )
    assert result.injection_detected is False
    candidates = await service.list_candidates(tenant_id="tenant-1", subject_id="user-1")
    assert {candidate.candidate_type for candidate in candidates} == {
        CandidateType.PROJECT,
        CandidateType.TASK,
        CandidateType.KNOWLEDGE,
    }
    project_candidates = await service.list_candidates(
        tenant_id="tenant-1", subject_id="user-1", candidate_type=CandidateType.PROJECT
    )
    assert project_candidates
    assert all(item.candidate_type is CandidateType.PROJECT for item in project_candidates)
    knowledge_candidate = next(
        candidate for candidate in candidates if candidate.candidate_type is CandidateType.KNOWLEDGE
    )
    reviewed = await service.review_candidate(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=knowledge_candidate.candidate_id,
        approved=True,
        expected_revision=knowledge_candidate.revision,
    )
    assert reviewed.status is CandidateState.APPROVED
    assert len(knowledge.submissions) == 0
    await service.apply_candidate(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=knowledge_candidate.candidate_id,
        approval_ref="review:knowledge-candidate",
    )
    assert len(knowledge.submissions) == 1
    applied = await repository.get_candidate(
        tenant_id="tenant-1", candidate_id=knowledge_candidate.candidate_id
    )
    assert applied is not None
    assert applied.status is CandidateState.APPLIED
    assert applied.payload["security_state"] == "cleared"
    assert applied.payload["rights_state"] == "approved"
    assert len(memory.references) == 1
    assert memory.references[0].knowledge_ref == "knowledge-approved-1"
    application_result = applied.payload["application_result"]
    assert isinstance(application_result, Mapping)
    assert "memory" in application_result
    project_candidate = next(
        candidate for candidate in candidates if candidate.candidate_type is CandidateType.PROJECT
    )
    with pytest.raises(ValueError, match="candidate_rejection_reason_required"):
        await service.review_candidate(
            tenant_id="tenant-1",
            subject_id="user-1",
            candidate_id=project_candidate.candidate_id,
            approved=False,
            expected_revision=project_candidate.revision,
        )
    rejected = await service.review_candidate(
        tenant_id="tenant-1",
        subject_id="user-1",
        candidate_id=project_candidate.candidate_id,
        approved=False,
        expected_revision=project_candidate.revision,
        review_reason="Project reference needs owner confirmation",
    )
    assert rejected.status is CandidateState.REJECTED
    assert rejected.payload["review_reason"] == "Project reference needs owner confirmation"
    await service.analyze_message(
        tenant_id="tenant-1", subject_id="user-1", message_id=message.message_id
    )
    replayed_candidates = await service.list_candidates(tenant_id="tenant-1", subject_id="user-1")
    assert len(replayed_candidates) == len(candidates)


@pytest.mark.asyncio
async def test_approved_project_candidate_requires_host_action_adapter() -> None:
    host = RecordingHostActionAdapter()
    # The apply path is exercised by a persisted synthetic candidate in the
    # dedicated service/host contract; this assertion protects the adapter's
    # explicit approval boundary.
    proposal = await host.propose(
        action=AgentActionRequest(
            action_id=uuid4(),
            action_type=ActionType.UPDATE_TASK,
            context=AgentActionContext(
                tenant_id="tenant-1",
                agent_subject_id="user-1",
                connection_id=None,
                folder_ref=None,
                thread_id=None,
            ),
            input_digest=digest_text("candidate"),
        )
    )
    assert proposal["status"] == "proposed"


@pytest.mark.asyncio
async def test_project_candidate_apply_forwards_bounded_host_action_contract() -> None:
    repository = InMemoryMailRepository()
    host = RecordingHostActionAdapter()
    approval = InMemoryApprovalPort()
    approval.add_confirmation("project-approval")
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        host_action_port=host,
        approval_port=approval,
    )
    connection = await service.create_connection(
        tenant_id="tenant-project",
        subject_id="owner-project",
        provider=ProviderName.SANDBOX,
        email_address="owner@project.example.test",
        credential_ref="sandbox",
    )
    thread_id = uuid4()
    message_id = uuid4()
    now = datetime.now(UTC)
    await repository.save_thread(
        MailThread(
            thread_id=thread_id,
            tenant_id="tenant-project",
            connection_id=connection.connection_id,
            provider_thread_ref="provider-thread-project",
            normalized_subject="Project update",
            participant_addresses=("owner@project.example.test", "supplier@example.test"),
            latest_at=now,
            message_count=1,
        )
    )
    await repository.save_message(
        MailMessageProjection(
            message_id=message_id,
            tenant_id="tenant-project",
            connection_id=connection.connection_id,
            thread_id=thread_id,
            provider_message_ref="provider-message-project",
            internet_message_id=None,
            sender_address="supplier@example.test",
            recipient_addresses=("owner@project.example.test",),
            subject="Project update",
            received_at=now,
            body_text="Project AL-42 task T-9 is due next week.",
            content_sha256=digest_text("Project AL-42 task T-9 is due next week."),
        )
    )
    candidate = MailActionCandidate(
        candidate_id=uuid4(),
        tenant_id="tenant-project",
        subject_id="owner-project",
        message_id=message_id,
        candidate_type=CandidateType.PROJECT,
        payload={
            "project_refs": ["AL-42"],
            "task_refs": ["T-9"],
            "summary": "Update the project task.",
            "body_text": "This raw field must not cross HostActionPort.",
        },
        evidence=({"locator": "body:1", "text": "raw evidence must not cross"},),
        confidence=0.92,
    )
    await repository.save_candidate(candidate)
    approved = await service.review_candidate(
        tenant_id="tenant-project",
        subject_id="owner-project",
        candidate_id=candidate.candidate_id,
        approved=True,
        expected_revision=candidate.revision,
    )
    result = await service.apply_candidate(
        tenant_id="tenant-project",
        subject_id="owner-project",
        candidate_id=approved.candidate_id,
        approval_ref="project-approval",
    )

    assert result["status"] == "applied"
    assert len(host.executions) == 1
    action = host.executions[0][0]
    assert action.parameters["candidate_id"] == str(candidate.candidate_id)
    assert action.parameters["candidate_revision"] == approved.revision
    payload = action.parameters["candidate_payload"]
    assert isinstance(payload, Mapping)
    assert payload["project_refs"] == ["AL-42"]
    assert "body_text" not in payload
    evidence = action.parameters["candidate_evidence"]
    assert isinstance(evidence, list)
    assert "text" not in evidence[0]


@pytest.mark.asyncio
async def test_host_action_adapter_replays_by_stable_action_id() -> None:
    host = RecordingHostActionAdapter()
    action = AgentActionRequest(
        action_id=uuid4(),
        action_type=ActionType.CREATE_TASK,
        context=AgentActionContext(
            tenant_id="tenant-1",
            agent_subject_id="user-1",
            connection_id=None,
            folder_ref=None,
            thread_id=None,
        ),
        input_digest=digest_text("candidate"),
    )
    first = await host.execute(action=action, approval_ref="approval-1")
    replay = await host.execute(action=action, approval_ref="approval-1")
    assert replay == first
    assert len(host.executions) == 1


def test_duplicate_project_action_keeps_lineage_for_review() -> None:
    first = MailActionCandidate(
        candidate_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="user-1",
        message_id=uuid4(),
        candidate_type=CandidateType.PROJECT,
        payload={"refs": ("AL-42",), "action_type": "follow_up"},
        evidence=(),
        confidence=0.9,
    )
    second = MailActionCandidate(
        candidate_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="user-1",
        message_id=uuid4(),
        candidate_type=CandidateType.PROJECT,
        payload={"refs": ("AL-42",), "action_type": "follow_up"},
        evidence=(),
        confidence=0.88,
    )
    deduplicated = _deduplicate_candidates((second,), (first,))
    assert len(deduplicated) == 1
    assert deduplicated[0].payload["action_dedupe_status"] == "duplicate_requires_review"
    assert deduplicated[0].payload["duplicate_action_refs"] == (str(first.candidate_id),)


@pytest.mark.asyncio
async def test_migration_interlock_and_shadow_comparison_are_fail_closed() -> None:
    interlock = NoDualSenderInterlock()
    assert await interlock.claim(tenant_id="tenant-1", account_ref="account-1", owner="mailhub")
    assert not await interlock.claim(tenant_id="tenant-1", account_ref="account-1", owner="legacy")
    await interlock.release(tenant_id="tenant-1", account_ref="account-1", owner="mailhub")
    comparison = compare_shadow(
        tenant_id="tenant-1",
        legacy_system="aerolink",
        legacy_ref="legacy-1",
        mailhub_ref="mail-1",
        legacy={"identity": "same", "cursor": "1", "content_sha256": "a"},
        mailhub={"identity": "same", "cursor": "2", "content_sha256": "a"},
    )
    assert comparison.authority_safe is False


@pytest.mark.asyncio
async def test_migration_controller_requires_ordered_flags_and_single_sender() -> None:
    controller = MigrationController()
    batch = await controller.start(
        tenant_id="tenant-1",
        legacy_system="aerolink",
        account_ref="account-1",
        owner="mailhub",
        flags=MigrationFeatureFlags(
            shadow_read=True, mailhub_read_authority=True, mailhub_send_authority=True
        ),
    )
    assert batch.flags.phase.value == "mailhub_send_authority"
    assert await controller.claim_send_authority(batch_id=batch.batch_id)
    assert not await controller.interlock.claim(
        tenant_id="tenant-1", account_ref="account-1", owner="legacy"
    )
    await controller.release_send_authority(batch_id=batch.batch_id)
    assert controller.batches[batch.batch_id].state == "rolled_back"
    assert [event.event_type for event in controller.events] == [
        "mail.migration.shadow_compared",
        "mail.migration.authority_changed",
        "mail.migration.rollback_completed",
    ]
    assert all("body" not in event.to_dict() for event in controller.events)


class _MutableAiExecution:
    """Fake model gateway whose output changes per call (regression driver)."""

    def __init__(self) -> None:
        self.calls = 0

    async def structure(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        operation: str,
        source: Mapping[str, object],
        schema: Mapping[str, object],
    ) -> Mapping[str, object]:
        del tenant_id, subject_id, operation, schema
        baseline = source.get("baseline", {})
        assert isinstance(baseline, dict)
        self.calls += 1
        return {
            "summary": f"模型摘要第 {self.calls} 版",
            "action_candidates": [
                {
                    "action_type": "follow_up",
                    "project_refs": list(baseline.get("project_refs", [])),
                    "task_refs": list(baseline.get("task_refs", [])),
                    "due_date_refs": list(baseline.get("date_refs", [])),
                    "decisions": [f"第 {self.calls} 版决定"],
                    "risks": list(baseline.get("risks", [])),
                    "commitments": list(baseline.get("commitments", [])),
                    "source_message_id": str(source.get("message_id", "")),
                    "confidence": 0.9,
                    "requires_review": True,
                }
            ],
            "knowledge_candidate": None,
            "confidence": 0.9,
            "model_ref": "mutable-test-model",
            "decisions": [],
            "risks": [],
            "commitments": [],
        }


@pytest.mark.asyncio
async def test_reanalysis_assigns_fresh_revisions_instead_of_unique_conflict() -> None:
    """Regression: a second analysis of the same message must not violate the
    (message, type, revision) unique key when the model output changed.
    """

    repository = InMemoryMailRepository()
    connector = SandboxConnector()
    ai = _MutableAiExecution()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: connector},
        ai_execution=ai,
        object_store=InMemoryObjectStore(),
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="sandbox",
    )
    body = "Project: AL-42 must close by 2026-08-20. Please follow up."
    await connector.seed(
        ProviderMessage(
            provider_message_ref="m-reanalyze",
            provider_thread_ref="t-reanalyze",
            internet_message_id="<m-reanalyze@example.test>",
            sender_address="buyer@example.test",
            recipient_addresses=("user@example.test",),
            subject="Reanalysis",
            received_at=datetime.now(UTC),
            body_text=body,
            body_object_ref=None,
            content_sha256=digest_text(body),
        )
    )
    await service.sync_connection(
        tenant_id="tenant-1", subject_id="user-1", connection_id=connection.connection_id
    )
    message = (await repository.list_messages(tenant_id="tenant-1", subject_id="user-1"))[0]

    await service.analyze_message(
        tenant_id="tenant-1", subject_id="user-1", message_id=message.message_id
    )
    await service.analyze_message(
        tenant_id="tenant-1", subject_id="user-1", message_id=message.message_id
    )

    project_candidates = await service.list_candidates(
        tenant_id="tenant-1", subject_id="user-1", candidate_type=CandidateType.PROJECT
    )
    revisions = sorted(item.revision for item in project_candidates)
    assert revisions == [1, 2]
    assert len({item.candidate_id for item in project_candidates}) == 2
    second = next(item for item in project_candidates if item.revision == 2)
    assert second.payload.get("action_dedupe_status") == "duplicate_requires_review"
