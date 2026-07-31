"""Versioned, declarative mail rules.

Rules are intentionally a small data language rather than Python expressions,
templates or webhooks.  The evaluator only reads bounded message metadata and
returns a proposal; a separate worker/Host Port must perform any side effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from mailhub.domain import ActionType, AutomationLevel, MailMessageProjection, utc_now


class RuleField(StrEnum):
    SUBJECT = "subject"
    SENDER_DOMAIN = "sender_domain"
    RECIPIENT_DOMAIN = "recipient_domain"
    LABEL = "label"
    IS_READ = "is_read"
    PROVIDER_REF = "provider_ref"


class RuleOperator(StrEnum):
    EQUALS = "equals"
    CONTAINS = "contains"
    IN = "in"


_LOW_RISK_ACTIONS = frozenset({ActionType.LABEL, ActionType.ARCHIVE, ActionType.MARK_READ})
_FORBIDDEN_PARAM_MARKERS = ("url", "webhook", "script", "code", "expression", "token")
_DIGEST_LENGTH = 64


@dataclass(frozen=True, slots=True)
class RuleCondition:
    field: RuleField
    operator: RuleOperator
    value: str | tuple[str, ...] | bool

    def __post_init__(self) -> None:
        if isinstance(self.value, str):
            if not self.value.strip() or len(self.value) > 320:
                raise ValueError("rule_condition_value_invalid")
        elif isinstance(self.value, tuple):
            if (
                not self.value
                or len(self.value) > 50
                or any(
                    not isinstance(item, str) or not item.strip() or len(item) > 320
                    for item in self.value
                )
            ):
                raise ValueError("rule_condition_values_invalid")
        elif not isinstance(self.value, bool):
            raise ValueError("rule_condition_value_type_invalid")


@dataclass(frozen=True, slots=True)
class MailRule:
    rule_id: UUID
    tenant_id: str
    owner_subject_id: str
    name: str
    conditions: tuple[RuleCondition, ...]
    action_type: ActionType
    action_params: Mapping[str, object] = field(default_factory=dict)
    automation_level: AutomationLevel = AutomationLevel.L2_REVIEW_QUEUE
    version: int = 1
    enabled: bool = False
    max_per_hour: int = 0
    max_per_day: int = 0
    valid_from: datetime = field(default_factory=utc_now)
    valid_until: datetime = field(default_factory=lambda: utc_now() + timedelta(days=30))
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.owner_subject_id.strip():
            raise ValueError("rule_scope_invalid")
        if not self.name.strip() or len(self.name) > 200:
            raise ValueError("rule_name_invalid")
        if not 1 <= len(self.conditions) <= 20:
            raise ValueError("rule_conditions_invalid")
        if self.version < 1 or self.max_per_hour < 0 or self.max_per_day < 0:
            raise ValueError("rule_version_or_limit_invalid")
        if self.action_type not in _LOW_RISK_ACTIONS:
            raise ValueError("rule_action_not_in_allowlist")
        if (
            self.automation_level is AutomationLevel.L3A_BOUNDED_ORGANIZE
            and self.action_type not in _LOW_RISK_ACTIONS
        ):
            raise ValueError("rule_l3a_action_invalid")
        _validate_params(self.action_params)
        start = _aware(self.valid_from, "valid_from")
        end = _aware(self.valid_until, "valid_until")
        if end <= start:
            raise ValueError("rule_validity_invalid")
        object.__setattr__(self, "valid_from", start)
        object.__setattr__(self, "valid_until", end)
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    rule_id: UUID
    version: int
    matched: bool
    reason: str
    action_type: ActionType | None = None
    action_params: Mapping[str, object] = field(default_factory=dict)
    dry_run: bool = True


@dataclass(frozen=True, slots=True)
class RuleSimulationSummary:
    """Bounded dry-run estimate shown before a rule is published."""

    evaluated_count: int
    matched_count: int
    matched_by_rule: Mapping[str, int]
    dry_run: bool = True


class RuleExecutionStatus(StrEnum):
    DRY_RUN = "dry_run"
    PROPOSED = "proposed"
    EXECUTED = "executed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class RuleExecution:
    """Durable, idempotent evidence for one rule/message/action decision."""

    execution_id: UUID
    tenant_id: str
    subject_id: str
    rule_id: UUID
    rule_version: int
    message_id: UUID
    action_id: UUID
    input_digest: str
    status: RuleExecutionStatus
    reason: str
    result: Mapping[str, object] = field(default_factory=dict)
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.subject_id.strip():
            raise ValueError("rule_execution_scope_invalid")
        if self.rule_version < 1 or self.revision < 1:
            raise ValueError("rule_execution_version_invalid")
        if len(self.input_digest) != _DIGEST_LENGTH or any(
            char not in "0123456789abcdef" for char in self.input_digest
        ):
            raise ValueError("rule_execution_digest_invalid")
        if not self.reason.strip() or len(self.reason) > 240:
            raise ValueError("rule_execution_reason_invalid")
        if len(self.result) > 50:
            raise ValueError("rule_execution_result_too_large")
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))


def summarize_rule_evaluations(
    evaluations: Sequence[RuleEvaluation],
) -> RuleSimulationSummary:
    matched_by_rule: dict[str, int] = {}
    matched_count = 0
    for evaluation in evaluations:
        if not evaluation.matched:
            continue
        matched_count += 1
        key = f"{evaluation.rule_id}:v{evaluation.version}"
        matched_by_rule[key] = matched_by_rule.get(key, 0) + 1
    return RuleSimulationSummary(
        evaluated_count=len(evaluations),
        matched_count=matched_count,
        matched_by_rule=matched_by_rule,
    )


def evaluate_rule(
    rule: MailRule,
    message: MailMessageProjection,
    *,
    now: datetime | None = None,
    dry_run: bool = True,
) -> RuleEvaluation:
    current = (now or utc_now()).astimezone(UTC)
    if message.tenant_id != rule.tenant_id:
        return RuleEvaluation(
            rule.rule_id, rule.version, False, "message_scope_mismatch", dry_run=dry_run
        )
    if not rule.enabled:
        return RuleEvaluation(rule.rule_id, rule.version, False, "rule_disabled", dry_run=dry_run)
    if not rule.valid_from <= current < rule.valid_until:
        return RuleEvaluation(rule.rule_id, rule.version, False, "rule_expired", dry_run=dry_run)
    for condition in rule.conditions:
        if not _matches(condition, message):
            return RuleEvaluation(
                rule.rule_id, rule.version, False, "condition_not_matched", dry_run=dry_run
            )
    return RuleEvaluation(
        rule.rule_id,
        rule.version,
        True,
        "matched_dry_run" if dry_run else "matched_proposal",
        action_type=rule.action_type,
        action_params=dict(rule.action_params),
        dry_run=dry_run,
    )


def simulate_rules(
    rules: Sequence[MailRule],
    messages: Sequence[MailMessageProjection],
    *,
    now: datetime | None = None,
) -> tuple[RuleEvaluation, ...]:
    evaluations: list[RuleEvaluation] = []
    for rule in rules:
        evaluations.extend(
            evaluate_rule(rule, message, now=now, dry_run=True) for message in messages
        )
    return tuple(evaluations)


def _matches(condition: RuleCondition, message: MailMessageProjection) -> bool:
    if condition.field is RuleField.SUBJECT:
        actual: str | bool = message.subject.casefold()
    elif condition.field is RuleField.SENDER_DOMAIN:
        actual = message.sender_address.rsplit("@", 1)[-1].casefold()
    elif condition.field is RuleField.RECIPIENT_DOMAIN:
        domains = {address.rsplit("@", 1)[-1].casefold() for address in message.recipient_addresses}
        actual = ",".join(sorted(domains))
    elif condition.field is RuleField.LABEL:
        actual = ",".join(label.casefold() for label in message.labels)
    elif condition.field is RuleField.IS_READ:
        actual = message.is_read
    else:
        actual = message.provider_message_ref.casefold()
    value = condition.value
    if isinstance(value, tuple):
        candidates = {item.casefold() for item in value}
        return str(actual).casefold() in candidates
    if isinstance(value, bool):
        return actual is value
    normalized = value.casefold()
    if condition.operator is RuleOperator.EQUALS:
        return str(actual).casefold() == normalized
    if condition.operator is RuleOperator.CONTAINS:
        return normalized in str(actual).casefold()
    return str(actual).casefold() in {item.casefold() for item in value.split(",")}


def _validate_params(value: Mapping[str, object], *, parent: str = "") -> None:
    if len(value) > 20:
        raise ValueError("rule_action_params_too_many")
    for key, item in value.items():
        key_text = str(key).casefold()
        if any(marker in key_text for marker in _FORBIDDEN_PARAM_MARKERS):
            raise ValueError("rule_action_param_forbidden")
        if isinstance(item, str):
            if len(item) > 500 or item.startswith(("http://", "https://")):
                raise ValueError("rule_action_param_untrusted")
        elif isinstance(item, Mapping):
            _validate_params(item, parent=parent + key_text + ".")
        elif isinstance(item, (list, tuple)):
            if len(item) > 50 or any(
                not isinstance(child, (str, int, float, bool)) for child in item
            ):
                raise ValueError("rule_action_param_shape_invalid")
        elif not isinstance(item, (int, float, bool)) and item is not None:
            raise ValueError("rule_action_param_type_invalid")


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(UTC)


def new_rule(
    *,
    tenant_id: str,
    owner_subject_id: str,
    name: str,
    conditions: tuple[RuleCondition, ...],
    action_type: ActionType,
    action_params: Mapping[str, object] | None = None,
) -> MailRule:
    return MailRule(
        rule_id=uuid4(),
        tenant_id=tenant_id,
        owner_subject_id=owner_subject_id,
        name=name,
        conditions=conditions,
        action_type=action_type,
        action_params=action_params or {},
    )
