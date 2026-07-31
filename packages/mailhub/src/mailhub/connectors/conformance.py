"""Provider-neutral connector conformance checks.

These checks are deliberately pure and offline. A real-provider evidence job
can run the same validators around an isolated account without treating a
mock/sandbox result as production support.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC

from mailhub.domain import MailboxConnection, ensure_addresses
from mailhub.ports import (
    ProviderCapabilities,
    ProviderMessage,
    ProviderSendReceipt,
    ProviderSyncPage,
)


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    provider: str
    checks: tuple[str, ...]


def validate_capabilities(
    capabilities: ProviderCapabilities, connection: MailboxConnection
) -> ConformanceReport:
    if capabilities.provider is not connection.provider:
        raise ValueError("capability_provider_mismatch")
    if not capabilities.authorization_modes:
        raise ValueError("capability_authorization_modes_missing")
    if not isinstance(capabilities.supports_incremental, bool):
        raise ValueError("capability_incremental_invalid")
    return ConformanceReport(
        provider=capabilities.provider.value,
        checks=("provider_identity", "authorization_modes", "boolean_capabilities"),
    )


def validate_sync_page(page: ProviderSyncPage, *, requested_limit: int) -> ConformanceReport:
    if not 1 <= requested_limit <= 1000:
        raise ValueError("conformance_limit_invalid")
    if len(page.messages) > requested_limit:
        raise ValueError("sync_page_exceeds_requested_limit")
    if not page.provider_cursor_kind or not page.provider_request_id:
        raise ValueError("sync_page_provenance_missing")
    refs = [message.provider_message_ref for message in page.messages]
    if any(not ref for ref in refs) or len(refs) != len(set(refs)):
        raise ValueError("sync_page_identity_duplicate")
    for message in page.messages:
        validate_message(message)
    return ConformanceReport(
        provider=page.provider_cursor_kind,
        checks=("bounded_page", "request_ref", "cursor_kind", "message_identity", "message_shape"),
    )


def validate_message(message: ProviderMessage) -> None:
    if not message.provider_message_ref or not message.provider_thread_ref:
        raise ValueError("provider_message_identity_missing")
    if not message.sender_address or not message.recipient_addresses:
        raise ValueError("provider_message_participants_missing")
    try:
        ensure_addresses((message.sender_address,))
        ensure_addresses(message.recipient_addresses)
        for addresses in (
            message.cc_addresses,
            message.bcc_addresses,
            message.reply_to_addresses,
        ):
            if addresses:
                ensure_addresses(addresses)
    except ValueError as exc:
        raise ValueError("provider_message_header_addresses_invalid") from exc
    if message.received_at.tzinfo is None or message.received_at.utcoffset() is None:
        raise ValueError("provider_message_timestamp_not_aware")
    if message.received_at.astimezone(UTC) != message.received_at:
        raise ValueError("provider_message_timestamp_not_utc")
    if len(message.content_sha256) != 64:
        raise ValueError("provider_message_digest_missing")
    if message.body_text is None and message.body_object_ref is None:
        raise ValueError("provider_message_body_reference_missing")


def validate_send_receipt(
    receipt: ProviderSendReceipt, *, idempotency_key: str, content_sha256: str
) -> None:
    if receipt.idempotency_key != idempotency_key:
        raise ValueError("send_receipt_idempotency_mismatch")
    if receipt.content_sha256 != content_sha256:
        raise ValueError("send_receipt_content_mismatch")
    if not receipt.provider_message_ref or not receipt.provider_request_id:
        raise ValueError("send_receipt_provenance_missing")
    if receipt.accepted_at.tzinfo is None or receipt.accepted_at.utcoffset() is None:
        raise ValueError("send_receipt_timestamp_not_aware")


def validate_send_retry_identity(
    receipts: Sequence[ProviderSendReceipt],
    *,
    idempotency_key: str,
    content_sha256: str,
) -> ConformanceReport:
    """Ensure retries preserve one provider operation identity."""

    if not receipts:
        raise ValueError("send_retry_sequence_empty")
    for receipt in receipts:
        validate_send_receipt(
            receipt, idempotency_key=idempotency_key, content_sha256=content_sha256
        )
    refs = {receipt.provider_message_ref for receipt in receipts}
    request_refs = {receipt.provider_request_id for receipt in receipts}
    if len(refs) != 1 or not request_refs:
        raise ValueError("send_retry_identity_changed")
    return ConformanceReport(
        provider="send",
        checks=(
            "retry_idempotency",
            "retry_content_digest",
            "retry_receipt_identity",
            "retry_provider_request_refs",
        ),
    )


def validate_cursor_reset(
    page: ProviderSyncPage, *, previous_cursor: str | None, requested_limit: int
) -> ConformanceReport:
    """Validate a cursor invalidation/reset page before reconciliation."""

    validate_sync_page(page, requested_limit=requested_limit)
    if not page.reset_required:
        raise ValueError("cursor_reset_not_declared")
    if previous_cursor is not None and page.next_cursor == previous_cursor:
        raise ValueError("cursor_reset_did_not_advance")
    return ConformanceReport(
        provider=page.provider_cursor_kind,
        checks=("cursor_reset_declared", "cursor_reset_request_ref", "cursor_reset_next_cursor"),
    )


def validate_provider_failure(
    error_code: str, *, retry_after_seconds: float | None = None
) -> ConformanceReport:
    """Classify provider throttling/5xx versus non-retryable authorization."""

    normalized = error_code.strip().casefold()
    if not normalized:
        raise ValueError("provider_error_code_missing")
    retryable = normalized.startswith("http_429") or any(
        normalized.startswith(f"http_{status}") for status in range(500, 600)
    )
    if not retryable:
        raise ValueError("provider_failure_not_retryable")
    if retry_after_seconds is not None and not 0 <= retry_after_seconds <= 86_400:
        raise ValueError("provider_retry_after_invalid")
    checks = ["retryable_status", "bounded_retry_after"]
    if normalized.startswith("http_429"):
        checks.append("throttle_classified")
    else:
        checks.append("server_error_classified")
    return ConformanceReport(provider="provider", checks=tuple(checks))


def validate_revocation_failure(error_code: str) -> ConformanceReport:
    """Require revoked/expired credentials to stop provider access."""

    normalized = error_code.strip().casefold()
    if not normalized or not any(
        marker in normalized for marker in ("credential", "access_token", "revoked", "unauthorized")
    ):
        raise ValueError("revocation_failure_not_identified")
    return ConformanceReport(provider="provider", checks=("revocation_detected", "access_blocked"))


def validate_message_batch(messages: Iterable[ProviderMessage]) -> None:
    refs: set[str] = set()
    for message in messages:
        validate_message(message)
        if message.provider_message_ref in refs:
            raise ValueError("provider_message_identity_duplicate")
        refs.add(message.provider_message_ref)


def validate_sync_page_sequence(
    pages: Sequence[ProviderSyncPage],
    *,
    requested_limit: int,
    allow_replay: bool = True,
) -> ConformanceReport:
    """Validate replay, out-of-order and cursor-reset behavior offline.

    Providers may redeliver a page after a worker crash. Replayed identities
    are accepted only when their content digest is stable; a changed digest is
    an integrity failure. Ordering is deliberately not assumed, while a
    ``reset_required`` page must still carry cursor/request provenance.
    """

    if not pages:
        raise ValueError("sync_sequence_empty")
    seen: dict[str, str] = {}
    duplicate_count = 0
    out_of_order_count = 0
    previous_received_at = None
    provider_kinds: set[str] = set()
    for page in pages:
        validate_sync_page(page, requested_limit=requested_limit)
        provider_kinds.add(page.provider_cursor_kind)
        if page.reset_required and not page.provider_cursor_kind:
            raise ValueError("sync_reset_cursor_kind_missing")
        for message in page.messages:
            existing_digest = seen.get(message.provider_message_ref)
            if existing_digest is not None:
                duplicate_count += 1
                if not allow_replay:
                    raise ValueError("sync_replay_not_allowed")
                if existing_digest != message.content_sha256:
                    raise ValueError("sync_replay_digest_changed")
            else:
                seen[message.provider_message_ref] = message.content_sha256
                if previous_received_at is not None and message.received_at < previous_received_at:
                    out_of_order_count += 1
                previous_received_at = (
                    max(previous_received_at, message.received_at)
                    if previous_received_at is not None
                    else message.received_at
                )
    checks = ["sequence_pages", "replay_identity", "cursor_reset_provenance"]
    if duplicate_count:
        checks.append("replay_redelivery_accepted")
    if out_of_order_count:
        checks.append("out_of_order_tolerated")
    return ConformanceReport(
        provider=next(iter(provider_kinds)),
        checks=tuple(checks),
    )


def validate_sync_replay(
    original: ProviderSyncPage, replay: ProviderSyncPage, *, requested_limit: int
) -> ConformanceReport:
    """Compare a worker-crash replay with the original Provider page."""

    validate_sync_page(original, requested_limit=requested_limit)
    validate_sync_page(replay, requested_limit=requested_limit)
    original_refs = {
        message.provider_message_ref: message.content_sha256 for message in original.messages
    }
    replay_refs = {
        message.provider_message_ref: message.content_sha256 for message in replay.messages
    }
    if original_refs != replay_refs:
        raise ValueError("sync_replay_page_mismatch")
    return ConformanceReport(
        provider=original.provider_cursor_kind,
        checks=("replay_page_identity", "replay_page_digest", "replay_page_provenance"),
    )
