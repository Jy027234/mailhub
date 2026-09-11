"""SQLite-backed local stores for the MailHub host contract.

Every store is deliberately bounded and local.  Object content is Fernet
encrypted at rest, events/actions/knowledge rows are idempotent by the
contract's idempotency keys, and quota leases are durable rows rather than
process-local counters.  This remains a controlled local/Beta adapter, not a
production host backend.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cryptography.fernet import Fernet

from mailhub.quota import QuotaLease

from local_host.vault import VaultKvClient, is_vault_pointer


class StoreError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(UTC)


def _token(bits: int = 128) -> str:
    return secrets.token_urlsafe(bits // 8)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_objects (
    object_ref TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    encrypted_content BLOB NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_objects_owner ON host_objects(tenant_id, subject_id);
CREATE TABLE IF NOT EXISTS host_events (
    event_id TEXT PRIMARY KEY,
    envelope_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_approvals (
    confirmation_ref TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_actions (
    idempotency_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_knowledge (
    idempotency_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_knowledge_revocations (
    request_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_quota_leases (
    lease_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    account_id TEXT,
    operation TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_quota_scope ON host_quota_leases(
    tenant_id, subject_id, account_id, operation
);
CREATE TABLE IF NOT EXISTS host_imap_credentials (
    credential_ref TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    username TEXT NOT NULL,
    encrypted_password BLOB NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_imap_credentials_owner
    ON host_imap_credentials(tenant_id, subject_id, status);
CREATE TABLE IF NOT EXISTS host_quota_usage (
    scope_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_quota_usage ON host_quota_usage(scope_key, operation, consumed_at);
CREATE TABLE IF NOT EXISTS host_oauth_flows (
    state TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_audit_events ON host_audit_events(event_type, created_at);
"""

#: Field-name markers that must never be persisted in the audit ledger.  The
#: adapter redacts first; the host redacts again so a direct caller cannot
#: smuggle content, a token or a credential reference into durable storage.
_FORBIDDEN_AUDIT_MARKERS = (
    "body",
    "html",
    "mime",
    "snippet",
    "raw",
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "cookie",
)


def redact_audit_fields(event: dict[str, object]) -> dict[str, object]:
    """Keep bounded identifiers, hashes, status and evidence refs only."""

    result: dict[str, object] = {}
    for key, value in event.items():
        normalized = key.strip().casefold()
        if any(marker in normalized for marker in _FORBIDDEN_AUDIT_MARKERS):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            result[key] = value
        elif isinstance(value, (list, tuple)):
            result[key] = [
                item for item in value if item is None or isinstance(item, (str, int, float, bool))
            ]
        elif isinstance(value, dict):
            result[key] = redact_audit_fields(
                {str(inner): inner_value for inner, inner_value in value.items()}
            )
    return result


@dataclass(frozen=True, slots=True)
class StoredObject:
    tenant_id: str
    subject_id: str
    purpose: str
    content: str
    content_sha256: str
    expires_at: datetime | None


class LocalStores:
    def __init__(
        self,
        database_path: Path,
        encryption_secret: str,
        vault: VaultKvClient | None = None,
    ) -> None:
        if len(encryption_secret) < 32:
            raise ValueError("host_encryption_secret_too_short")
        self.database_path = database_path
        digest = hashlib.sha256(encryption_secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))
        # With a Vault backend the row keeps a pointer, never the secret; the
        # encrypted column stays as the local/dev fallback.
        self._vault = vault
        self.lease_ttl = timedelta(hours=1)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    # ---- objects ---------------------------------------------------------

    def put_object(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        purpose: str,
        content: str,
        content_sha256: str,
        expires_at: datetime | None,
    ) -> str:
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != content_sha256:
            raise StoreError("object_store_digest_mismatch", status_code=422)
        object_ref = f"obj_{_token()}"
        encrypted = self._fernet.encrypt(content.encode("utf-8"))
        with self._connect() as db:
            db.execute(
                """INSERT INTO host_objects
                   (object_ref, tenant_id, subject_id, purpose, content_sha256,
                    encrypted_content, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    object_ref,
                    tenant_id,
                    subject_id,
                    purpose,
                    content_sha256,
                    encrypted,
                    expires_at.isoformat() if expires_at else None,
                    _now().isoformat(),
                ),
            )
        return object_ref

    def get_object(self, *, tenant_id: str, subject_id: str, object_ref: str) -> str:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM host_objects
                   WHERE object_ref=? AND tenant_id=? AND subject_id=?""",
                (object_ref, tenant_id, subject_id),
            ).fetchone()
        if row is None:
            raise StoreError("object_store_not_found", status_code=404)
        expires_at = row["expires_at"]
        if expires_at is not None and datetime.fromisoformat(str(expires_at)) <= _now():
            raise StoreError("object_store_expired", status_code=404)
        try:
            return self._fernet.decrypt(bytes(row["encrypted_content"])).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - bounded local fallback
            raise StoreError("object_store_decryption_failed", status_code=500) from exc

    def delete_object(self, *, tenant_id: str, subject_id: str, object_ref: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM host_objects WHERE object_ref=? AND tenant_id=? AND subject_id=?",
                (object_ref, tenant_id, subject_id),
            )

    # ---- events / telemetry ----------------------------------------------

    def record_event(self, *, event_id: str, envelope: dict[str, object]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO host_events (event_id, envelope_json, created_at) VALUES (?, ?, ?)",
                (
                    event_id,
                    json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                    _now().isoformat(),
                ),
            )

    def record_telemetry(self, *, name: str, fields: dict[str, object]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO host_telemetry (name, fields_json, created_at) VALUES (?, ?, ?)",
                (
                    name,
                    json.dumps(fields, sort_keys=True, separators=(",", ":")),
                    _now().isoformat(),
                ),
            )

    # ---- audit -------------------------------------------------------------

    def record_audit(self, *, event: dict[str, object]) -> str:
        """Persist a redacted audit record and return its retained-field digest."""

        raw_type = event.get("event_type") or event.get("event") or "mailhub.event"
        if not isinstance(raw_type, str) or not raw_type.strip():
            raise StoreError("audit_event_type_invalid", status_code=422)
        redacted = redact_audit_fields(event)
        encoded = json.dumps(redacted, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            db.execute(
                """INSERT INTO host_audit_events
                   (event_type, event_digest, fields_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (raw_type.strip(), _digest(encoded), encoded, _now().isoformat()),
            )
        return _digest(encoded)

    def audit_records(self, limit: int = 50) -> tuple[dict[str, object], ...]:
        """Return retained audit payloads, newest first."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT fields_json FROM host_audit_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(str(row["fields_json"]))
            if isinstance(payload, dict):
                records.append({str(key): value for key, value in payload.items()})
        return tuple(records)

    def audit_field_names(self, limit: int = 50) -> tuple[str, ...]:
        """Return retained field names, newest first (used by conformance)."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT fields_json FROM host_audit_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        names: list[str] = []
        for row in rows:
            payload = json.loads(str(row["fields_json"]))
            if isinstance(payload, dict):
                names.extend(str(key) for key in payload)
        return tuple(dict.fromkeys(names))

    # ---- approvals ---------------------------------------------------------

    def create_approval(
        self, *, tenant_id: str, subject_id: str, action_id: str, action_digest: str
    ) -> str:
        confirmation_ref = f"confirm_{_token()}"
        with self._connect() as db:
            db.execute(
                """INSERT INTO host_approvals
                   (confirmation_ref, tenant_id, subject_id, action_id, action_digest, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    confirmation_ref,
                    tenant_id,
                    subject_id,
                    action_id,
                    action_digest,
                    _now().isoformat(),
                ),
            )
        return confirmation_ref

    def verify_approval(
        self,
        *,
        confirmation_ref: str,
        action_id: str,
        action_digest: str,
        approver_subject_id: str | None = None,
    ) -> bool:
        # Accepted so the wire contract is additive, not yet enforced: this
        # reference host is single-principal, so separation of duties has to be
        # configured before the check can mean anything.
        del approver_subject_id
        with self._connect() as db:
            row = db.execute(
                "SELECT action_id, action_digest FROM host_approvals WHERE confirmation_ref=?",
                (confirmation_ref,),
            ).fetchone()
        if row is None:
            return False
        stored_id = str(row["action_id"])
        stored_digest = str(row["action_digest"])
        # Unbound confirmations (local single-user mode) verify by existence;
        # bound confirmations require both action id and digest to match.
        if not stored_id and not stored_digest:
            return True
        return stored_id == action_id and hmac.compare_digest(stored_digest, action_digest)

    # ---- actions / knowledge -----------------------------------------------

    def record_action(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        subject_id: str,
        action_id: str,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "status": "applied",
            "action_id": action_id,
            "result_ref": f"act_{_token()}",
            "execution_id": f"exec_{_token()}",
            "replayed": False,
        }
        with self._connect() as db:
            existing = db.execute(
                "SELECT result_json FROM host_actions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                loaded: Any = json.loads(str(existing["result_json"]))
                replay = dict(loaded) if isinstance(loaded, dict) else dict(result)
                replay["replayed"] = True
                return replay
            db.execute(
                """INSERT INTO host_actions
                   (idempotency_key, tenant_id, subject_id, action_id, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    idempotency_key,
                    tenant_id,
                    subject_id,
                    action_id,
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                    _now().isoformat(),
                ),
            )
        return result

    def record_knowledge(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        subject_id: str,
        candidate_id: str,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "status": "approved",
            "knowledge_ref": f"kref_{_token()}",
            "knowledge_candidate_ref": candidate_id,
            "approved_at": _now().isoformat(),
            "created": True,
        }
        with self._connect() as db:
            existing = db.execute(
                "SELECT result_json FROM host_knowledge WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                loaded = json.loads(str(existing["result_json"]))
                replay = dict(loaded) if isinstance(loaded, dict) else dict(result)
                replay["created"] = False
                return replay
            db.execute(
                """INSERT INTO host_knowledge
                   (idempotency_key, tenant_id, subject_id, candidate_id, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    idempotency_key,
                    tenant_id,
                    subject_id,
                    candidate_id,
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                    _now().isoformat(),
                ),
            )
        return result

    def record_knowledge_revocation(
        self,
        *,
        request_id: str,
        tenant_id: str,
        subject_id: str,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "status": "revoked",
            "revoke_ref": f"krev_{_token()}",
            "revoked_count": 0,
            "reindexed_count": 0,
        }
        with self._connect() as db:
            existing = db.execute(
                "SELECT result_json FROM host_knowledge_revocations WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                loaded = json.loads(str(existing["result_json"]))
                return loaded if isinstance(loaded, dict) else result
            db.execute(
                """INSERT INTO host_knowledge_revocations
                   (request_id, tenant_id, subject_id, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    request_id,
                    tenant_id,
                    subject_id,
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                    _now().isoformat(),
                ),
            )
        return result

    # ---- quota -------------------------------------------------------------

    def acquire_quota(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        account_id: UUID | None,
        operation: str,
        max_concurrent: int,
        max_per_hour: int,
        max_per_day: int,
    ) -> QuotaLease | None:
        now = _now()
        scope_key = f"{tenant_id}|{subject_id}|{account_id or ''}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if max_concurrent > 0:
                    active = db.execute(
                        """SELECT COUNT(*) AS count FROM host_quota_leases
                           WHERE tenant_id=? AND subject_id=? AND account_id=? AND operation=?
                             AND expires_at > ?""",
                        (
                            tenant_id,
                            subject_id,
                            str(account_id) if account_id else "",
                            operation,
                            now.isoformat(),
                        ),
                    ).fetchone()
                    if int(active["count"]) >= max_concurrent:
                        db.rollback()
                        return None
                if max_per_hour > 0:
                    hourly = db.execute(
                        """SELECT COUNT(*) AS count FROM host_quota_usage
                           WHERE scope_key=? AND operation=? AND consumed_at > ?""",
                        (scope_key, operation, (now - timedelta(hours=1)).isoformat()),
                    ).fetchone()
                    if int(hourly["count"]) >= max_per_hour:
                        db.rollback()
                        return None
                if max_per_day > 0:
                    daily = db.execute(
                        """SELECT COUNT(*) AS count FROM host_quota_usage
                           WHERE scope_key=? AND operation=? AND consumed_at > ?""",
                        (scope_key, operation, (now - timedelta(days=1)).isoformat()),
                    ).fetchone()
                    if int(daily["count"]) >= max_per_day:
                        db.rollback()
                        return None
                lease_id = uuid4()
                db.execute(
                    """INSERT INTO host_quota_leases
                       (lease_id, tenant_id, subject_id, account_id, operation, acquired_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(lease_id),
                        tenant_id,
                        subject_id,
                        str(account_id) if account_id else "",
                        operation,
                        now.isoformat(),
                        (now + self.lease_ttl).isoformat(),
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return QuotaLease(
            lease_id=lease_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            account_id=account_id,
            operation=operation,
            acquired_at=now,
        )

    def release_quota(self, *, lease_id: str, consume: bool) -> None:
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM host_quota_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
                if row is None:
                    db.commit()
                    return
                if consume:
                    db.execute(
                        """INSERT INTO host_quota_usage (scope_key, operation, consumed_at)
                           VALUES (?, ?, ?)""",
                        (
                            f"{row['tenant_id']}|{row['subject_id']}|{row['account_id'] or ''}",
                            str(row["operation"]),
                            now.isoformat(),
                        ),
                    )
                db.execute("DELETE FROM host_quota_leases WHERE lease_id=?", (lease_id,))
                db.commit()
            except Exception:
                db.rollback()
                raise

    # ---- IMAP application-password credentials ------------------------------

    def store_imap_credential(
        self, *, tenant_id: str, subject_id: str, username: str, password: str
    ) -> str:
        """Encrypt and store an IMAP application password; return an opaque ref.

        The password never leaves this table in plaintext and is only handed
        back as a short-lived credential during a provider resolve call.
        """

        if not password or any(ord(char) < 33 or ord(char) == 127 for char in password):
            raise StoreError("imap_password_invalid", status_code=422)
        if len(password) > 512:
            raise StoreError("imap_password_invalid", status_code=422)
        credential_ref = f"imapcred_{_token()}"
        if self._vault is not None:
            pointer = self._vault.put_secret(name=credential_ref, value=password)
            stored = pointer.encode("utf-8")
        else:
            stored = self._fernet.encrypt(password.encode("utf-8"))
        with self._connect() as db:
            db.execute(
                """INSERT INTO host_imap_credentials
                   (credential_ref, tenant_id, subject_id, username, encrypted_password,
                    status, version, revoked_at, created_at)
                   VALUES (?, ?, ?, ?, ?, 'ACTIVE', 1, NULL, ?)""",
                (
                    credential_ref,
                    tenant_id,
                    subject_id,
                    username,
                    stored,
                    _now().isoformat(),
                ),
            )
        return credential_ref

    def resolve_imap_credential(
        self, *, credential_ref: str, tenant_id: str, subject_id: str
    ) -> dict[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM host_imap_credentials
                   WHERE credential_ref=? AND tenant_id=? AND subject_id=?""",
                (credential_ref, tenant_id, subject_id),
            ).fetchone()
        if row is None or str(row["status"]) != "ACTIVE":
            return None
        stored = bytes(row["encrypted_password"])
        # The pointer prefix is the only discriminator: a row is either a Vault
        # pointer or an encrypted local blob, never a heuristic on blob shape.
        if is_vault_pointer(stored):
            if self._vault is None:
                raise StoreError("imap_vault_not_configured", status_code=503)
            password = self._vault.get_secret(pointer=stored.decode("utf-8", "replace"))
            if password is None:
                raise StoreError("imap_credential_missing_in_vault", status_code=404)
            return {"username": str(row["username"]), "password": password}
        try:
            password = self._fernet.decrypt(stored).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - bounded local fallback
            raise StoreError("imap_credential_decryption_failed", status_code=500) from exc
        return {"username": str(row["username"]), "password": password}

    def rotate_imap_credential(
        self, *, credential_ref: str, tenant_id: str, subject_id: str, password: str
    ) -> int | None:
        """Replace the secret material in place and bump the version.

        Rotation is what makes a secret backend operationally usable: the
        credential reference stays stable for MailHub while the material behind
        it changes.  Returns the new version, or None when the credential is not
        an active one owned by the caller.
        """

        if not password or any(ord(char) < 33 or ord(char) == 127 for char in password):
            raise StoreError("imap_password_invalid", status_code=422)
        if len(password) > 512:
            raise StoreError("imap_password_invalid", status_code=422)
        with self._connect() as db:
            row = db.execute(
                """SELECT encrypted_password, version FROM host_imap_credentials
                   WHERE credential_ref=? AND tenant_id=? AND subject_id=? AND status='ACTIVE'""",
                (credential_ref, tenant_id, subject_id),
            ).fetchone()
            if row is None:
                return None
            stored = bytes(row["encrypted_password"])
            if is_vault_pointer(stored):
                if self._vault is None:
                    raise StoreError("imap_vault_not_configured", status_code=503)
                replacement = self._vault.put_secret(name=credential_ref, value=password).encode(
                    "utf-8"
                )
            else:
                replacement = self._fernet.encrypt(password.encode("utf-8"))
            version = int(row["version"]) + 1
            db.execute(
                """UPDATE host_imap_credentials
                   SET encrypted_password=?, version=?, revoked_at=NULL
                   WHERE credential_ref=? AND tenant_id=? AND subject_id=? AND status='ACTIVE'""",
                (replacement, version, credential_ref, tenant_id, subject_id),
            )
        return version

    def revoke_imap_credential(
        self, *, credential_ref: str, tenant_id: str, subject_id: str
    ) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT encrypted_password FROM host_imap_credentials
                   WHERE credential_ref=? AND tenant_id=? AND subject_id=? AND status='ACTIVE'""",
                (credential_ref, tenant_id, subject_id),
            ).fetchone()
            if row is None:
                return False
            stored = bytes(row["encrypted_password"])
            updated = db.execute(
                """UPDATE host_imap_credentials
                   SET status='REVOKED', version=version+1, revoked_at=?
                   WHERE credential_ref=? AND tenant_id=? AND subject_id=? AND status='ACTIVE'""",
                (_now().isoformat(), credential_ref, tenant_id, subject_id),
            ).rowcount
        if updated == 1 and is_vault_pointer(stored) and self._vault is not None:
            # Revocation must remove the material, not just the local pointer.
            self._vault.delete_secret(pointer=stored.decode("utf-8", "replace"))
        return updated == 1

    def is_imap_credential_ref(self, credential_ref: str) -> bool:
        return credential_ref.startswith("imapcred_")

    # ---- oauth browser flow routing -----------------------------------------

    def record_oauth_flow(
        self,
        *,
        state: str,
        tenant_id: str,
        subject_id: str,
        provider: str,
        ttl: timedelta,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO host_oauth_flows (state, tenant_id, subject_id, provider, expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (state, tenant_id, subject_id, provider, (_now() + ttl).isoformat()),
            )

    def lookup_oauth_flow(self, *, state: str, provider: str) -> tuple[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT tenant_id, subject_id, expires_at FROM host_oauth_flows
                   WHERE state=? AND provider=?""",
                (state, provider),
            ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(str(row["expires_at"])) <= _now():
            return None
        return str(row["tenant_id"]), str(row["subject_id"])
