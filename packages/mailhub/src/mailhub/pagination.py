"""Opaque, scope-bound cursors for metadata-only MailHub listings."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID


def thread_page_scope_digest(
    *,
    tenant_id: str,
    subject_id: str,
    connection_id: UUID | None,
    unread: bool,
    important: bool,
    has_attachment: bool,
    project: bool,
    candidate: bool,
) -> str:
    value = "|".join(
        (
            tenant_id,
            subject_id,
            str(connection_id) if connection_id is not None else "*",
            "1" if unread else "0",
            "1" if important else "0",
            "1" if has_attachment else "0",
            "1" if project else "0",
            "1" if candidate else "0",
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def encode_thread_cursor(*, latest_at: datetime, thread_id: UUID, scope_digest: str) -> str:
    if latest_at.tzinfo is None or latest_at.utcoffset() is None:
        raise ValueError("thread_cursor_datetime_naive")
    payload = {
        "v": 1,
        "latest_at": latest_at.astimezone(UTC).isoformat(),
        "thread_id": str(thread_id),
        "scope": scope_digest,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_thread_cursor(value: str, *, expected_scope: str) -> tuple[datetime, UUID]:
    if not value or len(value) > 512:
        raise ValueError("thread_cursor_invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError("thread_cursor_version_invalid")
        if payload.get("scope") != expected_scope:
            raise ValueError("thread_cursor_scope_mismatch")
        latest_at = datetime.fromisoformat(str(payload["latest_at"])).astimezone(UTC)
        thread_id = UUID(str(payload["thread_id"]))
    except ValueError as exc:
        if str(exc) == "thread_cursor_scope_mismatch":
            raise
        raise ValueError("thread_cursor_invalid") from exc
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("thread_cursor_invalid") from exc
    return latest_at, thread_id
