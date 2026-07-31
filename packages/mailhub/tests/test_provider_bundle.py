from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

_SCRIPTS = Path(__file__).parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_SCRIPT_PATH = _SCRIPTS / "validate_provider_bundle.py"
_SPEC = importlib.util.spec_from_file_location("mailhub_validate_provider_bundle", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
BundleValidationError = cast(Any, _MODULE.BundleValidationError)
validate_bundle = cast(Any, _MODULE.validate_bundle)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sync_payload() -> dict[str, object]:
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
        "sync_filter": {"bounded": True, "folder_ref_sha256": _hash("INBOX")},
        "readiness": {"status": "ready", "environment": "injected", "missing_count": 0},
        "credentials_observed": False,
    }


def _write_bundle(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "sync-reconciliation.json").write_text(json.dumps(_sync_payload()), encoding="utf-8")
    account_hash = _hash("graph-account-1")
    tenant_hash = _hash("tenant-graph-1")
    (root / "preflight.json").write_text(
        json.dumps(
            {
                "schema_version": "mailhub.provider_preflight.v1",
                "network_access": False,
                "real_provider_evidence": False,
                "config_ready": True,
                "readiness": "config_ready",
                "ready": True,
                "providers": [
                    {
                        "provider": "microsoft_graph",
                        "ready": True,
                        "enabled": True,
                        "readiness": "config_ready",
                        "real_provider_evidence": False,
                        "missing": [],
                        "errors": [],
                        "config": {
                            "enabled": "true",
                            "read_only": "true",
                            "push_enabled": "false",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "oauth-consent.json").write_text(
        json.dumps(
            {
                "schema_version": "mailhub.provider_activation_oauth.v1",
                "provider": "microsoft_graph",
                "environment": "isolated-test",
                "run_id": "run-1",
                "network_access": True,
                "status": "succeeded",
                "credentials_observed": False,
                "account_identity_sha256": account_hash,
                "tenant_identity_sha256": tenant_hash,
                "scope_subset_verified": True,
                "requested_scopes": ["Mail.Read", "offline_access"],
                "granted_scopes": ["Mail.Read", "offline_access"],
                "state_replay_rejected": True,
                "redirect_allowlist_verified": True,
                "pkce_verified": True,
                "refresh_verified": True,
                "revoke_verified": True,
            }
        ),
        encoding="utf-8",
    )
    (root / "webhook-receipts.json").write_text(
        json.dumps(
            {
                "schema_version": "mailhub.provider_activation_notifications.v1",
                "provider": "microsoft_graph",
                "environment": "isolated-test",
                "run_id": "run-1",
                "network_access": True,
                "status": "succeeded",
                "credentials_observed": False,
                "account_identity_sha256": account_hash,
                "tenant_identity_sha256": tenant_hash,
                "subscription_active": True,
                "renewal_count": 1,
                "receipt_count": 2,
                "duplicate_count": 1,
                "ack_p95_ms": 120,
                "observation_started_at": "2026-07-20T00:00:00Z",
                "observation_finished_at": "2026-07-29T00:00:00Z",
                "dedupe_verified": True,
                "reconciliation_verified": True,
                "watermark_sha256": _hash("watermark"),
            }
        ),
        encoding="utf-8",
    )
    (root / "revocation-negative.json").write_text(
        json.dumps(
            {
                "schema_version": "mailhub.provider_activation_revocation.v1",
                "provider": "microsoft_graph",
                "environment": "isolated-test",
                "run_id": "run-1",
                "network_access": True,
                "status": "succeeded",
                "credentials_observed": False,
                "account_identity_sha256": account_hash,
                "tenant_identity_sha256": tenant_hash,
                "connection_status": "revoked",
                "subscription_cancelled": True,
                "sync_rejected_after_revoke": True,
                "provider_requests_after_revoke": 0,
                "credential_broker_requests_after_revoke": 0,
                "delete_proof_recorded": True,
            }
        ),
        encoding="utf-8",
    )
    (root / "security-review.md").write_text(
        "\n".join(
            (
                "# MailHub security review",
                "schema_version=mailhub.provider_security_review.v1",
                "provider=microsoft_graph",
                "environment=isolated-test",
                "run_id=run-1",
                "decision=approved",
                "scope_reviewed=true",
                "dlp_av_reviewed=true",
                "privacy_reviewed=true",
                "retention_reviewed=true",
                "deletion_reviewed=true",
                f"signoff_ref_sha256={_hash('security-signoff-1')}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "test-output.txt").write_text(
        "\n".join(
            (
                "schema_version=mailhub.provider_activation_test_output.v1",
                "provider=microsoft_graph",
                "environment=isolated-test",
                "run_id=run-1",
                "network_access=true",
                "status=succeeded",
                "credentials_observed=false",
                "exit_code=0",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_bundle_validator_accepts_complete_real_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "graph" / "isolated-test" / "run-1"
    _write_bundle(bundle)

    summary = validate_bundle(bundle, require_real=True)

    assert summary["schema_version"] == "mailhub.provider_activation_bundle_validation.v1"
    assert summary["real_provider_evidence"] is True
    assert "complete_a1_a4_bundle_verified" in summary["checks"]


def test_bundle_validator_accepts_explicit_push_with_host_endpoint_markers(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "graph" / "isolated-test" / "run-1"
    _write_bundle(bundle)
    preflight_path = bundle / "preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    config = preflight["providers"][0]["config"]
    config["push_enabled"] = "true"
    config["subscription_endpoint"] = "<set>"
    config["notification_verifier_endpoint"] = "<set>"
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    summary = validate_bundle(bundle, require_real=True)

    assert "push_host_boundary_declared" in summary["checks"]


def test_bundle_validator_rejects_push_without_host_endpoint_markers(tmp_path: Path) -> None:
    bundle = tmp_path / "graph" / "isolated-test" / "run-1"
    _write_bundle(bundle)
    preflight_path = bundle / "preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["providers"][0]["config"]["push_enabled"] = "true"
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="push_endpoint_missing"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_requires_all_real_artifacts(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    bundle.mkdir()
    (bundle / "sync-reconciliation.json").write_text(json.dumps(_sync_payload()), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="preflight_json_missing"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_cross_checks_identity_hashes(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    oauth_path = bundle / "oauth-consent.json"
    oauth = json.loads(oauth_path.read_text(encoding="utf-8"))
    oauth["account_identity_sha256"] = _hash("other-account")
    oauth_path.write_text(json.dumps(oauth), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="account_identity_mismatch"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_rejects_sensitive_artifact_text(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    (bundle / "security-review.md").write_text("access_token: leaked\n", encoding="utf-8")

    with pytest.raises(BundleValidationError, match="sensitive_text_present"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_binds_artifacts_to_environment(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    oauth_path = bundle / "oauth-consent.json"
    oauth = json.loads(oauth_path.read_text(encoding="utf-8"))
    oauth["environment"] = "production"
    oauth_path.write_text(json.dumps(oauth), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="environment_mismatch"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_rejects_disabled_or_write_enabled_preflight(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    preflight_path = bundle / "preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["providers"][0]["config"]["read_only"] = "false"
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="read_only_gate_invalid"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_requires_successful_machine_test_output(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    output_path = bundle / "test-output.txt"
    output = output_path.read_text(encoding="utf-8").replace("status=succeeded", "status=failed")
    output = output.replace("exit_code=0", "exit_code=2")
    output_path.write_text(output, encoding="utf-8")

    with pytest.raises(BundleValidationError, match="real_run_required"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_requires_structured_approved_security_review(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    review_path = bundle / "security-review.md"
    review = review_path.read_text(encoding="utf-8").replace(
        "decision=approved", "decision=blocked"
    )
    review_path.write_text(review, encoding="utf-8")

    with pytest.raises(BundleValidationError, match="security_review_not_approved"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_rejects_write_scope_in_oauth_artifact(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    oauth_path = bundle / "oauth-consent.json"
    oauth = json.loads(oauth_path.read_text(encoding="utf-8"))
    oauth["requested_scopes"].append("Mail.Send")
    oauth["granted_scopes"].append("Mail.Send")
    oauth_path.write_text(json.dumps(oauth), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="write_scope_disallowed"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_rejects_missing_required_read_scope(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    oauth_path = bundle / "oauth-consent.json"
    oauth = json.loads(oauth_path.read_text(encoding="utf-8"))
    oauth["granted_scopes"] = ["offline_access"]
    oauth_path.write_text(json.dumps(oauth), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="granted_read_scope_missing"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_rejects_empty_notification_window(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    receipts_path = bundle / "webhook-receipts.json"
    receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
    receipts["renewal_count"] = 0
    receipts_path.write_text(json.dumps(receipts), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="renewal_missing"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_rejects_more_duplicates_than_receipts(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    receipts_path = bundle / "webhook-receipts.json"
    receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
    receipts["duplicate_count"] = receipts["receipt_count"] + 1
    receipts_path.write_text(json.dumps(receipts), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="duplicate_count_invalid"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_rejects_short_observation_window(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    receipts_path = bundle / "webhook-receipts.json"
    receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
    receipts["observation_finished_at"] = "2026-07-21T00:00:00Z"
    receipts_path.write_text(json.dumps(receipts), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="observation_window_too_short"):
        validate_bundle(bundle, require_real=True)


def test_bundle_validator_rejects_future_observation_window(tmp_path: Path) -> None:
    bundle = tmp_path / "run-1"
    _write_bundle(bundle)
    receipts_path = bundle / "webhook-receipts.json"
    receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
    receipts["observation_started_at"] = "2099-01-01T00:00:00Z"
    receipts["observation_finished_at"] = "2100-01-08T00:00:00Z"
    receipts_path.write_text(json.dumps(receipts), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="observation_in_future"):
        validate_bundle(bundle, require_real=True)
