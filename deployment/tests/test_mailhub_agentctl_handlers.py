import asyncio
import json

import pytest

from mailhub_agentctl_handlers import (
    MailHubHandlerError,
    mail_connection_list,
    mail_message_analyze,
    mail_message_search,
    mail_thread_list,
    mail_autonomy_enqueue,
    mail_reply_draft,
    mail_reply_send,
    mail_rule_execute,
    mail_sync_enqueue,
    _sync_job_filter,
)


def test_mailhub_handler_fails_closed_without_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAILHUB_BASE_URL", raising=False)
    invocation = {
        "validated_arguments": {"message_id": "00000000-0000-0000-0000-000000000001"},
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="endpoint_not_configured"):
        asyncio.run(mail_message_analyze(invocation))


def test_mailhub_read_handlers_fail_closed_without_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAILHUB_BASE_URL", raising=False)
    invocation = {
        "validated_arguments": {"query": "RFQ", "limit": 10},
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="endpoint_not_configured"):
        asyncio.run(mail_message_search(invocation))
    with pytest.raises(MailHubHandlerError, match="endpoint_not_configured"):
        asyncio.run(mail_connection_list({**invocation, "validated_arguments": {}}))


def test_mailhub_thread_handler_forwards_scope_bound_cursor_and_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_BASE_URL", "http://127.0.0.1:8090")
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, _limit: int) -> bytes:
            return b'{"data":[],"next_cursor":"next-1","has_more":true,"filters":{"unread":true}}'

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        return FakeResponse()

    monkeypatch.setattr("mailhub_agentctl_handlers.request.urlopen", fake_urlopen)
    invocation = {
        "validated_arguments": {
            "limit": 25,
            "cursor": "opaque cursor",
            "connection_id": "00000000-0000-0000-0000-000000000001",
            "unread": True,
            "has_attachment": True,
            "project": True,
        },
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }

    result = asyncio.run(mail_thread_list(invocation))

    assert result["next_cursor"] == "next-1"
    assert result["has_more"] is True
    assert "cursor=opaque+cursor" in str(captured["url"])
    assert "has_attachment" not in str(captured["url"])
    assert "attachment=true" in str(captured["url"])


def test_mailhub_thread_handler_rejects_invalid_filter_types() -> None:
    invocation = {
        "validated_arguments": {"unread": "true"},
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="unread_invalid"):
        asyncio.run(mail_thread_list(invocation))


def test_mailhub_search_handler_rejects_unknown_search_mode() -> None:
    invocation = {
        "validated_arguments": {"query": "RFQ", "mode": "full_text"},
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="mode_invalid"):
        asyncio.run(mail_message_search(invocation))


def test_mailhub_handler_rejects_lookalike_plaintext_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_BASE_URL", "http://localhost.evil.example.test")
    invocation = {
        "validated_arguments": {"message_id": "00000000-0000-0000-0000-000000000001"},
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="endpoint_not_configured"):
        asyncio.run(mail_message_analyze(invocation))


def test_mailhub_handler_rejects_non_uuid_path_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_BASE_URL", "http://127.0.0.1:8090")
    invocation = {
        "validated_arguments": {"message_id": "message/../../admin"},
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="message_id_invalid"):
        asyncio.run(mail_message_analyze(invocation))


def test_mailhub_send_handler_requires_revision_and_digest_binding() -> None:
    invocation = {
        "validated_arguments": {
            "draft_id": "00000000-0000-0000-0000-000000000001",
            "expected_revision": 1,
            "expected_content_sha256": "bad",
            "expected_recipient_digest": "a" * 64,
            "confirmation_ref": "approval-1",
        },
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="expected_content_sha256_invalid"):
        asyncio.run(mail_reply_send(invocation))


def test_mailhub_sync_handler_validates_bounded_job_arguments() -> None:
    invocation = {
        "validated_arguments": {
            "connection_id": "not-a-uuid",
            "mode": "incremental",
            "limit": 50,
        },
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="connection_id_invalid"):
        asyncio.run(mail_sync_enqueue(invocation))


def test_mailhub_sync_filter_helper_is_backfill_only_and_normalizes_utc() -> None:
    body = _sync_job_filter(
        {
            "folder_ref": "INBOX",
            "label_refs": ["project"],
            "received_after": "2026-07-01T08:00:00+08:00",
            "received_before": "2026-07-08T00:00:00Z",
        },
        "backfill",
    )
    assert body["received_after"] == "2026-07-01T00:00:00Z"
    assert body["received_before"] == "2026-07-08T00:00:00Z"
    with pytest.raises(MailHubHandlerError, match="sync_filter_requires_backfill"):
        _sync_job_filter({"label_refs": ["project"]}, "incremental")
    with pytest.raises(MailHubHandlerError, match="received_range_invalid"):
        _sync_job_filter(
            {
                "received_after": "2026-07-08T00:00:00Z",
                "received_before": "2026-07-01T00:00:00Z",
            },
            "backfill",
        )


def test_mailhub_autonomy_handler_validates_replay_and_scope_arguments() -> None:
    invocation = {
        "validated_arguments": {
            "connection_id": "not-a-uuid",
            "replay_key": "cycle-1",
        },
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="connection_id_invalid"):
        asyncio.run(mail_autonomy_enqueue(invocation))


def test_mailhub_autonomy_handler_fails_closed_without_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAILHUB_BASE_URL", raising=False)
    invocation = {
        "validated_arguments": {
            "connection_id": "00000000-0000-0000-0000-000000000001",
            "replay_key": "cycle-1",
        },
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="endpoint_not_configured"):
        asyncio.run(mail_autonomy_enqueue(invocation))


def test_mailhub_rule_handler_validates_uuid_list_and_kill_switch_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_BASE_URL", "http://127.0.0.1:8090")
    invocation = {
        "validated_arguments": {
            "rule_id": "00000000-0000-0000-0000-000000000001",
            "message_ids": ["not-a-uuid"],
            "dry_run": True,
        },
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }
    with pytest.raises(MailHubHandlerError, match="message_ids_invalid"):
        asyncio.run(mail_rule_execute(invocation))


def test_mailhub_draft_handler_forwards_cc_bcc_and_attachment_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_BASE_URL", "http://127.0.0.1:8090")
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, _limit: int) -> bytes:
            return b'{"data":{"draft_id":"draft-1"}}'

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return FakeResponse()

    monkeypatch.setattr("mailhub_agentctl_handlers.request.urlopen", fake_urlopen)
    invocation = {
        "validated_arguments": {
            "connection_id": "00000000-0000-0000-0000-000000000001",
            "thread_id": None,
            "recipient_addresses": ["to@example.test"],
            "cc_addresses": ["cc@example.test"],
            "bcc_addresses": ["bcc@example.test"],
            "attachment_refs": ["obj:attachment-1"],
            "subject": "Subject",
            "body_text": "Body",
        },
        "trace_id": "trace-1",
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-1",
        "subject_id": "subject-1",
    }

    result = asyncio.run(mail_reply_draft(invocation))

    assert result["draft"] == {"draft_id": "draft-1"}
    assert captured["body"] == {
        "connection_id": "00000000-0000-0000-0000-000000000001",
        "thread_id": None,
        "recipient_addresses": ["to@example.test"],
        "cc_addresses": ["cc@example.test"],
        "bcc_addresses": ["bcc@example.test"],
        "attachment_refs": ["obj:attachment-1"],
        "subject": "Subject",
        "body_text": "Body",
    }
