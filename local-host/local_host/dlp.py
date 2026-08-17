"""Bounded local DLP checks for the host knowledge safety gate.

Real pattern-based detection for Chinese ID numbers, bank card numbers,
phone numbers, bulk email lists and password-like strings.  The host has no
production AV/DLP service in this local deployment, so the gate is honest:
pattern hits quarantine the candidate, and an optional ClamAV daemon can be
wired through HOST_CLAMAV_HOST for a real scanner.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass

_ID_RE = re.compile(r"\b\d{17}[\dXx]\b")
_BANK_CARD_RE = re.compile(r"\b\d{16,19}\b")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PASSWORD_HINT_RE = re.compile(
    r"(密码|口令|password|passwd|secret|token)\s*[:：=]\s*\S{6,64}", re.I
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A bounded DLP decision never contains message content; only counts and
# category names cross the audit boundary.


@dataclass(frozen=True, slots=True)
class DlpDecision:
    security_state: str
    rights_state: str
    categories: tuple[str, ...]
    counts: Mapping[str, int]
    scanner: str


def evaluate_text(text: str, *, clamav_host: str | None = None) -> DlpDecision:
    """Evaluate one bounded text payload and return a quarantine-safe decision."""

    categories: list[str] = []
    counts: dict[str, int] = {}
    checks = (
        ("cn_id_number", len(_ID_RE.findall(text))),
        ("bank_card_number", len(_BANK_CARD_RE.findall(text))),
        ("phone_number", len(_PHONE_RE.findall(text))),
        ("password_like", len(_PASSWORD_HINT_RE.findall(text))),
    )
    for category, count in checks:
        if count:
            categories.append(category)
            counts[category] = count
    # Bulk recipient lists are themselves sensitive metadata.
    email_count = len(set(_EMAIL_RE.findall(text)))
    if email_count >= 20:
        categories.append("bulk_email_list")
        counts["bulk_email_list"] = email_count

    scanner = "local-pattern-only"
    if clamav_host:
        scanner = "local-pattern+clamav"
        # The daemon probe is best-effort: a missing scanner must not weaken
        # the pattern gate, but a configured scanner that is down is a
        # quarantine condition for content that would otherwise pass.
        if not _clamav_available(clamav_host):
            categories.append("av_scanner_unavailable")
            counts["av_scanner_unavailable"] = 1

    if categories:
        return DlpDecision(
            security_state="quarantined",
            rights_state="review_required",
            categories=tuple(categories),
            counts=counts,
            scanner=scanner,
        )
    return DlpDecision(
        security_state="cleared",
        rights_state="approved",
        categories=(),
        counts=counts,
        scanner=scanner,
    )


def _clamav_available(host: str) -> bool:
    try:
        hostname, _, port_text = host.partition(":")
        port = int(port_text) if port_text else 3310
        with socket.create_connection((hostname, port), timeout=2.0):
            return True
    except (OSError, ValueError):
        return False
