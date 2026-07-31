"""Host-owned OAuth credential broker for CAPlatform MailHub.

The broker is deliberately outside the MailHub domain service.  It owns the
one-time OAuth state binding and encrypted provider tokens, while MailHub sees
only an opaque credential reference or a short-lived access token during a
provider call.  The included SQLite store is the local/Beta adapter; production
deployments must place the same contract behind an approved KMS/Secret backend.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, model_validator

ProviderName = Literal["gmail", "microsoft_graph"]

_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_GRAPH_READ_SCOPE = "mail.read"
_GRAPH_OFFLINE_SCOPE = "offline_access"
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
_MAX_PROVIDER_RESPONSE_BYTES = 65_536
_MAX_TOKEN_CHARS = 32_768


class MailHubCredentialBrokerError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OAuthStateWrite(_StrictModel):
    state_id: str = Field(min_length=16, max_length=512)
    tenant_id: str = Field(min_length=1, max_length=200)
    subject_id: str = Field(min_length=1, max_length=200)
    provider: ProviderName
    redirect_uri: str = Field(min_length=1, max_length=2000)
    code_verifier: str = Field(min_length=43, max_length=512)
    nonce: str = Field(min_length=16, max_length=512)
    expires_at: datetime
    connection_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    requested_scopes: list[str] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_binding(self) -> OAuthStateWrite:
        if self.expected_revision is not None and self.connection_id is None:
            raise ValueError("oauth_state_connection_id_required")
        _validate_redirect_uri(self.redirect_uri)
        _validate_scopes(self.provider, self.requested_scopes)
        _bounded_text(self.state_id, "oauth_state_id", maximum=512)
        _bounded_text(self.tenant_id, "oauth_tenant_id", maximum=200)
        _bounded_text(self.subject_id, "oauth_subject_id", maximum=200)
        _bounded_text(self.code_verifier, "oauth_code_verifier", maximum=512)
        _bounded_text(self.nonce, "oauth_nonce", maximum=512)
        if self.expires_at.tzinfo is None:
            raise ValueError("oauth_state_expiry_timezone_missing")
        return self


class OAuthStateConsume(_StrictModel):
    state_id: str = Field(min_length=16, max_length=512)


class OAuthExchange(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    subject_id: str = Field(min_length=1, max_length=200)
    provider: ProviderName
    redirect_uri: str = Field(min_length=1, max_length=2000)
    code: str = Field(min_length=1, max_length=8192)
    code_verifier: str = Field(min_length=43, max_length=512)
    nonce: str = Field(min_length=16, max_length=512)
    connection_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    requested_scopes: list[str] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_exchange(self) -> OAuthExchange:
        if self.expected_revision is not None and self.connection_id is None:
            raise ValueError("oauth_exchange_connection_id_required")
        _validate_redirect_uri(self.redirect_uri)
        _validate_scopes(self.provider, self.requested_scopes)
        for value, field, maximum in (
            (self.tenant_id, "oauth_tenant_id", 200),
            (self.subject_id, "oauth_subject_id", 200),
            (self.code, "oauth_code", 8192),
            (self.code_verifier, "oauth_code_verifier", 512),
            (self.nonce, "oauth_nonce", 512),
        ):
            _bounded_text(value, field, maximum=maximum)
        return self


class CredentialResolve(_StrictModel):
    credential_ref: str = Field(min_length=1, max_length=512)
    tenant_id: str = Field(min_length=1, max_length=200)
    subject_id: str = Field(min_length=1, max_length=200)


class CredentialRefresh(CredentialResolve):
    reason: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True, slots=True)
class ProviderOAuthRegistration:
    provider: ProviderName
    enabled: bool
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...]
    authority_tenant: str | None = None

    @property
    def token_endpoint(self) -> str:
        if self.provider == "gmail":
            return "https://oauth2.googleapis.com/token"
        tenant = self.authority_tenant or ""
        if not _is_tenant_uuid(tenant):
            raise MailHubCredentialBrokerError(
                "mailhub_graph_tenant_specific_required", status_code=503
            )
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    @property
    def identity_endpoint(self) -> str:
        if self.provider == "gmail":
            return "https://gmail.googleapis.com/gmail/v1/users/me/profile"
        return "https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName"

    def require_ready(self) -> None:
        if not self.enabled:
            raise MailHubCredentialBrokerError("mailhub_provider_disabled", status_code=503)
        if not self.client_id or not self.client_secret:
            raise MailHubCredentialBrokerError(
                "mailhub_oauth_client_credentials_missing", status_code=503
            )
        if not hmac.compare_digest(self.redirect_uri, _validate_redirect_uri(self.redirect_uri)):
            raise MailHubCredentialBrokerError("mailhub_oauth_redirect_invalid", status_code=503)
        _validate_scopes(self.provider, list(self.scopes))
        _ = self.token_endpoint


@dataclass(frozen=True, slots=True)
class _CredentialRecord:
    credential_ref: str
    tenant_id: str
    subject_id: str
    provider: ProviderName
    email_address: str
    provider_account_id: str
    provider_tenant_id: str | None
    granted_scopes: tuple[str, ...]
    version: int
    status: str
    expires_at: datetime
    secret: dict[str, str]


class EncryptedSQLiteMailCredentialBroker:
    """Encrypted local/Beta implementation of the MailHub host contract."""

    def __init__(
        self,
        *,
        database_path: str,
        encryption_secret: str,
        registrations: dict[ProviderName, ProviderOAuthRegistration],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if len(encryption_secret) < 32:
            raise ValueError("mailhub_credential_encryption_secret_too_short")
        digest = hashlib.sha256(encryption_secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))
        self.database_path = database_path
        self._registrations = dict(registrations)
        self._transport = transport
        self._db_lock = asyncio.Lock()
        self._rotation_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._db_lock:
            await asyncio.to_thread(self._initialize_sync)

    async def save_state(self, state: OAuthStateWrite) -> None:
        now = datetime.now(UTC)
        expires_at = state.expires_at.astimezone(UTC)
        if expires_at <= now or expires_at > now + timedelta(minutes=15):
            raise MailHubCredentialBrokerError("oauth_state_expiry_invalid", status_code=422)
        payload = state.model_dump(mode="json")
        encoded = _canonical_json(payload)
        state_digest = _digest(state.state_id)
        record_digest = hashlib.sha256(encoded).hexdigest()
        encrypted = self._fernet.encrypt(encoded)
        async with self._db_lock:
            await asyncio.to_thread(
                self._save_state_sync,
                state_digest,
                record_digest,
                state,
                encrypted,
                expires_at,
                now,
            )

    async def consume_state(self, state_id: str) -> dict[str, object] | None:
        _bounded_text(state_id, "oauth_state_id", minimum=16, maximum=512)
        async with self._db_lock:
            encrypted = await asyncio.to_thread(
                self._consume_state_sync, _digest(state_id), datetime.now(UTC)
            )
        if encrypted is None:
            return None
        try:
            payload = json.loads(self._fernet.decrypt(encrypted))
        except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MailHubCredentialBrokerError(
                "oauth_state_decryption_failed", status_code=500
            ) from exc
        if not isinstance(payload, dict) or payload.get("state_id") != state_id:
            raise MailHubCredentialBrokerError("oauth_state_binding_mismatch", status_code=500)
        # Re-validate the durable shape before returning it to MailHub.
        return OAuthStateWrite.model_validate(payload).model_dump(mode="json")

    async def exchange(self, request: OAuthExchange) -> dict[str, object]:
        registration = self._registration(request.provider)
        registration.require_ready()
        if request.redirect_uri != registration.redirect_uri:
            raise MailHubCredentialBrokerError("oauth_exchange_redirect_mismatch", status_code=403)
        if not _same_scopes(request.provider, request.requested_scopes, registration.scopes):
            raise MailHubCredentialBrokerError("oauth_exchange_scope_mismatch", status_code=403)
        exchange_form = {
            "client_id": registration.client_id,
            "client_secret": registration.client_secret,
            "code": request.code,
            "code_verifier": request.code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": request.redirect_uri,
        }
        if request.provider == "microsoft_graph":
            exchange_form["scope"] = " ".join(request.requested_scopes)
        token_payload = await self._token_request(registration, exchange_form)
        access_token, refresh_token, expires_at, granted_scopes = _validate_token_response(
            request.provider,
            token_payload,
            requested_scopes=tuple(request.requested_scopes),
            require_refresh_token=True,
        )
        identity = await self._provider_identity(registration, access_token)
        credential_ref = f"mailcred_{secrets.token_urlsafe(24)}"
        now = datetime.now(UTC)
        provider_tenant_id = (
            registration.authority_tenant if request.provider == "microsoft_graph" else None
        )
        secret_payload = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
        }
        encrypted = self._fernet.encrypt(_canonical_json(secret_payload))
        async with self._db_lock:
            await asyncio.to_thread(
                self._store_credential_sync,
                credential_ref,
                request,
                identity,
                provider_tenant_id,
                granted_scopes,
                expires_at,
                encrypted,
                now,
            )
        result: dict[str, object] = {
            "credential_ref": credential_ref,
            "email_address": identity["email_address"],
            "provider_account_id": identity["provider_account_id"],
            "granted_scopes": list(granted_scopes),
            "credential_version": 1,
        }
        if provider_tenant_id:
            result["provider_tenant_id"] = provider_tenant_id
        return result

    async def resolve(self, request: CredentialResolve) -> dict[str, str]:
        record = await self._load_active(request)
        if record.expires_at <= datetime.now(UTC) + timedelta(seconds=60):
            await self.refresh(
                CredentialRefresh(
                    credential_ref=request.credential_ref,
                    tenant_id=request.tenant_id,
                    subject_id=request.subject_id,
                    reason="automatic_access_token_rotation",
                )
            )
            record = await self._load_active(request)
        access_token = record.secret.get("access_token", "")
        _bounded_token(access_token, "credential_access_token")
        await self._audit(
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            credential_ref=record.credential_ref,
            event_type="mail.credential.resolved",
            metadata={"provider": record.provider, "credential_version": record.version},
        )
        return {"access_token": access_token, "token_type": "Bearer"}

    async def refresh(self, request: CredentialRefresh) -> dict[str, object]:
        _bounded_text(request.reason, "credential_refresh_reason", maximum=500)
        async with self._rotation_lock:
            record = await self._load_active(request)
            registration = self._registration(record.provider)
            registration.require_ready()
            refresh_token = record.secret.get("refresh_token", "")
            _bounded_token(refresh_token, "credential_refresh_token")
            refresh_form = {
                "client_id": registration.client_id,
                "client_secret": registration.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
            if record.provider == "microsoft_graph":
                refresh_form["scope"] = " ".join(record.granted_scopes)
            payload = await self._token_request(registration, refresh_form)
            access_token, replacement_refresh, expires_at, granted_scopes = (
                _validate_token_response(
                    record.provider,
                    payload,
                    requested_scopes=record.granted_scopes,
                    require_refresh_token=False,
                    existing_refresh_token=refresh_token,
                )
            )
            next_version = record.version + 1
            encrypted = self._fernet.encrypt(
                _canonical_json(
                    {
                        "access_token": access_token,
                        "refresh_token": replacement_refresh,
                        "token_type": "Bearer",
                    }
                )
            )
            async with self._db_lock:
                await asyncio.to_thread(
                    self._rotate_credential_sync,
                    record,
                    encrypted,
                    expires_at,
                    granted_scopes,
                    next_version,
                    request.reason,
                    datetime.now(UTC),
                )
        result: dict[str, object] = {
            "credential_ref": record.credential_ref,
            "email_address": record.email_address,
            "provider_account_id": record.provider_account_id,
            "credential_version": next_version,
        }
        if record.provider_tenant_id:
            result["provider_tenant_id"] = record.provider_tenant_id
        return result

    async def revoke(self, request: CredentialResolve) -> dict[str, object]:
        async with self._rotation_lock:
            try:
                record = await self._load_active(request)
            except MailHubCredentialBrokerError as exc:
                if exc.code == "credential_revoked":
                    return {"status": "already_revoked"}
                raise
            provider_request_id: str | None = None
            if record.provider == "gmail":
                token = record.secret.get("refresh_token") or record.secret.get("access_token", "")
                _bounded_token(token, "credential_revoke_token")
                provider_request_id = await self._revoke_google(token)
            # Microsoft does not expose an app-safe per-refresh-token revocation
            # endpoint.  Destroying the only host copy is the authoritative
            # application-side revocation; tenant/user consent revocation is a
            # separate operator action recorded by the activation runbook.
            revocation_id = f"mailrevoke_{secrets.token_urlsafe(18)}"
            async with self._db_lock:
                await asyncio.to_thread(
                    self._revoke_credential_sync,
                    record,
                    revocation_id,
                    datetime.now(UTC),
                    provider_request_id,
                )
        result: dict[str, object] = {
            "status": "revoked",
            "revocation_id": revocation_id,
        }
        if provider_request_id:
            result["provider_request_id"] = provider_request_id
        return result

    def _registration(self, provider: ProviderName) -> ProviderOAuthRegistration:
        registration = self._registrations.get(provider)
        if registration is None:
            raise MailHubCredentialBrokerError("mailhub_provider_unconfigured", status_code=503)
        return registration

    async def _load_active(self, request: CredentialResolve) -> _CredentialRecord:
        _bounded_text(request.credential_ref, "credential_ref", maximum=512)
        _bounded_text(request.tenant_id, "credential_tenant_id", maximum=200)
        _bounded_text(request.subject_id, "credential_subject_id", maximum=200)
        async with self._db_lock:
            row = await asyncio.to_thread(
                self._load_credential_sync,
                request.credential_ref,
                request.tenant_id,
                request.subject_id,
            )
        if row is None:
            raise MailHubCredentialBrokerError("credential_not_found", status_code=404)
        if row["status"] != "ACTIVE":
            raise MailHubCredentialBrokerError("credential_revoked", status_code=403)
        try:
            secret_payload = json.loads(self._fernet.decrypt(row["encrypted_secret"]))
        except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MailHubCredentialBrokerError(
                "credential_decryption_failed", status_code=500
            ) from exc
        if not isinstance(secret_payload, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in secret_payload.items()
        ):
            raise MailHubCredentialBrokerError("credential_payload_invalid", status_code=500)
        provider = str(row["provider"])
        if provider not in {"gmail", "microsoft_graph"}:
            raise MailHubCredentialBrokerError("credential_provider_invalid", status_code=500)
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at.tzinfo is None:
            raise MailHubCredentialBrokerError("credential_expiry_invalid", status_code=500)
        raw_scopes = json.loads(str(row["granted_scopes_json"]))
        if not isinstance(raw_scopes, list) or any(not isinstance(v, str) for v in raw_scopes):
            raise MailHubCredentialBrokerError("credential_scopes_invalid", status_code=500)
        return _CredentialRecord(
            credential_ref=str(row["credential_ref"]),
            tenant_id=str(row["tenant_id"]),
            subject_id=str(row["subject_id"]),
            provider=provider,  # type: ignore[arg-type]
            email_address=str(row["email_address"]),
            provider_account_id=str(row["provider_account_id"]),
            provider_tenant_id=(
                str(row["provider_tenant_id"]) if row["provider_tenant_id"] else None
            ),
            granted_scopes=tuple(raw_scopes),
            version=int(row["version"]),
            status=str(row["status"]),
            expires_at=expires_at.astimezone(UTC),
            secret={str(key): str(value) for key, value in secret_payload.items()},
        )

    async def _token_request(
        self,
        registration: ProviderOAuthRegistration,
        form: dict[str, str],
    ) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=30.0,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    registration.token_endpoint,
                    headers={"Accept": "application/json"},
                    data=form,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MailHubCredentialBrokerError(
                "oauth_token_endpoint_unavailable", status_code=502
            ) from exc
        if len(response.content) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise MailHubCredentialBrokerError("oauth_token_response_too_large", status_code=502)
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise MailHubCredentialBrokerError(
                "oauth_token_response_invalid", status_code=502
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            # Never reflect provider descriptions because they may contain
            # tenant/user details.  The bounded error code is enough for UI and audit.
            provider_code = payload.get("error") if isinstance(payload, dict) else None
            code = (
                str(provider_code)
                if isinstance(provider_code, str) and provider_code.isidentifier()
                else "rejected"
            )
            raise MailHubCredentialBrokerError(f"oauth_token_{code}", status_code=502)
        if not isinstance(payload, dict):
            raise MailHubCredentialBrokerError("oauth_token_response_invalid", status_code=502)
        return payload

    async def _provider_identity(
        self,
        registration: ProviderOAuthRegistration,
        access_token: str,
    ) -> dict[str, str]:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=20.0,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    registration.identity_endpoint,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {access_token}",
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MailHubCredentialBrokerError(
                "oauth_identity_endpoint_unavailable", status_code=502
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise MailHubCredentialBrokerError("oauth_identity_rejected", status_code=502)
        if len(response.content) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise MailHubCredentialBrokerError("oauth_identity_response_too_large", status_code=502)
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise MailHubCredentialBrokerError(
                "oauth_identity_response_invalid", status_code=502
            ) from exc
        if not isinstance(payload, dict):
            raise MailHubCredentialBrokerError("oauth_identity_response_invalid", status_code=502)
        if registration.provider == "gmail":
            email = payload.get("emailAddress")
            account_id = email
        else:
            account_id = payload.get("id")
            email = payload.get("mail") or payload.get("userPrincipalName")
        if not isinstance(email, str) or not _email_like(email):
            raise MailHubCredentialBrokerError("oauth_identity_email_invalid", status_code=502)
        if not isinstance(account_id, str):
            raise MailHubCredentialBrokerError("oauth_identity_account_invalid", status_code=502)
        _bounded_text(account_id, "oauth_identity_account", maximum=512)
        return {
            "email_address": email.strip().casefold(),
            "provider_account_id": account_id.strip(),
        }

    async def _revoke_google(self, token: str) -> str:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=20.0,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    headers={"Accept": "application/json"},
                    data={"token": token},
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MailHubCredentialBrokerError(
                "credential_provider_revoke_unavailable", status_code=502
            ) from exc
        if response.status_code != 200:
            raise MailHubCredentialBrokerError("credential_provider_revoke_failed", status_code=502)
        return response.headers.get("x-request-id") or f"google-{secrets.token_urlsafe(12)}"

    async def _audit(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        credential_ref: str,
        event_type: str,
        metadata: dict[str, object],
    ) -> None:
        async with self._db_lock:
            await asyncio.to_thread(
                self._audit_sync,
                tenant_id,
                subject_id,
                credential_ref,
                event_type,
                metadata,
                datetime.now(UTC),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_sync(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS mailhub_oauth_states (
                    state_digest TEXT PRIMARY KEY,
                    record_sha256 TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    encrypted_record BLOB NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mailhub_oauth_states_expiry
                    ON mailhub_oauth_states(expires_at, consumed_at);
                CREATE TABLE IF NOT EXISTS mailhub_credentials (
                    credential_ref TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    email_address TEXT NOT NULL,
                    provider_account_id TEXT NOT NULL,
                    provider_tenant_id TEXT,
                    granted_scopes_json TEXT NOT NULL,
                    encrypted_secret BLOB NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    rotated_at TEXT,
                    revoked_at TEXT,
                    revocation_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_mailhub_credentials_owner
                    ON mailhub_credentials(tenant_id, subject_id, status);
                CREATE TABLE IF NOT EXISTS mailhub_credential_audit (
                    event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    credential_ref_sha256 TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mailhub_credential_audit_owner
                    ON mailhub_credential_audit(tenant_id, subject_id, occurred_at);
                """
            )

    def _save_state_sync(
        self,
        state_digest: str,
        record_digest: str,
        state: OAuthStateWrite,
        encrypted: bytes,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        with self._connect() as db:
            existing = db.execute(
                "SELECT record_sha256 FROM mailhub_oauth_states WHERE state_digest=?",
                (state_digest,),
            ).fetchone()
            if existing is not None:
                if hmac.compare_digest(str(existing["record_sha256"]), record_digest):
                    return
                raise MailHubCredentialBrokerError("oauth_state_collision", status_code=409)
            db.execute(
                """INSERT INTO mailhub_oauth_states
                   (state_digest, record_sha256, tenant_id, subject_id, provider,
                    encrypted_record, expires_at, consumed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    state_digest,
                    record_digest,
                    state.tenant_id,
                    state.subject_id,
                    state.provider,
                    encrypted,
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )

    def _consume_state_sync(self, state_digest: str, now: datetime) -> bytes | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT encrypted_record, expires_at, consumed_at
                   FROM mailhub_oauth_states WHERE state_digest=?""",
                (state_digest,),
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                return None
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
            if expires_at.tzinfo is None or expires_at <= now:
                db.execute(
                    "UPDATE mailhub_oauth_states SET consumed_at=? WHERE state_digest=?",
                    (now.isoformat(), state_digest),
                )
                return None
            updated = db.execute(
                """UPDATE mailhub_oauth_states SET consumed_at=?
                   WHERE state_digest=? AND consumed_at IS NULL""",
                (now.isoformat(), state_digest),
            ).rowcount
            if updated != 1:
                return None
            return bytes(row["encrypted_record"])

    def _store_credential_sync(
        self,
        credential_ref: str,
        request: OAuthExchange,
        identity: dict[str, str],
        provider_tenant_id: str | None,
        granted_scopes: tuple[str, ...],
        expires_at: datetime,
        encrypted: bytes,
        now: datetime,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO mailhub_credentials
                   (credential_ref, tenant_id, subject_id, provider, email_address,
                    provider_account_id, provider_tenant_id, granted_scopes_json,
                    encrypted_secret, status, version, expires_at, created_at,
                    rotated_at, revoked_at, revocation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 1, ?, ?, NULL, NULL, NULL)""",
                (
                    credential_ref,
                    request.tenant_id,
                    request.subject_id,
                    request.provider,
                    identity["email_address"],
                    identity["provider_account_id"],
                    provider_tenant_id,
                    json.dumps(list(granted_scopes), separators=(",", ":")),
                    encrypted,
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )
            self._audit_with_db(
                db,
                request.tenant_id,
                request.subject_id,
                credential_ref,
                "mail.credential.created",
                {
                    "provider": request.provider,
                    "credential_version": 1,
                    "scope_count": len(granted_scopes),
                },
                now,
            )

    def _load_credential_sync(
        self, credential_ref: str, tenant_id: str, subject_id: str
    ) -> sqlite3.Row | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM mailhub_credentials
                   WHERE credential_ref=? AND tenant_id=? AND subject_id=?""",
                (credential_ref, tenant_id, subject_id),
            ).fetchone()
            return cast(sqlite3.Row | None, row)

    def _rotate_credential_sync(
        self,
        record: _CredentialRecord,
        encrypted: bytes,
        expires_at: datetime,
        granted_scopes: tuple[str, ...],
        next_version: int,
        reason: str,
        now: datetime,
    ) -> None:
        with self._connect() as db:
            updated = db.execute(
                """UPDATE mailhub_credentials
                   SET encrypted_secret=?, granted_scopes_json=?, version=?,
                       expires_at=?, rotated_at=?
                   WHERE credential_ref=? AND tenant_id=? AND subject_id=?
                     AND status='ACTIVE' AND version=?""",
                (
                    encrypted,
                    json.dumps(list(granted_scopes), separators=(",", ":")),
                    next_version,
                    expires_at.isoformat(),
                    now.isoformat(),
                    record.credential_ref,
                    record.tenant_id,
                    record.subject_id,
                    record.version,
                ),
            ).rowcount
            if updated != 1:
                raise MailHubCredentialBrokerError("credential_rotation_conflict", status_code=409)
            self._audit_with_db(
                db,
                record.tenant_id,
                record.subject_id,
                record.credential_ref,
                "mail.credential.refreshed",
                {
                    "provider": record.provider,
                    "credential_version": next_version,
                    "reason_sha256": _digest(reason),
                },
                now,
            )

    def _revoke_credential_sync(
        self,
        record: _CredentialRecord,
        revocation_id: str,
        now: datetime,
        provider_request_id: str | None,
    ) -> None:
        tombstone = self._fernet.encrypt(_canonical_json({"revoked": "true"}))
        with self._connect() as db:
            updated = db.execute(
                """UPDATE mailhub_credentials
                   SET encrypted_secret=?, status='REVOKED', version=version+1,
                       revoked_at=?, revocation_id=?
                   WHERE credential_ref=? AND tenant_id=? AND subject_id=?
                     AND status='ACTIVE' AND version=?""",
                (
                    tombstone,
                    now.isoformat(),
                    revocation_id,
                    record.credential_ref,
                    record.tenant_id,
                    record.subject_id,
                    record.version,
                ),
            ).rowcount
            if updated != 1:
                raise MailHubCredentialBrokerError(
                    "credential_revocation_conflict", status_code=409
                )
            metadata: dict[str, object] = {
                "provider": record.provider,
                "revocation_id_sha256": _digest(revocation_id),
                "secret_destroyed": True,
                "provider_revocation_mode": (
                    "provider_endpoint" if record.provider == "gmail" else "local_secret_destroyed"
                ),
            }
            if provider_request_id:
                metadata["provider_request_id_sha256"] = _digest(provider_request_id)
            self._audit_with_db(
                db,
                record.tenant_id,
                record.subject_id,
                record.credential_ref,
                "mail.credential.revoked",
                metadata,
                now,
            )

    def _audit_sync(
        self,
        tenant_id: str,
        subject_id: str,
        credential_ref: str,
        event_type: str,
        metadata: dict[str, object],
        now: datetime,
    ) -> None:
        with self._connect() as db:
            self._audit_with_db(
                db,
                tenant_id,
                subject_id,
                credential_ref,
                event_type,
                metadata,
                now,
            )

    @staticmethod
    def _audit_with_db(
        db: sqlite3.Connection,
        tenant_id: str,
        subject_id: str,
        credential_ref: str,
        event_type: str,
        metadata: dict[str, object],
        now: datetime,
    ) -> None:
        db.execute(
            "INSERT INTO mailhub_credential_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"mca_{secrets.token_urlsafe(18)}",
                tenant_id,
                subject_id,
                _digest(credential_ref),
                event_type,
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                now.isoformat(),
            ),
        )


def _validate_token_response(
    provider: ProviderName,
    payload: dict[str, object],
    *,
    requested_scopes: tuple[str, ...],
    require_refresh_token: bool,
    existing_refresh_token: str | None = None,
) -> tuple[str, str, datetime, tuple[str, ...]]:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token") or existing_refresh_token
    token_type = payload.get("token_type")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str):
        raise MailHubCredentialBrokerError("oauth_access_token_missing", status_code=502)
    _bounded_token(access_token, "oauth_access_token")
    if not isinstance(token_type, str) or token_type.casefold() != "bearer":
        raise MailHubCredentialBrokerError("oauth_token_type_invalid", status_code=502)
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        raise MailHubCredentialBrokerError("oauth_token_expiry_invalid", status_code=502)
    seconds = int(expires_in)
    if not 60 <= seconds <= 86_400:
        raise MailHubCredentialBrokerError("oauth_token_expiry_invalid", status_code=502)
    if not isinstance(refresh_token, str) or not refresh_token:
        if require_refresh_token:
            raise MailHubCredentialBrokerError("oauth_refresh_token_missing", status_code=502)
        raise MailHubCredentialBrokerError("oauth_refresh_token_unavailable", status_code=502)
    _bounded_token(refresh_token, "oauth_refresh_token")
    raw_scope = payload.get("scope")
    if not isinstance(raw_scope, str) or not raw_scope.strip():
        raise MailHubCredentialBrokerError("oauth_granted_scopes_missing", status_code=502)
    returned = {item.casefold() for item in raw_scope.split() if item.strip()}
    granted = [scope for scope in requested_scopes if scope.casefold() in returned]
    if (
        provider == "microsoft_graph"
        and _GRAPH_OFFLINE_SCOPE in {scope.casefold() for scope in requested_scopes}
        # The token endpoint proves offline access by issuing a refresh token,
        # but may omit this non-resource scope from the response `scope` field.
        and _GRAPH_OFFLINE_SCOPE not in {scope.casefold() for scope in granted}
    ):
        granted.append(
            next(scope for scope in requested_scopes if scope.casefold() == _GRAPH_OFFLINE_SCOPE)
        )
    _validate_scopes(provider, granted)
    return (
        access_token,
        refresh_token,
        datetime.now(UTC) + timedelta(seconds=seconds),
        tuple(dict.fromkeys(granted)),
    )


def _validate_scopes(provider: ProviderName, scopes: list[str] | tuple[str, ...]) -> None:
    if not scopes or len(scopes) > 40:
        raise ValueError("oauth_scopes_invalid")
    normalized: list[str] = []
    for scope in scopes:
        _bounded_text(scope, "oauth_scope", maximum=200)
        value = scope.casefold()
        if value not in normalized:
            normalized.append(value)
    required = (
        {_GMAIL_READ_SCOPE.casefold()}
        if provider == "gmail"
        else {_GRAPH_READ_SCOPE, _GRAPH_OFFLINE_SCOPE}
    )
    if not required.issubset(normalized):
        raise ValueError("oauth_read_scope_missing")
    for scope in normalized:
        if any(
            scope == marker or scope.startswith(marker) for marker in _WRITE_SCOPE_MARKERS[provider]
        ):
            raise ValueError("oauth_write_scope_forbidden")


def _same_scopes(
    provider: ProviderName,
    requested: list[str],
    registered: tuple[str, ...],
) -> bool:
    _validate_scopes(provider, requested)
    _validate_scopes(provider, registered)
    return {value.casefold() for value in requested} == {value.casefold() for value in registered}


def _validate_redirect_uri(value: str) -> str:
    _bounded_text(value, "oauth_redirect_uri", maximum=2000)
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("oauth_redirect_uri_invalid") from exc
    local_http = parsed.scheme == "http" and (parsed.hostname or "").casefold() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if (
        not parsed.hostname
        or (parsed.scheme != "https" and not local_http)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path
    ):
        raise ValueError("oauth_redirect_uri_invalid")
    return value


def _bounded_text(
    value: str,
    field: str,
    *,
    minimum: int = 1,
    maximum: int,
) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field}_invalid")
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field}_invalid")
    return value


def _bounded_token(value: str, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_TOKEN_CHARS:
        raise MailHubCredentialBrokerError(f"{field}_invalid", status_code=502)
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise MailHubCredentialBrokerError(f"{field}_invalid", status_code=502)
    return value


def _email_like(value: str) -> bool:
    stripped = value.strip()
    return (
        3 <= len(stripped) <= 320
        and stripped.count("@") == 1
        and not any(ord(char) < 33 or ord(char) == 127 for char in stripped)
    )


def _is_tenant_uuid(value: str) -> bool:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError):
        return False
    return str(parsed).casefold() == value.casefold()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
