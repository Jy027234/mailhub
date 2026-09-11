from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from mailhub.domain import (
    ActionType,
    AgentActionContext,
    AgentActionRequest,
    AutomationLevel,
    DelegationGrant,
    DeliveryStatus,
    MailAgentPolicy,
    MailboxConnection,
    MailMessageProjection,
    ProviderName,
    digest_text,
    evaluate_policy,
    normalize_provider_metadata,
    transition_delivery,
)


def _action(
    *,
    new_recipient: bool = False,
    bcc: bool = False,
    external: bool = False,
    large: bool = False,
    group: bool = False,
    recipients: tuple[str, ...] = ("buyer@example.test",),
) -> AgentActionRequest:
    return AgentActionRequest(
        action_id=uuid4(),
        action_type=ActionType.SEND_REPLY,
        context=AgentActionContext(
            tenant_id="tenant-1",
            agent_subject_id="agent-1",
            connection_id=uuid4(),
            folder_ref="INBOX",
            thread_id=uuid4(),
            recipient_addresses=recipients,
            has_new_recipient=new_recipient,
            has_bcc=bcc,
            has_external_recipient=external,
            has_large_recipient_set=large,
            has_group_recipient=group,
        ),
        input_digest="a" * 64,
    )


def _policy(action: AgentActionRequest) -> MailAgentPolicy:
    connection_id = action.context.connection_id
    assert connection_id is not None
    return MailAgentPolicy(
        policy_id=uuid4(),
        tenant_id="tenant-1",
        owner_subject_id="agent-1",
        allowed_connection_ids=frozenset({connection_id}),
        allowed_actions=frozenset({ActionType.SEND_REPLY}),
        allowed_domains=frozenset({"example.test"}),
        allowed_automation_level=AutomationLevel.L3B_BOUNDED_REPLY,
        valid_from=datetime.now(UTC) - timedelta(minutes=1),
        valid_until=datetime.now(UTC) + timedelta(hours=1),
    )


def test_l3b_requires_grant_and_allows_existing_thread() -> None:
    action = _action()
    policy = _policy(action)
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-1",
        policy_id=policy.policy_id,
        agent_subject_id="agent-1",
        granted_by_subject_id="agent-1",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
        granted_at=datetime.now(UTC) - timedelta(minutes=1),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    decision = evaluate_policy(policy, grant, action)

    assert decision.allowed is True
    assert decision.required_approval is False
    assert decision.automation_level is AutomationLevel.L3B_BOUNDED_REPLY


def _grant(policy: MailAgentPolicy) -> DelegationGrant:
    return DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-1",
        policy_id=policy.policy_id,
        agent_subject_id="agent-1",
        granted_by_subject_id="agent-1",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
        granted_at=datetime.now(UTC) - timedelta(minutes=1),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_a_recipient_outside_the_allowlist_is_refused() -> None:
    """Domain risk: the policy allowlist is the boundary, not a hint.

    Nothing asserted this before, so the refusal could have been deleted without
    a single test noticing.
    """

    action = _action(recipients=("buyer@other.test",))
    policy = _policy(action)

    decision = evaluate_policy(policy, _grant(policy), action)

    assert decision.allowed is False
    assert decision.reason_code == "recipient_domain_not_allowed"


def test_a_recipient_inside_the_allowlist_is_not_refused_for_its_domain() -> None:
    action = _action(recipients=("buyer@example.test",))
    policy = _policy(action)

    decision = evaluate_policy(policy, _grant(policy), action)

    assert decision.reason_code != "recipient_domain_not_allowed"


def test_new_recipient_is_never_autonomous() -> None:
    action = _action(new_recipient=True)
    policy = _policy(action)
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-1",
        policy_id=policy.policy_id,
        agent_subject_id="agent-1",
        granted_by_subject_id="agent-1",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
    )

    decision = evaluate_policy(policy, grant, action)

    assert decision.allowed is False
    assert decision.reason_code == "approval_required"


@pytest.mark.parametrize(
    "kwargs",
    (
        {"bcc": True},
        {"external": True},
        {"large": True},
        {"group": True},
    ),
)
def test_reply_risk_flags_require_approval(kwargs: dict[str, Any]) -> None:
    action = _action(**kwargs)
    policy = _policy(action)
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-1",
        policy_id=policy.policy_id,
        agent_subject_id="agent-1",
        granted_by_subject_id="agent-1",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
    )

    decision = evaluate_policy(policy, grant, action)

    assert decision.allowed is False
    assert decision.reason_code == "approval_required"


def test_delivery_state_machine_rejects_direct_success() -> None:
    with pytest.raises(ValueError, match="delivery_transition_invalid"):
        transition_delivery(DeliveryStatus.QUEUED, DeliveryStatus.SUCCEEDED)


def test_mailbox_connection_normalizes_and_rejects_invalid_scopes() -> None:
    connection = MailboxConnection(
        connection_id=uuid4(),
        tenant_id="tenant-scopes",
        subject_id="subject-scopes",
        provider=ProviderName.SANDBOX,
        email_address="scope@example.test",
        credential_ref="credential-ref",
        granted_scopes=(" mail.read ", "mail.read"),
    )
    assert connection.granted_scopes == ("mail.read",)
    with pytest.raises(ValueError, match="granted_scopes_invalid"):
        MailboxConnection(
            connection_id=uuid4(),
            tenant_id="tenant-scopes",
            subject_id="subject-scopes",
            provider=ProviderName.SANDBOX,
            email_address="scope@example.test",
            credential_ref="credential-ref",
            granted_scopes=("mail.read", "\nmail.send"),
        )


def test_delegated_agent_can_use_owner_policy_with_intersection() -> None:
    connection_id = uuid4()
    policy = MailAgentPolicy(
        policy_id=uuid4(),
        tenant_id="tenant-1",
        owner_subject_id="owner-1",
        allowed_connection_ids=frozenset({connection_id}),
        allowed_actions=frozenset({ActionType.SEND_REPLY}),
        allowed_domains=frozenset({"example.test"}),
        allowed_automation_level=AutomationLevel.L3B_BOUNDED_REPLY,
    )
    grant = DelegationGrant(
        grant_id=uuid4(),
        tenant_id="tenant-1",
        policy_id=policy.policy_id,
        agent_subject_id="agent-1",
        granted_by_subject_id="owner-1",
        capability_ids=frozenset({ActionType.SEND_REPLY}),
    )
    action = AgentActionRequest(
        action_id=uuid4(),
        action_type=ActionType.SEND_REPLY,
        context=AgentActionContext(
            tenant_id="tenant-1",
            agent_subject_id="agent-1",
            connection_id=connection_id,
            folder_ref="INBOX",
            thread_id=uuid4(),
            recipient_addresses=("buyer@example.test",),
        ),
        input_digest=digest_text("delegated"),
    )
    decision = evaluate_policy(policy, grant, action)
    assert decision.allowed is True
    assert decision.grant_id == grant.grant_id


def test_provider_metadata_is_bounded_string_only_and_normalized() -> None:
    metadata = normalize_provider_metadata(
        {"change_key": "ck-1", "body_content_type": "text", "folder_id": "inbox"}
    )
    assert list(metadata) == ["body_content_type", "change_key", "folder_id"]
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-1",
        connection_id=uuid4(),
        thread_id=uuid4(),
        provider_message_ref="provider-1",
        internet_message_id="<provider-1@example.test>",
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject="Update",
        received_at=datetime.now(UTC),
        body_text="body",
        content_sha256=digest_text("body"),
        provider_metadata=metadata,
    )
    assert message.provider_metadata == metadata

    with pytest.raises(ValueError, match="provider_metadata_secret_or_raw"):
        normalize_provider_metadata({"access_token": "not-allowed"})
    with pytest.raises(ValueError, match="provider_metadata_value_invalid"):
        normalize_provider_metadata({"folder_id": "bad\nvalue"})
