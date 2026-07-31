"""Product-owned JSON handlers for the MailHub Agentctl capabilities.

The handlers are intentionally thin HTTP adapters.  MailHub remains the fact
owner for mailbox state, policy, drafts, outbox and outcome reconciliation;
Agentctl supplies tenant/scope/approval/idempotency governance around these
commands.  Missing endpoint configuration is a hard failure, never a fake
success.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from typing import Any
from urllib import error, request
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID


class MailHubHandlerError(RuntimeError):
    """Stable, redacted MailHub handler failure."""


async def mail_connection_list(invocation: dict[str, Any]) -> dict[str, Any]:
    """List only authorized connection metadata; never return credential refs."""

    evidence = _evidence(invocation)
    payload = await _mailhub_json("GET", "/v1/mail/connections", invocation, None)
    value = payload.get("data", [])
    connections = (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )
    safe_connections = [
        {
            key: item[key]
            for key in (
                "connection_id",
                "provider",
                "email_address",
                "status",
                "revision",
                "granted_scopes",
                "content_mode",
            )
            if key in item
        }
        for item in connections
    ]
    return {"connections": safe_connections, "evidence": evidence}


async def mail_thread_list(invocation: dict[str, Any]) -> dict[str, Any]:
    """Read metadata-only thread projections for an authorized mailbox."""

    arguments = _arguments(invocation)
    limit = arguments.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise MailHubHandlerError("limit_invalid")
    query: dict[str, str] = {"limit": str(limit)}
    cursor = arguments.get("cursor")
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 512:
            raise MailHubHandlerError("cursor_invalid")
        query["cursor"] = cursor
    connection_id = arguments.get("connection_id")
    if connection_id is not None:
        query["connection_id"] = _required_uuid(arguments, "connection_id")
    for key in (
        "unread",
        "important",
        "attachment",
        "has_attachment",
        "project",
        "candidate",
    ):
        value = arguments.get(key)
        if value is not None:
            if not isinstance(value, bool):
                raise MailHubHandlerError(f"{key}_invalid")
            if value:
                # The API accepts both names for attachment filters.  Emit the
                # canonical spelling so a host cannot create ambiguous scopes.
                query["attachment" if key == "has_attachment" else key] = "true"
    evidence = _evidence(invocation)
    payload = await _mailhub_json(
        "GET", f"/v1/mail/threads?{urlencode(query)}", invocation, None
    )
    value = payload.get("data", [])
    threads = (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )
    return {
        "threads": threads,
        "next_cursor": payload.get("next_cursor"),
        "has_more": payload.get("has_more", False),
        "filters": payload.get("filters", query),
        "evidence": evidence,
    }


async def mail_message_search(invocation: dict[str, Any]) -> dict[str, Any]:
    """Search metadata only; body hydration remains a separate governed action."""

    arguments = _arguments(invocation)
    query = _required(arguments, "query")
    if len(query) > 200:
        raise MailHubHandlerError("query_invalid")
    limit = arguments.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise MailHubHandlerError("limit_invalid")
    mode = arguments.get("mode", "metadata")
    if mode not in {"metadata", "provider"}:
        raise MailHubHandlerError("mode_invalid")
    evidence = _evidence(invocation)
    payload = await _mailhub_json(
        "GET",
        f"/v1/mail/search?q={quote(query, safe='')}&limit={limit}&mode={mode}",
        invocation,
        None,
    )
    value = payload.get("data", [])
    messages = (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )
    return {
        "messages": messages,
        "mode": payload.get("mode", mode),
        "complete": payload.get("complete", False),
        "coverage": payload.get("coverage", []),
        "incomplete_reason": payload.get("incomplete_reason"),
        "evidence": evidence,
    }


async def mail_message_analyze(invocation: dict[str, Any]) -> dict[str, Any]:
    arguments = _arguments(invocation)
    message_id = _required_uuid(arguments, "message_id")
    evidence = _evidence(invocation)
    payload = await _mailhub_json(
        "POST",
        f"/v1/mail/messages/{message_id}:analyze",
        invocation,
        None,
    )
    return {"analysis": payload.get("data", {}), "evidence": evidence}


async def mail_candidate_review(invocation: dict[str, Any]) -> dict[str, Any]:
    arguments = _arguments(invocation)
    candidate_id = _required_uuid(arguments, "candidate_id")
    evidence = _evidence(invocation)
    approved = arguments.get("approved")
    if not isinstance(approved, bool):
        raise MailHubHandlerError("approved_invalid")
    expected_revision = arguments.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise MailHubHandlerError("expected_revision_invalid")
    review_reason = arguments.get("review_reason")
    if review_reason is not None and (
        not isinstance(review_reason, str)
        or not review_reason.strip()
        or len(review_reason) > 500
    ):
        raise MailHubHandlerError("review_reason_invalid")
    if not approved and review_reason is None:
        raise MailHubHandlerError("candidate_rejection_reason_required")
    payload = await _mailhub_json(
        "POST",
        f"/v1/mail/candidates/{candidate_id}:review",
        invocation,
        {
            "approved": approved,
            "expected_revision": expected_revision,
            "review_reason": review_reason,
        },
    )
    return {"candidate": payload.get("data", {}), "evidence": evidence}


async def mail_candidate_apply(invocation: dict[str, Any]) -> dict[str, Any]:
    arguments = _arguments(invocation)
    candidate_id = _required_uuid(arguments, "candidate_id")
    approval_ref = _required(arguments, "approval_ref")
    evidence = _evidence(invocation)
    payload = await _mailhub_json(
        "POST",
        f"/v1/mail/candidates/{candidate_id}:apply",
        invocation,
        {"approval_ref": approval_ref},
    )
    return {"result": payload.get("data", {}), "evidence": evidence}


async def mail_reply_draft(invocation: dict[str, Any]) -> dict[str, Any]:
    arguments = _arguments(invocation)
    evidence = _evidence(invocation)
    body = {
        "connection_id": _required_uuid(arguments, "connection_id"),
        "thread_id": _optional_uuid(arguments, "thread_id"),
        "recipient_addresses": _string_list(arguments, "recipient_addresses", 50, 320),
        "cc_addresses": _string_list(arguments, "cc_addresses", 50, 320),
        "bcc_addresses": _string_list(arguments, "bcc_addresses", 50, 320),
        "attachment_refs": _string_list(arguments, "attachment_refs", 20, 1000),
        "subject": _required(arguments, "subject"),
        "body_text": _required(arguments, "body_text"),
    }
    payload = await _mailhub_json("POST", "/v1/mail/drafts", invocation, body)
    return {"draft": payload.get("data", {}), "evidence": evidence}


async def mail_reply_send(invocation: dict[str, Any]) -> dict[str, Any]:
    arguments = _arguments(invocation)
    draft_id = _required_uuid(arguments, "draft_id")
    evidence = _evidence(invocation)
    body = {
        "expected_revision": _required_int(arguments, "expected_revision"),
        "expected_content_sha256": _required_digest(
            arguments, "expected_content_sha256"
        ),
        "expected_recipient_digest": _required_digest(
            arguments, "expected_recipient_digest"
        ),
        "confirmation_ref": _required(arguments, "confirmation_ref"),
        "policy_id": _optional_uuid(arguments, "policy_id"),
        "grant_id": _optional_uuid(arguments, "grant_id"),
        "agent_subject_id": arguments.get("agent_subject_id"),
    }
    payload = await _mailhub_json(
        "POST", f"/v1/mail/drafts/{draft_id}:send", invocation, body
    )
    return {"operation": payload.get("data", {}), "evidence": evidence}


async def mail_sync_enqueue(invocation: dict[str, Any]) -> dict[str, Any]:
    """Queue a durable sync job; provider I/O stays in the MailHub worker."""

    arguments = _arguments(invocation)
    connection_id = _required_uuid(arguments, "connection_id")
    mode = str(arguments.get("mode") or "incremental")
    if mode not in {"incremental", "backfill", "reconcile"}:
        raise MailHubHandlerError("mode_invalid")
    limit = arguments.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise MailHubHandlerError("limit_invalid")
    filter_body = _sync_job_filter(arguments, mode)
    evidence = _evidence(invocation)
    payload = await _mailhub_json(
        "POST",
        f"/v1/mail/connections/{connection_id}/sync-jobs",
        invocation,
        {"mode": mode, "limit": limit, **filter_body},
    )
    return {"job": payload.get("data", {}), "evidence": evidence}


async def mail_autonomy_enqueue(invocation: dict[str, Any]) -> dict[str, Any]:
    """Queue one bounded, recommendation-only owner-scoped mail cycle."""

    arguments = _arguments(invocation)
    connection_id = _required_uuid(arguments, "connection_id")
    limit = arguments.get("limit", 50)
    message_limit = arguments.get("message_limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise MailHubHandlerError("limit_invalid")
    if (
        not isinstance(message_limit, int)
        or isinstance(message_limit, bool)
        or not 1 <= message_limit <= 200
    ):
        raise MailHubHandlerError("message_limit_invalid")
    replay_key = arguments.get("replay_key")
    if (
        not isinstance(replay_key, str)
        or not replay_key.strip()
        or len(replay_key) > 200
    ):
        raise MailHubHandlerError("replay_key_invalid")
    evidence = _evidence(invocation)
    payload = await _mailhub_json(
        "POST",
        "/v1/mail/autonomy/runs",
        invocation,
        {
            "connection_id": str(connection_id),
            "limit": limit,
            "message_limit": message_limit,
            "replay_key": replay_key,
            "run_inline": False,
        },
    )
    return {
        "job": payload.get("data", {}),
        "mode": "recommend_only",
        "evidence": evidence,
    }


async def mail_rule_execute(invocation: dict[str, Any]) -> dict[str, Any]:
    """Run a bounded rule dry-run or explicitly authorized L3A execution."""

    arguments = _arguments(invocation)
    rule_id = _required_uuid(arguments, "rule_id")
    message_ids = _uuid_list(arguments, "message_ids", 100)
    dry_run = arguments.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise MailHubHandlerError("dry_run_invalid")
    policy_id = _optional_uuid(arguments, "policy_id")
    grant_id = _optional_uuid(arguments, "grant_id")
    evidence = _evidence(invocation)
    payload = await _mailhub_json(
        "POST",
        f"/v1/mail/rules/{rule_id}:execute",
        invocation,
        {
            "message_ids": message_ids,
            "policy_id": policy_id,
            "grant_id": grant_id,
            "dry_run": dry_run,
        },
    )
    return {"executions": payload.get("data", []), "evidence": evidence}


def _arguments(invocation: dict[str, Any]) -> dict[str, Any]:
    value = invocation.get("validated_arguments")
    return dict(value) if isinstance(value, dict) else {}


def _required(arguments: dict[str, Any], key: str) -> str:
    value = str(arguments.get(key) or "").strip()
    if not value or len(value) > 2048:
        raise MailHubHandlerError(f"{key}_missing")
    return value


def _required_uuid(arguments: dict[str, Any], key: str) -> str:
    value = _required(arguments, key)
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise MailHubHandlerError(f"{key}_invalid") from exc


def _optional_uuid(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MailHubHandlerError(f"{key}_invalid")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise MailHubHandlerError(f"{key}_invalid") from exc


def _uuid_list(arguments: dict[str, Any], key: str, maximum: int) -> list[str]:
    value = arguments.get(key)
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= maximum:
        raise MailHubHandlerError(f"{key}_invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MailHubHandlerError(f"{key}_invalid")
        try:
            result.append(str(UUID(item)))
        except ValueError as exc:
            raise MailHubHandlerError(f"{key}_invalid") from exc
    if len(set(result)) != len(result):
        raise MailHubHandlerError(f"{key}_duplicate")
    return result


def _required_int(arguments: dict[str, Any], key: str) -> int:
    value = arguments.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MailHubHandlerError(f"{key}_invalid")
    return value


def _required_digest(arguments: dict[str, Any], key: str) -> str:
    value = _required(arguments, key).lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise MailHubHandlerError(f"{key}_invalid")
    return value


def _string_list(
    arguments: dict[str, Any], key: str, maximum: int, item_maximum: int
) -> list[str]:
    value = arguments.get(key, [])
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise MailHubHandlerError(f"{key}_invalid")
    result = [item.strip() for item in value if isinstance(item, str)]
    if len(result) != len(value) or any(
        not item or len(item) > item_maximum for item in result
    ):
        raise MailHubHandlerError(f"{key}_invalid")
    return result


def _sync_job_filter(arguments: dict[str, Any], mode: str) -> dict[str, Any]:
    """Validate the narrow agent-facing backfill filter contract."""

    folder_ref = arguments.get("folder_ref", "INBOX")
    if (
        not isinstance(folder_ref, str)
        or not folder_ref.strip()
        or len(folder_ref) > 200
        or any(ord(char) < 33 or ord(char) == 127 for char in folder_ref)
    ):
        raise MailHubHandlerError("folder_ref_invalid")
    folder_ref = folder_ref.strip()
    label_refs = arguments.get("label_refs", [])
    if (
        not isinstance(label_refs, (list, tuple))
        or len(label_refs) > 20
        or any(
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 200
            or any(ord(char) < 33 or ord(char) == 127 for char in label)
            for label in label_refs
        )
    ):
        raise MailHubHandlerError("label_refs_invalid")
    labels = [label.strip() for label in label_refs]
    if len(set(labels)) != len(labels):
        raise MailHubHandlerError("label_refs_duplicate")
    after = _optional_sync_datetime(arguments, "received_after")
    before = _optional_sync_datetime(arguments, "received_before")
    if after is not None and before is not None:
        after_dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
        before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
        if after_dt >= before_dt:
            raise MailHubHandlerError("received_range_invalid")
    bounded = bool(
        folder_ref.casefold() != "inbox" or labels or after is not None or before is not None
    )
    if bounded and mode != "backfill":
        raise MailHubHandlerError("sync_filter_requires_backfill")
    body: dict[str, Any] = {"folder_ref": folder_ref, "label_refs": labels}
    if after is not None:
        body["received_after"] = after
    if before is not None:
        body["received_before"] = before
    return body


def _optional_sync_datetime(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise MailHubHandlerError(f"{key}_invalid")
    text = value.strip()
    if any(ord(char) < 33 or ord(char) == 127 for char in text):
        raise MailHubHandlerError(f"{key}_invalid")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise MailHubHandlerError(f"{key}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MailHubHandlerError(f"{key}_must_be_aware")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _evidence(invocation: dict[str, Any]) -> dict[str, str]:
    trace_id = str(invocation.get("trace_id") or "").strip()
    idempotency_key = str(invocation.get("idempotency_key") or "").strip()
    if not trace_id or not idempotency_key:
        raise MailHubHandlerError("trace_or_idempotency_missing")
    return {"trace_id": trace_id, "idempotency_key": idempotency_key}


async def _mailhub_json(
    method: str,
    path: str,
    invocation: dict[str, Any],
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    base_url = _validated_base_url(str(os.environ.get("MAILHUB_BASE_URL") or ""))
    if not path.startswith("/") or "//" in path or ".." in path.split("/"):
        raise MailHubHandlerError("mailhub_endpoint_not_configured")
    tenant_id = str(invocation.get("tenant_id") or "").strip()
    subject_id = str(
        invocation.get("subject_id") or invocation.get("actor_id") or ""
    ).strip()
    if not tenant_id or not subject_id:
        raise MailHubHandlerError("host_identity_missing")
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-MailHub-Tenant": tenant_id,
        "X-MailHub-Subject": subject_id,
        "X-Trace-Id": str(invocation.get("trace_id") or ""),
        "Idempotency-Key": str(invocation.get("idempotency_key") or ""),
    }

    def call() -> dict[str, Any]:
        try:
            with request.urlopen(
                request.Request(
                    base_url + path, data=payload, headers=headers, method=method
                ),
                timeout=30,
            ) as response:
                raw = response.read(2_000_000)
        except (error.HTTPError, error.URLError, TimeoutError) as exc:
            raise MailHubHandlerError("mailhub_request_failed") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MailHubHandlerError("mailhub_response_invalid") from exc
        if not isinstance(value, dict):
            raise MailHubHandlerError("mailhub_response_invalid")
        return value

    return await asyncio.to_thread(call)


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold()
    local_http = parsed.scheme == "http" and hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if (
        not hostname
        or (parsed.scheme != "https" and not local_http)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise MailHubHandlerError("mailhub_endpoint_not_configured")
    return value.rstrip("/")
