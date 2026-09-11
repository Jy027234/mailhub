"""Answer whether an outbound message reached a mailbox.

MailHub asks this only for sends whose outcome is unknown, and it decides what
to do from the answer, so the three-state contract matters more than the search
itself:

* found True  -- the Message-ID is in a mailbox we could read;
* found False -- we searched every configured folder successfully and it is in
  none of them;
* found None  -- we could not tell.  A folder we could not open, a server we
  could not reach, no credentials to log in with.  None must never be collapsed
  into False: MailHub treats absent as "safe to send again".

Sent mail usually does not live in INBOX, so the folders are configurable
(HOST_OUTBOUND_FOLDERS) and a partial search is reported as indeterminate.
"""

from __future__ import annotations

import imaplib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any


def _uids(payload: object) -> list[str]:
    if not isinstance(payload, (list, tuple)) or not payload:
        return []
    raw = payload[0]
    if not isinstance(raw, bytes):
        return []
    return [token.decode("ascii", "ignore") for token in raw.split() if token.isdigit()]


def observe_outbound_in_mailbox(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    folders: Sequence[str],
    internet_message_id: str,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Search the configured folders for one Message-ID."""

    if not host or not username or not password:
        return {"found": None, "detail": "host_imap_not_configured"}
    if not folders:
        return {"found": None, "detail": "host_outbound_folders_empty"}
    client: imaplib.IMAP4_SSL | None = None
    searched: list[str] = []
    unreadable: list[str] = []
    try:
        client = imaplib.IMAP4_SSL(host, port, timeout=timeout_seconds)
        client.login(username, password)
        for folder in folders:
            status, _ = client.select(folder, readonly=True)
            if status != "OK":
                unreadable.append(folder)
                continue
            try:
                status, data = client.uid(
                    "search", None, "HEADER", "Message-ID", internet_message_id
                )
            except imaplib.IMAP4.error:
                unreadable.append(folder)
                continue
            if status != "OK":
                unreadable.append(folder)
                continue
            searched.append(folder)
            uids = _uids(data)
            if uids:
                return {
                    "found": True,
                    "provider_message_ref": internet_message_id,
                    "mailbox": folder,
                    "detail": "uid " + uids[-1],
                }
    except (imaplib.IMAP4.error, OSError) as exc:
        return {"found": None, "detail": type(exc).__name__}
    finally:
        if client is not None:
            with suppress(imaplib.IMAP4.error, OSError):
                client.logout()

    if unreadable or not searched:
        # Something could not be read, so "not in any folder" is not a finding.
        return {
            "found": None,
            "detail": "folders_unreadable:" + ",".join(unreadable or list(folders)),
        }
    return {"found": False, "mailbox": ", ".join(searched), "detail": "no match"}


def folder_list(raw: str, default: Sequence[str] = ("INBOX",)) -> tuple[str, ...]:
    """Parse a comma-separated folder setting, falling back to the default."""

    folders = tuple(item.strip() for item in raw.split(",") if item.strip())
    return folders or tuple(default)


def credential_fields(resolved: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """Pull (username, password) out of a resolved credential mapping."""

    if not isinstance(resolved, Mapping):
        return None
    username = resolved.get("username")
    password = resolved.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    if not username or not password:
        return None
    return username, password
