from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mailhub.connectors.sandbox import SandboxConnector
from mailhub.domain import (
    ActionType,
    AutomationLevel,
    DelegationGrant,
    MailAgentPolicy,
    MailMessageProjection,
    MailThread,
    ProviderName,
    digest_text,
)
from mailhub.errors import KillSwitchError
from mailhub.hosts.inmemory import RecordingHostActionAdapter, RecordingKillSwitch
from mailhub.rules import (
    MailRule,
    RuleCondition,
    RuleExecutionStatus,
    RuleField,
    RuleOperator,
)
from mailhub.service import MailService
from mailhub.storage import InMemoryMailRepository


async def _seed_service(
    *, enabled: bool = False, kill_switch: RecordingKillSwitch | None = None
) -> tuple[
    MailService,
    InMemoryMailRepository,
    RecordingHostActionAdapter,
    MailRule,
    tuple[UUID, ...],
]:
    repository = InMemoryMailRepository()
    host = RecordingHostActionAdapter()
    service = MailService(
        repository,
        connectors={ProviderName.SANDBOX: SandboxConnector()},
        host_action_port=host,
        rule_automation_enabled=enabled,
        kill_switch=kill_switch,
    )
    connection = await service.create_connection(
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=ProviderName.SANDBOX,
        email_address="user@example.test",
        credential_ref="sandbox-ref",
    )
    message_ids: list[UUID] = []
    for index in range(2):
        thread = MailThread(
            thread_id=uuid4(),
            tenant_id="tenant-1",
            connection_id=connection.connection_id,
            provider_thread_ref=f"thread-{index}",
            normalized_subject="hello request",
            participant_addresses=("sender@example.test", "user@example.test"),
            latest_at=datetime.now(UTC),
            message_count=1,
        )
        await repository.save_thread(thread)
        message = MailMessageProjection(
            message_id=uuid4(),
            tenant_id="tenant-1",
            connection_id=connection.connection_id,
            thread_id=thread.thread_id,
            provider_message_ref=f"message-{index}",
            internet_message_id=None,
            sender_address="sender@example.test",
            recipient_addresses=("user@example.test",),
            subject="hello request",
            received_at=datetime.now(UTC),
            body_text="hello body",
            content_sha256=digest_text("hello body"),
        )
        await repository.save_message(message)
        message_ids.append(message.message_id)
    rule = MailRule(
        rule_id=uuid4(),
        tenant_id="tenant-1",
        owner_subject_id="user-1",
        name="label hello",
        conditions=(
            RuleCondition(
                field=RuleField.SUBJECT,
                operator=RuleOperator.CONTAINS,
                value="hello",
            ),
        ),
        action_type=ActionType.LABEL,
        action_params={"label": "triage"},
        automation_level=AutomationLevel.L3A_BOUNDED_ORGANIZE,
        max_per_hour=1,
        enabled=True,
    )
    await service.create_rule(rule)
    policy = MailAgentPolicy(
        policy_id=uuid4(),
        tenant_id="tenant-1",
        owner_subject_id="user-1",
        allowed_connection_ids=frozenset({connection.connection_id}),
        allowed_folder_refs=frozenset({"INBOX"}),
        allowed_actions=frozenset({ActionType.LABEL}),
        allowed_automation_level=AutomationLevel.L3A_BOUNDED_ORGANIZE,
    )
    await repository.save_policy(policy)
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-1",
        policy_id=policy.policy_id,
        agent_subject_id="user-1",
        granted_by_subject_id="user-1",
        capability_ids=frozenset({ActionType.LABEL}),
    )
    await repository.save_grant(grant)
    # Keep the helper's return type simple for tests while exposing policy/grant
    # through repository lookups in each test.
    return service, repository, host, rule, tuple(message_ids)


@pytest.mark.asyncio
async def test_rule_dry_run_records_evidence_without_host_side_effect() -> None:
    service, repository, host, rule, message_ids = await _seed_service()
    policy = next(iter(repository.policies.values()))
    grant = next(iter(repository.grants.values()))

    executions = await service.execute_rule(
        tenant_id="tenant-1",
        subject_id="user-1",
        rule_id=rule.rule_id,
        message_ids=(message_ids[0],),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        dry_run=True,
    )

    assert executions[0].status is RuleExecutionStatus.DRY_RUN
    assert executions[0].reason == "matched_dry_run"
    assert host.executions == []
    assert (
        len(await repository.list_rule_executions(tenant_id="tenant-1", subject_id="user-1")) == 1
    )


@pytest.mark.asyncio
async def test_rule_execution_requires_independent_kill_switch() -> None:
    service, repository, _host, rule, message_ids = await _seed_service(enabled=False)
    policy = next(iter(repository.policies.values()))
    grant = next(iter(repository.grants.values()))

    with pytest.raises(KillSwitchError, match="rule_automation_kill_switch_active"):
        await service.execute_rule(
            tenant_id="tenant-1",
            subject_id="user-1",
            rule_id=rule.rule_id,
            message_ids=(message_ids[0],),
            policy_id=policy.policy_id,
            grant_id=grant.grant_id,
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_rule_execution_respects_host_provider_kill_switch() -> None:
    kill_switch = RecordingKillSwitch(blocked_providers=(ProviderName.SANDBOX,))
    service, repository, host, rule, message_ids = await _seed_service(
        enabled=True, kill_switch=kill_switch
    )
    policy = next(iter(repository.policies.values()))
    grant = next(iter(repository.grants.values()))

    executions = await service.execute_rule(
        tenant_id="tenant-1",
        subject_id="user-1",
        rule_id=rule.rule_id,
        message_ids=(message_ids[0],),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        dry_run=False,
    )

    assert executions[0].status is RuleExecutionStatus.BLOCKED
    assert executions[0].reason == "provider_kill_switch_active"
    assert host.executions == []
    assert kill_switch.checks[0]["provider"] == "sandbox"
    assert any(event.get("event_type") == "mail.kill_switch.blocked" for event in repository.audits)


@pytest.mark.asyncio
async def test_rule_execution_is_idempotent_and_respects_hourly_limit() -> None:
    service, repository, host, rule, message_ids = await _seed_service(enabled=True)
    policy = next(iter(repository.policies.values()))
    grant = next(iter(repository.grants.values()))
    first = await service.execute_rule(
        tenant_id="tenant-1",
        subject_id="user-1",
        rule_id=rule.rule_id,
        message_ids=(message_ids[0],),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        dry_run=False,
    )
    replay = await service.execute_rule(
        tenant_id="tenant-1",
        subject_id="user-1",
        rule_id=rule.rule_id,
        message_ids=(message_ids[0],),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        dry_run=False,
    )
    second = await service.execute_rule(
        tenant_id="tenant-1",
        subject_id="user-1",
        rule_id=rule.rule_id,
        message_ids=(message_ids[1],),
        policy_id=policy.policy_id,
        grant_id=grant.grant_id,
        dry_run=False,
    )

    assert first[0].status is RuleExecutionStatus.EXECUTED
    assert replay[0].execution_id == first[0].execution_id
    assert len(host.executions) == 1
    assert second[0].status is RuleExecutionStatus.BLOCKED
    assert second[0].reason == "rule_hourly_limit"
