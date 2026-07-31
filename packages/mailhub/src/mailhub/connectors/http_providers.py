"""Official HTTP API adapters for Gmail and Microsoft Graph.

The connectors are deliberately thin: OAuth/refresh material comes from the
host CredentialBroker and is used only for the request.  The application layer
owns cursors, leases, idempotency, policy and durable operations.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from mailhub.domain import MailboxConnection, ProviderName
from mailhub.errors import OutcomeUnknownError, ProviderFailureError, RateLimitedError
from mailhub.ports import (
    ProviderCapabilities,
    ProviderConnector,
    ProviderMessage,
    ProviderSendReceipt,
    ProviderSendRequest,
    ProviderSyncFilter,
    ProviderSyncPage,
)

_MAX_BODY_CHARS = 200_000
_HTTP_SYNC_RETRY_ATTEMPTS = 3
_HTTP_SYNC_RETRY_BASE_SECONDS = 0.25
_HTTP_SYNC_RETRY_MAX_SECONDS = 30.0

_PROVIDER_RATE_LIMIT_REASONS = frozenset(
    {
        "ratelimitexceeded",
        "userratelimitexceeded",
        "dailylimitexceeded",
        "quotaexceeded",
        "toomanyrequests",
    }
)
_PROVIDER_PERMISSION_REASONS = frozenset(
    {
        "accessdenied",
        "erroraccessdenied",
        "forbidden",
        "insufficientpermissions",
        "invalidauthenticationcredentials",
        "invalidauthenticationtoken",
        "invalidcredentials",
        "invalidgrant",
        "invalidtoken",
        "unauthorized",
    }
)


class GmailConnector(ProviderConnector):
    def __init__(
        self,
        *,
        base_url: str = "https://gmail.googleapis.com",
        timeout_seconds: float = 30.0,
        read_only: bool = True,
        push_enabled: bool = False,
    ) -> None:
        self.base_url = _validate_provider_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self.read_only = read_only
        # Provider-specific watch creation and callback verification remain
        # behind the host-owned ProviderSubscriptionPort and
        # ProviderNotificationVerifierPort.  Enabling this flag therefore
        # advertises the already implemented A3 boundary; it never makes the
        # connector accept unverified provider callbacks directly.
        self.push_enabled = push_enabled

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=ProviderName.GMAIL,
            authorization_modes=("oauth2_authorization_code_pkce",),
            # Push receipt/subscription management is a separate activation
            # gate.  Do not advertise it merely because history sync exists.
            supports_push=self.push_enabled,
            supports_incremental=True,
            supports_backfill=True,
            # MailHub drafts are local; provider-side draft CRUD is not yet
            # implemented by this connector.
            supports_draft=False,
            # Real Gmail starts in read-only mode.  The send path remains
            # implemented for a separately approved outbound release, but it
            # must not be reachable through a read-only connection.
            supports_send=not self.read_only,
            supports_labels=True,
            # Attachment refs/AV handoff are deliberately not advertised until
            # the bounded attachment DTO and scanner contract are implemented.
            supports_attachments=False,
            # The public search API remains metadata-only until a separately
            # verified, scope-bound Provider search contract is released.
            supports_search=False,
            notes=(
                "historyId reconciliation required",
                "OAuth verification may be required",
                "provider search contract not enabled",
            ),
        )

    async def sync(
        self,
        connection: MailboxConnection,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        token = _access_token(credential)
        headers = {"Authorization": f"Bearer {token}"}
        active_filter = sync_filter or ProviderSyncFilter()
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            reset_required = False
            raw_messages: list[dict[str, str]] = []
            deleted_message_refs: list[str] = []
            history_id: str | None = None
            if cursor:
                try:
                    page_token: str | None = None
                    while len(raw_messages) < limit:
                        params: dict[str, str | int] = {
                            "startHistoryId": cursor,
                            "maxResults": min(limit - len(raw_messages), 100),
                        }
                        if page_token:
                            params["pageToken"] = page_token
                        payload = await _get_json(
                            client,
                            f"{self.base_url}/gmail/v1/users/me/history",
                            headers,
                            params,
                        )
                        history_id = _string_or_none(payload.get("historyId")) or history_id
                        history_refs, deleted_refs = _gmail_history_changes(
                            payload,
                            watched_labels=(active_filter.folder_ref, *active_filter.label_refs),
                        )
                        raw_messages.extend(history_refs)
                        for deleted_ref in deleted_refs:
                            if deleted_ref not in deleted_message_refs:
                                deleted_message_refs.append(deleted_ref)
                        page_token = _string_or_none(payload.get("nextPageToken"))
                        if not page_token:
                            break
                except ProviderFailureError as exc:
                    if exc.message.endswith("http_404"):
                        reset_required = True
                        raw_messages = []
                        history_id = None
                    else:
                        raise
            if not cursor or reset_required:
                # A stale historyId is a normal reconciliation path, not a
                # successful empty sync.  Rebuild a bounded inbox page and
                # advance the cursor from the provider profile below.
                page_token = None
                while len(raw_messages) < limit:
                    params = {"maxResults": min(limit - len(raw_messages), 100)}
                    query = _gmail_backfill_query(active_filter)
                    if query:
                        params["q"] = query
                    if page_token:
                        params["pageToken"] = page_token
                    payload = await _get_json(
                        client, f"{self.base_url}/gmail/v1/users/me/messages", headers, params
                    )
                    raw_messages.extend(_gmail_message_refs(payload))
                    page_token = _string_or_none(payload.get("nextPageToken"))
                    if not page_token:
                        break
            messages: list[ProviderMessage] = []
            seen_refs: set[str] = set()
            for item in raw_messages:
                message_ref = str(item.get("id", ""))
                if not message_ref:
                    continue
                if message_ref in deleted_message_refs:
                    # A deleted history entry has no fetchable message
                    # resource.  It is carried separately to the application
                    # deletion contract instead of causing a misleading 404.
                    continue
                if message_ref in seen_refs:
                    continue
                seen_refs.add(message_ref)
                full = await _get_json(
                    client,
                    f"{self.base_url}/gmail/v1/users/me/messages/{message_ref}",
                    headers,
                    {"format": "full"},
                )
                normalized = _gmail_message(full)
                if _matches_sync_filter(normalized, active_filter, folder_ref_is_label=True):
                    messages.append(normalized)
                if len(messages) >= limit:
                    break
            next_cursor = history_id
            if next_cursor is None and (not cursor or reset_required):
                profile = await _get_json(
                    client, f"{self.base_url}/gmail/v1/users/me/profile", headers, None
                )
                next_cursor = _string_or_none(profile.get("historyId"))
            if next_cursor is None and cursor and not reset_required:
                next_cursor = cursor
        return ProviderSyncPage(
            messages=tuple(messages),
            next_cursor=next_cursor,
            provider_cursor_kind="gmail_history_id",
            provider_request_id=f"gmail-sync-{uuid4()}",
            reset_required=reset_required,
            deleted_message_refs=tuple(deleted_message_refs),
        )

    async def send(
        self,
        request: ProviderSendRequest,
        *,
        credential: Mapping[str, str],
    ) -> ProviderSendReceipt:
        if self.read_only:
            raise ProviderFailureError("gmail_read_only_mode")
        token = _access_token(credential)
        message = _email_message(request)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        payload = {"raw": raw}
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/gmail/v1/users/me/messages/send",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise OutcomeUnknownError("gmail_send_outcome_unknown") from exc
        data = _response_json(response, operation="gmail_send", send=True)
        return ProviderSendReceipt(
            provider_message_ref=str(data.get("id", "")),
            provider_thread_ref=_string_or_none(data.get("threadId")),
            accepted_at=datetime.now(UTC),
            provider_request_id=response.headers.get("x-request-id", f"gmail-{uuid4()}"),
            idempotency_key=request.idempotency_key,
            content_sha256=request.content_sha256,
        )

    async def health_check(self, connection: MailboxConnection) -> Mapping[str, object]:
        return {
            "provider": connection.provider.value,
            "status": "configured",
            "base_url": self.base_url,
        }


class MicrosoftGraphConnector(ProviderConnector):
    def __init__(
        self,
        *,
        base_url: str = "https://graph.microsoft.com/v1.0",
        timeout_seconds: float = 30.0,
        read_only: bool = True,
        push_enabled: bool = False,
    ) -> None:
        self.base_url = _validate_provider_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self.read_only = read_only
        # Graph change notifications use the same host-owned subscription and
        # verifier boundary as Gmail Pub/Sub.  The connector only advertises
        # the capability; it does not bypass clientState/OIDC validation.
        self.push_enabled = push_enabled

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=ProviderName.MICROSOFT_GRAPH,
            authorization_modes=("oauth2_authorization_code_pkce_delegated",),
            supports_push=self.push_enabled,
            supports_incremental=True,
            supports_backfill=True,
            supports_draft=False,
            supports_send=not self.read_only,
            supports_labels=True,
            supports_attachments=False,
            # Delta sync is not a provider-search contract.  Keep the
            # capability false until query scope, paging and completeness are
            # verified independently from synchronization.
            supports_search=False,
            notes=(
                "delta query and lifecycle notification reconciliation required",
                "delegated permissions first",
                "provider search contract not enabled",
            ),
        )

    async def sync(
        self,
        connection: MailboxConnection,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        del connection
        token = _access_token(credential)
        headers = {"Authorization": f"Bearer {token}"}
        active_filter = sync_filter or ProviderSyncFilter()
        folder_path = _graph_folder_path(active_filter.folder_ref)
        url: str | None = (
            _safe_graph_cursor(cursor, self.base_url)
            if cursor
            else f"{self.base_url}/me/mailFolders/{folder_path}/messages/delta"
        )
        params: dict[str, str | int] = {
            "$top": min(limit, 100),
            "$select": (
                "id,conversationId,internetMessageId,from,toRecipients,"
                "ccRecipients,bccRecipients,replyTo,subject,receivedDateTime,"
                "body,categories,parentFolderId,isRead,hasAttachments,changeKey"
            ),
        }
        graph_filter = _graph_backfill_filter(active_filter)
        if graph_filter:
            params["$filter"] = graph_filter
        messages: list[ProviderMessage] = []
        deleted_message_refs: set[str] = set()
        delta_cursor: str | None = None
        continuation_cursor: str | None = None
        reset_required = False
        reset_attempted = False
        had_cursor = bool(cursor)
        first_page = True
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            while url and len(messages) < limit:
                try:
                    payload = await _get_json(
                        client, url, headers, params if first_page and not had_cursor else None
                    )
                except ProviderFailureError as exc:
                    # Graph delta tokens expire.  Rebuild from the inbox delta
                    # endpoint once, bounded by the requested limit, and make
                    # the reset explicit to the service/reconciliation layer.
                    # A second failure is surfaced instead of looping forever.
                    if (
                        had_cursor
                        and first_page
                        and not reset_attempted
                        and exc.message in {"provider_sync_http_404", "provider_sync_http_410"}
                    ):
                        reset_required = True
                        reset_attempted = True
                        had_cursor = False
                        first_page = True
                        url = f"{self.base_url}/me/mailFolders/{folder_path}/messages/delta"
                        continue
                    raise
                first_page = False
                for item in payload.get("value", []):
                    if not isinstance(item, dict):
                        continue
                    item_ref = item.get("id")
                    if "@removed" in item:
                        if isinstance(item_ref, str) and item_ref:
                            deleted_message_refs.add(item_ref)
                        continue
                    # Continue scanning the provider page after the message
                    # limit is reached so a later @removed record cannot be
                    # lost behind the bounded projection page.
                    if len(messages) >= limit:
                        continue
                    normalized = _graph_message(item)
                    if _matches_sync_filter(normalized, active_filter):
                        messages.append(normalized)
                next_link = _string_or_none(payload.get("@odata.nextLink"))
                if next_link:
                    safe_next_link = _safe_graph_cursor(next_link, self.base_url)
                    if len(messages) >= limit:
                        continuation_cursor = safe_next_link
                        url = None
                    else:
                        url = safe_next_link
                    continue
                delta_cursor = _string_or_none(payload.get("@odata.deltaLink"))
                url = None
        return ProviderSyncPage(
            messages=tuple(messages),
            next_cursor=continuation_cursor or delta_cursor,
            provider_cursor_kind="graph_delta_url",
            provider_request_id=f"graph-sync-{uuid4()}",
            reset_required=reset_required,
            deleted_message_refs=tuple(sorted(deleted_message_refs)),
        )

    async def send(
        self,
        request: ProviderSendRequest,
        *,
        credential: Mapping[str, str],
    ) -> ProviderSendReceipt:
        if self.read_only:
            raise ProviderFailureError("graph_read_only_mode")
        token = _access_token(credential)
        message = _email_message(request)
        payload = {
            "message": {
                "subject": request.subject,
                "body": {"contentType": "Text", "content": request.body_text},
                "toRecipients": [
                    {"emailAddress": {"address": address}}
                    for address in request.recipient_addresses
                ],
                "ccRecipients": [
                    {"emailAddress": {"address": address}} for address in request.cc_addresses
                ],
                "bccRecipients": [
                    {"emailAddress": {"address": address}} for address in request.bcc_addresses
                ],
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": message["Message-ID"]},
                    {"name": "X-MailHub-Idempotency-Key", "value": request.idempotency_key},
                ],
            },
            "saveToSentItems": True,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/me/sendMail",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise OutcomeUnknownError("graph_send_outcome_unknown") from exc
        _response_json(response, operation="graph_send", send=True, allow_empty=True)
        return ProviderSendReceipt(
            provider_message_ref=f"graph-operation:{request.operation_id}",
            provider_thread_ref=request.thread_ref,
            accepted_at=datetime.now(UTC),
            provider_request_id=response.headers.get("request-id", f"graph-{uuid4()}"),
            idempotency_key=request.idempotency_key,
            content_sha256=request.content_sha256,
        )

    async def health_check(self, connection: MailboxConnection) -> Mapping[str, object]:
        return {
            "provider": connection.provider.value,
            "status": "configured",
            "base_url": self.base_url,
        }


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    headers: Mapping[str, str],
    params: Mapping[str, str | int] | None,
) -> dict[str, Any]:
    last_error: ProviderFailureError | RateLimitedError | None = None
    for attempt in range(_HTTP_SYNC_RETRY_ATTEMPTS):
        try:
            response = await client.get(url, headers=headers, params=params)
            return _response_json(response, operation="provider_sync")
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = ProviderFailureError("provider_http_unavailable")
            if attempt + 1 >= _HTTP_SYNC_RETRY_ATTEMPTS:
                raise last_error from exc
        except (ProviderFailureError, RateLimitedError) as exc:
            if not _retryable_http_sync_error(exc) or attempt + 1 >= _HTTP_SYNC_RETRY_ATTEMPTS:
                raise
            last_error = exc
        if _retry_after_exceeds_cap(last_error):
            raise last_error
        await asyncio.sleep(_http_retry_delay(last_error, attempt))
    raise AssertionError("provider_sync_retry_exhausted")


def _response_json(
    response: httpx.Response,
    *,
    operation: str,
    send: bool = False,
    allow_empty: bool = False,
) -> dict[str, Any]:
    status_code = response.status_code
    provider_reason = (
        _provider_error_reason(response) if status_code < 200 or status_code >= 300 else None
    )
    error_details = _provider_error_details(response, provider_reason)
    if status_code == 429 or provider_reason in _PROVIDER_RATE_LIMIT_REASONS:
        raise RateLimitedError(
            f"{operation}_rate_limited",
            details=error_details,
        )
    if 500 <= status_code <= 599:
        if send:
            raise OutcomeUnknownError(f"{operation}_outcome_unknown", details=error_details)
        raise ProviderFailureError(f"{operation}_provider_failure", details=error_details)
    if status_code in (401, 403) and provider_reason in _PROVIDER_PERMISSION_REASONS:
        raise ProviderFailureError(f"{operation}_permission_denied", details=error_details)
    if status_code < 200 or status_code >= 300:
        raise ProviderFailureError(f"{operation}_http_{status_code}", details=error_details)
    if allow_empty or status_code == 202:
        return {}
    try:
        value = response.json()
    except ValueError as exc:
        raise ProviderFailureError(f"{operation}_invalid_json") from exc
    if not isinstance(value, dict):
        raise ProviderFailureError(f"{operation}_invalid_payload")
    return value


def _provider_error_reason(response: httpx.Response) -> str | None:
    """Return a bounded, normalized provider error reason/code.

    Gmail places a reason in ``error.errors[].reason`` while Graph generally
    uses ``error.code``.  Only the short machine-readable value is retained;
    provider messages and response bodies never enter MailHub error details.
    """

    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None

    error = payload.get("error")
    candidates: list[object] = []
    if isinstance(error, Mapping):
        nested = error.get("errors")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            candidates.extend(
                item.get("reason")
                for item in nested
                if isinstance(item, Mapping) and item.get("reason") is not None
            )
        candidates.extend(error.get(key) for key in ("code", "status", "reason"))
    elif isinstance(error, str):
        candidates.append(error)
    candidates.extend(payload.get(key) for key in ("reason", "code", "status"))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        normalized = re.sub(r"[^a-z0-9]", "", candidate.casefold())
        if normalized:
            return normalized[:96]
    return None


def _retryable_http_sync_error(error: ProviderFailureError | RateLimitedError) -> bool:
    if isinstance(error, RateLimitedError):
        return True
    status = error.details.get("provider_status")
    return error.message == "provider_http_unavailable" or (
        isinstance(status, int) and 500 <= status <= 599
    )


def _http_retry_delay(
    error: ProviderFailureError | RateLimitedError | None,
    attempt: int,
) -> float:
    retry_after = error.details.get("retry_after_seconds") if error is not None else None
    if isinstance(retry_after, int) and 0 <= retry_after <= 86_400:
        return min(float(retry_after), _HTTP_SYNC_RETRY_MAX_SECONDS)
    return float(
        min(
            _HTTP_SYNC_RETRY_MAX_SECONDS,
            _HTTP_SYNC_RETRY_BASE_SECONDS * (2**attempt),
        )
    )


def _retry_after_exceeds_cap(error: ProviderFailureError | RateLimitedError | None) -> bool:
    retry_after = error.details.get("retry_after_seconds") if error is not None else None
    return isinstance(retry_after, int) and retry_after > _HTTP_SYNC_RETRY_MAX_SECONDS


def _provider_error_details(
    response: httpx.Response,
    provider_reason: str | None,
) -> dict[str, Any]:
    details: dict[str, Any] = {"provider_status": response.status_code}
    if provider_reason is not None:
        details["provider_reason"] = provider_reason
    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    return details


def _gmail_message(value: Mapping[str, Any]) -> ProviderMessage:
    raw_payload = value.get("payload")
    payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    headers = {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in payload.get("headers", [])
        if isinstance(item, dict)
    }
    body = _gmail_body(payload)
    labels = tuple(str(item) for item in value.get("labelIds", []) if isinstance(item, str))
    provider_metadata: dict[str, str] = {}
    body_content_type = _gmail_body_content_type(payload)
    if body_content_type is not None:
        provider_metadata["body_content_type"] = body_content_type
    history_id = _string_or_none(value.get("historyId"))
    if history_id is not None and history_id.isdigit():
        provider_metadata["history_id"] = history_id
    sender = _first_address(headers.get("from", ""))
    recipients = _header_addresses(headers.get("to", ""))
    cc_addresses = _header_addresses(headers.get("cc", ""))
    bcc_addresses = _header_addresses(headers.get("bcc", ""))
    reply_to_addresses = _header_addresses(headers.get("reply-to", ""))
    if not sender or not recipients:
        raise ProviderFailureError("gmail_message_address_missing")
    return ProviderMessage(
        provider_message_ref=str(value.get("id", "")),
        provider_thread_ref=str(value.get("threadId", value.get("id", ""))),
        internet_message_id=headers.get("message-id"),
        sender_address=sender,
        recipient_addresses=recipients,
        subject=headers.get("subject", "(no subject)")[:1000],
        received_at=_parse_datetime(headers.get("date")),
        body_text=body,
        body_object_ref=None,
        content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        labels=labels,
        is_read="UNREAD" not in {label.upper() for label in labels},
        attachment_count=_gmail_attachment_count(payload),
        provider_metadata=provider_metadata,
        cc_addresses=cc_addresses,
        bcc_addresses=bcc_addresses,
        reply_to_addresses=reply_to_addresses,
    )


def _gmail_history_changes(
    value: Mapping[str, Any],
    *,
    watched_labels: Sequence[str] = ("INBOX",),
) -> tuple[list[dict[str, str]], list[str]]:
    refs: list[dict[str, str]] = []
    deleted_refs: list[str] = []
    history = value.get("history", [])
    if not isinstance(history, list):
        return refs, deleted_refs
    seen: set[str] = set()
    seen_deleted: set[str] = set()
    watched = {label.casefold() for label in watched_labels if isinstance(label, str)}
    for item in history:
        if not isinstance(item, dict):
            continue
        for key in ("messagesAdded", "messages", "labelsAdded", "labelsRemoved"):
            changes = item.get(key, [])
            if not isinstance(changes, list):
                continue
            for change in changes:
                message = change.get("message") if isinstance(change, dict) else change
                message_id = message.get("id") if isinstance(message, dict) else None
                if key == "labelsRemoved" and isinstance(change, dict):
                    removed_labels = change.get("labelIds", ())
                    if isinstance(removed_labels, list) and any(
                        isinstance(label, str) and label.casefold() in watched
                        for label in removed_labels
                    ):
                        if (
                            isinstance(message_id, str)
                            and message_id
                            and message_id not in seen_deleted
                        ):
                            deleted_refs.append(message_id)
                            seen_deleted.add(message_id)
                        continue
                if isinstance(message_id, str) and message_id and message_id not in seen:
                    refs.append({"id": message_id})
                    seen.add(message_id)
        deleted_changes = item.get("messagesDeleted", [])
        if isinstance(deleted_changes, list):
            for change in deleted_changes:
                message = change.get("message") if isinstance(change, dict) else change
                message_id = message.get("id") if isinstance(message, dict) else None
                if isinstance(message_id, str) and message_id and message_id not in seen_deleted:
                    deleted_refs.append(message_id)
                    seen_deleted.add(message_id)
    return refs, deleted_refs


def _gmail_history_message_refs(value: Mapping[str, Any]) -> list[dict[str, str]]:
    """Backward-compatible helper returning only fetchable history refs."""

    refs, _deleted_refs = _gmail_history_changes(value)
    return refs


def _gmail_message_refs(value: Mapping[str, Any]) -> list[dict[str, str]]:
    messages = value.get("messages", [])
    if not isinstance(messages, list):
        return []
    return [
        {"id": str(item.get("id"))}
        for item in messages
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("id")
    ]


def _gmail_max_history_id(
    value: Mapping[str, Any], _messages: Sequence[ProviderMessage]
) -> str | None:
    return _string_or_none(value.get("historyId"))


def _gmail_body(payload: Mapping[str, Any]) -> str:
    candidates: list[Mapping[str, Any]] = [payload]
    index = 0
    while index < len(candidates) and index < 128:
        part = candidates[index]
        nested = part.get("parts", [])
        if isinstance(nested, list):
            candidates.extend(item for item in nested if isinstance(item, dict))
        index += 1
    for part in candidates:
        if not isinstance(part, dict):
            continue
        if part.get("mimeType") != "text/plain":
            continue
        data = part.get("body", {}).get("data") if isinstance(part.get("body"), dict) else None
        if isinstance(data, str):
            try:
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                    "utf-8", "replace"
                )[:_MAX_BODY_CHARS]
            except (ValueError, UnicodeError):
                continue
    return "[body_not_readable]"


def _gmail_body_content_type(payload: Mapping[str, Any]) -> str | None:
    """Return the selected bounded body-part type without copying MIME data."""

    candidates: list[Mapping[str, Any]] = [payload]
    index = 0
    while index < len(candidates) and index < 128:
        part = candidates[index]
        mime_type = part.get("mimeType")
        if mime_type in {"text/plain", "text/html"}:
            return str(mime_type)
        nested = part.get("parts", [])
        if isinstance(nested, list):
            candidates.extend(item for item in nested if isinstance(item, dict))
        index += 1
    return None


def _gmail_attachment_count(payload: Mapping[str, Any]) -> int:
    """Count bounded attachment metadata without reading attachment bytes."""

    count = 0
    candidates: list[Mapping[str, Any]] = [payload]
    index = 0
    while index < len(candidates) and index < 128:
        part = candidates[index]
        if part.get("filename") or (
            isinstance(part.get("body"), dict) and part["body"].get("attachmentId")
        ):
            count += 1
        nested = part.get("parts", [])
        if isinstance(nested, list):
            candidates.extend(item for item in nested if isinstance(item, dict))
        index += 1
    return min(count, 100)


def _graph_message(value: Mapping[str, Any]) -> ProviderMessage:
    sender = _graph_address(value.get("from"))
    recipients = _graph_addresses(value.get("toRecipients"))
    cc_addresses = _graph_addresses(value.get("ccRecipients"))
    bcc_addresses = _graph_addresses(value.get("bccRecipients"))
    reply_to_addresses = _graph_addresses(value.get("replyTo"))
    body_value = value.get("body", {})
    body = (
        str(body_value.get("content", ""))[:_MAX_BODY_CHARS] if isinstance(body_value, dict) else ""
    )
    body = body or "[body_not_readable]"
    if not sender or not recipients:
        raise ProviderFailureError("graph_message_address_missing")
    labels = _graph_categories(value.get("categories"))
    provider_metadata: dict[str, str] = {}
    content_type = _graph_body_content_type(body_value)
    if content_type is not None:
        provider_metadata["body_content_type"] = content_type
    folder_id = _bounded_provider_value(value.get("parentFolderId"), "graph_folder_id")
    if folder_id is not None:
        provider_metadata["folder_id"] = folder_id
    change_key = _bounded_provider_value(value.get("changeKey"), "graph_change_key")
    if change_key is not None:
        provider_metadata["change_key"] = change_key
    return ProviderMessage(
        provider_message_ref=str(value.get("id", "")),
        provider_thread_ref=str(value.get("conversationId", value.get("id", ""))),
        internet_message_id=_string_or_none(value.get("internetMessageId")),
        sender_address=sender,
        recipient_addresses=recipients,
        subject=str(value.get("subject", "(no subject)"))[:1000],
        received_at=_parse_datetime(_string_or_none(value.get("receivedDateTime"))),
        body_text=body,
        body_object_ref=None,
        content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        labels=labels,
        is_read=bool(value.get("isRead", True)),
        attachment_count=1 if bool(value.get("hasAttachments", False)) else 0,
        provider_metadata=provider_metadata,
        cc_addresses=cc_addresses,
        bcc_addresses=bcc_addresses,
        reply_to_addresses=reply_to_addresses,
    )


def _graph_categories(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value[:100]:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if not normalized or len(normalized) > 200:
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ProviderFailureError("graph_category_invalid")
        label = f"category:{normalized}"
        if label not in seen:
            result.append(label)
            seen.add(label)
    return tuple(result)


def _graph_body_content_type(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("contentType")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().casefold()
    if normalized in {"text", "html"}:
        return normalized
    return "other" if normalized else None


def _matches_sync_filter(
    message: ProviderMessage,
    sync_filter: ProviderSyncFilter,
    *,
    folder_ref_is_label: bool = False,
) -> bool:
    """Apply the bounded portion that cannot be expressed by every provider."""

    if sync_filter.folder_ref.casefold() != "inbox":
        if folder_ref_is_label:
            if sync_filter.folder_ref not in message.labels:
                return False
        else:
            actual_folder = message.provider_metadata.get("folder_id", message.folder_ref)
            if actual_folder != sync_filter.folder_ref:
                return False
    if sync_filter.label_refs and not set(sync_filter.label_refs).issubset(set(message.labels)):
        return False
    if sync_filter.received_after is not None and message.received_at < sync_filter.received_after:
        return False
    return not (
        sync_filter.received_before is not None
        and message.received_at >= sync_filter.received_before
    )


def _gmail_backfill_query(sync_filter: ProviderSyncFilter) -> str:
    terms: list[str] = []
    if sync_filter.received_after is not None:
        terms.append(f"after:{int(sync_filter.received_after.timestamp())}")
    if sync_filter.received_before is not None:
        terms.append(f"before:{int(sync_filter.received_before.timestamp())}")
    if sync_filter.folder_ref.casefold() != "inbox":
        terms.append(_gmail_label_term(sync_filter.folder_ref))
    for label in sync_filter.label_refs:
        terms.append(_gmail_label_term(label))
    return " ".join(terms)


def _gmail_label_term(label: str) -> str:
    """Build one quoted Gmail label predicate from a validated ref."""

    if any(char in label for char in ('"', "\\", "{", "}", "(", ")")):
        raise ProviderFailureError("gmail_label_filter_invalid")
    return f'label:"{label}"'


def _graph_folder_path(folder_ref: str) -> str:
    if folder_ref.casefold() == "inbox":
        return "inbox"
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", folder_ref):
        raise ProviderFailureError("graph_folder_filter_invalid")
    return quote(folder_ref, safe="")


def _graph_backfill_filter(sync_filter: ProviderSyncFilter) -> str | None:
    clauses: list[str] = []
    if sync_filter.received_after is not None:
        clauses.append(
            "receivedDateTime ge "
            f"'{sync_filter.received_after.astimezone(UTC).isoformat().replace('+00:00', 'Z')}'"
        )
    if sync_filter.received_before is not None:
        clauses.append(
            "receivedDateTime lt "
            f"'{sync_filter.received_before.astimezone(UTC).isoformat().replace('+00:00', 'Z')}'"
        )
    for label in sync_filter.label_refs:
        if not label.casefold().startswith("category:"):
            raise ProviderFailureError("graph_label_filter_unsupported")
        category = label.split(":", 1)[1]
        if not category:
            raise ProviderFailureError("graph_label_filter_invalid")
        escaped = category.replace("'", "''")
        clauses.append(f"categories/any(c:c eq '{escaped}')")
    return " and ".join(clauses) or None


def _bounded_provider_value(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 2_000:
        raise ProviderFailureError(f"{field_name}_invalid")
    normalized = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ProviderFailureError(f"{field_name}_invalid")
    return normalized


def _email_message(request: ProviderSendRequest) -> EmailMessage:
    message = EmailMessage()
    message["To"] = ", ".join(request.recipient_addresses)
    if request.cc_addresses:
        message["Cc"] = ", ".join(request.cc_addresses)
    if request.bcc_addresses:
        message["Bcc"] = ", ".join(request.bcc_addresses)
    message["Subject"] = request.subject
    message["Message-ID"] = f"<mailhub-{request.operation_id}@mailhub.invalid>"
    if request.in_reply_to_message_ref:
        message["In-Reply-To"] = request.in_reply_to_message_ref
        message["References"] = request.in_reply_to_message_ref
    message.set_content(request.body_text)
    return message


def _access_token(credentials: Mapping[str, str]) -> str:
    token = credentials.get("access_token")
    if not token or len(token) > 8192 or any(ord(char) < 33 for char in token):
        raise ProviderFailureError("oauth_access_token_missing")
    return token


def _first_address(value: str) -> str:
    if "<" in value and ">" in value:
        value = value.split("<", 1)[1].split(">", 1)[0]
    return value.strip().lower()


def _header_addresses(value: str | None) -> tuple[str, ...]:
    """Normalize an RFC 5322 address header without retaining display names."""

    if not value:
        return ()
    return tuple(
        address.strip().lower()
        for _display_name, address in getaddresses([value])
        if address.strip()
    )


def _graph_address(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    address = value.get("emailAddress", {})
    return str(address.get("address", "")).strip().lower() if isinstance(address, dict) else ""


def _graph_addresses(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(address for item in value if (address := _graph_address(item)))


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    raw_value = value.strip()
    if not raw_value:
        return None
    try:
        seconds = int(float(raw_value))
    except (OverflowError, ValueError):
        try:
            retry_at = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = int((retry_at - datetime.now(UTC)).total_seconds())
    return max(0, min(seconds, 86_400))


def _safe_graph_cursor(cursor: str, base_url: str) -> str:
    try:
        parsed = urlsplit(cursor)
        base = urlsplit(base_url)
        parsed_port = parsed.port
        base_port = base.port
    except ValueError as exc:
        raise ProviderFailureError("graph_delta_cursor_origin_invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != base.hostname
        or parsed.username
        or parsed.password
        or parsed_port != base_port
        or parsed.fragment
    ):
        raise ProviderFailureError("graph_delta_cursor_origin_invalid")
    if len(cursor) > 8000:
        raise ProviderFailureError("graph_delta_cursor_too_large")
    base_path = base.path.rstrip("/")
    if base_path and not parsed.path.startswith(f"{base_path}/"):
        raise ProviderFailureError("graph_delta_cursor_path_invalid")
    return cursor


def _validate_provider_base_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if (
        (parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"})
        or not parsed.netloc
        or parsed.username
        or parsed.fragment
    ):
        raise ValueError("provider_base_url_invalid")
    if len(value) > 2000:
        raise ValueError("provider_base_url_too_large")
    return value.rstrip("/")
