"""TLS IMAP/SMTP adapter with bounded MIME parsing.

The adapter accepts only short-lived credential material supplied by
CredentialBrokerPort.  It never persists credentials and returns normalized
Provider DTOs to the application layer.
"""

from __future__ import annotations

import asyncio
import hashlib
import imaplib
import random
import re
import smtplib
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from uuid import uuid4

from mailhub.domain import MailboxConnection, ProviderName
from mailhub.errors import OutcomeUnknownError, ProviderFailureError
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
_MAX_MESSAGE_BYTES = 25 * 1024 * 1024


class ImapSmtpConnector(ProviderConnector):
    """Provider adapter for common TLS IMAP plus SMTP accounts."""

    def __init__(
        self,
        *,
        imap_host: str,
        smtp_host: str,
        imap_port: int = 993,
        smtp_port: int = 465,
        folder: str = "INBOX",
        timeout_seconds: float = 30.0,
        send_enabled: bool = False,
        max_connections: int = 2,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        if not 1 <= imap_port <= 65535 or not 1 <= smtp_port <= 65535:
            raise ValueError("port_invalid")
        if not 0.1 <= timeout_seconds <= 120:
            raise ValueError("timeout_invalid")
        if not 1 <= max_connections <= 16:
            raise ValueError("max_connections_invalid")
        if not 1 <= retry_attempts <= 5:
            raise ValueError("retry_attempts_invalid")
        if not 0.01 <= retry_backoff_seconds <= 30:
            raise ValueError("retry_backoff_invalid")
        self.imap_host = _host(imap_host, "imap_host")
        self.smtp_host = _host(smtp_host, "smtp_host")
        self.imap_port = imap_port
        self.smtp_port = smtp_port
        self.folder = _text(folder, "folder", 200)
        self.timeout_seconds = timeout_seconds
        self.send_enabled = send_enabled
        self.max_connections = max_connections
        self.retry_attempts = retry_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self._connection_gate = asyncio.Semaphore(max_connections)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=ProviderName.IMAP_SMTP,
            authorization_modes=("oauth2", "application_password"),
            supports_push=False,
            supports_incremental=True,
            supports_backfill=True,
            supports_draft=False,
            supports_send=self.send_enabled,
            supports_labels=True,
            # Attachment refs are retained for host review, but this connector
            # does not yet hydrate governed objects into MIME parts.
            supports_attachments=False,
            supports_search=False,
            notes=(
                "TLS required",
                "UIDVALIDITY cursor",
                "SMTP send requires explicit enable and application-password credential",
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
        if connection.provider is not ProviderName.IMAP_SMTP:
            raise ValueError("imap_connection_required")
        if not 1 <= limit <= 100:
            raise ValueError("limit_invalid")
        async with self._connection_gate:
            last_error: ProviderFailureError | None = None
            for attempt in range(self.retry_attempts):
                try:
                    if sync_filter is None:
                        return await asyncio.to_thread(
                            self._sync_blocking, credential, cursor, limit
                        )
                    return await asyncio.to_thread(
                        self._sync_blocking, credential, cursor, limit, sync_filter
                    )
                except ProviderFailureError as exc:
                    last_error = exc
                    if not _retryable_sync_error(exc.message) or attempt + 1 >= self.retry_attempts:
                        raise
                    delay = min(
                        30.0,
                        self.retry_backoff_seconds * (2**attempt) * random.uniform(0.8, 1.2),
                    )
                    await asyncio.sleep(delay)
            assert last_error is not None
            raise last_error

    async def send(
        self,
        request: ProviderSendRequest,
        *,
        credential: Mapping[str, str],
    ) -> ProviderSendReceipt:
        if not self.send_enabled:
            raise ProviderFailureError("smtp_send_capability_disabled")
        try:
            return await asyncio.to_thread(self._send_blocking, request, credential)
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            raise OutcomeUnknownError("smtp_outcome_unknown") from exc

    async def health_check(self, connection: MailboxConnection) -> Mapping[str, object]:
        return {
            "provider": connection.provider.value,
            "status": "configured",
            "imap_host": self.imap_host,
            "smtp_host": self.smtp_host,
        }

    def _sync_blocking(
        self,
        credential: Mapping[str, str],
        cursor: str | None,
        limit: int,
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        username = _credential(credential, "username")
        password = credential.get("password")
        access_token = credential.get("access_token")
        if not password and not access_token:
            raise ProviderFailureError("imap_credential_missing")
        active_filter = sync_filter or ProviderSyncFilter(folder_ref=self.folder)
        if active_filter.label_refs:
            # IMAP keyword semantics are not portable across providers.  Do
            # not silently broaden a bounded backfill when a server-specific
            # label mapping has not been certified.
            raise ProviderFailureError("imap_label_filter_unsupported")
        client: imaplib.IMAP4_SSL | None = None
        try:
            client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=self.timeout_seconds)
            if access_token:
                auth_string = f"user={username}\x01auth=Bearer {access_token}\x01\x01"
                client.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
            else:
                client.login(username, password or "")
            status, select_data = client.select(active_filter.folder_ref, readonly=True)
            if status != "OK":
                raise ProviderFailureError("imap_select_failed")
            uid_validity = _uid_validity(client, select_data)
            capability_names = _capability_names(client)
            highest_modseq = _highest_modseq(client) if "CONDSTORE" in capability_names else None
            saved_validity, last_uid, saved_modseq = _parse_cursor(cursor)
            if saved_validity is not None and saved_validity != uid_validity:
                last_uid = None
                saved_modseq = None
            criteria: list[str] = []
            if saved_modseq is not None and highest_modseq is not None:
                criteria.append(f"MODSEQ {saved_modseq + 1}")
            elif last_uid is None:
                criteria.append("ALL")
            else:
                criteria.append(f"UID {last_uid + 1}:*")
            if active_filter.received_after is not None:
                criteria.append(f"SINCE {_imap_date(active_filter.received_after)}")
            if active_filter.received_before is not None:
                criteria.append(f"BEFORE {_imap_date(active_filter.received_before)}")
            search_status, search_data = client.uid("search", "", *criteria)
            if search_status != "OK":
                raise ProviderFailureError("imap_search_failed")
            uids = [item for item in (search_data[0] or b"").split() if item.isdigit()][-limit:]
            messages: list[ProviderMessage] = []
            highest_uid = last_uid or 0
            for raw_uid in uids:
                uid = int(raw_uid)
                highest_uid = max(highest_uid, uid)
                fetch_status, fetch_data = client.uid("fetch", str(uid), "(RFC822)")
                if fetch_status != "OK":
                    raise ProviderFailureError("imap_fetch_failed")
                raw_message = _extract_rfc822(fetch_data)
                if len(raw_message) > _MAX_MESSAGE_BYTES:
                    continue
                messages.append(
                    _parse_message(raw_message, uid_validity, uid, active_filter.folder_ref)
                )
            next_cursor = f"{uid_validity}:{highest_uid}"
            if highest_modseq is not None:
                next_cursor = f"{next_cursor}:{max(highest_modseq, saved_modseq or 0)}"
            return ProviderSyncPage(
                messages=tuple(messages),
                next_cursor=next_cursor,
                provider_cursor_kind="imap_uidvalidity_uid",
                provider_request_id=f"imap-sync-{uuid4()}",
                reset_required=bool(cursor and not _same_uidvalidity(cursor, uid_validity)),
            )
        except ProviderFailureError:
            raise
        except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
            raise ProviderFailureError("imap_provider_unavailable") from exc
        finally:
            if client is not None:
                with suppress(imaplib.IMAP4.error, OSError):
                    client.logout()

    def _send_blocking(
        self,
        request: ProviderSendRequest,
        credential: Mapping[str, str],
    ) -> ProviderSendReceipt:
        username = _credential(credential, "username")
        password = credential.get("password")
        if not password:
            raise ProviderFailureError("smtp_password_required")
        message = EmailMessage()
        message["From"] = username
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
        with smtplib.SMTP_SSL(
            self.smtp_host, self.smtp_port, timeout=self.timeout_seconds
        ) as client:
            client.login(username, password)
            client.send_message(message)
        return ProviderSendReceipt(
            provider_message_ref=message["Message-ID"],
            provider_thread_ref=request.thread_ref,
            accepted_at=datetime.now(UTC),
            provider_request_id=f"smtp-{uuid4()}",
            idempotency_key=request.idempotency_key,
            content_sha256=request.content_sha256,
        )


def _parse_message(raw: bytes, uid_validity: str, uid: int, folder: str) -> ProviderMessage:
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    sender = _first_address(parsed.get("From", ""))
    recipients = tuple(address for _, address in getaddresses(parsed.get_all("To", [])) if address)
    cc_addresses = tuple(
        address for _, address in getaddresses(parsed.get_all("Cc", [])) if address
    )
    bcc_addresses = tuple(
        address for _, address in getaddresses(parsed.get_all("Bcc", [])) if address
    )
    reply_to_addresses = tuple(
        address for _, address in getaddresses(parsed.get_all("Reply-To", [])) if address
    )
    if not sender or not recipients:
        raise ProviderFailureError("imap_message_address_missing")
    body = _plain_body(parsed)
    message_id = parsed.get("Message-ID")
    references = (
        parsed.get("References")
        or parsed.get("In-Reply-To")
        or message_id
        or f"subject:{parsed.get('Subject', '')}"
    )
    subject = _decode_header(parsed.get("Subject", "(no subject)"))
    received = _parse_date(parsed.get("Date"))
    return ProviderMessage(
        provider_message_ref=f"{folder}:{uid_validity}:{uid}",
        provider_thread_ref=references.split()[0],
        internet_message_id=message_id,
        sender_address=sender,
        recipient_addresses=recipients,
        subject=subject,
        received_at=received,
        body_text=body,
        body_object_ref=None,
        content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        folder_ref=folder,
        attachment_count=min(
            100,
            sum(
                1
                for part in parsed.walk()
                if part.get_filename() or part.get_content_disposition() == "attachment"
            ),
        ),
        cc_addresses=tuple(address.lower() for address in cc_addresses),
        bcc_addresses=tuple(address.lower() for address in bcc_addresses),
        reply_to_addresses=tuple(address.lower() for address in reply_to_addresses),
    )


def _imap_date(value: datetime) -> str:
    """Format a UTC date for the portable IMAP SINCE/BEFORE criteria."""

    return value.astimezone(UTC).strftime("%d-%b-%Y")


def _plain_body(message: Message) -> str:
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if (
            part.get_content_type() != "text/plain"
            or part.get_content_disposition() == "attachment"
        ):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, "replace")[:_MAX_BODY_CHARS]
            except (LookupError, UnicodeError):
                continue
        if isinstance(payload, str):
            return payload[:_MAX_BODY_CHARS]
    return "[body_not_readable]"


def _extract_rfc822(fetch_data: list[object]) -> bytes:
    expected_length: int | None = None
    payload: bytes | None = None
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
            metadata = item[0]
            if isinstance(metadata, bytes):
                match = re.search(rb"\{(\d+)\}\)?\s*$", metadata)
                if match:
                    expected_length = int(match.group(1))
            payload = item[1]
            break
    if payload is not None:
        if expected_length is not None and len(payload) != expected_length:
            raise ProviderFailureError("imap_fetch_partial")
        return payload
    raise ProviderFailureError("imap_rfc822_missing")


def _first_text(value: list[bytes] | tuple[bytes, ...] | object) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, bytes) and item.isdigit():
                return item.decode("ascii")
    return "0"


def _uid_validity(client: imaplib.IMAP4_SSL, select_data: object) -> str:
    """Read the server UIDVALIDITY response code; never use message count."""

    try:
        status, data = client.response("UIDVALIDITY")
    except (imaplib.IMAP4.error, OSError):
        status, data = "", None
    if status == "OK":
        value = _first_text(data)
        if value != "0":
            return value
    # ``select`` data is normally the message count, not UIDVALIDITY.  Do not
    # treat that count as a cursor; a server that withholds the response code
    # is unsupported until a conformance adapter can obtain it safely.
    del select_data
    raise ProviderFailureError("imap_uidvalidity_missing")


def _last_uid(cursor: str | None, uid_validity: str) -> int | None:
    saved_validity, saved_uid, _ = _parse_cursor(cursor)
    if saved_validity != uid_validity:
        return None
    return saved_uid


def _same_uidvalidity(cursor: str | None, uid_validity: str) -> bool:
    saved_validity, _, _ = _parse_cursor(cursor)
    return saved_validity == uid_validity


def _parse_cursor(cursor: str | None) -> tuple[str | None, int | None, int | None]:
    if not cursor:
        return None, None, None
    parts = cursor.split(":")
    if len(parts) not in {2, 3}:
        raise ProviderFailureError("imap_cursor_invalid")
    try:
        uid = int(parts[1])
        modseq = int(parts[2]) if len(parts) == 3 else None
    except (TypeError, ValueError):
        raise ProviderFailureError("imap_cursor_invalid") from None
    if uid < 0 or (modseq is not None and modseq < 0):
        raise ProviderFailureError("imap_cursor_invalid")
    return parts[0], uid, modseq


def _capability_names(client: imaplib.IMAP4_SSL) -> frozenset[str]:
    try:
        status, data = client.capability()
    except (imaplib.IMAP4.error, OSError):
        return frozenset()
    if status != "OK" or not isinstance(data, (list, tuple)):
        return frozenset()
    values: set[str] = set()
    for item in data:
        if isinstance(item, bytes):
            values.update(token.decode("ascii", "ignore").upper() for token in item.split())
    return frozenset(values)


def _highest_modseq(client: imaplib.IMAP4_SSL) -> int | None:
    try:
        status, data = client.response("HIGHESTMODSEQ")
    except (imaplib.IMAP4.error, OSError):
        return None
    if status != "OK":
        return None
    value = _first_text(data)
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _retryable_sync_error(error_code: str) -> bool:
    return error_code in {
        "imap_provider_unavailable",
        "imap_select_failed",
        "imap_search_failed",
        "imap_fetch_failed",
        "imap_fetch_partial",
        "imap_rfc822_missing",
    }


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _decode_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))[:1000]
    except (LookupError, UnicodeError):
        return value[:1000]


def _first_address(value: str) -> str:
    return next((address for _, address in getaddresses([value]) if address), "").lower()


def _credential(credentials: Mapping[str, str], key: str) -> str:
    value = credentials.get(key)
    if not value or len(value) > 4096:
        raise ProviderFailureError(f"imap_{key}_missing")
    return value


def _host(value: str, field_name: str) -> str:
    return _text(value, field_name, 253).lower()


def _text(value: str, field_name: str, maximum: int) -> str:
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field_name}_invalid")
    return value.strip()
