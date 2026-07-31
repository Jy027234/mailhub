"""Validate a complete, redacted MailHub provider activation bundle.

This command is an offline release gate.  It never contacts Gmail, Microsoft
Graph, a host endpoint, or a secret store.  The sync evidence validator owns
the durable-sync contract; this command adds the cross-file OAuth,
notification, revocation, and artifact checks needed for an A1--A4 bundle.
The A0 preflight may describe either scheduled read-only polling
(``push_enabled=false``) or an explicitly host-bound push capability; the
latter must include both Host endpoint markers and real notification evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from validate_provider_evidence import EvidenceValidationError, validate_evidence

_PROVIDERS = {"gmail", "microsoft_graph"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization\s*:\s*bearer|"
    r"id[_-]?token|credential[_-]?ref)"
)
_BUNDLE_KEYS = {
    "schema_version",
    "provider",
    "environment",
    "run_id",
    "network_access",
    "status",
    "credentials_observed",
    "account_identity_sha256",
    "tenant_identity_sha256",
    "scope_subset_verified",
    "requested_scopes",
    "granted_scopes",
    "state_replay_rejected",
    "redirect_allowlist_verified",
    "pkce_verified",
    "refresh_verified",
    "revoke_verified",
    "subscription_active",
    "renewal_count",
    "receipt_count",
    "duplicate_count",
    "ack_p95_ms",
    "observation_started_at",
    "observation_finished_at",
    "dedupe_verified",
    "reconciliation_verified",
    "watermark_sha256",
    "connection_status",
    "subscription_cancelled",
    "sync_rejected_after_revoke",
    "provider_requests_after_revoke",
    "credential_broker_requests_after_revoke",
    "delete_proof_recorded",
}
_TEST_OUTPUT_KEYS = {
    "schema_version",
    "provider",
    "environment",
    "run_id",
    "network_access",
    "status",
    "credentials_observed",
    "exit_code",
    "error_code",
}
_SECURITY_REVIEW_KEYS = {
    "schema_version",
    "provider",
    "environment",
    "run_id",
    "decision",
    "scope_reviewed",
    "dlp_av_reviewed",
    "privacy_reviewed",
    "retention_reviewed",
    "deletion_reviewed",
    "signoff_ref_sha256",
}
_REQUIRED_READ_SCOPES = {
    "gmail": {"https://www.googleapis.com/auth/gmail.readonly"},
    "microsoft_graph": {"mail.read", "offline_access"},
}
_WRITE_SCOPE_MARKERS = {
    "gmail": (
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.settings.",
    ),
    "microsoft_graph": (
        "mail.send",
        "mail.readwrite",
        "mail.manage",
        "mail.fullaccessasuser",
    ),
}


class BundleValidationError(ValueError):
    """A stable, non-sensitive bundle validation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BundleValidationError(f"bundle_{name}_invalid")
    return value


def _text(value: object, name: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BundleValidationError(f"bundle_{name}_invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BundleValidationError(f"bundle_{name}_invalid")
    return value.strip()


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise BundleValidationError(f"bundle_{name}_invalid")
    return value


def _int(value: object, name: str, *, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise BundleValidationError(f"bundle_{name}_invalid")
    return value


def _hash(value: object, name: str) -> str:
    result = _text(value, name, 64).casefold()
    if _HASH_RE.fullmatch(result) is None:
        raise BundleValidationError(f"bundle_{name}_hash_invalid")
    return result


def _timestamp(value: object, name: str) -> datetime:
    raw = _text(value, name, 80)
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise BundleValidationError(f"bundle_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BundleValidationError(f"bundle_{name}_must_be_aware")
    return parsed.astimezone(UTC)


def _scopes(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 40:
        raise BundleValidationError(f"bundle_{name}_invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not 1 <= len(item) <= 200:
            raise BundleValidationError(f"bundle_{name}_invalid")
        normalized = item.strip()
        if not normalized or any(ord(char) < 33 or ord(char) == 127 for char in normalized):
            raise BundleValidationError(f"bundle_{name}_invalid")
        if normalized in result:
            raise BundleValidationError(f"bundle_{name}_duplicate")
        result.append(normalized)
    return tuple(result)


def _reject_unknown(payload: Mapping[str, object], allowed: set[str], name: str) -> None:
    if any(key not in allowed for key in payload):
        raise BundleValidationError(f"bundle_{name}_unknown_field")


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and _SENSITIVE_TEXT_RE.search(key):
                raise BundleValidationError("bundle_sensitive_field_present")
            _reject_sensitive(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            _reject_sensitive(child)
    elif isinstance(value, str) and _SENSITIVE_TEXT_RE.search(value):
        raise BundleValidationError("bundle_sensitive_text_present")


def _read_json(path: Path, name: str) -> Mapping[str, object]:
    try:
        if not path.is_file() or path.stat().st_size > 1_000_000:
            raise BundleValidationError(f"bundle_{name}_missing_or_too_large")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"bundle_{name}_invalid") from exc
    payload = _mapping(raw, name)
    _reject_sensitive(payload)
    return payload


def _read_text(path: Path, name: str) -> str:
    try:
        if not path.is_file() or path.stat().st_size > 1_000_000:
            raise BundleValidationError(f"bundle_{name}_missing_or_too_large")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BundleValidationError(f"bundle_{name}_invalid") from exc
    if any(ord(char) < 9 or (13 < ord(char) < 32) for char in text):
        raise BundleValidationError(f"bundle_{name}_control_character")
    if _SENSITIVE_TEXT_RE.search(text):
        raise BundleValidationError("bundle_sensitive_text_present")
    if not text.strip():
        raise BundleValidationError(f"bundle_{name}_empty")
    return text


def _validate_test_output(
    text: str,
    *,
    provider: str,
    environment: str,
    run_id: str,
    require_real: bool,
) -> None:
    """Validate the machine-readable command result inside a bundle."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or "=" not in line:
            raise BundleValidationError("bundle_test_output_line_invalid")
        key, value = line.split("=", 1)
        if key not in _TEST_OUTPUT_KEYS or key in values:
            raise BundleValidationError("bundle_test_output_unknown_or_duplicate_field")
        values[key] = _text(value, f"test_output_{key}", 1000)
    if set(values) - {"error_code"} != {
        "schema_version",
        "provider",
        "environment",
        "run_id",
        "network_access",
        "status",
        "credentials_observed",
        "exit_code",
    }:
        raise BundleValidationError("bundle_test_output_required_field_missing")
    if values["schema_version"] != "mailhub.provider_activation_test_output.v1":
        raise BundleValidationError("bundle_test_output_schema_invalid")
    if values["provider"] != provider:
        raise BundleValidationError("bundle_test_output_provider_mismatch")
    if values["environment"] != environment:
        raise BundleValidationError("bundle_test_output_environment_mismatch")
    if values["run_id"] != run_id:
        raise BundleValidationError("bundle_test_output_run_id_mismatch")
    if values["credentials_observed"] != "false":
        raise BundleValidationError("bundle_test_output_credentials_observed")
    if values["network_access"] not in {"true", "false"}:
        raise BundleValidationError("bundle_test_output_network_access_invalid")
    if values["status"] not in {"succeeded", "failed"}:
        raise BundleValidationError("bundle_test_output_status_invalid")
    if values["exit_code"] not in {"0", "2"}:
        raise BundleValidationError("bundle_test_output_exit_code_invalid")
    if values["status"] == "succeeded" and values["exit_code"] != "0":
        raise BundleValidationError("bundle_test_output_success_exit_invalid")
    if values["status"] == "failed" and values["exit_code"] != "2":
        raise BundleValidationError("bundle_test_output_failure_exit_invalid")
    if "error_code" in values and values["status"] != "failed":
        raise BundleValidationError("bundle_test_output_error_on_success")
    if require_real and (
        values["network_access"] != "true"
        or values["status"] != "succeeded"
        or values["exit_code"] != "0"
    ):
        raise BundleValidationError("bundle_test_output_real_run_required")


def _validate_security_review(text: str, *, provider: str, environment: str, run_id: str) -> None:
    """Validate the bounded metadata block in the human-readable review file."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise BundleValidationError("bundle_security_review_line_invalid")
        key, value = stripped.split("=", 1)
        if key not in _SECURITY_REVIEW_KEYS or key in values:
            raise BundleValidationError("bundle_security_review_unknown_or_duplicate_field")
        values[key] = _text(value, f"security_review_{key}", 1000)
    required = {
        "schema_version",
        "provider",
        "environment",
        "run_id",
        "decision",
        "scope_reviewed",
        "dlp_av_reviewed",
        "privacy_reviewed",
        "retention_reviewed",
        "deletion_reviewed",
        "signoff_ref_sha256",
    }
    if set(values) != required:
        raise BundleValidationError("bundle_security_review_required_field_missing")
    if values["schema_version"] != "mailhub.provider_security_review.v1":
        raise BundleValidationError("bundle_security_review_schema_invalid")
    if values["provider"] != provider:
        raise BundleValidationError("bundle_security_review_provider_mismatch")
    if values["environment"] != environment:
        raise BundleValidationError("bundle_security_review_environment_mismatch")
    if values["run_id"] != run_id:
        raise BundleValidationError("bundle_security_review_run_id_mismatch")
    if values["decision"] != "approved":
        raise BundleValidationError("bundle_security_review_not_approved")
    for key in (
        "scope_reviewed",
        "dlp_av_reviewed",
        "privacy_reviewed",
        "retention_reviewed",
        "deletion_reviewed",
    ):
        if values[key] != "true":
            raise BundleValidationError(f"bundle_security_review_{key}_missing")
    _hash(values["signoff_ref_sha256"], "security_review_signoff_ref")


def _provider_and_identity(
    payload: Mapping[str, object], *, provider: str, environment: str, run_id: str
) -> tuple[str, str | None]:
    _reject_unknown(payload, _BUNDLE_KEYS, "artifact")
    if payload.get("provider") != provider:
        raise BundleValidationError("bundle_provider_mismatch")
    if _text(payload.get("environment"), "environment", 100) != environment:
        raise BundleValidationError("bundle_environment_mismatch")
    if _text(payload.get("run_id"), "run_id", 100) != run_id:
        raise BundleValidationError("bundle_run_id_mismatch")
    account_hash = _hash(payload.get("account_identity_sha256"), "account_identity")
    tenant_hash: str | None = None
    if provider == "microsoft_graph":
        tenant_hash = _hash(payload.get("tenant_identity_sha256"), "tenant_identity")
    return account_hash, tenant_hash


def _validate_preflight(
    payload: Mapping[str, object], *, provider: str, require_real: bool
) -> tuple[str, ...]:
    if payload.get("schema_version") != "mailhub.provider_preflight.v1":
        raise BundleValidationError("bundle_preflight_schema_invalid")
    if (
        payload.get("network_access") is not False
        or payload.get("real_provider_evidence") is not False
        or payload.get("config_ready") is not True
        or payload.get("readiness") != "config_ready"
        or payload.get("ready") is not True
    ):
        raise BundleValidationError("bundle_preflight_must_be_offline")
    providers = payload.get("providers")
    if not isinstance(providers, list):
        raise BundleValidationError("bundle_preflight_providers_invalid")
    report = next(
        (
            item
            for item in providers
            if isinstance(item, Mapping) and item.get("provider") == provider
        ),
        None,
    )
    if report is None:
        raise BundleValidationError("bundle_preflight_provider_missing")
    if (
        report.get("enabled") is not True
        or report.get("ready") is not True
        or report.get("readiness") != "config_ready"
        or report.get("real_provider_evidence") is not False
    ):
        raise BundleValidationError("bundle_preflight_provider_not_config_ready")
    missing = report.get("missing")
    errors = report.get("errors")
    if not isinstance(missing, list) or missing or not isinstance(errors, list) or errors:
        raise BundleValidationError("bundle_preflight_provider_has_missing_dependencies")
    config = _mapping(report.get("config"), "preflight_config")
    if config.get("enabled") != "true" or config.get("read_only") != "true":
        raise BundleValidationError("bundle_preflight_read_only_gate_invalid")
    push_enabled = config.get("push_enabled")
    if push_enabled not in {"false", "true"}:
        raise BundleValidationError("bundle_preflight_push_gate_invalid")
    if push_enabled == "true":
        for key in ("subscription_endpoint", "notification_verifier_endpoint"):
            if config.get(key) != "<set>":
                raise BundleValidationError("bundle_preflight_push_endpoint_missing")
    readiness = _text(report.get("readiness"), "preflight_readiness", 40)
    if require_real and (readiness != "config_ready" or report.get("ready") is not True):
        raise BundleValidationError("bundle_preflight_not_config_ready")
    checks = ["offline_preflight_verified"]
    if push_enabled == "true":
        checks.append("push_host_boundary_declared")
    return tuple(checks)


def _validate_oauth(
    payload: Mapping[str, object], *, provider: str, environment: str, run_id: str
) -> tuple[str, str | None, tuple[str, ...]]:
    if payload.get("schema_version") != "mailhub.provider_activation_oauth.v1":
        raise BundleValidationError("bundle_oauth_schema_invalid")
    if payload.get("network_access") is not True or payload.get("status") != "succeeded":
        raise BundleValidationError("bundle_oauth_not_succeeded")
    if payload.get("credentials_observed") is not False:
        raise BundleValidationError("bundle_oauth_credentials_observed")
    account_hash, tenant_hash = _provider_and_identity(
        payload, provider=provider, environment=environment, run_id=run_id
    )
    for key in (
        "scope_subset_verified",
        "state_replay_rejected",
        "redirect_allowlist_verified",
        "pkce_verified",
        "refresh_verified",
        "revoke_verified",
    ):
        if payload.get(key) is not True:
            raise BundleValidationError(f"bundle_oauth_{key}_missing")
    requested_scopes = _scopes(payload.get("requested_scopes"), "oauth_requested_scopes")
    granted_scopes = _scopes(payload.get("granted_scopes"), "oauth_granted_scopes")
    requested = {scope.casefold() for scope in requested_scopes}
    granted = {scope.casefold() for scope in granted_scopes}
    required = _REQUIRED_READ_SCOPES[provider]
    if not required.issubset(requested):
        raise BundleValidationError("bundle_oauth_requested_read_scope_missing")
    if not required.issubset(granted):
        raise BundleValidationError("bundle_oauth_granted_read_scope_missing")
    if not granted.issubset(requested):
        raise BundleValidationError("bundle_oauth_scope_escalation")
    if any(
        scope == marker or scope.startswith(marker)
        for scope in granted
        for marker in _WRITE_SCOPE_MARKERS[provider]
    ):
        raise BundleValidationError("bundle_oauth_write_scope_disallowed")
    return account_hash, tenant_hash, ("oauth_identity_and_scope_verified",)


def _validate_notifications(
    payload: Mapping[str, object], *, provider: str, environment: str, run_id: str
) -> tuple[str, str | None, tuple[str, ...]]:
    if payload.get("schema_version") != "mailhub.provider_activation_notifications.v1":
        raise BundleValidationError("bundle_notifications_schema_invalid")
    if payload.get("network_access") is not True or payload.get("status") != "succeeded":
        raise BundleValidationError("bundle_notifications_not_succeeded")
    if payload.get("credentials_observed") is not False:
        raise BundleValidationError("bundle_notifications_credentials_observed")
    account_hash, tenant_hash = _provider_and_identity(
        payload, provider=provider, environment=environment, run_id=run_id
    )
    if payload.get("subscription_active") is not True:
        raise BundleValidationError("bundle_subscription_not_active")
    if (
        payload.get("dedupe_verified") is not True
        or payload.get("reconciliation_verified") is not True
    ):
        raise BundleValidationError("bundle_notification_reconciliation_missing")
    renewal_count = _int(payload.get("renewal_count"), "renewal_count", maximum=1000)
    receipt_count = _int(payload.get("receipt_count"), "receipt_count", maximum=1_000_000)
    _int(payload.get("duplicate_count"), "duplicate_count", maximum=1_000_000)
    duplicate_count = int(payload["duplicate_count"])
    ack_p95_ms = _int(payload.get("ack_p95_ms"), "ack_p95_ms", maximum=60_000)
    if renewal_count < 1:
        raise BundleValidationError("bundle_notification_renewal_missing")
    if receipt_count < 1:
        raise BundleValidationError("bundle_notification_receipt_missing")
    if duplicate_count > receipt_count:
        raise BundleValidationError("bundle_notification_duplicate_count_invalid")
    if ack_p95_ms < 1:
        raise BundleValidationError("bundle_notification_ack_latency_invalid")
    started_at = _timestamp(payload.get("observation_started_at"), "observation_started_at")
    finished_at = _timestamp(payload.get("observation_finished_at"), "observation_finished_at")
    if finished_at > datetime.now(UTC):
        raise BundleValidationError("bundle_notification_observation_in_future")
    observation_hours = (finished_at - started_at).total_seconds() / 3600
    if observation_hours < 168:
        raise BundleValidationError("bundle_notification_observation_window_too_short")
    _hash(payload.get("watermark_sha256"), "watermark")
    return account_hash, tenant_hash, ("notification_receipt_and_reconciliation_verified",)


def _validate_revocation(
    payload: Mapping[str, object], *, provider: str, environment: str, run_id: str
) -> tuple[str, str | None, tuple[str, ...]]:
    if payload.get("schema_version") != "mailhub.provider_activation_revocation.v1":
        raise BundleValidationError("bundle_revocation_schema_invalid")
    if payload.get("network_access") is not True or payload.get("status") != "succeeded":
        raise BundleValidationError("bundle_revocation_not_succeeded")
    if payload.get("credentials_observed") is not False:
        raise BundleValidationError("bundle_revocation_credentials_observed")
    account_hash, tenant_hash = _provider_and_identity(
        payload, provider=provider, environment=environment, run_id=run_id
    )
    if payload.get("connection_status") not in {
        "revoked",
        "reauthorization_required",
        "deleted",
    }:
        raise BundleValidationError("bundle_revocation_connection_status_invalid")
    for key in (
        "subscription_cancelled",
        "sync_rejected_after_revoke",
        "delete_proof_recorded",
    ):
        if payload.get(key) is not True:
            raise BundleValidationError(f"bundle_revocation_{key}_missing")
    if _int(payload.get("provider_requests_after_revoke"), "provider_requests_after_revoke") != 0:
        raise BundleValidationError("bundle_revocation_provider_requests_nonzero")
    if (
        _int(
            payload.get("credential_broker_requests_after_revoke"),
            "credential_broker_requests_after_revoke",
        )
        != 0
    ):
        raise BundleValidationError("bundle_revocation_credential_requests_nonzero")
    return account_hash, tenant_hash, ("revocation_zero_access_verified",)


def validate_bundle(bundle_root: Path, *, require_real: bool = False) -> dict[str, object]:
    """Validate a bundle and return a safe summary without echoing source fields."""

    if not bundle_root.is_dir():
        raise BundleValidationError("bundle_root_missing")
    sync_path = bundle_root / "sync-reconciliation.json"
    sync_payload = _read_json(sync_path, "sync_reconciliation")
    sync_summary = validate_evidence(sync_payload, require_real=require_real)
    provider = str(sync_summary["provider"])
    run_id = _text(sync_payload.get("run_id"), "run_id", 100)
    environment = _text(sync_payload.get("environment"), "environment", 100)
    present: list[str] = ["sync-reconciliation.json"]
    checks = ["sync_evidence_verified"]
    identity = _mapping(sync_payload.get("connection"), "sync_connection") if require_real else None
    expected_account: str | None = None
    expected_tenant: str | None = None
    if identity is not None:
        identity_map = _mapping(identity.get("identity"), "sync_identity")
        expected_account = _hash(identity_map.get("account_identity_sha256"), "account_identity")
        if provider == "microsoft_graph":
            expected_tenant = _hash(identity_map.get("tenant_identity_sha256"), "tenant_identity")

    preflight_path = bundle_root / "preflight.json"
    if preflight_path.exists():
        preflight_payload = _read_json(preflight_path, "preflight_json")
        checks.extend(
            _validate_preflight(preflight_payload, provider=provider, require_real=require_real)
        )
        present.append("preflight.json")
    elif require_real:
        raise BundleValidationError("bundle_preflight_json_missing")

    artifact_specs = (
        ("oauth-consent.json", _validate_oauth),
        ("webhook-receipts.json", _validate_notifications),
        ("revocation-negative.json", _validate_revocation),
    )
    for filename, validator in artifact_specs:
        path = bundle_root / filename
        if not path.exists():
            if require_real:
                raise BundleValidationError(
                    f"bundle_{filename.replace('-', '_').replace('.', '_')}_missing"
                )
            continue
        payload = _read_json(path, filename.replace(".", "_"))
        account_hash, tenant_hash, artifact_checks = validator(
            payload, provider=provider, environment=environment, run_id=run_id
        )
        if expected_account is not None and account_hash != expected_account:
            raise BundleValidationError("bundle_account_identity_mismatch")
        if expected_tenant is not None and tenant_hash != expected_tenant:
            raise BundleValidationError("bundle_tenant_identity_mismatch")
        checks.extend(artifact_checks)
        present.append(filename)

    security_review_path = bundle_root / "security-review.md"
    if security_review_path.exists():
        security_review_text = _read_text(security_review_path, "security_review_md")
        _validate_security_review(
            security_review_text,
            provider=provider,
            environment=environment,
            run_id=run_id,
        )
        present.append("security-review.md")
    elif require_real:
        raise BundleValidationError("bundle_security_review_md_missing")

    test_output_path = bundle_root / "test-output.txt"
    if test_output_path.exists():
        test_output_text = _read_text(test_output_path, "test_output_txt")
        _validate_test_output(
            test_output_text,
            provider=provider,
            environment=environment,
            run_id=run_id,
            require_real=require_real,
        )
        present.append("test-output.txt")
    elif require_real:
        raise BundleValidationError("bundle_test_output_txt_missing")

    if require_real:
        checks.append("complete_a1_a4_bundle_verified")
    return {
        "schema_version": "mailhub.provider_activation_bundle_validation.v1",
        "provider": provider,
        "evidence_kind": sync_summary["evidence_kind"],
        "real_provider_evidence": bool(sync_summary["real_provider_evidence"]),
        "required_files": [
            "preflight.json",
            "oauth-consent.json",
            "webhook-receipts.json",
            "revocation-negative.json",
            "sync-reconciliation.json",
            "security-review.md",
            "test-output.txt",
        ],
        "present_files": present,
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="provider activation bundle directory")
    parser.add_argument(
        "--require-real",
        action="store_true",
        help="require a complete real A1-A4 bundle; never accepts gate failures",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = validate_bundle(args.bundle.resolve(), require_real=args.require_real)
    except (BundleValidationError, EvidenceValidationError) as exc:
        code = (
            exc.code
            if isinstance(exc, (BundleValidationError, EvidenceValidationError))
            else "bundle_invalid"
        )
        print(f"provider bundle invalid: {code}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
