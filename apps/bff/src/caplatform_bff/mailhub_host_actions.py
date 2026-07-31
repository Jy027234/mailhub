"""Durable CAPlatform approval and action ledger for MailHub host writes.

The public browser never invents an approval reference.  The BFF issues an
opaque one-time reference after an authenticated user clicks apply, and the
MailHub Host Approval/Action callbacks bind it to the exact tenant, subject,
candidate revision and deterministic action digest.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken


class MailHubHostActionError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MailHostActionBinding:
    action_id: UUID
    action_type: str
    tenant_id: str
    subject_id: str
    input_digest: str
    candidate_id: UUID
    candidate_revision: int
    parameters: dict[str, object]


@dataclass(frozen=True, slots=True)
class MailHostActionClaim:
    binding: MailHostActionBinding
    replayed: bool
    result: dict[str, object] | None = None


class EncryptedSQLiteMailHostActionBroker:
    """Local/pilot approval and action ledger with encrypted derived payloads."""

    def __init__(
        self,
        *,
        database_path: str,
        encryption_secret: str,
        approval_ttl_seconds: int = 300,
    ) -> None:
        if len(encryption_secret) < 32:
            raise ValueError("mailhub_host_action_encryption_secret_too_short")
        if not 60 <= approval_ttl_seconds <= 1800:
            raise ValueError("mailhub_approval_ttl_invalid")
        key = hashlib.sha256(
            f"caplatform-mailhub-host-actions:{encryption_secret}".encode()
        ).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))
        self._database_path = database_path
        self._approval_ttl_seconds = approval_ttl_seconds
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    async def issue_candidate_approval(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
        candidate_revision: int,
    ) -> str:
        _scope(tenant_id, "tenant_id")
        _scope(subject_id, "subject_id")
        if candidate_revision < 1:
            raise ValueError("mailhub_candidate_revision_invalid")
        reference = f"mailapprove_{secrets.token_urlsafe(32)}"
        now = time.time()
        async with self._lock:
            await asyncio.to_thread(
                self._issue_sync,
                _digest(reference),
                tenant_id,
                subject_id,
                str(candidate_id),
                candidate_revision,
                now,
                now + self._approval_ttl_seconds,
            )
        return reference

    async def verify_confirmation(
        self,
        *,
        confirmation_ref: str,
        action: dict[str, object],
    ) -> bool:
        _reference(confirmation_ref)
        binding = _action_binding(action)
        async with self._lock:
            return await asyncio.to_thread(
                self._verify_sync,
                _digest(confirmation_ref),
                binding,
                time.time(),
            )

    async def claim_execution(
        self,
        *,
        confirmation_ref: str,
        action: dict[str, object],
    ) -> MailHostActionClaim:
        _reference(confirmation_ref)
        binding = _action_binding(action)
        encrypted_action = self._fernet.encrypt(_json_bytes(action))
        async with self._lock:
            row = await asyncio.to_thread(
                self._claim_sync,
                _digest(confirmation_ref),
                binding,
                encrypted_action,
                time.time(),
            )
        if row[0] is None:
            return MailHostActionClaim(binding=binding, replayed=False)
        try:
            result = json.loads(self._fernet.decrypt(row[0]))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MailHubHostActionError(
                "mailhub_host_action_result_unreadable", status_code=500
            ) from exc
        if not isinstance(result, dict):
            raise MailHubHostActionError("mailhub_host_action_result_invalid", status_code=500)
        return MailHostActionClaim(
            binding=binding,
            replayed=True,
            result={str(key): value for key, value in result.items()},
        )

    async def complete_execution(
        self,
        *,
        binding: MailHostActionBinding,
        result: dict[str, object],
    ) -> None:
        encrypted = self._fernet.encrypt(_json_bytes(result))
        async with self._lock:
            await asyncio.to_thread(
                self._complete_sync,
                str(binding.action_id),
                binding.input_digest,
                encrypted,
                time.time(),
            )

    async def mark_retryable_failure(
        self,
        *,
        binding: MailHostActionBinding,
        error_code: str,
    ) -> None:
        normalized = error_code.strip()[:160] or "mailhub_host_action_failed"
        async with self._lock:
            await asyncio.to_thread(
                self._failure_sync,
                str(binding.action_id),
                binding.input_digest,
                normalized,
                time.time(),
            )

    async def submit_knowledge_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
        content_sha256: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        _scope(tenant_id, "tenant_id")
        _scope(subject_id, "subject_id")
        _content_digest(content_sha256)
        encoded = _json_bytes(payload)
        payload_digest = hashlib.sha256(encoded).hexdigest()
        encrypted = self._fernet.encrypt(encoded)
        knowledge_ref = f"mailknowledge_{candidate_id}"
        now = time.time()
        async with self._lock:
            created = await asyncio.to_thread(
                self._submit_knowledge_sync,
                knowledge_ref,
                tenant_id,
                subject_id,
                str(candidate_id),
                content_sha256,
                payload_digest,
                encrypted,
                now,
            )
        return {
            "status": "approved",
            "knowledge_candidate_ref": knowledge_ref,
            "result_ref": knowledge_ref,
            "approved_at": _iso_time(now),
            "idempotency_replayed": not created,
        }

    def issue_knowledge_safety_gate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
        content_sha256: str,
    ) -> str:
        _scope(tenant_id, "tenant_id")
        _scope(subject_id, "subject_id")
        _content_digest(content_sha256)
        issued_at = int(time.time())
        token = self._fernet.encrypt(
            _json_bytes(
                {
                    "t": _digest(tenant_id),
                    "s": _digest(subject_id),
                    "c": str(candidate_id),
                    "d": content_sha256,
                    "e": issued_at + self._approval_ttl_seconds,
                }
            )
        ).decode("ascii")
        return f"mailknowgate_{token}"

    def verify_knowledge_safety_gate(
        self,
        *,
        gate_ref: str,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
        content_sha256: str,
    ) -> None:
        _scope(tenant_id, "tenant_id")
        _scope(subject_id, "subject_id")
        _content_digest(content_sha256)
        if not gate_ref.startswith("mailknowgate_") or len(gate_ref) > 2048:
            raise MailHubHostActionError("mailhub_knowledge_safety_gate_invalid", status_code=403)
        try:
            raw = self._fernet.decrypt(
                gate_ref.removeprefix("mailknowgate_").encode("ascii"),
                ttl=self._approval_ttl_seconds,
            )
            payload = json.loads(raw)
        except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MailHubHostActionError(
                "mailhub_knowledge_safety_gate_invalid", status_code=403
            ) from exc
        if not isinstance(payload, dict):
            raise MailHubHostActionError("mailhub_knowledge_safety_gate_invalid", status_code=403)
        expected = {
            "t": _digest(tenant_id),
            "s": _digest(subject_id),
            "c": str(candidate_id),
            "d": content_sha256,
        }
        if any(
            not hmac.compare_digest(str(payload.get(key) or ""), value)
            for key, value in expected.items()
        ) or int(payload.get("e") or 0) <= int(time.time()):
            raise MailHubHostActionError("mailhub_knowledge_safety_gate_invalid", status_code=403)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize_sync(self) -> None:
        Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS mailhub_host_approvals (
                    reference_sha256 TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    candidate_revision INTEGER NOT NULL,
                    verified_action_id TEXT,
                    verified_input_digest TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    verified_at REAL
                );
                CREATE INDEX IF NOT EXISTS ix_mailhub_host_approvals_owner
                    ON mailhub_host_approvals(tenant_id, subject_id, expires_at);
                CREATE TABLE IF NOT EXISTS mailhub_host_action_operations (
                    action_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    encrypted_action BLOB NOT NULL,
                    encrypted_result BLOB,
                    error_code TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_mailhub_host_actions_owner
                    ON mailhub_host_action_operations(tenant_id, subject_id, updated_at);
                CREATE TABLE IF NOT EXISTS mailhub_host_knowledge_candidates (
                    knowledge_ref TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    encrypted_payload BLOB NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(tenant_id, subject_id, candidate_id)
                );
                CREATE INDEX IF NOT EXISTS ix_mailhub_host_knowledge_owner
                    ON mailhub_host_knowledge_candidates(tenant_id, subject_id, updated_at);
                """
            )
            db.execute(
                """UPDATE mailhub_host_action_operations
                   SET status='retry_wait',
                       error_code='bff_restart_after_dispatch',
                       updated_at=?
                   WHERE status='running'""",
                (time.time(),),
            )

    def _issue_sync(
        self,
        reference_digest: str,
        tenant_id: str,
        subject_id: str,
        candidate_id: str,
        candidate_revision: int,
        created_at: float,
        expires_at: float,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """DELETE FROM mailhub_host_approvals
                   WHERE expires_at <= ? AND verified_action_id IS NULL""",
                (created_at,),
            )
            db.execute(
                """INSERT INTO mailhub_host_approvals
                   (reference_sha256, tenant_id, subject_id, candidate_id,
                    candidate_revision, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    reference_digest,
                    tenant_id,
                    subject_id,
                    candidate_id,
                    candidate_revision,
                    created_at,
                    expires_at,
                ),
            )

    def _verify_sync(
        self,
        reference_digest: str,
        binding: MailHostActionBinding,
        now: float,
    ) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM mailhub_host_approvals WHERE reference_sha256=?",
                (reference_digest,),
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                return False
            if (
                not hmac.compare_digest(str(row["tenant_id"]), binding.tenant_id)
                or not hmac.compare_digest(str(row["subject_id"]), binding.subject_id)
                or not hmac.compare_digest(str(row["candidate_id"]), str(binding.candidate_id))
                or int(row["candidate_revision"]) != binding.candidate_revision
            ):
                return False
            existing_action = row["verified_action_id"]
            existing_digest = row["verified_input_digest"]
            if existing_action is not None:
                return hmac.compare_digest(
                    str(existing_action), str(binding.action_id)
                ) and hmac.compare_digest(str(existing_digest), binding.input_digest)
            updated = db.execute(
                """UPDATE mailhub_host_approvals
                   SET verified_action_id=?, verified_input_digest=?, verified_at=?
                   WHERE reference_sha256=? AND verified_action_id IS NULL""",
                (str(binding.action_id), binding.input_digest, now, reference_digest),
            ).rowcount
            return updated == 1

    def _claim_sync(
        self,
        reference_digest: str,
        binding: MailHostActionBinding,
        encrypted_action: bytes,
        now: float,
    ) -> tuple[bytes | None]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            approval = db.execute(
                """SELECT verified_action_id, verified_input_digest
                   FROM mailhub_host_approvals WHERE reference_sha256=?""",
                (reference_digest,),
            ).fetchone()
            if (
                approval is None
                or not hmac.compare_digest(
                    str(approval["verified_action_id"] or ""), str(binding.action_id)
                )
                or not hmac.compare_digest(
                    str(approval["verified_input_digest"] or ""), binding.input_digest
                )
            ):
                raise MailHubHostActionError("mailhub_host_approval_invalid", status_code=403)
            row = db.execute(
                "SELECT * FROM mailhub_host_action_operations WHERE action_id=?",
                (str(binding.action_id),),
            ).fetchone()
            if row is not None:
                if (
                    not hmac.compare_digest(str(row["tenant_id"]), binding.tenant_id)
                    or not hmac.compare_digest(str(row["subject_id"]), binding.subject_id)
                    or not hmac.compare_digest(str(row["input_digest"]), binding.input_digest)
                ):
                    raise MailHubHostActionError(
                        "mailhub_host_action_idempotency_conflict", status_code=409
                    )
                if row["status"] == "succeeded" and row["encrypted_result"] is not None:
                    return (bytes(row["encrypted_result"]),)
                db.execute(
                    """UPDATE mailhub_host_action_operations
                       SET status='running', attempt_count=attempt_count+1,
                           error_code=NULL, updated_at=? WHERE action_id=?""",
                    (now, str(binding.action_id)),
                )
                return (None,)
            db.execute(
                """INSERT INTO mailhub_host_action_operations
                   (action_id, tenant_id, subject_id, input_digest, status,
                    encrypted_action, attempt_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'running', ?, 1, ?, ?)""",
                (
                    str(binding.action_id),
                    binding.tenant_id,
                    binding.subject_id,
                    binding.input_digest,
                    encrypted_action,
                    now,
                    now,
                ),
            )
            return (None,)

    def _complete_sync(
        self,
        action_id: str,
        input_digest: str,
        encrypted_result: bytes,
        now: float,
    ) -> None:
        with self._connect() as db:
            updated = db.execute(
                """UPDATE mailhub_host_action_operations
                   SET status='succeeded', encrypted_result=?, error_code=NULL, updated_at=?
                   WHERE action_id=? AND input_digest=? AND status='running'""",
                (encrypted_result, now, action_id, input_digest),
            ).rowcount
            if updated != 1:
                raise MailHubHostActionError(
                    "mailhub_host_action_completion_conflict", status_code=409
                )

    def _failure_sync(
        self,
        action_id: str,
        input_digest: str,
        error_code: str,
        now: float,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE mailhub_host_action_operations
                   SET status='retry_wait', error_code=?, updated_at=?
                   WHERE action_id=? AND input_digest=? AND status='running'""",
                (error_code, now, action_id, input_digest),
            )

    def _submit_knowledge_sync(
        self,
        knowledge_ref: str,
        tenant_id: str,
        subject_id: str,
        candidate_id: str,
        content_sha256: str,
        payload_sha256: str,
        encrypted_payload: bytes,
        now: float,
    ) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT content_sha256, payload_sha256
                   FROM mailhub_host_knowledge_candidates
                   WHERE tenant_id=? AND subject_id=? AND candidate_id=?""",
                (tenant_id, subject_id, candidate_id),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(
                    str(existing["content_sha256"]), content_sha256
                ) or not hmac.compare_digest(str(existing["payload_sha256"]), payload_sha256):
                    raise MailHubHostActionError(
                        "mailhub_knowledge_idempotency_conflict", status_code=409
                    )
                return False
            db.execute(
                """INSERT INTO mailhub_host_knowledge_candidates
                   (knowledge_ref, tenant_id, subject_id, candidate_id,
                    content_sha256, payload_sha256, encrypted_payload, status,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?)""",
                (
                    knowledge_ref,
                    tenant_id,
                    subject_id,
                    candidate_id,
                    content_sha256,
                    payload_sha256,
                    encrypted_payload,
                    now,
                    now,
                ),
            )
            return True


def _action_binding(action: dict[str, object]) -> MailHostActionBinding:
    raw_parameters = action.get("parameters")
    if not isinstance(raw_parameters, dict) or len(raw_parameters) > 20:
        raise ValueError("mailhub_host_action_parameters_invalid")
    try:
        action_id = UUID(str(action.get("action_id") or ""))
        candidate_id = UUID(str(raw_parameters.get("candidate_id") or ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("mailhub_host_action_identity_invalid") from exc
    action_type = str(action.get("action_type") or "").strip()
    if action_type not in {"create_task", "update_task", "knowledge_candidate"}:
        raise ValueError("mailhub_host_action_type_invalid")
    tenant_id = _scope(str(action.get("tenant_id") or ""), "tenant_id")
    subject_id = _scope(str(action.get("agent_subject_id") or ""), "subject_id")
    input_digest = str(action.get("input_digest") or "").strip()
    if len(input_digest) != 64 or any(char not in "0123456789abcdef" for char in input_digest):
        raise ValueError("mailhub_host_action_digest_invalid")
    revision = raw_parameters.get("candidate_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("mailhub_candidate_revision_invalid")
    return MailHostActionBinding(
        action_id=action_id,
        action_type=action_type,
        tenant_id=tenant_id,
        subject_id=subject_id,
        input_digest=input_digest,
        candidate_id=candidate_id,
        candidate_revision=revision,
        parameters={str(key): value for key, value in raw_parameters.items()},
    )


def _scope(value: str, field: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(ord(character) < 33 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError(f"mailhub_host_{field}_invalid")
    return normalized


def _reference(value: str) -> str:
    if (
        not value.startswith("mailapprove_")
        or len(value) > 512
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError("mailhub_host_approval_reference_invalid")
    return value


def _content_digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("mailhub_knowledge_content_digest_invalid")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("mailhub_host_action_json_invalid") from exc


def _iso_time(value: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(value, tz=UTC).isoformat()


__all__ = [
    "EncryptedSQLiteMailHostActionBroker",
    "MailHostActionBinding",
    "MailHostActionClaim",
    "MailHubHostActionError",
]
