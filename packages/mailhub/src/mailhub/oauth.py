"""Provider-neutral OAuth authorization state and PKCE primitives.

The host owns client registration, consent text and token exchange.  MailHub
only issues a short-lived, one-time state and a verifier that the host sends to
the provider.  Tenant/subject identity stays server-side; the browser state is
an opaque signed handle and never contains a token or mailbox content.  When a
user reauthorizes an existing connection, the signed state also binds the
connection id and expected revision so the callback cannot create a second
mailbox or overwrite a newer credential rotation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID


class OAuthProvider(StrEnum):
    GMAIL = "gmail"
    MICROSOFT_GRAPH = "microsoft_graph"
    IMAP_SMTP = "imap_smtp"


@dataclass(frozen=True, slots=True)
class OAuthStateRecord:
    state_id: str
    tenant_id: str
    subject_id: str
    provider: OAuthProvider
    redirect_uri: str
    code_verifier: str
    nonce: str
    expires_at: datetime
    connection_id: UUID | None = None
    expected_revision: int | None = None
    # The state store binds the callback to the exact non-secret scope set
    # requested by the server.  This prevents a host/provider exchange from
    # silently widening a read-only grant between authorize and callback.
    requested_scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationRequest:
    provider: OAuthProvider
    authorization_url: str
    state: str
    code_verifier: str
    code_challenge: str
    expires_at: datetime
    connection_id: UUID | None = None
    expected_revision: int | None = None


@dataclass(frozen=True, slots=True)
class OAuthCallbackContext:
    tenant_id: str
    subject_id: str
    provider: OAuthProvider
    redirect_uri: str
    code_verifier: str
    nonce: str
    connection_id: UUID | None = None
    expected_revision: int | None = None
    requested_scopes: tuple[str, ...] = ()


class OAuthStateStore(Protocol):
    async def save(self, record: OAuthStateRecord) -> None: ...

    async def consume(self, state_id: str) -> OAuthStateRecord | None: ...


class OAuthCallbackPort(Protocol):
    """Host-owned code exchange and credential broker handoff.

    Implementations exchange the one-time provider code outside MailHub and
    return only non-secret connection metadata plus a Secret Manager ref.
    Access/refresh tokens must never be returned to the API caller or stored in
    the MailHub database.
    """

    async def exchange(
        self, *, context: OAuthCallbackContext, code: str
    ) -> Mapping[str, object]: ...


class InMemoryOAuthStateStore(OAuthStateStore):
    """Test-only state store; production uses a shared encrypted/TTL store."""

    def __init__(self) -> None:
        self._records: dict[str, OAuthStateRecord] = {}

    async def save(self, record: OAuthStateRecord) -> None:
        self._records[record.state_id] = record

    async def consume(self, state_id: str) -> OAuthStateRecord | None:
        return self._records.pop(state_id, None)


class OAuthAuthorizationService:
    def __init__(
        self,
        *,
        signing_secret: bytes,
        state_store: OAuthStateStore,
        allowed_redirect_uris: Mapping[OAuthProvider, tuple[str, ...]],
        state_ttl: timedelta = timedelta(minutes=10),
        allowed_authorization_endpoints: Mapping[OAuthProvider, tuple[str, ...]] | None = None,
        allowed_client_ids: Mapping[OAuthProvider, tuple[str, ...]] | None = None,
        allowed_scopes: Mapping[OAuthProvider, tuple[str, ...]] | None = None,
    ) -> None:
        if len(signing_secret) < 32:
            raise ValueError("oauth_signing_secret_too_short")
        if not 30 <= state_ttl.total_seconds() <= 900:
            raise ValueError("oauth_state_ttl_invalid")
        self._signing_secret = bytes(signing_secret)
        self._state_store = state_store
        self._allowed_redirect_uris = {
            provider: tuple(_validate_redirect(uri) for uri in uris)
            for provider, uris in allowed_redirect_uris.items()
        }
        self._allowed_authorization_endpoints = {
            provider: tuple(_validate_endpoint(endpoint) for endpoint in endpoints)
            for provider, endpoints in (allowed_authorization_endpoints or {}).items()
        }
        self._allowed_client_ids = {
            provider: tuple(_validate_client_id(client_id) for client_id in client_ids)
            for provider, client_ids in (allowed_client_ids or {}).items()
        }
        self._allowed_scopes = {
            provider: tuple(_validate_scope(scope) for scope in scopes)
            for provider, scopes in (allowed_scopes or {}).items()
        }
        self._state_ttl = state_ttl

    async def begin(
        self,
        *,
        provider: OAuthProvider,
        tenant_id: str,
        subject_id: str,
        authorization_endpoint: str,
        client_id: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
        extra_parameters: Mapping[str, str] | None = None,
        connection_id: UUID | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> OAuthAuthorizationRequest:
        _identity(tenant_id, "tenant_id")
        _identity(subject_id, "subject_id")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("oauth_expected_revision_invalid")
        if expected_revision is not None and connection_id is None:
            raise ValueError("oauth_connection_id_required")
        if not client_id.strip() or len(client_id) > 512:
            raise ValueError("oauth_client_id_invalid")
        endpoint = _validate_endpoint(authorization_endpoint)
        allowed_endpoints = self._allowed_authorization_endpoints.get(provider)
        if allowed_endpoints is not None and endpoint not in allowed_endpoints:
            raise ValueError("oauth_authorization_endpoint_not_allowlisted")
        allowed_clients = self._allowed_client_ids.get(provider)
        if allowed_clients is not None and client_id not in allowed_clients:
            raise ValueError("oauth_client_id_not_allowlisted")
        redirect = _validate_redirect(redirect_uri)
        if redirect not in self._allowed_redirect_uris.get(provider, ()):
            raise ValueError("oauth_redirect_uri_not_allowlisted")
        if not scopes or any(
            not isinstance(scope, str) or not scope.strip() or len(scope) > 200 for scope in scopes
        ):
            raise ValueError("oauth_scopes_invalid")
        if any(
            not isinstance(scope, str) or any(ord(char) < 33 or ord(char) == 127 for char in scope)
            for scope in scopes
        ):
            raise ValueError("oauth_scopes_invalid")
        requested_scopes = tuple(dict.fromkeys(scope.strip() for scope in scopes))
        allowed_scope_values = self._allowed_scopes.get(provider)
        if allowed_scope_values is not None and not set(requested_scopes).issubset(
            allowed_scope_values
        ):
            raise ValueError("oauth_scope_not_allowlisted")
        if provider in _READ_ONLY_REQUIRED_SCOPES:
            # Real-provider authorization must request the complete bounded
            # read-only grant.  This keeps in-memory/test state stores aligned
            # with the production host store instead of relying on the later
            # activation path to reject an under-scoped callback.
            validate_granted_scopes(provider, requested_scopes, requested_scopes)
        current = (now or datetime.now(UTC)).astimezone(UTC)
        expires_at = current + self._state_ttl
        state_id = _token(24)
        code_verifier = _token(48)
        nonce = _token(24)
        code_challenge = _pkce_challenge(code_verifier)
        record = OAuthStateRecord(
            state_id=state_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            provider=provider,
            redirect_uri=redirect,
            code_verifier=code_verifier,
            nonce=nonce,
            expires_at=expires_at,
            connection_id=connection_id,
            expected_revision=expected_revision,
            requested_scopes=requested_scopes,
        )
        state_payload: dict[str, object] = {
            "sid": state_id,
            "provider": provider.value,
            "exp": int(expires_at.timestamp()),
        }
        if connection_id is not None:
            state_payload["cid"] = str(connection_id)
        if expected_revision is not None:
            state_payload["rev"] = expected_revision
        await self._state_store.save(record)
        state = _sign_state(
            state_payload,
            self._signing_secret,
        )
        parameters: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": " ".join(dict.fromkeys(scopes)),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
        if extra_parameters:
            for key, value in extra_parameters.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(value, str)
                    or key in parameters
                    or not key
                    or len(key) > 100
                    or len(value) > 1000
                ):
                    raise ValueError("oauth_extra_parameter_invalid")
                parameters[key] = value
        authorization_url = _append_query(endpoint, parameters)
        return OAuthAuthorizationRequest(
            provider=provider,
            authorization_url=authorization_url,
            state=state,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
            expires_at=expires_at,
            connection_id=connection_id,
            expected_revision=expected_revision,
        )

    def state_observation_ref(self, state: str) -> str:
        """Return a non-secret digest for an OAuth flow observation.

        Hosts may need to correlate a successful callback with a deliberately
        replayed callback when producing an activation evidence bundle.  The
        signed state is never returned or persisted by this helper; only the
        hash of its opaque server-side state id crosses the audit boundary.
        A malformed/tampered state still fails closed through the normal state
        verifier.
        """

        payload = _verify_state(state, self._signing_secret)
        state_id = payload.get("sid")
        if not isinstance(state_id, str) or not state_id:
            raise ValueError("oauth_state_payload_invalid")
        return hashlib.sha256(state_id.encode("utf-8")).hexdigest()

    async def consume(
        self,
        *,
        provider: OAuthProvider,
        state: str,
        redirect_uri: str,
        now: datetime | None = None,
    ) -> OAuthCallbackContext:
        payload = _verify_state(state, self._signing_secret)
        if payload.get("provider") != provider.value:
            raise ValueError("oauth_state_provider_mismatch")
        state_id = payload.get("sid")
        expires = payload.get("exp")
        if not isinstance(state_id, str) or not isinstance(expires, int):
            raise ValueError("oauth_state_payload_invalid")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if expires <= int(current.timestamp()):
            raise ValueError("oauth_state_expired")
        record = await self._state_store.consume(state_id)
        if record is None:
            raise ValueError("oauth_state_replayed_or_missing")
        payload_connection_id: UUID | None = None
        payload_connection = payload.get("cid")
        if payload_connection is not None:
            if not isinstance(payload_connection, str):
                raise ValueError("oauth_state_payload_invalid")
            try:
                payload_connection_id = UUID(payload_connection)
            except ValueError as exc:
                raise ValueError("oauth_state_payload_invalid") from exc
        payload_revision = payload.get("rev")
        if payload_revision is not None and (
            isinstance(payload_revision, bool)
            or not isinstance(payload_revision, int)
            or payload_revision < 1
        ):
            raise ValueError("oauth_state_payload_invalid")
        if (
            record.connection_id != payload_connection_id
            or record.expected_revision != payload_revision
        ):
            raise ValueError("oauth_state_binding_mismatch")
        redirect = _validate_redirect(redirect_uri)
        if record.provider is not provider or record.redirect_uri != redirect:
            raise ValueError("oauth_callback_binding_mismatch")
        if record.expires_at <= current:
            raise ValueError("oauth_state_expired")
        return OAuthCallbackContext(
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            provider=record.provider,
            redirect_uri=record.redirect_uri,
            code_verifier=record.code_verifier,
            nonce=record.nonce,
            connection_id=record.connection_id,
            expected_revision=record.expected_revision,
            requested_scopes=record.requested_scopes,
        )


_READ_ONLY_REQUIRED_SCOPES: dict[OAuthProvider, frozenset[str]] = {
    OAuthProvider.GMAIL: frozenset({"https://www.googleapis.com/auth/gmail.readonly"}),
    OAuthProvider.MICROSOFT_GRAPH: frozenset({"mail.read", "offline_access"}),
}
_READ_ONLY_WRITE_SCOPE_MARKERS: dict[OAuthProvider, tuple[str, ...]] = {
    OAuthProvider.GMAIL: (
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.settings.",
    ),
    OAuthProvider.MICROSOFT_GRAPH: (
        "mail.send",
        "mail.readwrite",
        "mail.manage",
        "mail.fullaccessasuser",
    ),
}


def validate_granted_scopes(
    provider: OAuthProvider,
    raw_scopes: object,
    requested_scopes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Validate the host's returned grant against the authorize request.

    ``requested_scopes`` is empty only for legacy/direct adapter callers.  The
    real OAuth callback always carries the state-bound set; when present, a
    returned scope must be a member of that exact allowlist and include the
    provider's required read-only scopes.  Write-capable scopes are rejected
    in both modes.
    """

    if not isinstance(raw_scopes, (list, tuple, set, frozenset)) or len(raw_scopes) > 40:
        raise ValueError("oauth_exchange_scopes_invalid")
    scopes: list[str] = []
    for item in raw_scopes:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 200
            or any(ord(char) < 33 or ord(char) == 127 for char in item)
        ):
            raise ValueError("oauth_exchange_scopes_invalid")
        normalized = item.strip()
        if normalized not in scopes:
            scopes.append(normalized)
    normalized_scopes = frozenset(scope.casefold() for scope in scopes)
    if any(
        scope == marker or scope.startswith(marker)
        for scope in normalized_scopes
        for marker in _READ_ONLY_WRITE_SCOPE_MARKERS.get(provider, ())
    ):
        raise ValueError("oauth_exchange_write_scope_disallowed")
    if requested_scopes:
        requested = frozenset(scope.casefold() for scope in requested_scopes)
        if not normalized_scopes.issubset(requested):
            raise ValueError("oauth_exchange_scope_escalation")
        required = _READ_ONLY_REQUIRED_SCOPES.get(provider, frozenset())
        if not required.issubset(normalized_scopes):
            raise ValueError("oauth_exchange_required_scope_missing")
    return tuple(scopes)


def _token(byte_count: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).decode("ascii").rstrip("=")


def _pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )


def _sign_state(payload: Mapping[str, object], secret: bytes) -> str:
    encoded = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    return encoded + "." + _b64url(signature)


def _verify_state(value: str, secret: bytes) -> dict[str, object]:
    if not isinstance(value, str) or not 20 <= len(value) <= 4000:
        raise ValueError("oauth_state_format_invalid")
    parts = value.split(".")
    if len(parts) != 2 or any(not part for part in parts):
        raise ValueError("oauth_state_format_invalid")
    encoded, signature = parts
    expected = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        )
    except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("oauth_state_encoding_invalid") from exc
    if not hmac.compare_digest(expected, actual) or not isinstance(payload, dict):
        raise ValueError("oauth_state_signature_invalid")
    return payload


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _validate_redirect(value: str) -> str:
    if not value or len(value) > 2000 or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError("oauth_redirect_uri_invalid")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("oauth_redirect_uri_invalid") from exc
    if (
        parsed.scheme not in {"https", "http"}
        or not hostname
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and hostname not in {"localhost", "127.0.0.1", "::1"})
        or not parsed.path
    ):
        raise ValueError("oauth_redirect_uri_invalid")
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("oauth_redirect_uri_requires_https")
    if parsed.query:
        raise ValueError("oauth_redirect_uri_query_forbidden")
    if parsed.fragment:
        raise ValueError("oauth_redirect_uri_fragment_forbidden")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _validate_endpoint(value: str) -> str:
    endpoint = _validate_redirect(value)
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("oauth_authorization_endpoint_requires_https")
    return endpoint


def _validate_client_id(value: str) -> str:
    if not value.strip() or len(value) > 512 or any(ord(char) < 33 for char in value):
        raise ValueError("oauth_client_id_invalid")
    return value


def _validate_scope(value: str) -> str:
    if not value.strip() or len(value) > 200 or any(ord(char) < 33 for char in value):
        raise ValueError("oauth_scope_invalid")
    return value


def _append_query(endpoint: str, parameters: Mapping[str, str]) -> str:
    parsed = urlsplit(endpoint)
    query = parsed.query + ("&" if parsed.query else "") + urlencode(parameters)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _identity(value: str, field_name: str) -> None:
    if not value.strip() or len(value) > 200:
        raise ValueError(f"oauth_{field_name}_invalid")
