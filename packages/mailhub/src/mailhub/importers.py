"""Bounded, read-only EML/MBOX import for migration and contract testing.

This module deliberately has no database or Provider credentials.  It parses
local files into the same ``ProviderMessage`` shape used by the conformance
harness, but never claims push, incremental, send or live-mail capability.
"""

from __future__ import annotations

import hashlib
import mailbox
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from mailhub.ports import ProviderMessage, ProviderSyncPage

MAX_IMPORT_BYTES = 25 * 1024 * 1024
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_MESSAGES = 10_000
MAX_BODY_CHARS = 200_000


@dataclass(frozen=True, slots=True)
class ImportedMessage:
    message: ProviderMessage
    attachment_names: tuple[str, ...] = ()
    source_index: int = 0


@dataclass(frozen=True, slots=True)
class ReadOnlyMailboxImport:
    format: str
    source_name: str
    messages: tuple[ImportedMessage, ...]
    warnings: tuple[str, ...] = ()

    @property
    def provider_capabilities(self) -> tuple[str, ...]:
        return ("offline_read_only", "backfill_fixture", "metadata_and_bounded_text")

    def to_sync_page(self, *, cursor: str | None = None, limit: int = 100) -> ProviderSyncPage:
        """Expose one deterministic page without mutating the source file."""

        if not 1 <= limit <= 500:
            raise ValueError("import_limit_invalid")
        try:
            start = int(cursor or "0")
        except ValueError as exc:
            raise ValueError("import_cursor_invalid") from exc
        if start < 0 or start > len(self.messages):
            raise ValueError("import_cursor_invalid")
        selected = tuple(item.message for item in self.messages[start : start + limit])
        return ProviderSyncPage(
            messages=selected,
            next_cursor=str(start + len(selected)),
            provider_cursor_kind="offline_sequence",
            provider_request_id=f"offline-import:{self.source_name}:{start}",
        )


def import_eml(path: str | Path) -> ReadOnlyMailboxImport:
    source = _checked_path(path, suffixes=(".eml",))
    payload = source.read_bytes()
    if len(payload) > MAX_IMPORT_BYTES:
        raise ValueError("import_file_too_large")
    return _build_import("eml", source.name, (payload,))


def import_mbox(path: str | Path) -> ReadOnlyMailboxImport:
    source = _checked_path(path, suffixes=(".mbox",))
    if source.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("import_file_too_large")
    parsed: list[bytes] = []
    try:
        box = mailbox.mbox(str(source), create=False)
        try:
            for index, message in enumerate(box):
                if index >= MAX_MESSAGES:
                    raise ValueError("import_message_limit_exceeded")
                raw = message.as_bytes(policy=policy.default)
                if len(raw) > MAX_MESSAGE_BYTES:
                    continue
                parsed.append(raw)
        finally:
            box.close()
    except ValueError:
        raise
    except (OSError, mailbox.Error) as exc:
        raise ValueError("mbox_parse_failed") from exc
    return _build_import("mbox", source.name, parsed)


def _checked_path(path: str | Path, *, suffixes: tuple[str, ...]) -> Path:
    source = Path(path)
    if source.suffix.lower() not in suffixes:
        raise ValueError("import_format_unsupported")
    if not source.is_file():
        raise ValueError("import_source_not_found")
    return source


def _build_import(
    import_format: str, source_name: str, payloads: Iterable[bytes]
) -> ReadOnlyMailboxImport:
    imported: list[ImportedMessage] = []
    warnings: list[str] = []
    parser = BytesParser(policy=policy.default)
    for index, payload in enumerate(payloads):
        if index >= MAX_MESSAGES:
            warnings.append("message_limit_reached")
            break
        if len(payload) > MAX_MESSAGE_BYTES:
            warnings.append(f"message_{index}_too_large")
            continue
        try:
            parsed = parser.parsebytes(payload)
            imported.append(_normalize(parsed, source_index=index))
        except (LookupError, TypeError, ValueError):
            warnings.append(f"message_{index}_parse_failed")
    return ReadOnlyMailboxImport(
        format=import_format,
        source_name=source_name,
        messages=tuple(imported),
        warnings=tuple(warnings),
    )


def _normalize(message: object, *, source_index: int) -> ImportedMessage:
    # BytesParser always returns Message under the selected policy; keeping the
    # check explicit makes this boundary safe if a different parser is injected.
    if not isinstance(message, EmailMessage):
        raise TypeError("message_type_invalid")
    parsed = message
    sender = _first_address(parsed.get("From", ""))
    recipients = tuple(
        address
        for address in getaddresses([parsed.get("To", ""), parsed.get("Cc", "")])
        if address[1]
    )
    recipient_addresses = tuple(address for _, address in recipients)
    cc_addresses = tuple(
        address for _, address in getaddresses(parsed.get_all("Cc", [])) if address
    )
    bcc_addresses = tuple(
        address for _, address in getaddresses(parsed.get_all("Bcc", [])) if address
    )
    reply_to_addresses = tuple(
        address for _, address in getaddresses(parsed.get_all("Reply-To", [])) if address
    )
    subject = str(parsed.get("Subject", "(no subject)"))[:1_000]
    received_at = _date_or_now(parsed.get("Date"))
    body_parts: list[str] = []
    attachments: list[str] = []
    for part in parsed.walk():
        content_type = str(part.get_content_type()).lower()
        disposition = str(part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if filename or disposition == "attachment":
            attachments.append(str(filename or "unnamed-attachment")[:255])
            continue
        if content_type != "text/plain" or part.is_multipart():
            continue
        try:
            text = str(part.get_content())
        except (LookupError, UnicodeError):
            continue
        body_parts.append(text)
    body_text = "\n\n".join(body_parts).strip()[:MAX_BODY_CHARS] or None
    content_digest = hashlib.sha256((body_text or "").encode("utf-8")).hexdigest()
    internet_id = str(parsed.get("Message-ID", "")).strip() or None
    stable_ref = (
        internet_id
        or hashlib.sha256(
            f"{subject}\n{sender}\n{received_at.isoformat()}\n{content_digest}".encode()
        ).hexdigest()
    )
    provider_message = ProviderMessage(
        provider_message_ref=f"offline:{stable_ref}",
        provider_thread_ref=f"offline-thread:{_thread_key(subject, sender, recipient_addresses)}",
        internet_message_id=internet_id,
        sender_address=sender or "unknown@invalid",
        recipient_addresses=recipient_addresses,
        subject=subject or "(no subject)",
        received_at=received_at,
        body_text=body_text,
        body_object_ref=None,
        content_sha256=content_digest,
        labels=("offline_import",),
        folder_ref="OFFLINE_IMPORT",
        attachment_count=min(len(attachments), 100),
        cc_addresses=tuple(address.lower() for address in cc_addresses),
        bcc_addresses=tuple(address.lower() for address in bcc_addresses),
        reply_to_addresses=tuple(address.lower() for address in reply_to_addresses),
    )
    return ImportedMessage(
        message=provider_message,
        attachment_names=tuple(dict.fromkeys(attachments)),
        source_index=source_index,
    )


def _first_address(value: str) -> str:
    for _, address in getaddresses([value]):
        if address:
            return address.lower()
    return ""


def _date_or_now(value: object) -> datetime:
    if not isinstance(value, str):
        return datetime.now(UTC)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, IndexError, OverflowError):
        return datetime.now(UTC)


def _thread_key(subject: str, sender: str, recipients: tuple[str, ...]) -> str:
    normalized = " ".join(subject.lower().removeprefix("re:").split())
    return hashlib.sha256(
        f"{normalized}\n{sender.lower()}\n{','.join(sorted(recipients))}".encode()
    ).hexdigest()[:32]
