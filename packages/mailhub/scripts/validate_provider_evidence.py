"""Validate a redacted MailHub provider-activation evidence bundle.

The validator never contacts a provider and never treats a fixture or an
activation-gate failure as real evidence.  With ``--require-real`` it accepts
only a successful ``real_api_observation`` bundle with readiness, identity
scope, durable sync and idempotent replay assertions present.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

_PROVIDERS = {"gmail", "microsoft_graph"}
_SECRET_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "cookie",
    "credential_ref",
    "id_token",
    "password",
    "refresh_token",
    "set_cookie",
    "token",
}
_SENSITIVE_VALUE_KEYS = {
    "body",
    "body_html",
    "body_text",
    "content",
    "email_address",
    "provider_account_id",
    "provider_tenant_id",
    "raw_headers",
    "raw_response",
    "recipient_addresses",
    "sender_address",
}
_TOP_LEVEL_KEYS = {
    "schema_version",
    "evidence_kind",
    "network_access",
    "real_provider_evidence",
    "provider",
    "environment",
    "run_id",
    "status",
    "started_at",
    "finished_at",
    "tenant_sha256",
    "subject_sha256",
    "connection",
    "sync",
    "replay",
    "sync_filter",
    "readiness",
    "credentials_observed",
    "error_code",
}
_CONNECTION_KEYS = {
    "connection_id",
    "provider",
    "status",
    "revision",
    "scope_count",
    "identity",
}
_IDENTITY_KEYS = {
    "account_identity_sha256",
    "email_identity_sha256",
    "tenant_identity_sha256",
}
_SYNC_KEYS = {
    "job_ref",
    "status",
    "fetched_count",
    "saved_count",
    "duplicate_count",
    "deleted_count",
    "provider_request_id",
    "error_code",
    "reset_required",
    "cursor_before_sha256",
    "cursor_after_sha256",
}
_REPLAY_KEYS = {"same_job_ref", "initial_status", "replayed_status"}
_SYNC_FILTER_KEYS = {
    "bounded",
    "folder_ref_sha256",
    "label_count",
    "label_refs_sha256",
    "received_after",
    "received_before",
}
_READINESS_KEYS = {"status", "environment", "missing_count"}


class EvidenceValidationError(ValueError):
    """A stable, non-sensitive evidence validation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceValidationError(f"evidence_{name}_invalid")
    return value


def _text(value: object, name: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise EvidenceValidationError(f"evidence_{name}_invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise EvidenceValidationError(f"evidence_{name}_invalid")
    return value.strip()


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceValidationError(f"evidence_{name}_invalid")
    return value


def _int(value: object, name: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise EvidenceValidationError(f"evidence_{name}_invalid")
    return value


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                normalized = key.casefold().replace("-", "_")
                if normalized in _SECRET_KEYS or normalized in _SENSITIVE_VALUE_KEYS:
                    return True
            if _contains_forbidden_key(child):
                return True
    elif isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_forbidden_key(child) for child in value)
    return False


def _reject_unknown_keys(value: Mapping[str, object], allowed: set[str], name: str) -> None:
    """Keep evidence bundles closed-world so unreviewed fields cannot leak data."""

    unknown = sorted(key for key in value if key not in allowed)
    if unknown:
        raise EvidenceValidationError(f"evidence_{name}_unknown_field")


def _timestamp(value: object, name: str) -> datetime:
    result = _text(value, name, 80)
    candidate = result[:-1] + "+00:00" if result.endswith(("Z", "z")) else result
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EvidenceValidationError(f"evidence_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceValidationError(f"evidence_{name}_must_be_aware")
    return parsed.astimezone(UTC)


def _require_hash(value: object, name: str) -> str:
    result = _text(value, name, 128)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result.casefold()):
        raise EvidenceValidationError(f"evidence_{name}_hash_invalid")
    return result


def _validate_common(payload: Mapping[str, object]) -> tuple[str, str, bool, bool]:
    if payload.get("schema_version") != "mailhub.provider_activation_sync.v1":
        raise EvidenceValidationError("evidence_schema_version_invalid")
    provider = _text(payload.get("provider"), "provider", 40)
    if provider not in _PROVIDERS:
        raise EvidenceValidationError("evidence_provider_invalid")
    evidence_kind = _text(payload.get("evidence_kind"), "evidence_kind", 80)
    if evidence_kind not in {"activation_gate_failure", "real_api_observation"}:
        raise EvidenceValidationError("evidence_kind_invalid")
    network_access = _bool(payload.get("network_access"), "network_access")
    real_provider_evidence = _bool(payload.get("real_provider_evidence"), "real_provider_evidence")
    credentials_observed = _bool(payload.get("credentials_observed"), "credentials_observed")
    if credentials_observed:
        raise EvidenceValidationError("evidence_credentials_observed")
    if _contains_forbidden_key(payload):
        raise EvidenceValidationError("evidence_sensitive_field_present")
    _reject_unknown_keys(payload, _TOP_LEVEL_KEYS, "payload")
    if real_provider_evidence != (evidence_kind == "real_api_observation"):
        raise EvidenceValidationError("evidence_real_flag_inconsistent")
    _text(payload.get("environment"), "environment", 80)
    _text(payload.get("run_id"), "run_id", 100)
    _text(payload.get("status"), "status", 40)
    started_at = _timestamp(payload.get("started_at"), "started_at")
    finished_at = _timestamp(payload.get("finished_at"), "finished_at")
    if finished_at < started_at:
        raise EvidenceValidationError("evidence_observation_time_reversed")
    if finished_at > datetime.now(UTC):
        raise EvidenceValidationError("evidence_observation_in_future")
    _require_hash(payload.get("tenant_sha256"), "tenant")
    _require_hash(payload.get("subject_sha256"), "subject")
    return provider, evidence_kind, network_access, credentials_observed


def _validate_gate_failure(
    payload: Mapping[str, object], *, network_access: bool
) -> tuple[str, ...]:
    _reject_unknown_keys(
        _mapping(payload.get("connection"), "gate_connection")
        if payload.get("connection") is not None
        else {},
        _CONNECTION_KEYS,
        "gate_connection",
    )
    if payload.get("status") != "failed":
        raise EvidenceValidationError("evidence_gate_status_invalid")
    _text(payload.get("error_code"), "error_code")
    if payload.get("connection") is not None:
        raise EvidenceValidationError("evidence_gate_connection_must_be_null")
    if payload.get("real_provider_evidence") is not False:
        raise EvidenceValidationError("evidence_gate_real_flag_invalid")
    if network_access not in {False, True}:
        raise EvidenceValidationError("evidence_gate_network_access_invalid")
    return ("gate_failure_recorded",)


def _validate_real_observation(
    payload: Mapping[str, object], *, provider: str, network_access: bool
) -> tuple[str, ...]:
    if not network_access:
        raise EvidenceValidationError("evidence_real_network_access_required")
    if payload.get("status") != "succeeded":
        raise EvidenceValidationError("evidence_real_status_invalid")
    if payload.get("error_code") is not None:
        raise EvidenceValidationError("evidence_real_error_code_invalid")

    connection = _mapping(payload.get("connection"), "connection")
    _reject_unknown_keys(connection, _CONNECTION_KEYS, "connection")
    if connection.get("provider") != provider or connection.get("status") != "active":
        raise EvidenceValidationError("evidence_connection_identity_invalid")
    _text(connection.get("connection_id"), "connection_id", 200)
    _int(connection.get("revision"), "connection_revision", minimum=1)
    _int(connection.get("scope_count"), "connection_scope_count", minimum=1, maximum=100)
    identity = _mapping(connection.get("identity"), "connection_identity")
    _reject_unknown_keys(identity, _IDENTITY_KEYS, "connection_identity")
    _require_hash(identity.get("account_identity_sha256"), "account_identity")
    if provider == "microsoft_graph":
        _require_hash(identity.get("tenant_identity_sha256"), "tenant_identity")

    readiness = _mapping(payload.get("readiness"), "readiness")
    _reject_unknown_keys(readiness, _READINESS_KEYS, "readiness")
    if readiness.get("status") != "ready" or readiness.get("missing_count") != 0:
        raise EvidenceValidationError("evidence_runtime_not_ready")
    _text(readiness.get("environment"), "readiness_environment", 80)

    replay = _mapping(payload.get("replay"), "replay")
    _reject_unknown_keys(replay, _REPLAY_KEYS, "replay")
    if replay.get("same_job_ref") is not True:
        raise EvidenceValidationError("evidence_replay_not_idempotent")
    _text(replay.get("initial_status"), "initial_status", 40)
    _text(replay.get("replayed_status"), "replayed_status", 40)

    sync = _mapping(payload.get("sync"), "sync")
    _reject_unknown_keys(sync, _SYNC_KEYS, "sync")
    if sync.get("status") != "succeeded":
        raise EvidenceValidationError("evidence_sync_not_succeeded")
    _text(sync.get("job_ref"), "job_ref", 200)
    for key in ("fetched_count", "saved_count", "duplicate_count", "deleted_count"):
        if key in sync:
            _int(sync[key], key)
    for key in ("cursor_before_sha256", "cursor_after_sha256"):
        if key in sync:
            _require_hash(sync[key], key.removesuffix("_sha256"))
    if "reset_required" in sync:
        _bool(sync["reset_required"], "sync_reset_required")

    sync_filter = _mapping(payload.get("sync_filter"), "sync_filter")
    _reject_unknown_keys(sync_filter, _SYNC_FILTER_KEYS, "sync_filter")
    _bool(sync_filter.get("bounded"), "sync_filter_bounded")
    _require_hash(sync_filter.get("folder_ref_sha256"), "folder_ref")
    if "label_count" in sync_filter:
        _int(sync_filter["label_count"], "label_count", maximum=20)
    if "label_refs_sha256" in sync_filter:
        label_hashes = sync_filter["label_refs_sha256"]
        if not isinstance(label_hashes, list) or len(label_hashes) > 20:
            raise EvidenceValidationError("evidence_label_refs_sha256_invalid")
        for index, value in enumerate(label_hashes):
            _require_hash(value, f"label_ref_{index}")
    for key in ("received_after", "received_before"):
        if key in sync_filter:
            _timestamp(sync_filter[key], key)
    return (
        "runtime_readiness_verified",
        "active_connection_verified",
        "durable_sync_succeeded",
        "idempotent_replay_verified",
    )


def validate_evidence(
    payload: Mapping[str, object], *, require_real: bool = False
) -> dict[str, object]:
    """Validate and return a safe summary without echoing the source payload."""

    provider, evidence_kind, network_access, _credentials_observed = _validate_common(payload)
    if evidence_kind == "real_api_observation":
        checks = _validate_real_observation(
            payload, provider=provider, network_access=network_access
        )
        real_provider_evidence = True
    else:
        checks = _validate_gate_failure(payload, network_access=network_access)
        real_provider_evidence = False
    if require_real and not real_provider_evidence:
        raise EvidenceValidationError("evidence_real_observation_required")
    return {
        "schema_version": "mailhub.provider_activation_validation.v1",
        "provider": provider,
        "evidence_kind": evidence_kind,
        "status": _text(payload.get("status"), "status", 40),
        "network_access": network_access,
        "real_provider_evidence": real_provider_evidence,
        "checks": list(checks),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="redacted sync-reconciliation.json")
    parser.add_argument(
        "--require-real",
        action="store_true",
        help="reject activation-gate failures and accept only real API observations",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        raw = json.loads(args.evidence.read_text(encoding="utf-8"))
        payload = _mapping(raw, "payload")
        summary = validate_evidence(payload, require_real=args.require_real)
    except (OSError, json.JSONDecodeError, EvidenceValidationError) as exc:
        code = exc.code if isinstance(exc, EvidenceValidationError) else "evidence_file_invalid"
        print(f"provider evidence invalid: {code}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
