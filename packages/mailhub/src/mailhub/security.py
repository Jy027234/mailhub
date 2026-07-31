"""Attachment and content security boundaries.

MailHub does not ship an AV/DLP engine.  The gate therefore returns
``quarantined`` when the deployment has not supplied one and never treats a
metadata-only check as a clean scan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class AttachmentSecurityState(StrEnum):
    PENDING_SCAN = "pending_scan"
    CLEAN = "clean"
    QUARANTINED = "quarantined"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class AttachmentDescriptor:
    attachment_ref: str
    filename: str
    media_type: str
    byte_size: int
    content_sha256: str
    has_active_content: bool = False

    def __post_init__(self) -> None:
        if (
            not self.attachment_ref.strip()
            or not self.filename.strip()
            or len(self.attachment_ref) > 512
            or len(self.filename) > 255
            or any(char in self.filename for char in ("/", "\\", "\x00"))
        ):
            raise ValueError("attachment_identity_missing")
        if (
            not self.media_type.strip()
            or len(self.media_type) > 200
            or any(ord(char) < 32 for char in self.media_type)
        ):
            raise ValueError("attachment_media_type_invalid")
        if not 0 <= self.byte_size <= 2**63 - 1:
            raise ValueError("attachment_size_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise ValueError("attachment_digest_invalid")


@dataclass(frozen=True, slots=True)
class AttachmentScanResult:
    attachment_ref: str
    state: AttachmentSecurityState
    scanner_ref: str | None
    reason_code: str | None


class AttachmentScannerPort(Protocol):
    async def scan(self, attachment: AttachmentDescriptor) -> AttachmentScanResult: ...


class AttachmentSecurityGate:
    def __init__(
        self,
        scanner: AttachmentScannerPort | None,
        *,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("attachment_max_bytes_invalid")
        self.scanner = scanner
        self.max_bytes = max_bytes

    async def inspect(self, attachment: AttachmentDescriptor) -> AttachmentScanResult:
        if attachment.byte_size < 0 or attachment.byte_size > self.max_bytes:
            return AttachmentScanResult(
                attachment_ref=attachment.attachment_ref,
                state=AttachmentSecurityState.BLOCKED,
                scanner_ref=None,
                reason_code="attachment_size_limit",
            )
        if attachment.has_active_content or _blocked_media_type(attachment.media_type):
            return AttachmentScanResult(
                attachment_ref=attachment.attachment_ref,
                state=AttachmentSecurityState.QUARANTINED,
                scanner_ref=None,
                reason_code="attachment_active_content",
            )
        if self.scanner is None:
            return AttachmentScanResult(
                attachment_ref=attachment.attachment_ref,
                state=AttachmentSecurityState.QUARANTINED,
                scanner_ref=None,
                reason_code="scanner_unconfigured",
            )
        result = await self.scanner.scan(attachment)
        if result.attachment_ref != attachment.attachment_ref:
            raise ValueError("attachment_scan_identity_mismatch")
        if result.state is AttachmentSecurityState.CLEAN and not result.scanner_ref:
            raise ValueError("clean_scan_provenance_missing")
        return result


def _blocked_media_type(media_type: str) -> bool:
    normalized = media_type.casefold().strip()
    return normalized in {
        "application/x-msdownload",
        "application/x-msdos-program",
        "application/vnd.ms-office",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.macroenabled.12",
        "application/zip",
        "application/x-7z-compressed",
    }
