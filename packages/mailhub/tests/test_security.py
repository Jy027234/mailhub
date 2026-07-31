import pytest

from mailhub.security import (
    AttachmentDescriptor,
    AttachmentScanResult,
    AttachmentSecurityGate,
    AttachmentSecurityState,
)


def _attachment(media_type: str = "text/plain") -> AttachmentDescriptor:
    return AttachmentDescriptor(
        attachment_ref="attachment-1",
        filename="note.txt",
        media_type=media_type,
        byte_size=10,
        content_sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_missing_scanner_quarantines_attachment() -> None:
    result = await AttachmentSecurityGate(None).inspect(_attachment())
    assert result.state is AttachmentSecurityState.QUARANTINED
    assert result.reason_code == "scanner_unconfigured"


@pytest.mark.asyncio
async def test_active_content_is_quarantined_before_scanner() -> None:
    result = await AttachmentSecurityGate(None).inspect(_attachment("application/zip"))
    assert result.state is AttachmentSecurityState.QUARANTINED
    assert result.reason_code == "attachment_active_content"


@pytest.mark.asyncio
async def test_clean_scan_requires_scanner_provenance() -> None:
    class Scanner:
        async def scan(self, attachment: AttachmentDescriptor) -> AttachmentScanResult:
            return AttachmentScanResult(
                attachment_ref=attachment.attachment_ref,
                state=AttachmentSecurityState.CLEAN,
                scanner_ref="scan-1",
                reason_code=None,
            )

    result = await AttachmentSecurityGate(Scanner()).inspect(_attachment())
    assert result.state is AttachmentSecurityState.CLEAN
    assert result.scanner_ref == "scan-1"
