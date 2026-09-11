"""Tests for the Host Port conformance kit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mailhub.hosts.conformance import (
    SCHEMA_VERSION,
    HostConformanceBundle,
    build_bundle,
    bundle_from_mapping,
    load_bundle,
    run_host_conformance,
    sample_bundle,
    template_bundle,
)


def test_compliant_sample_host_passes_every_check() -> None:
    report = run_host_conformance(sample_bundle(compliant=True))
    assert report.passed, [f"{item.area}.{item.name}: {item.detail}" for item in report.failures]
    assert len(report.checks) >= 30


def test_broken_sample_host_fails_every_port_area() -> None:
    report = run_host_conformance(sample_bundle(compliant=False))
    areas = {failure.area for failure in report.failures}
    assert {
        "HostIdentityPort",
        "CredentialBrokerPort",
        "ApprovalPort",
        "HostActionPort",
        "KnowledgeSinkPort",
        "KnowledgeSafetyPort",
        "AuditPort",
        "QuotaPort",
        "KillSwitchPort",
        "TelemetryPort",
    } <= areas


def test_missing_area_is_reported_rather_than_skipped() -> None:
    report = run_host_conformance(build_bundle("empty-host", {}))
    assert not report.passed
    assert len(report.failures) == 11
    assert all(failure.name == "observations_present" for failure in report.failures)


def test_identity_never_authorizes_an_empty_scope() -> None:
    bundle = sample_bundle(compliant=True)
    observations = dict(bundle.observations)
    observations["identity"] = [
        {"tenant_id": "", "subject_id": "", "capability": "", "allowed": True}
    ]
    report = run_host_conformance(build_bundle("bad-identity", observations))
    names = {failure.name for failure in report.failures}
    assert "empty_scope_never_allowed" in names
    assert "deny_path_observed" in names


def test_approval_must_refuse_expired_and_foreign_confirmations() -> None:
    report = run_host_conformance(
        build_bundle(
            "stale-approval",
            {
                "approval": [
                    {
                        "confirmation_ref": "a",
                        "bound": True,
                        "expired": True,
                        "foreign_scope": False,
                        "accepted": True,
                    },
                    {
                        "confirmation_ref": "b",
                        "bound": True,
                        "expired": False,
                        "foreign_scope": True,
                        "accepted": True,
                    },
                ]
            },
        )
    )
    failures = {failure.name for failure in report.failures}
    assert "unbound_or_stale_rejected" in failures
    assert "valid_confirmation_observed" in failures


def test_replayed_host_action_must_not_execute_twice() -> None:
    report = run_host_conformance(
        build_bundle(
            "double-action",
            {
                "host_action": [
                    {"action_id": "a1", "attempt": 1, "executed": True, "result_ref": "task-1"},
                    {"action_id": "a1", "attempt": 2, "executed": True, "result_ref": "task-2"},
                ]
            },
        )
    )
    failures = {failure.name for failure in report.failures}
    assert "replay_not_reexecuted" in failures
    assert "replay_returns_same_result_ref" in failures


def test_audit_and_telemetry_reject_body_and_address_fields() -> None:
    report = run_host_conformance(
        build_bundle(
            "leaky-telemetry",
            {
                "audit": [{"event": "e", "fields": ["body_text", "credential_ref"]}],
                "telemetry": [
                    {
                        "metric_name": "m",
                        "attributes": ["recipient_address", "body_text"],
                        "attribute_values": ["a@b.example"],
                    }
                ],
            },
        )
    )
    failures = {failure.name for failure in report.failures}
    # audit: forbidden field names
    assert "metadata_only_fields" in failures
    # telemetry: a forbidden attribute name (body_text)
    assert "metadata_only_attributes" in failures
    # telemetry: an address label name and an address-shaped value
    assert "no_high_cardinality_addresses" in failures


def test_quota_lease_must_be_bounded_and_expiring() -> None:
    report = run_host_conformance(
        build_bundle(
            "stuck-lease",
            {"quota": [{"scope": "s", "lease_seconds": 0, "expires": False}]},
        )
    )
    failures = {failure.name for failure in report.failures}
    assert "bounded_lease" in failures
    assert "lease_expires" in failures


def test_unknown_schema_version_is_rejected() -> None:
    report = run_host_conformance(
        HostConformanceBundle(host="old", observations={}, schema="mailhub.host_conformance.v0")
    )
    assert "schema_version_supported" in {failure.name for failure in report.failures}


def test_bundle_round_trips_through_json(tmp_path: Path) -> None:
    payload = template_bundle()
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    bundle = load_bundle(path)
    assert bundle.schema == SCHEMA_VERSION
    assert bundle.host == "replace-with-your-product"
    assert run_host_conformance(bundle).passed


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "host_conformance_schema_missing"),
        ({"schema": SCHEMA_VERSION}, "host_conformance_host_missing"),
        ({"schema": SCHEMA_VERSION, "host": "h"}, "host_conformance_observations_missing"),
        (
            {"schema": SCHEMA_VERSION, "host": "h", "observations": {"identity": "nope"}},
            "host_conformance_area_invalid:identity",
        ),
        (
            {"schema": SCHEMA_VERSION, "host": "h", "observations": {"identity": [1]}},
            "host_conformance_observation_invalid:identity",
        ),
    ],
)
def test_malformed_bundles_fail_closed(payload: dict[str, object], code: str) -> None:
    with pytest.raises(ValueError, match=code):
        bundle_from_mapping(payload)
