from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, cast

import pytest

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "validate_provider_evidence.py"
_SPEC = importlib.util.spec_from_file_location("mailhub_validate_provider_evidence", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
EvidenceValidationError = cast(Any, _MODULE.EvidenceValidationError)
validate_evidence = cast(Any, _MODULE.validate_evidence)
validate_main = cast(Any, _MODULE.main)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _real_payload() -> dict[str, object]:
    return {
        "schema_version": "mailhub.provider_activation_sync.v1",
        "evidence_kind": "real_api_observation",
        "network_access": True,
        "real_provider_evidence": True,
        "provider": "microsoft_graph",
        "environment": "isolated-test",
        "run_id": "run-1",
        "status": "succeeded",
        "started_at": "2026-07-29T00:00:00Z",
        "finished_at": "2026-07-29T00:01:00Z",
        "tenant_sha256": _hash("tenant"),
        "subject_sha256": _hash("subject"),
        "connection": {
            "connection_id": "00000000-0000-0000-0000-000000000001",
            "provider": "microsoft_graph",
            "status": "active",
            "revision": 2,
            "scope_count": 2,
            "identity": {
                "account_identity_sha256": _hash("graph-account-1"),
                "tenant_identity_sha256": _hash("tenant-graph-1"),
            },
        },
        "sync": {
            "job_ref": "00000000-0000-0000-0000-000000000002",
            "status": "succeeded",
            "fetched_count": 2,
            "saved_count": 2,
            "duplicate_count": 0,
            "deleted_count": 0,
        },
        "replay": {
            "same_job_ref": True,
            "initial_status": "queued",
            "replayed_status": "queued",
        },
        "sync_filter": {
            "bounded": True,
            "folder_ref_sha256": _hash("INBOX"),
        },
        "readiness": {"status": "ready", "environment": "injected", "missing_count": 0},
        "credentials_observed": False,
    }


def test_validator_accepts_real_observation_only_with_all_runtime_checks() -> None:
    summary = validate_evidence(_real_payload(), require_real=True)

    assert summary["real_provider_evidence"] is True
    assert summary["checks"] == [
        "runtime_readiness_verified",
        "active_connection_verified",
        "durable_sync_succeeded",
        "idempotent_replay_verified",
    ]


def test_validator_accepts_gate_failure_but_require_real_rejects_it() -> None:
    payload = {
        "schema_version": "mailhub.provider_activation_sync.v1",
        "evidence_kind": "activation_gate_failure",
        "network_access": False,
        "real_provider_evidence": False,
        "provider": "gmail",
        "environment": "test",
        "run_id": "run-gate",
        "status": "failed",
        "started_at": "2026-07-29T00:00:00Z",
        "finished_at": "2026-07-29T00:00:01Z",
        "tenant_sha256": _hash("tenant"),
        "subject_sha256": _hash("subject"),
        "connection": None,
        "sync": None,
        "replay": None,
        "sync_filter": None,
        "readiness": None,
        "credentials_observed": False,
        "error_code": "activation_confirmation_required",
    }

    assert validate_evidence(payload)["evidence_kind"] == "activation_gate_failure"
    with pytest.raises(EvidenceValidationError, match="real_observation_required"):
        validate_evidence(payload, require_real=True)


def test_validator_rejects_raw_sensitive_fields() -> None:
    payload = _real_payload()
    payload["connection"] = {
        **cast(dict[str, object], payload["connection"]),
        "email_address": "user@example.test",
    }

    with pytest.raises(EvidenceValidationError, match="sensitive_field_present"):
        validate_evidence(payload)


def test_validator_rejects_unreviewed_fields_even_when_not_named_sensitive() -> None:
    payload = _real_payload()
    payload["operator_note"] = "free-form evidence text"

    with pytest.raises(EvidenceValidationError, match="payload_unknown_field"):
        validate_evidence(payload)


def test_validator_rejects_inconsistent_real_evidence_flag() -> None:
    payload = _real_payload()
    payload["real_provider_evidence"] = False

    with pytest.raises(EvidenceValidationError, match="real_flag_inconsistent"):
        validate_evidence(payload)


def test_validator_rejects_reversed_observation_window() -> None:
    payload = _real_payload()
    payload["started_at"] = "2026-07-29T00:02:00Z"
    payload["finished_at"] = "2026-07-29T00:01:00Z"

    with pytest.raises(EvidenceValidationError, match="observation_time_reversed"):
        validate_evidence(payload)


def test_validator_rejects_future_observation_window() -> None:
    payload = _real_payload()
    payload["started_at"] = "2099-01-01T00:00:00Z"
    payload["finished_at"] = "2099-01-01T00:01:00Z"

    with pytest.raises(EvidenceValidationError, match="observation_in_future"):
        validate_evidence(payload)


def test_validator_rejects_unreviewed_nested_fields() -> None:
    payload = _real_payload()
    connection = cast(dict[str, object], payload["connection"])
    payload["connection"] = {**connection, "operator_note": "free-form evidence text"}

    with pytest.raises(EvidenceValidationError, match="connection_unknown_field"):
        validate_evidence(payload)


def test_validator_cli_emits_only_safe_summary(tmp_path: Path, capsys: Any) -> None:
    evidence_path = tmp_path / "sync-reconciliation.json"
    evidence_path.write_text(json.dumps(_real_payload()), encoding="utf-8")

    assert validate_main([str(evidence_path), "--require-real"]) == 0
    output = capsys.readouterr().out
    assert "provider_activation_validation.v1" in output
    assert "tenant" not in output
    assert "connection_id" not in output
