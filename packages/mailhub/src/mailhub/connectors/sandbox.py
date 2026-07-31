"""Deterministic isolated connector for contract and local integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from mailhub.domain import MailboxConnection, ProviderName
from mailhub.ports import (
    ProviderCapabilities,
    ProviderConnector,
    ProviderMessage,
    ProviderSendReceipt,
    ProviderSendRequest,
    ProviderSyncFilter,
    ProviderSyncPage,
)


class SandboxConnector(ProviderConnector):
    """In-memory provider; never claim it as a real mailbox capability."""

    def __init__(self, *, allowed_domains: tuple[str, ...] = ("example.test",)) -> None:
        self._allowed_domains = frozenset(domain.lower() for domain in allowed_domains)
        # Keep fixture messages addressable by a monotonic cursor instead of
        # popping them.  A worker crash before cursor commit must be able to
        # replay the same page, just like a real Provider history/delta feed.
        self._messages: list[ProviderMessage] = []
        self.sent: list[ProviderSendRequest] = []
        self._lock = asyncio.Lock()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=ProviderName.SANDBOX,
            authorization_modes=("fixture",),
            supports_push=False,
            supports_incremental=True,
            supports_backfill=True,
            supports_draft=False,
            supports_send=True,
            supports_labels=False,
            supports_attachments=False,
            supports_search=False,
            notes=("isolated_test_only", "not_production_evidence"),
        )

    async def seed(self, message: ProviderMessage) -> None:
        async with self._lock:
            self._messages.append(message)

    async def sync(
        self,
        connection: MailboxConnection,
        *,
        cursor: str | None,
        limit: int,
        credential: Mapping[str, str],
        sync_filter: ProviderSyncFilter | None = None,
    ) -> ProviderSyncPage:
        del credential
        if connection.provider is not ProviderName.SANDBOX:
            raise RuntimeError("sandbox_connection_required")
        if not 1 <= limit <= 100:
            raise ValueError("limit_invalid")
        try:
            start = int(cursor or "0")
        except ValueError as exc:
            raise ValueError("sandbox_cursor_invalid") from exc
        if start < 0:
            raise ValueError("sandbox_cursor_invalid")
        active_filter = sync_filter or ProviderSyncFilter()
        async with self._lock:
            eligible = tuple(
                message
                for message in self._messages
                if message.folder_ref == active_filter.folder_ref
                and (
                    not active_filter.label_refs
                    or set(active_filter.label_refs).issubset(set(message.labels))
                )
                and (
                    active_filter.received_after is None
                    or message.received_at >= active_filter.received_after
                )
                and (
                    active_filter.received_before is None
                    or message.received_at < active_filter.received_before
                )
            )
            items = tuple(eligible[start : start + limit])
        next_cursor = str(start + len(items))
        return ProviderSyncPage(
            messages=items,
            next_cursor=next_cursor,
            provider_cursor_kind="sandbox_sequence",
            provider_request_id=f"sandbox-sync-{uuid4()}",
        )

    async def send(
        self,
        request: ProviderSendRequest,
        *,
        credential: Mapping[str, str],
    ) -> ProviderSendReceipt:
        if credential.get("mode") != "sandbox":
            raise RuntimeError("sandbox_credential_required")
        domains = {
            address.rsplit("@", 1)[-1].lower()
            for address in (
                *request.recipient_addresses,
                *request.cc_addresses,
                *request.bcc_addresses,
            )
        }
        if not domains.issubset(self._allowed_domains):
            raise RuntimeError("sandbox_recipient_not_allowlisted")
        async with self._lock:
            if any(item.idempotency_key == request.idempotency_key for item in self.sent):
                existing = next(
                    item for item in self.sent if item.idempotency_key == request.idempotency_key
                )
                return ProviderSendReceipt(
                    provider_message_ref=f"sandbox:{existing.operation_id}",
                    provider_thread_ref=existing.thread_ref,
                    accepted_at=datetime.now(UTC),
                    provider_request_id="sandbox-replay",
                    idempotency_key=request.idempotency_key,
                    content_sha256=request.content_sha256,
                )
            self.sent.append(request)
        return ProviderSendReceipt(
            provider_message_ref=f"sandbox:{request.operation_id}",
            provider_thread_ref=request.thread_ref or f"sandbox-thread:{request.operation_id}",
            accepted_at=datetime.now(UTC),
            provider_request_id=f"sandbox-send-{uuid4()}",
            idempotency_key=request.idempotency_key,
            content_sha256=request.content_sha256,
        )

    async def health_check(self, connection: MailboxConnection) -> Mapping[str, object]:
        return {
            "status": "degraded",
            "reason": "sandbox_only",
            "provider": connection.provider.value,
        }
