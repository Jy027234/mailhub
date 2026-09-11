import asyncio
import imaplib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from mailhub.connectors.conformance import (
    validate_capabilities,
    validate_cursor_reset,
    validate_provider_failure,
    validate_revocation_failure,
    validate_send_retry_identity,
    validate_sync_page,
    validate_sync_page_sequence,
    validate_sync_replay,
)
from mailhub.connectors.http_providers import (
    GmailConnector,
    MicrosoftGraphConnector,
    _email_message,
    _get_json,
    _gmail_message,
    _graph_backfill_filter,
    _parse_datetime,
    _response_json,
    _safe_graph_cursor,
)
from mailhub.connectors.imap_smtp import (
    ImapSmtpConnector,
    _extract_rfc822,
    _last_uid,
    _parse_message,
    _same_uidvalidity,
    _uid_validity,
)
from mailhub.domain import MailboxConnection, ProviderName, digest_text
from mailhub.errors import ProviderFailureError, RateLimitedError
from mailhub.ports import (
    ProviderMessage,
    ProviderSendReceipt,
    ProviderSendRequest,
    ProviderSyncFilter,
    ProviderSyncPage,
)


def _connection(provider: ProviderName) -> MailboxConnection:
    return MailboxConnection(
        connection_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="user-1",
        provider=provider,
        email_address="user@example.test",
        credential_ref="credential-ref",
    )


@pytest.mark.asyncio
async def test_gmail_history_cursor_and_message_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str | int] | None,
        ) -> httpx.Response:
            del headers
            if url.endswith("/history"):
                value: dict[str, Any] = {
                    "historyId": "124",
                    "history": [
                        {
                            "messagesAdded": [{"message": {"id": "m-1"}}],
                            "messagesDeleted": [{"message": {"id": "m-deleted"}}],
                            "labelsRemoved": [
                                {"message": {"id": "m-moved"}, "labelIds": ["INBOX"]}
                            ],
                        }
                    ],
                }
            elif url.endswith("/messages/m-1"):
                value = {
                    "id": "m-1",
                    "threadId": "t-1",
                    "historyId": "124",
                    "labelIds": ["INBOX", "IMPORTANT"],
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "buyer@example.test"},
                            {"name": "To", "value": "user@example.test"},
                            {"name": "Cc", "value": "Project Team <team@example.test>"},
                            {"name": "Bcc", "value": "audit@example.test"},
                            {"name": "Reply-To", "value": "replies@example.test"},
                            {"name": "Subject", "value": "Project update"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": "SGVsbG8="},
                    },
                }
            else:
                raise AssertionError(f"unexpected URL {url} params={params}")
            return httpx.Response(200, json=value, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    page = await GmailConnector().sync(
        _connection(ProviderName.GMAIL),
        cursor="123",
        limit=10,
        credential={"access_token": "token"},
    )
    assert page.provider_cursor_kind == "gmail_history_id"
    assert page.next_cursor == "124"
    assert page.messages[0].body_text == "Hello"
    assert page.messages[0].is_read is True
    assert page.messages[0].attachment_count == 0
    assert page.messages[0].labels == ("INBOX", "IMPORTANT")
    assert page.messages[0].cc_addresses == ("team@example.test",)
    assert page.messages[0].bcc_addresses == ("audit@example.test",)
    assert page.messages[0].reply_to_addresses == ("replies@example.test",)
    assert page.deleted_message_refs == ("m-moved", "m-deleted")
    assert page.messages[0].provider_metadata == {
        "body_content_type": "text/plain",
        "history_id": "124",
    }

    class RetryClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(
            self,
            url: str,
            *,
            headers: Mapping[str, str],
            params: Mapping[str, str | int] | None,
        ) -> httpx.Response:
            del headers, params
            self.calls += 1
            if self.calls == 1:
                return httpx.Response(
                    503,
                    json={"error": {"code": "backendError"}},
                    headers={"Retry-After": "0"},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

    retry_client = RetryClient()
    retry_payload = await _get_json(
        cast(httpx.AsyncClient, retry_client),
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        {},
        None,
    )
    assert retry_payload == {"ok": True}
    assert retry_client.calls == 2

    class LongRetryClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(
            self,
            url: str,
            *,
            headers: Mapping[str, str],
            params: Mapping[str, str | int] | None,
        ) -> httpx.Response:
            del headers, params
            self.calls += 1
            return httpx.Response(
                429,
                headers={"Retry-After": "60"},
                request=httpx.Request("GET", url),
            )

    long_retry_client = LongRetryClient()
    with pytest.raises(RateLimitedError):
        await _get_json(
            cast(httpx.AsyncClient, long_retry_client),
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            {},
            None,
        )
    assert long_retry_client.calls == 1


@pytest.mark.asyncio
async def test_gmail_stale_history_rebuilds_bounded_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[tuple[str, dict[str, str | int] | None]] = []

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str | int] | None,
        ) -> httpx.Response:
            del headers
            requested.append((url, params))
            if url.endswith("/history"):
                return httpx.Response(
                    404, json={"error": "history_expired"}, request=httpx.Request("GET", url)
                )
            if url.endswith("/messages"):
                if params and params.get("pageToken") == "page-2":
                    value: dict[str, Any] = {"messages": [{"id": "m-2"}]}
                else:
                    value = {"messages": [{"id": "m-1"}], "nextPageToken": "page-2"}
                return httpx.Response(200, json=value, request=httpx.Request("GET", url))
            if url.endswith("/messages/m-1") or url.endswith("/messages/m-2"):
                message_id = url.rsplit("/", 1)[-1]
                value = {
                    "id": message_id,
                    "threadId": "t-1",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "sender@example.test"},
                            {"name": "To", "value": "user@example.test"},
                            {"name": "Subject", "value": message_id},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": "SGVsbG8="},
                    },
                }
                return httpx.Response(200, json=value, request=httpx.Request("GET", url))
            if url.endswith("/profile"):
                return httpx.Response(
                    200, json={"historyId": "900"}, request=httpx.Request("GET", url)
                )
            raise AssertionError(f"unexpected URL {url} params={params}")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    page = await GmailConnector().sync(
        _connection(ProviderName.GMAIL),
        cursor="stale-history",
        limit=2,
        credential={"access_token": "token"},
    )

    assert page.reset_required is True
    assert page.next_cursor == "900"
    assert [item.provider_message_ref for item in page.messages] == ["m-1", "m-2"]
    assert any(params and params.get("pageToken") == "page-2" for _, params in requested)


@pytest.mark.asyncio
async def test_gmail_backfill_filter_is_translated_to_safe_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[dict[str, str | int] | None] = []

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str | int] | None,
        ) -> httpx.Response:
            del headers
            requested.append(params)
            if url.endswith("/messages"):
                return httpx.Response(
                    200,
                    json={"messages": [{"id": "m-filter"}]},
                    request=httpx.Request("GET", url),
                )
            if url.endswith("/messages/m-filter"):
                return httpx.Response(
                    200,
                    json={
                        "id": "m-filter",
                        "threadId": "t-filter",
                        "labelIds": ["INBOX", "Project"],
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "sender@example.test"},
                                {"name": "To", "value": "user@example.test"},
                                {"name": "Subject", "value": "Filtered"},
                                {"name": "Date", "value": "Wed, 01 Jul 2026 00:00:00 +0000"},
                            ],
                            "mimeType": "text/plain",
                            "body": {"data": "SGVsbG8="},
                        },
                    },
                    request=httpx.Request("GET", url),
                )
            if url.endswith("/profile"):
                return httpx.Response(
                    200, json={"historyId": "200"}, request=httpx.Request("GET", url)
                )
            raise AssertionError(f"unexpected URL {url} params={params}")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    page = await GmailConnector().sync(
        _connection(ProviderName.GMAIL),
        cursor=None,
        limit=10,
        credential={"access_token": "token"},
        sync_filter=ProviderSyncFilter(
            label_refs=("Project",),
            received_after=datetime(2026, 6, 1, tzinfo=UTC),
            received_before=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    )

    assert requested and requested[0] is not None
    query = requested[0].get("q")
    assert isinstance(query, str)
    assert 'label:"Project"' in query
    assert "after:" in query and "before:" in query
    assert [message.provider_message_ref for message in page.messages] == ["m-filter"]


def test_parse_datetime_accepts_rfc5322_gmail_date_headers() -> None:
    # Gmail Date headers are RFC 5322, not ISO-8601.  A regression here guards
    # against the silent now() fallback corrupting durable received_at
    # projections and backfill date filters.
    assert _parse_datetime("Wed, 01 Jul 2026 00:00:00 +0000") == datetime(2026, 7, 1, tzinfo=UTC)
    assert _parse_datetime("Mon, 20 Jul 2026 10:30:00 -0700") == datetime(
        2026, 7, 20, 17, 30, tzinfo=UTC
    )
    assert _parse_datetime("2026-07-01T00:00:00Z") == datetime(2026, 7, 1, tzinfo=UTC)


def test_gmail_message_prefers_internal_date_over_date_header() -> None:
    received = datetime(2026, 7, 1, tzinfo=UTC)
    message = _gmail_message(
        {
            "id": "m-internal",
            "threadId": "t-internal",
            "internalDate": str(int(received.timestamp() * 1000)),
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "sender@example.test"},
                    {"name": "To", "value": "user@example.test"},
                    {"name": "Subject", "value": "Internal date wins"},
                    # A deliberately different RFC 5322 value: the authoritative
                    # internalDate must win over the header.
                    {"name": "Date", "value": "Wed, 08 Jul 2026 00:00:00 +0000"},
                ],
                "mimeType": "text/plain",
                "body": {"data": "SGVsbG8="},
            },
        }
    )
    assert message.received_at == received


@pytest.mark.asyncio
async def test_graph_delta_uses_delta_endpoint_and_returns_continuation_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str | int] | None,
        ) -> httpx.Response:
            del headers
            assert params is not None
            assert "categories" in str(params["$select"])
            assert "parentFolderId" in str(params["$select"])
            assert "changeKey" in str(params["$select"])
            requested_urls.append(url)
            value = {
                "value": [
                    {
                        "id": "m-1",
                        "conversationId": "t-1",
                        "internetMessageId": "<m-1@example.test>",
                        "from": {"emailAddress": {"address": "sender@example.test"}},
                        "toRecipients": [{"emailAddress": {"address": "user@example.test"}}],
                        "ccRecipients": [{"emailAddress": {"address": "team@example.test"}}],
                        "bccRecipients": [{"emailAddress": {"address": "audit@example.test"}}],
                        "replyTo": [{"emailAddress": {"address": "replies@example.test"}}],
                        "subject": "Update",
                        "receivedDateTime": "2026-07-28T00:00:00Z",
                        "body": {"content": "Body", "contentType": "text"},
                        "categories": ["Project", "Important"],
                        "parentFolderId": "folder-inbox",
                        "changeKey": "change-1",
                    },
                    {"id": "graph-deleted-after-limit", "@removed": {"reason": "deleted"}},
                ],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages/delta?$skiptoken=2",
            }
            return httpx.Response(200, json=value, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    page = await MicrosoftGraphConnector().sync(
        _connection(ProviderName.MICROSOFT_GRAPH),
        cursor=None,
        limit=1,
        credential={"access_token": "token"},
    )

    assert requested_urls[0].endswith("/me/mailFolders/inbox/messages/delta")
    assert page.next_cursor == "https://graph.microsoft.com/v1.0/me/messages/delta?$skiptoken=2"
    assert page.messages[0].provider_message_ref == "m-1"
    assert page.deleted_message_refs == ("graph-deleted-after-limit",)
    assert page.messages[0].is_read is True
    assert page.messages[0].attachment_count == 0
    assert page.messages[0].labels == ("category:Project", "category:Important")
    assert page.messages[0].cc_addresses == ("team@example.test",)
    assert page.messages[0].bcc_addresses == ("audit@example.test",)
    assert page.messages[0].reply_to_addresses == ("replies@example.test",)
    assert page.messages[0].provider_metadata == {
        "body_content_type": "text",
        "change_key": "change-1",
        "folder_id": "folder-inbox",
    }


@pytest.mark.asyncio
async def test_graph_delta_carries_removed_message_refs_without_fetching_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str | int] | None,
        ) -> httpx.Response:
            del headers
            requested_urls.append(url)
            assert params is not None
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "graph-deleted", "@removed": {"reason": "deleted"}}],
                    "@odata.deltaLink": (
                        "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=done"
                    ),
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    page = await MicrosoftGraphConnector().sync(
        _connection(ProviderName.MICROSOFT_GRAPH),
        cursor=None,
        limit=10,
        credential={"access_token": "token"},
    )

    assert requested_urls == [
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"
    ]
    assert page.messages == ()
    assert page.deleted_message_refs == ("graph-deleted",)
    assert page.next_cursor and "deltatoken=done" in page.next_cursor


@pytest.mark.asyncio
async def test_graph_backfill_filter_scopes_folder_and_received_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[tuple[str, dict[str, str | int] | None]] = []

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str | int] | None,
        ) -> httpx.Response:
            del headers
            requested.append((url, params))
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "graph-filter",
                            "conversationId": "thread-filter",
                            "from": {"emailAddress": {"address": "sender@example.test"}},
                            "toRecipients": [{"emailAddress": {"address": "user@example.test"}}],
                            "subject": "Graph filtered",
                            "receivedDateTime": "2026-07-01T00:00:00Z",
                            "body": {"content": "Body", "contentType": "text"},
                            "categories": ["Project"],
                            "parentFolderId": "folder-project",
                        }
                    ],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=done",
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    page = await MicrosoftGraphConnector().sync(
        _connection(ProviderName.MICROSOFT_GRAPH),
        cursor=None,
        limit=10,
        credential={"access_token": "token"},
        sync_filter=ProviderSyncFilter(
            folder_ref="folder-project",
            label_refs=("category:Project",),
            received_after=datetime(2026, 6, 1, tzinfo=UTC),
            received_before=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    )

    assert requested[0][0].endswith("/me/mailFolders/folder-project/messages/delta")
    params = requested[0][1]
    assert params is not None
    assert "receivedDateTime ge" in str(params["$filter"])
    assert "receivedDateTime lt" in str(params["$filter"])
    assert "categories/any" in str(params["$filter"])
    assert page.messages[0].provider_message_ref == "graph-filter"


def test_graph_backfill_rejects_untyped_label_query() -> None:
    with pytest.raises(ProviderFailureError, match="graph_label_filter_unsupported"):
        _graph_backfill_filter(ProviderSyncFilter(label_refs=("free-form",)))


def test_graph_delta_cursor_is_same_origin_and_under_api_path() -> None:
    assert _safe_graph_cursor(
        "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=ok",
        "https://graph.microsoft.com/v1.0",
    ).startswith("https://graph.microsoft.com/v1.0/")
    with pytest.raises(ProviderFailureError, match="graph_delta_cursor_origin_invalid"):
        _safe_graph_cursor(
            "https://graph.microsoft.com:8443/v1.0/me/messages/delta",
            "https://graph.microsoft.com/v1.0",
        )
    with pytest.raises(ProviderFailureError, match="graph_delta_cursor_path_invalid"):
        _safe_graph_cursor(
            "https://graph.microsoft.com/v2.0/me/messages/delta",
            "https://graph.microsoft.com/v1.0",
        )


@pytest.mark.asyncio
async def test_graph_expired_delta_token_rebuilds_once_and_declares_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[tuple[str, dict[str, str | int] | None]] = []

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str | int] | None,
        ) -> httpx.Response:
            del headers
            requested.append((url, params))
            if len(requested) == 1:
                return httpx.Response(
                    410, json={"error": "delta_expired"}, request=httpx.Request("GET", url)
                )
            value = {
                "value": [
                    {
                        "id": "m-reset",
                        "conversationId": "t-reset",
                        "from": {"emailAddress": {"address": "sender@example.test"}},
                        "toRecipients": [{"emailAddress": {"address": "user@example.test"}}],
                        "subject": "Reset",
                        "receivedDateTime": "2026-07-28T00:00:00Z",
                        "body": {"content": "Body", "contentType": "text"},
                    }
                ],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=reset",
            }
            return httpx.Response(200, json=value, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    page = await MicrosoftGraphConnector().sync(
        _connection(ProviderName.MICROSOFT_GRAPH),
        cursor="https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=stale",
        limit=10,
        credential={"access_token": "token"},
    )

    assert page.reset_required is True
    assert page.next_cursor and "reset" in page.next_cursor
    assert requested[0][1] is None
    assert requested[1][0].endswith("/me/mailFolders/inbox/messages/delta")


def test_imap_parser_bounds_plain_text_and_uidvalidity_cursor() -> None:
    message = EmailMessage()
    message["From"] = "buyer@example.test"
    message["To"] = "user@example.test"
    message["Cc"] = "Team <team@example.test>"
    message["Bcc"] = "audit@example.test"
    message["Reply-To"] = "replies@example.test"
    message["Subject"] = "RFQ"
    message["Message-ID"] = "<m-1@example.test>"
    message["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    message.set_content("bounded body")
    message.add_attachment(b"pdf-bytes", maintype="application", subtype="pdf", filename="rfq.pdf")
    parsed = _parse_message(message.as_bytes(), "77", 9, "INBOX")
    assert parsed.provider_message_ref == "INBOX:77:9"
    assert parsed.body_text == "bounded body\n"
    assert parsed.attachment_count == 1
    assert parsed.cc_addresses == ("team@example.test",)
    assert parsed.bcc_addresses == ("audit@example.test",)
    assert parsed.reply_to_addresses == ("replies@example.test",)
    assert _last_uid("77:8", "77") == 8
    assert _last_uid("76:8", "77") is None
    assert _same_uidvalidity("77:8", "77") is True


def test_imap_parser_falls_back_for_empty_to_and_missing_from() -> None:
    # Business BCC mail commonly arrives with an empty To header; the
    # connected mailbox is a recipient by definition.  A missing From falls
    # back to the envelope Return-Path instead of failing the whole batch.
    bcc_message = EmailMessage()
    bcc_message["From"] = "Supplier <supplier@example.test>"
    bcc_message["To"] = ""
    bcc_message["Subject"] = "BCC RFQ"
    bcc_message["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    bcc_message.set_content("bcc body")
    parsed = _parse_message(
        bcc_message.as_bytes(), "77", 10, "INBOX", fallback_recipient="Owner@Example.Test"
    )
    assert parsed.sender_address == "supplier@example.test"
    assert parsed.recipient_addresses == ("owner@example.test",)

    return_path_message = EmailMessage()
    return_path_message["Return-Path"] = "<bounce@example.test>"
    return_path_message["To"] = "owner@example.test"
    return_path_message["Subject"] = "Envelope sender"
    return_path_message.set_content("body")
    parsed = _parse_message(return_path_message.as_bytes(), "77", 11, "INBOX")
    assert parsed.sender_address == "bounce@example.test"

    broken = EmailMessage()
    broken["To"] = "owner@example.test"
    broken["Subject"] = "No sender anywhere"
    broken.set_content("body")
    with pytest.raises(ProviderFailureError, match="imap_message_address_missing"):
        _parse_message(broken.as_bytes(), "77", 12, "INBOX")


def test_imap_cursor_uses_uidvalidity_response_not_message_count() -> None:
    class FakeClient:
        def response(self, name: str) -> tuple[str, list[bytes]]:
            assert name == "UIDVALIDITY"
            # Modern imaplib returns the response code itself as the first
            # element ("UIDVALIDITY"), not the historical "OK" tag.  The
            # connector must rely on the data payload (see real-server
            # conformance against imap.qiye.163.com).
            return "UIDVALIDITY", [b"77"]

    assert _uid_validity(FakeClient(), [b"999"]) == "77"  # type: ignore[arg-type]

    class MissingResponse:
        def response(self, name: str) -> tuple[str, list[bytes] | None]:
            del name
            return "UIDVALIDITY", None

    with pytest.raises(ProviderFailureError, match="uidvalidity_missing"):
        _uid_validity(MissingResponse(), [b"999"])  # type: ignore[arg-type]

    class EmptyResponse:
        def response(self, name: str) -> tuple[str, list[bytes]]:
            del name
            return "UIDVALIDITY", []

    with pytest.raises(ProviderFailureError, match="uidvalidity_missing"):
        _uid_validity(EmptyResponse(), [b"999"])  # type: ignore[arg-type]


def test_imap_modseq_cursor_and_partial_fetch_fail_closed() -> None:
    assert _last_uid("77:8:123", "77") == 8
    assert _same_uidvalidity("77:8:123", "77") is True
    with pytest.raises(ProviderFailureError, match="imap_fetch_partial"):
        _extract_rfc822([(b"1 (RFC822 {4})", b"abc")])


@pytest.mark.asyncio
async def test_imap_sync_retries_transient_disconnect_with_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = ImapSmtpConnector(
        imap_host="imap.example.test",
        smtp_host="smtp.example.test",
        retry_attempts=3,
        retry_backoff_seconds=0.01,
    )
    attempts = 0

    def flaky(
        credential: dict[str, str],
        cursor: str | None,
        limit: int,
        mailbox_address: str,
        sync_filter: object = None,
    ) -> ProviderSyncPage:
        nonlocal attempts
        del credential, cursor, limit, mailbox_address, sync_filter
        attempts += 1
        if attempts < 3:
            raise ProviderFailureError("imap_provider_unavailable")
        return ProviderSyncPage(
            messages=(),
            next_cursor="77:1",
            provider_cursor_kind="imap_uidvalidity_uid",
            provider_request_id="request-1",
        )

    monkeypatch.setattr(connector, "_sync_blocking", flaky)
    page = await connector.sync(
        _connection(ProviderName.IMAP_SMTP),
        cursor=None,
        limit=10,
        credential={"password": "x", "username": "u"},
    )
    assert page.next_cursor == "77:1"
    assert attempts == 3


def test_connector_conformance_checks_capabilities_and_page_provenance() -> None:
    connection = _connection(ProviderName.GMAIL)
    connector = GmailConnector()
    report = validate_capabilities(connector.capabilities, connection)
    assert "provider_identity" in report.checks
    body = "bounded"
    page = ProviderSyncPage(
        messages=(
            ProviderMessage(
                provider_message_ref="m-1",
                provider_thread_ref="t-1",
                internet_message_id=None,
                sender_address="buyer@example.test",
                recipient_addresses=("user@example.test",),
                subject="Update",
                received_at=datetime.now(UTC),
                body_text=body,
                body_object_ref=None,
                content_sha256=digest_text(body),
            ),
        ),
        next_cursor="2",
        provider_cursor_kind="gmail_history_id",
        provider_request_id="request-1",
    )
    assert validate_sync_page(page, requested_limit=10).provider == "gmail_history_id"


def test_connector_conformance_rejects_unparsed_header_addresses() -> None:
    body = "bounded"
    message = ProviderMessage(
        provider_message_ref="m-header",
        provider_thread_ref="t-header",
        internet_message_id=None,
        sender_address="buyer@example.test",
        recipient_addresses=("user@example.test",),
        cc_addresses=("Display Name <team@example.test>",),
        subject="Update",
        received_at=datetime.now(UTC),
        body_text=body,
        body_object_ref=None,
        content_sha256=digest_text(body),
    )
    with pytest.raises(ValueError, match="provider_message_header_addresses_invalid"):
        validate_sync_page(
            ProviderSyncPage(
                messages=(message,),
                next_cursor="cursor-header",
                provider_cursor_kind="fixture",
                provider_request_id="request-header",
            ),
            requested_limit=1,
        )


def test_provider_sync_page_validates_deleted_refs_and_deduplicates_them() -> None:
    page = ProviderSyncPage(
        messages=(),
        next_cursor="cursor-1",
        provider_cursor_kind="fixture",
        provider_request_id="request-1",
        deleted_message_refs=("deleted-1", "deleted-1", "deleted-2"),
    )
    assert page.deleted_message_refs == ("deleted-1", "deleted-2")
    with pytest.raises(ValueError, match="provider_deleted_message_ref_invalid"):
        ProviderSyncPage(
            messages=(),
            next_cursor=None,
            provider_cursor_kind="fixture",
            provider_request_id="request-2",
            deleted_message_refs=("bad\nref",),
        )
    with pytest.raises(ValueError, match="provider_deleted_message_refs_too_many"):
        ProviderSyncPage(
            messages=(),
            next_cursor=None,
            provider_cursor_kind="fixture",
            provider_request_id="request-3",
            deleted_message_refs=tuple(f"ref-{index}" for index in range(1001)),
        )


def test_real_http_connectors_are_read_only_and_do_not_advertise_push_by_default() -> None:
    gmail = GmailConnector()
    graph = MicrosoftGraphConnector()

    assert gmail.capabilities.supports_send is False
    assert gmail.capabilities.supports_push is False
    assert gmail.capabilities.supports_search is False
    assert graph.capabilities.supports_send is False
    assert graph.capabilities.supports_push is False
    assert graph.capabilities.supports_search is False


@pytest.mark.asyncio
async def test_real_http_connector_send_is_blocked_in_read_only_mode() -> None:
    request = ProviderSendRequest(
        operation_id=uuid4(),
        connection_id=uuid4(),
        thread_ref=None,
        recipient_addresses=("recipient@example.test",),
        subject="Subject",
        body_text="Body",
        content_sha256=digest_text("Body"),
        idempotency_key="send-read-only",
    )

    with pytest.raises(ProviderFailureError, match="gmail_read_only_mode"):
        await GmailConnector().send(request, credential={})
    with pytest.raises(ProviderFailureError, match="graph_read_only_mode"):
        await MicrosoftGraphConnector().send(request, credential={})


def test_real_http_connector_write_gate_can_be_enabled_without_push() -> None:
    gmail = GmailConnector(read_only=False)
    graph = MicrosoftGraphConnector(read_only=False)

    assert gmail.capabilities.supports_send is True
    assert gmail.capabilities.supports_push is False
    assert gmail.capabilities.supports_search is False
    assert graph.capabilities.supports_send is True
    assert graph.capabilities.supports_push is False
    assert graph.capabilities.supports_search is False


def test_real_http_connector_advertises_host_owned_push_gate_when_enabled() -> None:
    gmail = GmailConnector(push_enabled=True)
    graph = MicrosoftGraphConnector(push_enabled=True)

    assert gmail.capabilities.supports_push is True
    assert graph.capabilities.supports_push is True


def test_connector_conformance_accepts_stable_replay_and_out_of_order_pages() -> None:
    body_one = "one"
    body_two = "two"
    first = ProviderSyncPage(
        messages=(
            ProviderMessage(
                provider_message_ref="m-1",
                provider_thread_ref="t-1",
                internet_message_id=None,
                sender_address="sender@example.test",
                recipient_addresses=("user@example.test",),
                subject="One",
                received_at=datetime(2026, 1, 2, tzinfo=UTC),
                body_text=body_one,
                body_object_ref=None,
                content_sha256=digest_text(body_one),
            ),
        ),
        next_cursor="2",
        provider_cursor_kind="fixture",
        provider_request_id="request-1",
    )
    replay = ProviderSyncPage(
        messages=first.messages,
        next_cursor="2",
        provider_cursor_kind="fixture",
        provider_request_id="request-2",
    )
    second = ProviderSyncPage(
        messages=(
            ProviderMessage(
                provider_message_ref="m-2",
                provider_thread_ref="t-1",
                internet_message_id=None,
                sender_address="sender@example.test",
                recipient_addresses=("user@example.test",),
                subject="Two",
                received_at=datetime(2026, 1, 1, tzinfo=UTC),
                body_text=body_two,
                body_object_ref=None,
                content_sha256=digest_text(body_two),
            ),
        ),
        next_cursor="3",
        provider_cursor_kind="fixture",
        provider_request_id="request-3",
        reset_required=True,
    )

    replay_report = validate_sync_replay(first, replay, requested_limit=10)
    sequence_report = validate_sync_page_sequence((first, replay, second), requested_limit=10)

    assert "replay_page_digest" in replay_report.checks
    assert "replay_redelivery_accepted" in sequence_report.checks
    assert "out_of_order_tolerated" in sequence_report.checks


def test_connector_conformance_rejects_changed_replay_digest() -> None:
    body = "stable"
    changed = "changed"
    original_message = ProviderMessage(
        provider_message_ref="m-1",
        provider_thread_ref="t-1",
        internet_message_id=None,
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject="Subject",
        received_at=datetime.now(UTC),
        body_text=body,
        body_object_ref=None,
        content_sha256=digest_text(body),
    )
    changed_message = replace(
        original_message, body_text=changed, content_sha256=digest_text(changed)
    )
    page = ProviderSyncPage(
        messages=(original_message,),
        next_cursor="1",
        provider_cursor_kind="fixture",
        provider_request_id="request-1",
    )
    changed_page = ProviderSyncPage(
        messages=(changed_message,),
        next_cursor="2",
        provider_cursor_kind="fixture",
        provider_request_id="request-2",
    )

    with pytest.raises(ValueError, match="sync_replay_digest_changed"):
        validate_sync_page_sequence((page, changed_page), requested_limit=10)


def test_connector_conformance_covers_reset_throttle_revocation_and_send_retry() -> None:
    body = "stable"
    message = ProviderMessage(
        provider_message_ref="m-1",
        provider_thread_ref="t-1",
        internet_message_id=None,
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject="Subject",
        received_at=datetime.now(UTC),
        body_text=body,
        body_object_ref=None,
        content_sha256=digest_text(body),
    )
    page = ProviderSyncPage(
        messages=(message,),
        next_cursor="reset-2",
        provider_cursor_kind="fixture",
        provider_request_id="request-reset",
        reset_required=True,
    )
    assert (
        "cursor_reset_declared"
        in validate_cursor_reset(page, previous_cursor="stale-1", requested_limit=10).checks
    )
    assert (
        "throttle_classified" in validate_provider_failure("http_429", retry_after_seconds=2).checks
    )
    assert "server_error_classified" in validate_provider_failure("http_503").checks
    assert "access_blocked" in validate_revocation_failure("access_token_revoked").checks

    quota_response = httpx.Response(
        403,
        json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}},
        headers={"Retry-After": "7"},
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me"),
    )
    with pytest.raises(RateLimitedError) as quota_error:
        _response_json(quota_response, operation="provider_sync")
    assert quota_error.value.details == {
        "provider_status": 403,
        "provider_reason": "userratelimitexceeded",
        "retry_after_seconds": 7,
    }

    date_retry_response = httpx.Response(
        429,
        headers={"Retry-After": format_datetime(datetime.now(UTC) + timedelta(seconds=60))},
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me"),
    )
    with pytest.raises(RateLimitedError) as date_retry_error:
        _response_json(date_retry_response, operation="provider_sync")
    date_retry_seconds = date_retry_error.value.details["retry_after_seconds"]
    assert isinstance(date_retry_seconds, int)
    assert 0 <= date_retry_seconds <= 60

    permission_response = httpx.Response(
        403,
        json={"error": {"code": "ErrorAccessDenied"}},
        request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/me/messages"),
    )
    with pytest.raises(
        ProviderFailureError, match="provider_sync_permission_denied"
    ) as permission_error:
        _response_json(permission_response, operation="provider_sync")
    assert permission_error.value.details == {
        "provider_status": 403,
        "provider_reason": "erroraccessdenied",
    }

    unavailable_response = httpx.Response(
        503,
        json={"error": {"code": "backendError"}},
        headers={"Retry-After": "11"},
        request=httpx.Request("GET", "https://graph.microsoft.com/v1.0/me/messages"),
    )
    with pytest.raises(ProviderFailureError) as unavailable_error:
        _response_json(unavailable_response, operation="provider_sync")
    assert unavailable_error.value.details == {
        "provider_status": 503,
        "provider_reason": "backenderror",
        "retry_after_seconds": 11,
    }
    receipt = ProviderSendReceipt(
        provider_message_ref="provider-message-1",
        provider_thread_ref="provider-thread-1",
        accepted_at=datetime.now(UTC),
        provider_request_id="request-1",
        idempotency_key="send-1",
        content_sha256=digest_text(body),
    )
    retry_report = validate_send_retry_identity(
        (receipt, replace(receipt, provider_request_id="request-2")),
        idempotency_key="send-1",
        content_sha256=digest_text(body),
    )
    assert "retry_idempotency" in retry_report.checks


def test_smtp_send_is_explicitly_disabled_until_enabled() -> None:
    connector = ImapSmtpConnector(imap_host="imap.example.test", smtp_host="smtp.example.test")
    assert connector.capabilities.supports_send is False


@pytest.mark.asyncio
async def test_smtp_send_refuses_a_message_over_the_outbound_bound() -> None:
    """The bound is enforced before connecting, so the provider never sees it."""

    from mailhub.errors import ValidationError

    connector = ImapSmtpConnector(
        imap_host="imap.example.test",
        smtp_host="smtp.example.test",
        # Port 1 is unreachable on purpose: a connection attempt would fail with
        # a different error, which proves the refusal happens before any socket.
        smtp_port=1,
        send_enabled=True,
        max_send_bytes=64 * 1024,
    )
    request = ProviderSendRequest(
        operation_id=uuid4(),
        connection_id=uuid4(),
        thread_ref="thread-1",
        recipient_addresses=("to@example.test",),
        subject="Subject",
        body_text="x" * (64 * 1024),
        content_sha256=digest_text("x"),
        idempotency_key="send-large",
    )

    with pytest.raises(ValidationError, match="smtp_message_too_large"):
        await connector.send(request, credential={"username": "a@example.test", "password": "p"})


def test_smtp_outbound_bound_is_validated() -> None:
    with pytest.raises(ValueError, match="max_send_bytes_invalid"):
        ImapSmtpConnector(
            imap_host="imap.example.test", smtp_host="smtp.example.test", max_send_bytes=1024
        )


def test_http_provider_message_preserves_cc_bcc_and_reply_headers() -> None:
    request = ProviderSendRequest(
        operation_id=uuid4(),
        connection_id=uuid4(),
        thread_ref="thread-1",
        recipient_addresses=("to@example.test",),
        subject="Subject",
        body_text="Body",
        content_sha256=digest_text("Body"),
        idempotency_key="send-1",
        cc_addresses=("cc@example.test",),
        bcc_addresses=("bcc@example.test",),
        in_reply_to_message_ref="<message-1@example.test>",
    )

    message = _email_message(request)

    assert message["To"] == "to@example.test"
    assert message["Cc"] == "cc@example.test"
    assert message["Bcc"] == "bcc@example.test"
    assert message["In-Reply-To"] == "<message-1@example.test>"
    assert message["References"] == "<message-1@example.test>"


class FakeImapSession:
    """imaplib stand-in that records the command order of one session.

    Only the surface ImapSmtpConnector actually touches is implemented; the
    point of the fake is to observe *when* CONDSTORE is negotiated and which
    search key the connector then chooses.
    """

    def __init__(self, capabilities: tuple[bytes, ...], *, enable_ok: bool = True) -> None:
        self.capabilities = capabilities
        self.enable_ok = enable_ok
        self.commands: list[str] = []
        self.condstore_in_effect = False
        self.search_arguments: tuple[str, ...] = ()
        FakeImapSession.instances.append(self)

    instances: list["FakeImapSession"] = []

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        del user, password
        self.commands.append("LOGIN")
        return ("OK", [b"logged in"])

    def capability(self) -> tuple[str, list[bytes]]:
        self.commands.append("CAPABILITY")
        return ("OK", [b" ".join(self.capabilities)])

    def enable(self, name: str) -> tuple[str, list[bytes]]:
        self.commands.append("ENABLE " + name)
        if not self.enable_ok:
            raise imaplib.IMAP4.error("ENABLE rejected")
        self.condstore_in_effect = True
        return ("OK", [name.encode("ascii")])

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        del mailbox, readonly
        self.commands.append("SELECT")
        return ("OK", [b"0"])

    def response(self, key: str) -> tuple[str, Any]:
        self.commands.append("RESPONSE " + key)
        if key == "UIDVALIDITY":
            return ("UIDVALIDITY", [b"77"])
        if self.condstore_in_effect or b"CONDSTORE" in self.capabilities:
            return ("OK", [b"4242"])
        return ("NO", [None])

    def uid(self, command: str, *args: str) -> tuple[str, list[bytes]]:
        self.commands.append("UID " + command + " " + " ".join(args))
        if command == "search":
            self.search_arguments = args
        return ("OK", [b""])

    def logout(self) -> tuple[str, list[bytes]]:
        self.commands.append("LOGOUT")
        return ("BYE", [b"bye"])


def _sync_with_fake(
    monkeypatch: pytest.MonkeyPatch,
    capabilities: tuple[bytes, ...],
    *,
    enable_ok: bool = True,
    cursor: str | None = None,
) -> FakeImapSession:
    FakeImapSession.instances = []

    def factory(*args: object, **kwargs: object) -> FakeImapSession:
        del args, kwargs
        return FakeImapSession(capabilities, enable_ok=enable_ok)

    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    connector = ImapSmtpConnector(imap_host="imap.example.test", smtp_host="smtp.example.test")
    page = asyncio.run(
        connector.sync(
            _connection(ProviderName.IMAP_SMTP),
            cursor=cursor,
            limit=10,
            credential={"username": "u", "password": "p"},
        )
    )
    del page
    return FakeImapSession.instances[0]


def test_imap_enables_condstore_before_select_when_only_enable_is_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server may require ENABLE before it honours MODSEQ.

    No server in the compatibility matrix does this today, so the case is pinned
    by a fake: a literal token check would fall back to UID-only pagination and
    report nothing, which is the failure mode this branch exists to prevent.
    """

    session = _sync_with_fake(monkeypatch, (b"IMAP4rev1", b"ENABLE", b"IDLE"), cursor="77:5:1")

    assert "ENABLE CONDSTORE" in session.commands
    assert session.commands.index("ENABLE CONDSTORE") < session.commands.index("SELECT")
    assert any(argument.startswith("MODSEQ") for argument in session.search_arguments)


def test_imap_uses_modseq_when_condstore_is_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _sync_with_fake(monkeypatch, (b"IMAP4rev1", b"CONDSTORE"), cursor="77:5:1")

    # Already in effect for the session, so there is nothing to negotiate.
    assert "ENABLE CONDSTORE" not in session.commands
    assert any(argument.startswith("MODSEQ") for argument in session.search_arguments)


def test_imap_treats_qresync_as_condstore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC 7162 3.1.8: QRESYNC implies CONDSTORE."""

    session = _sync_with_fake(monkeypatch, (b"IMAP4rev1", b"QRESYNC"), cursor="77:5:1")

    assert "ENABLE CONDSTORE" not in session.commands
    assert any(argument.startswith("MODSEQ") for argument in session.search_arguments)


def test_imap_without_condstore_or_enable_falls_back_to_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _sync_with_fake(monkeypatch, (b"IMAP4rev1", b"IDLE"), cursor="77:5:1")

    assert not any(command.startswith("ENABLE") for command in session.commands)
    assert any(argument.startswith("UID 6:") for argument in session.search_arguments)
    assert not any(argument.startswith("MODSEQ") for argument in session.search_arguments)


def test_imap_fails_closed_when_enable_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _sync_with_fake(
        monkeypatch, (b"IMAP4rev1", b"ENABLE"), enable_ok=False, cursor="77:5:1"
    )

    assert "ENABLE CONDSTORE" in session.commands
    assert not any(argument.startswith("MODSEQ") for argument in session.search_arguments)
    assert any(argument.startswith("UID 6:") for argument in session.search_arguments)
