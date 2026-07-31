from datetime import UTC, datetime

import pytest

from mailhub.domain import CandidateState, ProviderName, digest_text
from mailhub.errors import AuthorizationError
from mailhub.ports import ProviderMessage
from mailhub.service import InMemoryApprovalPort
from mailhub.worker import MailWorker


@pytest.mark.asyncio
async def test_second_host_runs_autonomy_and_host_action_without_ca_platform_imports() -> None:
    from mailhub.hosts.second_host import build_second_host

    service, connector, host_actions, knowledge = build_second_host()
    connection = await service.create_connection(
        tenant_id="tenant-second-host",
        subject_id="owner-second-host",
        provider=ProviderName.SANDBOX,
        email_address="owner@second-host.test",
        credential_ref="sandbox-ref",
    )
    body = (
        "Project: SECOND-42\nTask: T-7\nDecision: approve the test handoff.\n"
        "We will send the evidence package by 2026-08-01.\n" + "Context " * 30
    )
    await connector.seed(
        ProviderMessage(
            provider_message_ref="second-host-message-1",
            provider_thread_ref="second-host-thread-1",
            internet_message_id="<second-host-1@example.test>",
            sender_address="supplier@second-host.test",
            recipient_addresses=("owner@second-host.test",),
            subject="Second host project follow-up",
            received_at=datetime.now(UTC),
            body_text=body,
            body_object_ref=None,
            content_sha256=digest_text(body),
        )
    )

    run = await MailWorker(service, worker_id="second-host-worker").autonomy_one(
        tenant_id="tenant-second-host",
        subject_id="owner-second-host",
        connection_id=connection.connection_id,
        replay_key="second-host-cycle",
    )
    candidates = await service.list_candidates(
        tenant_id="tenant-second-host", subject_id="owner-second-host", limit=20
    )
    project = next(item for item in candidates if item.candidate_type.value == "project")
    await service.review_candidate(
        tenant_id="tenant-second-host",
        subject_id="owner-second-host",
        candidate_id=project.candidate_id,
        approved=True,
        expected_revision=project.revision,
    )
    with pytest.raises(AuthorizationError, match="candidate_approval_invalid"):
        await service.apply_candidate(
            tenant_id="tenant-second-host",
            subject_id="owner-second-host",
            candidate_id=project.candidate_id,
            approval_ref="untrusted-string",
        )
    assert not host_actions.executions
    assert isinstance(service.approval_port, InMemoryApprovalPort)
    service.approval_port.add_confirmation("second-host-approval")
    applied = await service.apply_candidate(
        tenant_id="tenant-second-host",
        subject_id="owner-second-host",
        candidate_id=project.candidate_id,
        approval_ref="second-host-approval",
    )
    replayed = await service.apply_candidate(
        tenant_id="tenant-second-host",
        subject_id="owner-second-host",
        candidate_id=project.candidate_id,
        approval_ref="second-host-approval",
    )

    assert run.mode == "recommend_only"
    assert applied["status"] == "applied"
    assert replayed["replayed"] is True
    assert host_actions.executions
    assert len(host_actions.executions) == 1
    knowledge_candidate = next(
        item for item in candidates if item.candidate_type.value == "knowledge"
    )
    await service.review_candidate(
        tenant_id="tenant-second-host",
        subject_id="owner-second-host",
        candidate_id=knowledge_candidate.candidate_id,
        approved=True,
        expected_revision=knowledge_candidate.revision,
    )
    knowledge_result = await service.apply_candidate(
        tenant_id="tenant-second-host",
        subject_id="owner-second-host",
        candidate_id=knowledge_candidate.candidate_id,
        approval_ref="second-host-approval",
    )
    replayed_knowledge = await service.apply_candidate(
        tenant_id="tenant-second-host",
        subject_id="owner-second-host",
        candidate_id=knowledge_candidate.candidate_id,
        approval_ref="second-host-approval",
    )
    assert knowledge_result["status"] == "awaiting_host_review"
    assert replayed_knowledge["replayed"] is True
    assert len(knowledge.submissions) == 1
    assert (
        await service.list_candidates(
            tenant_id="other-tenant", subject_id="owner-second-host", limit=20
        )
        == ()
    )
    stored = await service.repository.get_candidate(
        tenant_id="tenant-second-host", candidate_id=project.candidate_id
    )
    assert stored is not None and stored.status is CandidateState.APPLIED
