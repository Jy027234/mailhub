"""Run a bounded real-provider sync and write a redacted activation evidence bundle.

This command talks only to a configured MailHub HTTP API.  OAuth exchange,
provider credentials, host identity and scheduler ownership remain outside the
script.  It deliberately requires two explicit operator gates before making
network calls and never accepts an access/refresh token as an argument.

The output is an observation of a real API run, not a fixture or connector
contract result.  Cursor values, tenant/subject identifiers and all response
bodies are reduced to hashes/counts before they are written.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

_PROVIDERS = ("gmail", "microsoft_graph")
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_LABEL_REFS = 20
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_READ_SCOPES = {
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
_SECRET_KEYS = {
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "id_token",
    "idtoken",
    "client_secret",
    "clientsecret",
    "authorization",
    "cookie",
    "set_cookie",
    "token",
    "token_type",
    "tokentype",
}


class ActivationError(RuntimeError):
    """A bounded configuration, transport or response error."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ActivationError(f"activation_{name}_invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ActivationError(f"activation_{name}_invalid")
    return value.strip()


def _slug(value: object, name: str) -> str:
    result = _text(value, name, 64)
    if _SLUG_RE.fullmatch(result) is None:
        raise ActivationError(f"activation_{name}_invalid")
    return result


def _run_id(value: object) -> str:
    result = _text(value, "run_id", 100)
    if _RUN_ID_RE.fullmatch(result) is None:
        raise ActivationError("activation_run_id_invalid")
    return result


def _iso_datetime(value: object, name: str) -> str:
    """Validate and normalize an operator-supplied aware UTC timestamp."""

    text = _text(value, name, 80)
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ActivationError(f"activation_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ActivationError(f"activation_{name}_must_be_aware")
    normalized = parsed.astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z")


def _label_refs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ActivationError("activation_label_refs_invalid")
    if len(value) > _MAX_LABEL_REFS:
        raise ActivationError("activation_label_refs_invalid")
    labels = tuple(_text(item, "label_ref", 200) for item in value)
    if len(set(labels)) != len(labels):
        raise ActivationError("activation_label_refs_invalid")
    return labels


def _sync_filter(
    *,
    mode: str,
    folder_ref: object,
    label_refs: object,
    received_after: object,
    received_before: object,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return a safe HTTP filter payload and a non-sensitive evidence summary."""

    folder = _text(folder_ref, "folder_ref", 200)
    labels = _label_refs(label_refs)
    after = _iso_datetime(received_after, "received_after") if received_after else None
    before = _iso_datetime(received_before, "received_before") if received_before else None
    if after is not None and before is not None:
        after_dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
        before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
        if after_dt >= before_dt:
            raise ActivationError("activation_received_range_invalid")
    bounded = folder != "INBOX" or bool(labels) or after is not None or before is not None
    if bounded and mode != "backfill":
        raise ActivationError("activation_sync_filter_requires_backfill")
    body: dict[str, object] = {"folder_ref": folder, "label_refs": list(labels)}
    if after is not None:
        body["received_after"] = after
    if before is not None:
        body["received_before"] = before
    summary: dict[str, object] = {
        "bounded": bounded,
        "folder_ref_sha256": _hash_text(folder),
        "label_count": len(labels),
        "label_refs_sha256": [_hash_text(label) for label in labels],
    }
    if after is not None:
        summary["received_after"] = after
    if before is not None:
        summary["received_before"] = before
    return body, summary


def _api_url(value: object) -> str:
    result = _text(value, "api_url", 2000).rstrip("/")
    parsed = urlsplit(result)
    hostname = (parsed.hostname or "").casefold()
    local_http = parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not hostname
        or (parsed.scheme != "https" and not local_http)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ActivationError("activation_api_url_must_be_https")
    return result


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _contains_secret_shape(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                normalized = key.casefold().replace("-", "_")
                if normalized in _SECRET_KEYS:
                    return True
            if _contains_secret_shape(child):
                return True
    elif isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_secret_shape(child) for child in value)
    return False


def _data(payload: Mapping[str, object]) -> object:
    result = payload.get("data", payload)
    if _contains_secret_shape(result):
        raise ActivationError("activation_secret_shaped_response")
    return result


def _response_error(response: httpx.Response) -> ActivationError:
    code = f"activation_http_{response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return ActivationError(code, status_code=response.status_code)
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("code"), str):
            candidate = error["code"].strip()
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", candidate) is not None:
                code = f"activation_remote_{candidate}"
    return ActivationError(code, status_code=response.status_code)


def _safe_job_payload(value: Mapping[str, object]) -> dict[str, object]:
    """Keep counts/status/request refs; never persist cursor values or bodies."""

    allowed = (
        "status",
        "fetched_count",
        "saved_count",
        "duplicate_count",
        "deleted_count",
        "provider_request_id",
        "error_code",
        "reset_required",
    )
    output: dict[str, object] = {}
    for key in allowed:
        item = value.get(key)
        if item is None:
            continue
        if key == "status":
            output[key] = _text(item, "job_status", 40).casefold()
        elif key in {"fetched_count", "saved_count", "duplicate_count", "deleted_count"}:
            if not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= 1_000_000:
                raise ActivationError("activation_job_count_invalid")
            output[key] = item
        elif key == "reset_required":
            if not isinstance(item, bool):
                raise ActivationError("activation_job_reset_invalid")
            output[key] = item
        else:
            output[key] = _text(item, key, 1000)
    for cursor_key in ("cursor_before", "cursor_after"):
        cursor = value.get(cursor_key)
        if cursor is not None:
            cursor_text = _text(cursor, cursor_key, 20_000)
            output[f"{cursor_key}_sha256"] = _hash_text(cursor_text)
    return output


def _safe_audit_scopes(value: object, name: str) -> tuple[str, ...]:
    """Read only the bounded, non-secret scope list from a Host audit event."""

    if not isinstance(value, (list, tuple, set, frozenset)) or not 1 <= len(value) <= 40:
        raise ActivationError(f"activation_{name}_invalid")
    scopes: list[str] = []
    for item in value:
        scope = _text(item, name, 200)
        if scope in scopes:
            raise ActivationError(f"activation_{name}_duplicate")
        scopes.append(scope)
    return tuple(scopes)


def _safe_hash(value: object, name: str) -> str:
    result = _text(value, name, 64).casefold()
    if _SHA256_RE.fullmatch(result) is None:
        raise ActivationError(f"activation_{name}_invalid")
    return result


def _audit_timestamp(value: object, name: str) -> datetime:
    """Parse an API audit timestamp and reject naive/future observations."""

    text = _text(value, name, 80)
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ActivationError(f"activation_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ActivationError(f"activation_{name}_must_be_aware")
    normalized = parsed.astimezone(UTC)
    if normalized > datetime.now(UTC):
        raise ActivationError(f"activation_{name}_in_future")
    return normalized


class MailHubActivationClient:
    """Small, scope-bound HTTP client used only by the activation command."""

    def __init__(
        self,
        *,
        api_url: str,
        tenant_id: str,
        subject_id: str,
        trace_id: str,
        authorization: str | None = None,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_url = _api_url(api_url)
        self.tenant_id = _text(tenant_id, "tenant_id", 200)
        self.subject_id = _text(subject_id, "subject_id", 200)
        self.trace_id = _text(trace_id, "trace_id", 200)
        if authorization is not None:
            self.authorization = _text(authorization, "authorization", 4096)
        else:
            self.authorization = None
        if not 0.1 <= timeout_seconds <= 120:
            raise ActivationError("activation_timeout_invalid")
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> MailHubActivationClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self._client.close()

    def readiness(self) -> dict[str, object]:
        """Require an injected, ready runtime before observing a Provider."""

        payload = self._request("GET", "/health/ready")
        raw = _data(payload)
        if not isinstance(raw, Mapping):
            raise ActivationError("activation_readiness_response_invalid")
        status = _text(raw.get("status"), "readiness_status", 40).casefold()
        if status != "ready":
            raise ActivationError(f"activation_runtime_not_ready:{status}")
        environment = _text(raw.get("environment", "unknown"), "readiness_environment", 80)
        missing = raw.get("missing", ())
        if not isinstance(missing, (list, tuple)) or len(missing) > 100:
            raise ActivationError("activation_readiness_response_invalid")
        missing_names = tuple(_text(item, "readiness_missing", 120) for item in missing)
        if missing_names:
            raise ActivationError("activation_runtime_not_ready:missing_dependencies")
        return {
            "status": status,
            "environment": environment,
            "missing_count": len(missing_names),
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        params: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        if not path.startswith("/") or "?" in path or "#" in path or ".." in path.split("/"):
            raise ActivationError("activation_path_invalid")
        query: dict[str, str] = {}
        if params is not None:
            if len(params) > 10:
                raise ActivationError("activation_query_invalid")
            for key, value in params.items():
                normalized_key = _text(key, "query_key", 64)
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", normalized_key) is None:
                    raise ActivationError("activation_query_invalid")
                query[normalized_key] = _text(value, "query_value", 200)
        headers = {
            "Accept": "application/json",
            "X-MailHub-Tenant": self.tenant_id,
            "X-MailHub-Subject": self.subject_id,
            "X-Trace-Id": self.trace_id,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _text(idempotency_key, "idempotency_key", 300)
        if self.authorization is not None:
            # The value is used only for the request and is never included in
            # evidence, logs or exception messages.
            headers["Authorization"] = self.authorization
        try:
            response = self._client.request(
                method,
                f"{self.api_url}{path}",
                headers=headers,
                json=dict(body) if body is not None else None,
                params=query or None,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ActivationError("activation_api_unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise _response_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ActivationError("activation_response_invalid_json") from exc
        if not isinstance(payload, Mapping) or _contains_secret_shape(payload):
            raise ActivationError("activation_response_invalid")
        return dict(payload)

    def connection(
        self,
        *,
        connection_id: UUID,
        provider: str,
        allowed_statuses: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        payload = self._request("GET", "/v1/mail/connections")
        raw = _data(payload)
        if not isinstance(raw, list) or len(raw) > 1000:
            raise ActivationError("activation_connections_response_invalid")
        expected = str(connection_id)
        for item in raw:
            if not isinstance(item, Mapping):
                raise ActivationError("activation_connection_item_invalid")
            item_id = item.get("connection_id", item.get("id"))
            if item_id != expected:
                continue
            if item.get("provider") != provider:
                raise ActivationError("activation_provider_mismatch")
            status = _text(item.get("status"), "connection_status", 80).casefold()
            if status != "active" and status not in allowed_statuses:
                raise ActivationError(f"activation_connection_not_active:{status}")
            revision = item.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise ActivationError("activation_connection_revision_invalid")
            scopes = item.get("granted_scopes", ())
            if not isinstance(scopes, (list, tuple, set, frozenset)) or len(scopes) > 100:
                raise ActivationError("activation_connection_scopes_invalid")
            email_address = item.get("email_address")
            account_id = item.get("provider_account_id")
            tenant_id = item.get("provider_tenant_id")
            if not isinstance(account_id, str) or not account_id.strip():
                raise ActivationError("activation_provider_account_identity_missing")
            if provider == "microsoft_graph" and (
                not isinstance(tenant_id, str) or not tenant_id.strip()
            ):
                raise ActivationError("activation_provider_tenant_identity_missing")
            identity: dict[str, str] = {
                "account_identity_sha256": _hash_text(account_id.strip()),
            }
            if isinstance(email_address, str) and email_address.strip():
                identity["email_identity_sha256"] = _hash_text(email_address.strip().casefold())
            if isinstance(tenant_id, str) and tenant_id.strip():
                identity["tenant_identity_sha256"] = _hash_text(tenant_id.strip())
            return {
                "connection_id": expected,
                "provider": provider,
                "status": status,
                "revision": revision,
                "scope_count": len(scopes),
                "identity": identity,
            }
        raise ActivationError("activation_connection_not_found")

    def revocation_observation(
        self,
        *,
        connection_id: UUID,
        provider: str,
        environment: str,
        run_id: str,
    ) -> dict[str, object]:
        """Build the negative-access artifact from durable revocation facts."""

        if provider not in _READ_SCOPES:
            raise ActivationError("activation_provider_invalid")
        connection = self.connection(
            connection_id=connection_id,
            provider=provider,
            allowed_statuses=frozenset({"revoked", "reauthorization_required", "deleted"}),
        )
        expected_ref = str(connection_id)
        payload = self._request("GET", "/v1/mail/audit", params={"limit": "200"})
        raw = _data(payload)
        if not isinstance(raw, list) or len(raw) > 500:
            raise ActivationError("activation_audit_response_invalid")
        events: list[Mapping[str, object]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ActivationError("activation_audit_event_invalid")
            metadata = item.get("metadata")
            if isinstance(metadata, Mapping):
                normalized = dict(metadata)
                for key in ("event_type", "target_ref", "occurred_at"):
                    if key in item:
                        normalized.setdefault(key, item[key])
                events.append(normalized)
            else:
                events.append(item)
        scoped_events = [item for item in events if item.get("target_ref") == expected_ref]
        if not any(
            item.get("event_type") == "mail.connection.credential_revoked" for item in scoped_events
        ):
            raise ActivationError("activation_revocation_audit_missing")
        if not any(item.get("event_type") == "mail.connection.revoked" for item in scoped_events):
            raise ActivationError("activation_connection_revoked_audit_missing")
        if not any(
            item.get("event_type") == "mail.subscription.cancelled" for item in scoped_events
        ):
            raise ActivationError("activation_subscription_cancel_audit_missing")

        rejected = next(
            (
                item
                for item in scoped_events
                if item.get("event_type") == "mail.sync.rejected_after_revoke"
            ),
            None,
        )
        if rejected is None:
            raise ActivationError("activation_sync_rejection_missing")
        for key, audit_key in (
            ("provider_requests_after_revoke", "provider_requests_after_revoke"),
            ("credential_broker_requests_after_revoke", "broker_requests_after_revoke"),
        ):
            value = rejected.get(audit_key)
            if isinstance(value, bool) or not isinstance(value, int) or value != 0:
                raise ActivationError(f"activation_{key}_nonzero")

        deleted = next(
            (item for item in scoped_events if item.get("event_type") == "mail.connection.deleted"),
            None,
        )
        if deleted is None or not isinstance(deleted.get("deletion_proof"), Mapping):
            raise ActivationError("activation_delete_proof_missing")
        deletion_proof = deleted["deletion_proof"]
        if not deletion_proof:
            raise ActivationError("activation_delete_proof_missing")
        identity = connection.get("identity")
        if not isinstance(identity, Mapping):
            raise ActivationError("activation_connection_identity_invalid")
        result: dict[str, object] = {
            "schema_version": "mailhub.provider_activation_revocation.v1",
            "network_access": True,
            "status": "succeeded",
            "credentials_observed": False,
            "provider": provider,
            "environment": _slug(environment, "environment"),
            "run_id": _run_id(run_id),
            "account_identity_sha256": _safe_hash(
                identity.get("account_identity_sha256"), "account_identity"
            ),
            "connection_status": connection["status"],
            "subscription_cancelled": True,
            "sync_rejected_after_revoke": True,
            "provider_requests_after_revoke": 0,
            "credential_broker_requests_after_revoke": 0,
            "delete_proof_recorded": True,
        }
        if provider == "microsoft_graph":
            result["tenant_identity_sha256"] = _safe_hash(
                identity.get("tenant_identity_sha256"), "tenant_identity"
            )
        return result

    def oauth_observation(
        self,
        *,
        connection_id: UUID,
        provider: str,
        environment: str,
        run_id: str,
    ) -> dict[str, object]:
        """Build A1 evidence from the Host-owned, redacted audit ledger.

        This is intentionally not a JSON input/fixture collector.  The only
        accepted source is the authenticated MailHub audit endpoint, whose
        OAuth callback records contain scope/PKCE/state digests and provider
        identity hashes.  A real A1 observation must also include a deliberate
        replay rejection plus refresh and revoke audit events.
        """

        if provider not in _READ_SCOPES:
            raise ActivationError("activation_provider_invalid")
        payload = self._request("GET", "/v1/mail/audit", params={"limit": "200"})
        raw = _data(payload)
        if not isinstance(raw, list) or len(raw) > 500:
            raise ActivationError("activation_audit_response_invalid")
        expected_ref = str(connection_id)
        if any(not isinstance(item, Mapping) for item in raw):
            raise ActivationError("activation_audit_event_invalid")
        events = [item for item in raw if isinstance(item, Mapping)]
        completed = next(
            (
                item
                for item in events
                if item.get("event_type") == "mail.oauth.completed"
                and item.get("target_ref") == expected_ref
                and item.get("provider") == provider
            ),
            None,
        )
        if completed is None:
            raise ActivationError("activation_oauth_audit_missing")
        flow_ref = _safe_hash(completed.get("oauth_flow_ref_sha256"), "oauth_flow_ref")
        requested = _safe_audit_scopes(completed.get("requested_scopes"), "oauth_requested_scopes")
        granted = _safe_audit_scopes(completed.get("granted_scopes"), "oauth_granted_scopes")
        requested_folded = {scope.casefold() for scope in requested}
        granted_folded = {scope.casefold() for scope in granted}
        required = {scope.casefold() for scope in _READ_SCOPES[provider]}
        if not required.issubset(requested_folded) or not required.issubset(granted_folded):
            raise ActivationError("activation_oauth_read_scope_missing")
        if not granted_folded.issubset(requested_folded):
            raise ActivationError("activation_oauth_scope_escalation")
        if any(
            scope == marker or scope.startswith(marker)
            for scope in granted_folded
            for marker in _WRITE_SCOPE_MARKERS[provider]
        ):
            raise ActivationError("activation_oauth_write_scope_disallowed")
        for key in (
            "scope_subset_verified",
            "redirect_allowlist_verified",
            "pkce_verified",
        ):
            if completed.get(key) is not True:
                raise ActivationError(f"activation_oauth_{key}_missing")
        account_hash = _safe_hash(completed.get("provider_account_id_sha256"), "account_identity")
        tenant_hash: str | None = None
        if provider == "microsoft_graph":
            tenant_hash = _safe_hash(completed.get("provider_tenant_id_sha256"), "tenant_identity")
        replay_rejected = any(
            item.get("event_type") == "mail.oauth.callback_rejected"
            and item.get("provider") == provider
            and item.get("oauth_flow_ref_sha256") == flow_ref
            and item.get("reason") == "oauth_state_replayed_or_missing"
            for item in events
        )
        if not replay_rejected:
            raise ActivationError("activation_oauth_replay_rejection_missing")
        refresh_verified = any(
            item.get("event_type") == "mail.connection.credentials.refreshed"
            and item.get("target_ref") == expected_ref
            for item in events
        )
        if not refresh_verified:
            raise ActivationError("activation_oauth_refresh_observation_missing")
        revoke_verified = any(
            item.get("event_type") == "mail.connection.credential_revoked"
            and item.get("target_ref") == expected_ref
            for item in events
        )
        if not revoke_verified:
            raise ActivationError("activation_oauth_revoke_observation_missing")
        result: dict[str, object] = {
            "schema_version": "mailhub.provider_activation_oauth.v1",
            "network_access": True,
            "status": "succeeded",
            "credentials_observed": False,
            "provider": provider,
            "environment": _slug(environment, "environment"),
            "run_id": _run_id(run_id),
            "account_identity_sha256": account_hash,
            "scope_subset_verified": True,
            "state_replay_rejected": True,
            "redirect_allowlist_verified": True,
            "pkce_verified": True,
            "refresh_verified": True,
            "revoke_verified": True,
            "requested_scopes": list(requested),
            "granted_scopes": list(granted),
        }
        if tenant_hash is not None:
            result["tenant_identity_sha256"] = tenant_hash
        return result

    def notification_observation(
        self,
        *,
        connection_id: UUID,
        provider: str,
        environment: str,
        run_id: str,
    ) -> dict[str, object]:
        """Build A3/A4 notification evidence from owner-scoped observations.

        The collector reads only the MailHub connection projection, durable
        sync state, receipt store and audit ledger.  It never accepts a
        provider callback body or a hand-written count.  Missing duplicate,
        renewal, ACK or reconciliation observations fail closed.
        """

        if provider not in _READ_SCOPES:
            raise ActivationError("activation_provider_invalid")
        connection = self.connection(connection_id=connection_id, provider=provider)
        expected_ref = str(connection_id)

        state_payload = self._request("GET", f"/v1/mail/connections/{expected_ref}/sync-state")
        raw_states = _data(state_payload)
        if not isinstance(raw_states, list) or len(raw_states) > 100:
            raise ActivationError("activation_sync_state_response_invalid")
        state: Mapping[str, object] | None = None
        for item in raw_states:
            if not isinstance(item, Mapping):
                raise ActivationError("activation_sync_state_item_invalid")
            item_connection = item.get("connection_id")
            if item_connection is not None and str(item_connection) != expected_ref:
                continue
            if item.get("folder_ref") == "INBOX":
                state = item
                break
        if state is None:
            raise ActivationError("activation_sync_state_missing")
        if state.get("subscription_status") != "active":
            raise ActivationError("activation_subscription_not_active")
        subscription_ref = _text(state.get("subscription_ref"), "subscription_ref", 1000)
        watermark_at = _audit_timestamp(state.get("watermark"), "watermark")

        receipt_payload = self._request(
            "GET", "/v1/mail/webhooks/receipts", params={"limit": "200"}
        )
        raw_receipts = _data(receipt_payload)
        if not isinstance(raw_receipts, list) or len(raw_receipts) > 500:
            raise ActivationError("activation_receipts_response_invalid")
        receipt_times: list[datetime] = []
        receipt_count = 0
        for item in raw_receipts:
            if not isinstance(item, Mapping):
                raise ActivationError("activation_receipt_item_invalid")
            if item.get("provider") != provider or item.get("verified") is not True:
                continue
            route = item.get("route_metadata")
            if not isinstance(route, Mapping) or str(route.get("connection_id")) != expected_ref:
                continue
            receipt_count += 1
            receipt_times.append(_audit_timestamp(item.get("received_at"), "receipt_received_at"))
        if receipt_count < 1:
            raise ActivationError("activation_notification_receipt_missing")

        audit_payload = self._request("GET", "/v1/mail/audit", params={"limit": "200"})
        raw_events = _data(audit_payload)
        if not isinstance(raw_events, list) or len(raw_events) > 500:
            raise ActivationError("activation_audit_response_invalid")
        events: list[Mapping[str, object]] = []
        for item in raw_events:
            if not isinstance(item, Mapping):
                raise ActivationError("activation_audit_event_invalid")
            # SQL-backed audit readers may expose the durable JSON as a
            # ``metadata`` child.  Normalize that shape without trusting any
            # caller-provided fields outside the audit record.
            metadata = item.get("metadata")
            if isinstance(metadata, Mapping):
                normalized = dict(metadata)
                for key in ("event_type", "target_ref", "occurred_at"):
                    if key in item:
                        normalized.setdefault(key, item[key])
                events.append(normalized)
            else:
                events.append(item)

        scoped_events = [
            item
            for item in events
            if item.get("provider") in {None, provider}
            and (
                item.get("target_ref") == expected_ref or item.get("connection_id") == expected_ref
            )
        ]
        renewal_events = [
            item for item in scoped_events if item.get("event_type") == "mail.subscription.renewed"
        ]
        if not renewal_events:
            raise ActivationError("activation_subscription_renewal_missing")
        duplicate_events = [
            item
            for item in scoped_events
            if item.get("event_type") == "mail.provider.notification.duplicate"
            and item.get("duplicate") is True
        ]
        if not duplicate_events:
            raise ActivationError("activation_notification_dedupe_missing")
        ack_values: list[int] = []
        ack_times: list[datetime] = []
        for item in scoped_events:
            if item.get("event_type") != "mail.provider.notification.ack":
                continue
            latency = item.get("ack_latency_ms")
            if (
                isinstance(latency, bool)
                or not isinstance(latency, int)
                or not 1 <= latency <= 60_000
            ):
                raise ActivationError("activation_ack_latency_invalid")
            ack_values.append(latency)
            ack_times.append(_audit_timestamp(item.get("occurred_at"), "ack_occurred_at"))
        if not ack_values:
            raise ActivationError("activation_notification_ack_missing")
        ack_values.sort()
        p95_index = max(0, (95 * len(ack_values) + 99) // 100 - 1)
        ack_p95_ms = ack_values[p95_index]
        reconciliation_events = [
            item
            for item in scoped_events
            if item.get("event_type") == "mail.sync.completed" and item.get("mode") == "reconcile"
        ]
        if not reconciliation_events:
            raise ActivationError("activation_reconciliation_missing")

        observation_times = receipt_times + ack_times
        for item in renewal_events + duplicate_events + reconciliation_events:
            observation_times.append(_audit_timestamp(item.get("occurred_at"), "audit_occurred_at"))
        if not observation_times:
            raise ActivationError("activation_notification_observation_missing")
        started_at = min(observation_times)
        finished_at = max(observation_times)
        if finished_at > datetime.now(UTC):
            raise ActivationError("activation_notification_observation_in_future")
        if finished_at - started_at < timedelta(hours=168):
            raise ActivationError("activation_notification_observation_window_too_short")

        identity = connection.get("identity")
        if not isinstance(identity, Mapping):
            raise ActivationError("activation_connection_identity_invalid")
        account_hash = _safe_hash(identity.get("account_identity_sha256"), "account_identity")
        result: dict[str, object] = {
            "schema_version": "mailhub.provider_activation_notifications.v1",
            "network_access": True,
            "status": "succeeded",
            "credentials_observed": False,
            "provider": provider,
            "environment": _slug(environment, "environment"),
            "run_id": _run_id(run_id),
            "account_identity_sha256": account_hash,
            "subscription_active": True,
            "renewal_count": len(renewal_events),
            "receipt_count": receipt_count,
            "duplicate_count": len(duplicate_events),
            "ack_p95_ms": ack_p95_ms,
            "observation_started_at": started_at.isoformat().replace("+00:00", "Z"),
            "observation_finished_at": finished_at.isoformat().replace("+00:00", "Z"),
            "dedupe_verified": True,
            "reconciliation_verified": True,
            "watermark_sha256": _hash_text(watermark_at.isoformat()),
        }
        if provider == "microsoft_graph":
            tenant_hash = _safe_hash(identity.get("tenant_identity_sha256"), "tenant_identity")
            result["tenant_identity_sha256"] = tenant_hash
        # Keep the extracted subscription ref only in process for matching;
        # the evidence artifact contains its watermark digest instead.
        del subscription_ref
        return result

    def enqueue_sync(
        self,
        *,
        connection_id: UUID,
        mode: str,
        limit: int,
        idempotency_key: str,
        folder_ref: str = "INBOX",
        label_refs: tuple[str, ...] = (),
        received_after: str | None = None,
        received_before: str | None = None,
    ) -> dict[str, object]:
        if mode not in {"incremental", "backfill", "reconcile"}:
            raise ActivationError("activation_sync_mode_invalid")
        if not 1 <= limit <= 500:
            raise ActivationError("activation_sync_limit_invalid")
        filter_body, _summary = _sync_filter(
            mode=mode,
            folder_ref=folder_ref,
            label_refs=label_refs,
            received_after=received_after,
            received_before=received_before,
        )
        payload = self._request(
            "POST",
            f"/v1/mail/connections/{connection_id}/sync-jobs",
            body={"mode": mode, "limit": limit, "run_inline": False, **filter_body},
            idempotency_key=idempotency_key,
        )
        raw = _data(payload)
        if not isinstance(raw, Mapping):
            raise ActivationError("activation_sync_enqueue_invalid")
        job_ref = _text(raw.get("job_ref"), "job_ref", 200)
        status = _text(raw.get("status"), "job_status", 40).casefold()
        return {"job_ref": job_ref, "status": status}

    def job(self, *, job_ref: str) -> dict[str, object]:
        safe_ref = _text(job_ref, "job_ref", 200)
        try:
            UUID(safe_ref)
        except ValueError as exc:
            raise ActivationError("activation_job_ref_invalid") from exc
        payload = self._request("GET", f"/v1/mail/sync-jobs/{safe_ref}")
        raw = _data(payload)
        if not isinstance(raw, Mapping):
            raise ActivationError("activation_job_response_invalid")
        return _safe_job_payload(raw)


def _poll_job(
    client: MailHubActivationClient,
    *,
    job_ref: str,
    timeout_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    if not 1 <= timeout_seconds <= 3600:
        raise ActivationError("activation_poll_timeout_invalid")
    if not 0.1 <= interval_seconds <= 30:
        raise ActivationError("activation_poll_interval_invalid")
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] = {}
    while True:
        latest = client.job(job_ref=job_ref)
        status = latest.get("status")
        if isinstance(status, str) and status in _TERMINAL_STATUSES:
            return latest
        if time.monotonic() >= deadline:
            raise ActivationError("activation_sync_poll_timeout")
        sleep(interval_seconds)


def _evidence_payload(
    *,
    provider: str,
    environment: str,
    run_id: str,
    started_at: str,
    finished_at: str,
    tenant_id: str,
    subject_id: str,
    connection: Mapping[str, object] | None,
    sync: Mapping[str, object] | None,
    replay: Mapping[str, object] | None,
    sync_filter: Mapping[str, object] | None,
    readiness: Mapping[str, object] | None,
    status: str,
    network_access: bool,
    error_code: str | None = None,
) -> dict[str, object]:
    evidence_kind = (
        "real_api_observation"
        if network_access and connection is not None
        else "activation_gate_failure"
    )
    payload: dict[str, object] = {
        "schema_version": "mailhub.provider_activation_sync.v1",
        "evidence_kind": evidence_kind,
        "network_access": network_access,
        "real_provider_evidence": evidence_kind == "real_api_observation",
        "provider": provider,
        "environment": environment,
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "tenant_sha256": _hash_text(tenant_id),
        "subject_sha256": _hash_text(subject_id),
        "connection": dict(connection) if connection is not None else None,
        "sync": dict(sync) if sync is not None else None,
        "replay": dict(replay) if replay is not None else None,
        "sync_filter": dict(sync_filter) if sync_filter is not None else None,
        "readiness": dict(readiness) if readiness is not None else None,
        "credentials_observed": False,
    }
    if error_code is not None:
        payload["error_code"] = _text(error_code, "error_code", 200)
    return payload


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_test_output(
    path: Path,
    *,
    provider: str,
    environment: str,
    run_id: str,
    status: str,
    network_access: bool,
    error_code: str | None = None,
) -> None:
    lines = [
        "schema_version=mailhub.provider_activation_test_output.v1",
        f"provider={_text(provider, 'provider', 40)}",
        f"environment={_text(environment, 'environment', 100)}",
        f"run_id={_text(run_id, 'run_id', 100)}",
        f"network_access={'true' if network_access else 'false'}",
        f"status={status}",
        "credentials_observed=false",
        f"exit_code={'0' if status == 'succeeded' else '2'}",
    ]
    if error_code is not None:
        lines.append(f"error_code={_text(error_code, 'error_code', 200)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _confirm_network(args: argparse.Namespace) -> None:
    if not args.confirm_real_provider:
        raise ActivationError("activation_confirmation_required")
    if os.getenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", "").casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise ActivationError("activation_network_gate_required")


def _offline_preflight(provider: str) -> dict[str, object]:
    """Load the repository's offline A0 gate without contacting a provider."""

    script_path = Path(__file__).with_name("provider_preflight.py")
    spec = importlib.util.spec_from_file_location("mailhub_provider_preflight", script_path)
    if spec is None or spec.loader is None:
        raise ActivationError("activation_preflight_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        report = module.build_report((provider,))
    except (OSError, AttributeError, ImportError, ValueError) as exc:
        raise ActivationError("activation_preflight_invalid") from exc
    if (
        not isinstance(report, dict)
        or report.get("network_access") is not False
        or report.get("real_provider_evidence") is not False
        or report.get("config_ready") is not True
    ):
        raise ActivationError("activation_preflight_not_ready")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=_PROVIDERS, required=True)
    parser.add_argument("--environment", default=os.getenv("MAILHUB_ACTIVATION_ENV", "test"))
    parser.add_argument("--api-url", default=os.getenv("MAILHUB_ACTIVATION_API_URL"))
    parser.add_argument("--tenant-id", default=os.getenv("MAILHUB_ACTIVATION_TENANT_ID"))
    parser.add_argument("--subject-id", default=os.getenv("MAILHUB_ACTIVATION_SUBJECT_ID"))
    parser.add_argument("--connection-id", default=os.getenv("MAILHUB_ACTIVATION_CONNECTION_ID"))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--mode", choices=("incremental", "backfill", "reconcile"), default="incremental"
    )
    parser.add_argument("--folder-ref", default=os.getenv("MAILHUB_ACTIVATION_FOLDER_REF", "INBOX"))
    parser.add_argument(
        "--label-ref",
        action="append",
        dest="label_refs",
        default=None,
        help="repeat for each bounded label/category reference",
    )
    parser.add_argument("--received-after", default=os.getenv("MAILHUB_ACTIVATION_RECEIVED_AFTER"))
    parser.add_argument(
        "--received-before", default=os.getenv("MAILHUB_ACTIVATION_RECEIVED_BEFORE")
    )
    parser.add_argument("--poll-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--evidence-root",
        default=os.getenv("MAILHUB_ACTIVATION_EVIDENCE_ROOT", "provider-activation"),
    )
    parser.add_argument(
        "--confirm-real-provider",
        action="store_true",
        help="required together with MAILHUB_ACTIVATION_ALLOW_NETWORK=true",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[..., MailHubActivationClient] = MailHubActivationClient,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_id = _run_id(args.run_id or uuid4().hex)
        provider = _text(args.provider, "provider", 40)
        environment = _slug(args.environment, "environment")
        evidence_root = Path(_text(args.evidence_root, "evidence_root", 1000)).resolve()
    except ActivationError as exc:
        print(f"activation blocked: {exc.code}", file=sys.stderr)
        return 2
    evidence_dir = evidence_root / provider / environment / run_id
    evidence_dir_created = False
    started_at = clock().astimezone(UTC).isoformat()
    tenant_id = args.tenant_id
    subject_id = args.subject_id
    connection_value = args.connection_id
    connection: dict[str, object] | None = None
    sync: dict[str, object] | None = None
    replay: dict[str, object] | None = None
    sync_filter_summary: dict[str, object] | None = None
    readiness: dict[str, object] | None = None
    preflight: dict[str, object] | None = None
    network_access = False
    try:
        _confirm_network(args)
        preflight = _offline_preflight(provider)
        api_url = _api_url(args.api_url)
        tenant_id = _text(tenant_id, "tenant_id", 200)
        subject_id = _text(subject_id, "subject_id", 200)
        raw_labels = args.label_refs
        if raw_labels is None:
            env_labels = os.getenv("MAILHUB_ACTIVATION_LABEL_REFS", "")
            raw_labels = [item.strip() for item in env_labels.split(",") if item.strip()]
        filter_body, sync_filter_summary = _sync_filter(
            mode=args.mode,
            folder_ref=args.folder_ref,
            label_refs=raw_labels,
            received_after=args.received_after,
            received_before=args.received_before,
        )
        if not isinstance(connection_value, str):
            raise ActivationError("activation_connection_id_missing")
        connection_id = UUID(_text(connection_value, "connection_id", 80))
        trace_id = f"mailhub-activation:{run_id}"
        evidence_dir.mkdir(parents=True, exist_ok=False)
        evidence_dir_created = True
        with client_factory(
            api_url=api_url,
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            authorization=os.getenv("MAILHUB_ACTIVATION_AUTHORIZATION"),
        ) as client:
            network_access = True
            readiness = client.readiness()
            connection = client.connection(connection_id=connection_id, provider=provider)
            idempotency_key = f"mailhub-activation:{run_id}:sync"
            initial = client.enqueue_sync(
                connection_id=connection_id,
                mode=args.mode,
                limit=args.limit,
                idempotency_key=idempotency_key,
                folder_ref=str(filter_body["folder_ref"]),
                label_refs=tuple(str(label) for label in filter_body["label_refs"]),
                received_after=(
                    str(filter_body["received_after"]) if "received_after" in filter_body else None
                ),
                received_before=(
                    str(filter_body["received_before"])
                    if "received_before" in filter_body
                    else None
                ),
            )
            completed = _poll_job(
                client,
                job_ref=str(initial["job_ref"]),
                timeout_seconds=args.poll_timeout_seconds,
                interval_seconds=args.poll_interval_seconds,
            )
            replayed = client.enqueue_sync(
                connection_id=connection_id,
                mode=args.mode,
                limit=args.limit,
                idempotency_key=idempotency_key,
                folder_ref=str(filter_body["folder_ref"]),
                label_refs=tuple(str(label) for label in filter_body["label_refs"]),
                received_after=(
                    str(filter_body["received_after"]) if "received_after" in filter_body else None
                ),
                received_before=(
                    str(filter_body["received_before"])
                    if "received_before" in filter_body
                    else None
                ),
            )
            replay = {
                "same_job_ref": replayed.get("job_ref") == initial.get("job_ref"),
                "initial_status": initial.get("status"),
                "replayed_status": replayed.get("status"),
            }
            if replay["same_job_ref"] is not True:
                raise ActivationError("activation_idempotency_replay_mismatch")
            sync = {
                "job_ref": initial.get("job_ref"),
                **_safe_job_payload(completed),
            }
            if completed.get("status") != "succeeded":
                raise ActivationError(f"activation_sync_not_succeeded:{completed.get('status')}")
        finished_at = clock().astimezone(UTC).isoformat()
        _write_json(
            evidence_dir / "sync-reconciliation.json",
            _evidence_payload(
                provider=provider,
                environment=environment,
                run_id=run_id,
                started_at=started_at,
                finished_at=finished_at,
                tenant_id=tenant_id,
                subject_id=subject_id,
                connection=connection,
                sync=sync,
                replay=replay,
                sync_filter=sync_filter_summary,
                readiness=readiness,
                status="succeeded",
                network_access=True,
            ),
        )
        _write_json(evidence_dir / "preflight.json", preflight)
        _write_test_output(
            evidence_dir / "test-output.txt",
            provider=provider,
            environment=environment,
            run_id=run_id,
            status="succeeded",
            network_access=True,
        )
        print(f"activation evidence: {evidence_dir}")
        return 0
    except (ActivationError, ValueError, OSError) as exc:
        error_code = exc.code if isinstance(exc, ActivationError) else "activation_local_error"
        finished_at = clock().astimezone(UTC).isoformat()
        try:
            if not evidence_dir_created:
                evidence_dir.mkdir(parents=True, exist_ok=False)
                evidence_dir_created = True
            if evidence_dir_created:
                _write_json(
                    evidence_dir / "sync-reconciliation.json",
                    _evidence_payload(
                        provider=provider,
                        environment=environment,
                        run_id=run_id,
                        started_at=started_at,
                        finished_at=finished_at,
                        tenant_id=tenant_id if isinstance(tenant_id, str) else "unknown",
                        subject_id=subject_id if isinstance(subject_id, str) else "unknown",
                        connection=connection,
                        sync=sync,
                        replay=replay,
                        sync_filter=sync_filter_summary,
                        readiness=readiness,
                        status="failed",
                        network_access=network_access,
                        error_code=error_code,
                    ),
                )
                if preflight is not None:
                    _write_json(evidence_dir / "preflight.json", preflight)
                _write_test_output(
                    evidence_dir / "test-output.txt",
                    provider=provider,
                    environment=environment,
                    run_id=run_id,
                    status="failed",
                    network_access=network_access,
                    error_code=error_code,
                )
        except OSError:
            pass
        print(f"activation blocked: {error_code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
