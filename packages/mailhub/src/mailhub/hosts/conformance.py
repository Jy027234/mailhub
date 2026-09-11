"""Host Port conformance kit.

A product that embeds MailHub implements Host Ports (see
`docs/host-adapter-sdk.md`).  This kit turns the port matrix into executable
checks: the host records what its ports actually returned, and the kit verifies
the fail-closed properties that MailHub relies on.

The checks are pure and offline.  They never contact a provider and never call
the host, so the same bundle can be re-validated in CI, in review, or as release
evidence.  A bundle that merely proves "the happy path works" is intentionally
not enough: every port area also requires a deny/duplicate/stale observation.

Bundle schema (`mailhub.host_conformance.v1`)::

    {
      "schema": "mailhub.host_conformance.v1",
      "host": "my-product",
      "observations": {
        "identity": [{"tenant_id": "t1", "subject_id": "u1",
                      "capability": "mail.read", "allowed": true}],
        "credential": [{"credential_ref": "cred_1", "expires_in_seconds": 300,
                        "returned_fields": ["account_email", "scopes"]}]
      }
    }

Run `python scripts/host_conformance.py --emit-template` to get a starter
bundle, or `--self-test` to prove the kit itself works.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "mailhub.host_conformance.v1"

#: Field names that must never cross a host port boundary.  Matching is exact
#: after case-folding so that "somebody" is not mistaken for "body".
FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "access_token",
        "authorization",
        "authorization_code",
        "body",
        "body_html",
        "body_text",
        "client_secret",
        "code_verifier",
        "content",
        "cookie",
        "credential",
        "credential_ref",
        "html",
        "id_token",
        "mime",
        "password",
        "passwd",
        "private_key",
        "raw",
        "raw_body",
        "raw_headers",
        "refresh_token",
        "secret",
        "snippet",
        "text",
        "token",
    }
)

#: Port areas checked, in report order.
PORT_AREAS: tuple[str, ...] = (
    "identity",
    "credential",
    "approval",
    "host_action",
    "knowledge",
    "knowledge_safety",
    "audit",
    "quota",
    "kill_switch",
    "event",
    "telemetry",
)

Observation = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ConformanceCheck:
    """One executable requirement for one port area."""

    area: str
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HostConformanceBundle:
    """Recorded host port behaviour, ready to validate."""

    host: str
    observations: Mapping[str, Sequence[Observation]]
    schema: str = SCHEMA_VERSION

    def area(self, name: str) -> tuple[Observation, ...]:
        return tuple(self.observations.get(name, ()))


@dataclass(frozen=True, slots=True)
class HostConformanceReport:
    host: str
    checks: tuple[ConformanceCheck, ...]

    @property
    def failures(self) -> tuple[ConformanceCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "host": self.host,
            "passed": self.passed,
            "checks": [
                {
                    "area": check.area,
                    "name": check.name,
                    "passed": check.passed,
                    "detail": check.detail,
                }
                for check in self.checks
            ],
        }


def _check(area: str, name: str, passed: bool, detail: str = "") -> ConformanceCheck:
    return ConformanceCheck(area=area, name=name, passed=passed, detail=detail)


def _text(observation: Observation, key: str) -> str | None:
    value = observation.get(key)
    return value if isinstance(value, str) else None


def _flag(observation: Observation, key: str) -> bool | None:
    value = observation.get(key)
    return value if isinstance(value, bool) else None


def _integer(observation: Observation, key: str) -> int | None:
    value = observation.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _names(observation: Observation, key: str) -> tuple[str, ...]:
    value = observation.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str))


#: Fields that must never appear in a credential metadata projection.  The
#: credential reference itself is required by the refresh/exchange contract, so
#: it is not in this set.
_SECRET_FIELD_NAMES = frozenset(
    {
        "access_token",
        "authorization",
        "authorization_code",
        "client_secret",
        "code_verifier",
        "cookie",
        "id_token",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)


def _secret_names(names: Sequence[str]) -> tuple[str, ...]:
    return tuple(name for name in names if name.strip().casefold() in _SECRET_FIELD_NAMES)


def _forbidden(names: Sequence[str]) -> tuple[str, ...]:
    return tuple(name for name in names if name.strip().casefold() in FORBIDDEN_FIELD_NAMES)


def _looks_like_address(value: str) -> bool:
    candidate = value.strip()
    if "@" not in candidate or " " in candidate:
        return False
    local, _, domain = candidate.partition("@")
    return bool(local) and "." in domain


def _present(area: str, observations: Sequence[Observation]) -> ConformanceCheck:
    return _check(
        area,
        "observations_present",
        bool(observations),
        f"{len(observations)} observation(s)" if observations else "no observations recorded",
    )


def check_identity(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    area = "HostIdentityPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    typed = True
    empty_scope_denied = True
    deny_observed = False
    for observation in observations:
        allowed = _flag(observation, "allowed")
        if allowed is None:
            typed = False
            continue
        if not allowed:
            deny_observed = True
            continue
        for key in ("tenant_id", "subject_id", "capability"):
            value = _text(observation, key)
            if value is None or not value.strip():
                empty_scope_denied = False
    checks.append(
        _check(area, "explicit_boolean_decision", typed, "allowed must be a JSON boolean")
    )
    checks.append(
        _check(
            area,
            "empty_scope_never_allowed",
            empty_scope_denied,
            "an empty tenant/subject/capability must never be authorized",
        )
    )
    checks.append(
        _check(
            area,
            "deny_path_observed",
            deny_observed,
            "record at least one denied request",
        )
    )
    return checks


def check_credential(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    """Check both credential paths.

    `resolve` is *supposed* to hand MailHub short-lived provider material, so
    token-shaped fields are expected there.  The metadata projection returned by
    refresh/exchange/callback is the opposite: it must carry only a credential
    ref, identity and version, never a token.  Record `kind` to select the
    contract; the default is the stricter metadata projection.
    """

    area = "CredentialBrokerPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    refs_present = True
    bounded_lifetime = True
    metadata_clean = True
    for observation in observations:
        kind = (_text(observation, "kind") or "metadata").strip().casefold()
        reference = _text(observation, "credential_ref")
        if reference is None or not reference.strip():
            refs_present = False
        expires = _integer(observation, "expires_in_seconds")
        if expires is not None:
            if not 1 <= expires <= 7200:
                bounded_lifetime = False
        elif _flag(observation, "long_lived") is not True:
            # Silence is never an answer: a credential with no stated lifetime
            # must be explicitly acknowledged (for example an IMAP application
            # password that is rotated operationally instead of per call).
            bounded_lifetime = False
        if kind != "resolve" and _secret_names(_names(observation, "returned_fields")):
            metadata_clean = False
    checks.append(_check(area, "credential_ref_present", refs_present))
    checks.append(
        _check(
            area,
            "lifetime_explicit_and_bounded",
            bounded_lifetime,
            "state expires_in_seconds (1-7200) or explicitly acknowledge long_lived",
        )
    )
    checks.append(
        _check(
            area,
            "metadata_projection_free_of_secrets",
            metadata_clean,
            "a refresh/exchange projection must return ref/identity/version only; "
            "record kind=resolve for short-lived material",
        )
    )
    return checks


def check_approval(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    area = "ApprovalPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    typed = True
    stale_rejected = True
    valid_accept_observed = False
    for observation in observations:
        accepted = _flag(observation, "accepted")
        if accepted is None:
            typed = False
            continue
        bound = _flag(observation, "bound")
        expired = _flag(observation, "expired") is True
        foreign = _flag(observation, "foreign_scope") is True
        stale = bound is False or expired or foreign
        if stale and accepted:
            stale_rejected = False
        if accepted and bound is True and not stale:
            valid_accept_observed = True
    checks.append(_check(area, "explicit_decision", typed, "accepted must be a JSON boolean"))
    checks.append(
        _check(
            area,
            "unbound_or_stale_rejected",
            stale_rejected,
            "an unbound, expired or foreign-scope confirmation must be refused",
        )
    )
    checks.append(
        _check(
            area,
            "valid_confirmation_observed",
            valid_accept_observed,
            "record at least one correctly bound confirmation that was accepted",
        )
    )
    return checks


def check_host_action(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    area = "HostActionPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    groups: dict[str, list[Observation]] = {}
    for observation in observations:
        action_id = _text(observation, "action_id")
        if action_id is None or not action_id.strip():
            continue
        groups.setdefault(action_id, []).append(observation)
    if not groups:
        checks.append(_check(area, "stable_action_identity", False, "action_id missing"))
        return checks
    first_executed = True
    replay_not_reexecuted = True
    replay_result_stable = True
    replay_observed = False
    for group in groups.values():
        ordered = sorted(group, key=lambda item: _integer(item, "attempt") or 0)
        head = ordered[0]
        if _flag(head, "executed") is not True:
            first_executed = False
        head_result = _text(head, "result_ref")
        for follow in ordered[1:]:
            replay_observed = True
            if _flag(follow, "executed") is not False:
                replay_not_reexecuted = False
            if _text(follow, "result_ref") != head_result:
                replay_result_stable = False
    checks.append(_check(area, "first_attempt_executed", first_executed))
    checks.append(
        _check(
            area,
            "replay_not_reexecuted",
            replay_not_reexecuted,
            "a repeated action_id must not perform the side effect twice",
        )
    )
    checks.append(
        _check(
            area,
            "replay_returns_same_result_ref",
            replay_result_stable,
            "a replay must return the original result reference",
        )
    )
    checks.append(
        _check(area, "replay_path_observed", replay_observed, "record a retried action_id")
    )
    return checks


def check_knowledge(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    area = "KnowledgeSinkPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    groups: dict[str, list[Observation]] = {}
    for observation in observations:
        candidate_ref = _text(observation, "candidate_ref")
        if candidate_ref is None or not candidate_ref.strip():
            continue
        groups.setdefault(candidate_ref, []).append(observation)
    if not groups:
        checks.append(_check(area, "candidate_identity_present", False, "candidate_ref missing"))
        return checks
    duplicate_not_published = True
    result_ref_returned = True
    duplicate_observed = False
    for group in groups.values():
        created_count = sum(1 for item in group if _flag(item, "created") is True)
        if created_count > 1:
            duplicate_not_published = False
        if len(group) > 1:
            duplicate_observed = True
        for item in group:
            if not (_text(item, "result_ref") or "").strip():
                result_ref_returned = False
    checks.append(
        _check(
            area,
            "duplicate_candidate_not_published",
            duplicate_not_published,
            "retrying one candidate ref must not create a second knowledge record",
        )
    )
    checks.append(_check(area, "result_ref_returned", result_ref_returned))
    checks.append(
        _check(area, "duplicate_path_observed", duplicate_observed, "record a retried candidate")
    )
    return checks


def check_knowledge_safety(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    """Fail-closed is a pass; silently publishing unscanned content is not.

    A host whose AV/DLP/rights service is unavailable must say so and refuse to
    publish.  Claiming `publishable` therefore requires explicit, non-blocked
    security and rights states.
    """

    area = "KnowledgeSafetyPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    typed = True
    never_unscanned = True
    blocked_states = {"", "unknown", "pending", "unscanned", "rights_unknown"}
    for observation in observations:
        publishable = _flag(observation, "publishable")
        if publishable is None:
            typed = False
            continue
        if not publishable:
            continue
        security = (_text(observation, "security_state") or "").strip().casefold()
        rights = (_text(observation, "rights_state") or "").strip().casefold()
        if security in blocked_states or rights in blocked_states:
            never_unscanned = False
    checks.append(
        _check(area, "explicit_publish_decision", typed, "publishable must be a JSON boolean")
    )
    checks.append(
        _check(
            area,
            "never_publishes_unscanned_content",
            never_unscanned,
            "publishable=true requires explicit non-blocked security_state and rights_state",
        )
    )
    return checks


def check_audit(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    area = "AuditPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    metadata_only = True
    offending: list[str] = []
    for observation in observations:
        leaked = _forbidden(_names(observation, "fields"))
        if leaked:
            metadata_only = False
            offending.extend(leaked)
    checks.append(
        _check(
            area,
            "metadata_only_fields",
            metadata_only,
            "forbidden audit fields: " + ", ".join(sorted(set(offending)))
            if offending
            else "audit records carry ids, hashes and status only",
        )
    )
    return checks


def check_quota(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    area = "QuotaPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    bounded = True
    expiring = True
    for observation in observations:
        lease = _integer(observation, "lease_seconds")
        if lease is None or not 1 <= lease <= 86_400:
            bounded = False
        if _flag(observation, "expires") is not True:
            expiring = False
    checks.append(
        _check(
            area,
            "bounded_lease",
            bounded,
            "lease_seconds must be an integer between 1 and 86400",
        )
    )
    checks.append(
        _check(
            area,
            "lease_expires",
            expiring,
            "every lease must expire so an abandoned worker cannot block an account",
        )
    )
    return checks


def check_kill_switch(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    area = "KillSwitchPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    explicit = True
    known_decisions = True
    for observation in observations:
        if _flag(observation, "explicit") is not True:
            explicit = False
        if (_text(observation, "decision") or "").strip().casefold() not in {"allow", "deny"}:
            known_decisions = False
    checks.append(
        _check(
            area,
            "explicit_decision",
            explicit,
            "each side effect needs an explicit allow/deny answer, never an inferred default",
        )
    )
    checks.append(_check(area, "known_decision_values", known_decisions))
    return checks


def check_event(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    area = "EventPublisherPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    stable_ids = True
    named = True
    metadata_only = True
    offending: list[str] = []
    for observation in observations:
        if not (_text(observation, "event_id") or "").strip():
            stable_ids = False
        if not (_text(observation, "event_type") or "").strip():
            named = False
        leaked = _forbidden(_names(observation, "fields"))
        if leaked:
            metadata_only = False
            offending.extend(leaked)
    checks.append(_check(area, "stable_event_id", stable_ids))
    checks.append(_check(area, "event_type_named", named))
    checks.append(
        _check(
            area,
            "metadata_only_envelope",
            metadata_only,
            "forbidden envelope fields: " + ", ".join(sorted(set(offending)))
            if offending
            else "envelopes carry identifiers and status only",
        )
    )
    return checks


#: Attribute-name markers that imply per-address (high-cardinality) labels.
HIGH_CARDINALITY_MARKERS = ("address", "email", "recipient", "sender")


def check_telemetry(observations: Sequence[Observation]) -> list[ConformanceCheck]:
    area = "TelemetryPort"
    checks = [_present(area, observations)]
    if not observations:
        return checks
    metadata_only = True
    low_cardinality = True
    offending: list[str] = []
    address_labels: list[str] = []
    for observation in observations:
        attribute_names = _names(observation, "attributes")
        leaked = _forbidden(attribute_names)
        if leaked:
            metadata_only = False
            offending.extend(leaked)
        address_labels.extend(
            name
            for name in attribute_names
            if any(marker in name.strip().casefold() for marker in HIGH_CARDINALITY_MARKERS)
        )
        raw_values = observation.get("attribute_values")
        if (
            isinstance(raw_values, Sequence)
            and not isinstance(raw_values, (str, bytes))
            and any(_looks_like_address(item) for item in raw_values if isinstance(item, str))
        ):
            low_cardinality = False
    if address_labels:
        low_cardinality = False
    checks.append(
        _check(
            area,
            "metadata_only_attributes",
            metadata_only,
            "forbidden attribute names: " + ", ".join(sorted(set(offending)))
            if offending
            else "attributes carry bounded identifiers and codes",
        )
    )
    checks.append(
        _check(
            area,
            "no_high_cardinality_addresses",
            low_cardinality,
            "telemetry must not carry per-address labels"
            + (": " + ", ".join(sorted(set(address_labels))) if address_labels else ""),
        )
    )
    return checks


_VALIDATORS: Mapping[str, Any] = {
    "identity": check_identity,
    "credential": check_credential,
    "approval": check_approval,
    "host_action": check_host_action,
    "knowledge": check_knowledge,
    "knowledge_safety": check_knowledge_safety,
    "audit": check_audit,
    "quota": check_quota,
    "kill_switch": check_kill_switch,
    "event": check_event,
    "telemetry": check_telemetry,
}


def run_host_conformance(bundle: HostConformanceBundle) -> HostConformanceReport:
    """Validate every port area in a recorded host bundle."""

    checks: list[ConformanceCheck] = []
    for area in PORT_AREAS:
        validator = _VALIDATORS[area]
        checks.extend(validator(bundle.area(area)))
    if bundle.schema != SCHEMA_VERSION:
        checks.append(
            _check(
                "bundle", "schema_version_supported", False, f"unexpected schema: {bundle.schema}"
            )
        )
    else:
        checks.append(_check("bundle", "schema_version_supported", True, bundle.schema))
    return HostConformanceReport(host=bundle.host, checks=tuple(checks))


def build_bundle(
    host: str, observations: Mapping[str, Sequence[Observation]]
) -> HostConformanceBundle:
    return HostConformanceBundle(host=host, observations=dict(observations))


def bundle_from_mapping(payload: Mapping[str, Any]) -> HostConformanceBundle:
    """Build a bundle from decoded JSON, rejecting a malformed envelope."""

    schema = payload.get("schema")
    if not isinstance(schema, str):
        raise ValueError("host_conformance_schema_missing")
    host = payload.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host_conformance_host_missing")
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, Mapping):
        raise ValueError("host_conformance_observations_missing")
    observations: dict[str, tuple[Observation, ...]] = {}
    for area, items in raw_observations.items():
        if (
            not isinstance(area, str)
            or not isinstance(items, Sequence)
            or isinstance(items, (str, bytes))
        ):
            raise ValueError(f"host_conformance_area_invalid:{area}")
        entries: list[Observation] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError(f"host_conformance_observation_invalid:{area}")
            entries.append(item)
        observations[area] = tuple(entries)
    return HostConformanceBundle(host=host, schema=schema, observations=observations)


def load_bundle(path: Path) -> HostConformanceBundle:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("host_conformance_bundle_not_object")
    return bundle_from_mapping(payload)


def sample_bundle(*, compliant: bool) -> HostConformanceBundle:
    """Return a compliant or deliberately broken bundle for self-tests."""

    identity: list[dict[str, Any]] = [
        {
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "capability": "mail.read",
            "allowed": True,
        },
        {"tenant_id": "", "subject_id": "user-1", "capability": "mail.read", "allowed": False},
    ]
    credential: list[dict[str, Any]] = [
        {"kind": "resolve", "credential_ref": "cred_1", "expires_in_seconds": 300},
        {
            "kind": "metadata",
            "credential_ref": "cred_1",
            "expires_in_seconds": 300,
            "returned_fields": ["account_email", "credential_version"],
        },
    ]
    approval: list[dict[str, Any]] = [
        {
            "confirmation_ref": "approve_1",
            "bound": True,
            "expired": False,
            "foreign_scope": False,
            "accepted": True,
        },
        {
            "confirmation_ref": "approve_2",
            "bound": False,
            "expired": False,
            "foreign_scope": False,
            "accepted": False,
        },
        {
            "confirmation_ref": "approve_3",
            "bound": True,
            "expired": True,
            "foreign_scope": False,
            "accepted": False,
        },
    ]
    host_action: list[dict[str, Any]] = [
        {"action_id": "action-1", "attempt": 1, "executed": True, "result_ref": "task-1"},
        {"action_id": "action-1", "attempt": 2, "executed": False, "result_ref": "task-1"},
    ]
    knowledge: list[dict[str, Any]] = [
        {"candidate_ref": "cand-1", "created": True, "result_ref": "know-1"},
        {"candidate_ref": "cand-1", "created": False, "result_ref": "know-1"},
    ]
    knowledge_safety: list[dict[str, Any]] = [
        {"publishable": False, "security_state": "unscanned", "rights_state": "rights_unknown"},
        {"publishable": True, "security_state": "clean", "rights_state": "cleared"},
    ]
    audit: list[dict[str, Any]] = [
        {"event": "mail.connection.created", "fields": ["connection_id", "status"]}
    ]
    quota: list[dict[str, Any]] = [
        {"scope": "tenant-1/account-1", "lease_seconds": 300, "expires": True}
    ]
    kill_switch: list[dict[str, Any]] = [{"decision": "allow", "explicit": True}]
    event: list[dict[str, Any]] = [
        {
            "event_id": "evt-1",
            "event_type": "mail.message.observed",
            "fields": ["tenant_id", "message_id"],
        }
    ]
    telemetry: list[dict[str, Any]] = [
        {
            "metric_name": "mailhub.sync.duration_ms",
            "attributes": ["tenant_hash", "status"],
            "attribute_values": ["9f2c", "ok"],
        }
    ]

    if compliant:
        return build_bundle(
            "self-test-compliant",
            {
                "identity": identity,
                "credential": credential,
                "approval": approval,
                "host_action": host_action,
                "knowledge": knowledge,
                "knowledge_safety": knowledge_safety,
                "audit": audit,
                "quota": quota,
                "kill_switch": kill_switch,
                "event": event,
                "telemetry": telemetry,
            },
        )

    broken_identity = [dict(identity[0]), {**identity[1], "allowed": True}]
    broken_credential = [
        {"kind": "resolve", "credential_ref": "cred_1", "expires_in_seconds": 0},
        {
            "kind": "metadata",
            "credential_ref": "cred_1",
            "expires_in_seconds": 300,
            "returned_fields": ["access_token", "refresh_token"],
        },
    ]
    broken_approval = [{**approval[1], "accepted": True}, approval[0]]
    broken_action = [host_action[0], {**host_action[1], "executed": True}]
    broken_knowledge = [knowledge[0], {**knowledge[1], "created": True}]
    broken_knowledge_safety = [
        {"publishable": True, "security_state": "unscanned", "rights_state": "rights_unknown"}
    ]
    broken_audit = [{"event": "mail.message.observed", "fields": ["body_text", "raw_headers"]}]
    broken_quota = [{"scope": "tenant-1/account-1", "lease_seconds": 0, "expires": False}]
    broken_kill_switch = [{"decision": "maybe", "explicit": False}]
    broken_telemetry = [
        {
            "metric_name": "mailhub.sync.count",
            "attributes": ["recipient_address"],
            "attribute_values": ["someone@example.com"],
        }
    ]
    return build_bundle(
        "self-test-broken",
        {
            "identity": broken_identity,
            "credential": broken_credential,
            "approval": broken_approval,
            "host_action": broken_action,
            "knowledge": broken_knowledge,
            "knowledge_safety": broken_knowledge_safety,
            "audit": broken_audit,
            "quota": broken_quota,
            "kill_switch": broken_kill_switch,
            "event": event,
            "telemetry": broken_telemetry,
        },
    )


def template_bundle() -> dict[str, Any]:
    """A starter bundle showing every accepted observation shape."""

    return {
        "schema": SCHEMA_VERSION,
        "host": "replace-with-your-product",
        "observations": {
            "identity": [
                {"tenant_id": "t1", "subject_id": "u1", "capability": "mail.read", "allowed": True},
                {"tenant_id": "", "subject_id": "u1", "capability": "mail.read", "allowed": False},
            ],
            "credential": [
                {"kind": "resolve", "credential_ref": "cred_1", "expires_in_seconds": 300},
                {
                    "kind": "metadata",
                    "credential_ref": "cred_1",
                    "expires_in_seconds": 300,
                    "returned_fields": ["account_email", "credential_version"],
                },
            ],
            "approval": [
                {
                    "confirmation_ref": "approve_1",
                    "bound": True,
                    "expired": False,
                    "foreign_scope": False,
                    "accepted": True,
                },
                {
                    "confirmation_ref": "approve_2",
                    "bound": False,
                    "expired": False,
                    "foreign_scope": False,
                    "accepted": False,
                },
            ],
            "host_action": [
                {"action_id": "action-1", "attempt": 1, "executed": True, "result_ref": "task-1"},
                {"action_id": "action-1", "attempt": 2, "executed": False, "result_ref": "task-1"},
            ],
            "knowledge": [
                {"candidate_ref": "cand-1", "created": True, "result_ref": "know-1"},
                {"candidate_ref": "cand-1", "created": False, "result_ref": "know-1"},
            ],
            "knowledge_safety": [
                {
                    "publishable": False,
                    "security_state": "unscanned",
                    "rights_state": "rights_unknown",
                },
                {"publishable": True, "security_state": "clean", "rights_state": "cleared"},
            ],
            "audit": [{"event": "mail.connection.created", "fields": ["connection_id", "status"]}],
            "quota": [{"scope": "tenant-1/account-1", "lease_seconds": 300, "expires": True}],
            "kill_switch": [{"decision": "allow", "explicit": True}],
            "event": [
                {
                    "event_id": "evt-1",
                    "event_type": "mail.message.observed",
                    "fields": ["tenant_id", "message_id"],
                }
            ],
            "telemetry": [
                {
                    "metric_name": "mailhub.sync.duration_ms",
                    "attributes": ["tenant_hash", "status"],
                    "attribute_values": ["9f2c", "ok"],
                }
            ],
        },
    }
