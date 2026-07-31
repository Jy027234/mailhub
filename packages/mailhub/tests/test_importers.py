from __future__ import annotations

from pathlib import Path

import pytest

from mailhub.importers import import_eml, import_mbox

EML = (
    b"From: Sender <sender@example.test>\n"
    b"To: User <user@example.test>\n"
    b"Subject: Offline RFQ\n"
    b"Date: Tue, 28 Jul 2026 10:00:00 +0000\n"
    b"Message-ID: <rfq-1@example.test>\n"
    b"MIME-Version: 1.0\n"
    b"Content-Type: multipart/mixed; boundary=demo\n\n"
    b"--demo\nContent-Type: text/plain; charset=utf-8\n\n"
    b"Please confirm delivery date.\n"
    b"--demo\nContent-Type: application/pdf\n"
    b"Content-Disposition: attachment; filename=quote.pdf\n\n"
    b"not-a-real-file\n--demo--\n"
)


def test_eml_import_is_bounded_read_only_and_preserves_attachment_metadata(tmp_path: Path) -> None:
    source = tmp_path / "message.eml"
    source.write_bytes(EML)

    imported = import_eml(source)

    assert imported.format == "eml"
    assert imported.provider_capabilities == (
        "offline_read_only",
        "backfill_fixture",
        "metadata_and_bounded_text",
    )
    assert len(imported.messages) == 1
    item = imported.messages[0]
    assert item.message.sender_address == "sender@example.test"
    assert item.message.recipient_addresses == ("user@example.test",)
    assert item.message.body_text == "Please confirm delivery date."
    assert item.attachment_names == ("quote.pdf",)
    assert item.message.attachment_count == 1
    page = imported.to_sync_page(limit=10)
    assert page.provider_cursor_kind == "offline_sequence"
    assert page.next_cursor == "1"


def test_mbox_import_replays_deterministically_and_rejects_wrong_format(tmp_path: Path) -> None:
    source = tmp_path / "mailbox.mbox"
    source.write_bytes(b"From sender@example.test Tue Jul 28 10:00:00 2026\n" + EML + b"\n")

    imported = import_mbox(source)
    replay = import_mbox(source)

    assert (
        imported.messages[0].message.provider_message_ref
        == replay.messages[0].message.provider_message_ref
    )
    assert imported.to_sync_page(cursor="0", limit=1).messages[0].subject == "Offline RFQ"
    with pytest.raises(ValueError, match="import_format_unsupported"):
        import_eml(source)


def test_import_rejects_missing_source_and_invalid_cursor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="import_source_not_found"):
        import_eml(tmp_path / "missing.eml")
    source = tmp_path / "message.eml"
    source.write_bytes(EML)
    imported = import_eml(source)
    with pytest.raises(ValueError, match="import_cursor_invalid"):
        imported.to_sync_page(cursor="not-a-number")
