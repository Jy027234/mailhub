"""Provider notification subscription lifecycle orchestration.

The provider-specific watch/subscription API is deliberately kept behind
``ProviderSubscriptionPort``.  This module owns only the provider-neutral
durable state transition: a bounded subscription lease is written alongside
the mailbox cursor, renewal is idempotent, and revocation cancels all active
subscriptions before a connection is considered revoked/deleted.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from mailhub.domain import (
    ConnectionStatus,
    MailboxConnection,
    MailboxSyncState,
    ProviderName,
    SubscriptionStatus,
    utc_now,
)
from mailhub.errors import ProviderFailureError
from mailhub.events import MailHubEventType
from mailhub.ports import (
    EventPublisherPort,
    MailRepository,
    ProviderSubscriptionLease,
    ProviderSubscriptionPort,
)

_ACTIVE_STATUSES = frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.RENEWAL_REQUIRED})
_SAFE_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


class MailSubscriptionCoordinator:
    """Persist and reconcile host-owned Gmail/Graph subscription leases."""

    def __init__(
        self,
        repository: MailRepository,
        *,
        subscription_port: ProviderSubscriptionPort,
        event_publisher: EventPublisherPort | None = None,
    ) -> None:
        self.repository = repository
        self.subscription_port = subscription_port
        self.event_publisher = event_publisher

    async def ensure(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        callback_endpoint: str,
        desired_expiry: datetime,
        folder_ref: str = "INBOX",
        client_state_ref: str | None = None,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> MailboxSyncState:
        self._assert_connection(tenant_id, subject_id, connection)
        current_time = _aware_now(now)
        normalized_expiry = _aware_now(desired_expiry)
        if normalized_expiry <= current_time:
            raise ValueError("subscription_expiry_in_past")
        current = await self.repository.get_sync_state(
            tenant_id=tenant_id, connection_id=connection.connection_id, folder_ref=folder_ref
        )
        expected_ref = current.subscription_ref if current is not None else None
        lease = await self.subscription_port.ensure_subscription(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection.connection_id,
            provider=connection.provider,
            callback_endpoint=callback_endpoint,
            desired_expiry=normalized_expiry,
            client_state_ref=client_state_ref,
            idempotency_key=idempotency_key,
        )
        self._assert_lease(connection, callback_endpoint, lease, current_time)
        next_state = self._state_from_lease(
            tenant_id=tenant_id,
            folder_ref=folder_ref,
            current=current,
            lease=lease,
            callback_endpoint=callback_endpoint,
            client_state_ref=client_state_ref,
            now=current_time,
        )
        saved = await self.repository.save_sync_state(
            next_state, expected_subscription_ref=expected_ref
        )
        event_type = (
            MailHubEventType.SUBSCRIPTION_RENEWED
            if current is not None and current.subscription_ref == lease.subscription_ref
            else MailHubEventType.SUBSCRIPTION_CREATED
        )
        await self._record_event(
            event_type,
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            state=saved,
            idempotency_key=f"subscription:{connection.connection_id}:{folder_ref}:{lease.subscription_ref}:{event_type.value}",
        )
        return saved

    async def cancel(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        folder_ref: str = "INBOX",
        request_id: str,
        now: datetime | None = None,
    ) -> MailboxSyncState | None:
        self._assert_connection(tenant_id, subject_id, connection, require_active=False)
        current = await self.repository.get_sync_state(
            tenant_id=tenant_id, connection_id=connection.connection_id, folder_ref=folder_ref
        )
        if current is None or not current.subscription_ref:
            return current
        if current.subscription_status is SubscriptionStatus.CANCELLED:
            return current
        result = await self.subscription_port.cancel_subscription(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection.connection_id,
            provider=connection.provider,
            subscription_ref=current.subscription_ref,
            request_id=request_id,
        )
        provider_request_id = _optional_bounded(result.get("provider_request_id"), 512)
        next_state = replace(
            current,
            subscription_status=SubscriptionStatus.CANCELLED,
            subscription_provider_request_id=provider_request_id
            or current.subscription_provider_request_id,
            updated_at=_aware_now(now),
        )
        saved = await self.repository.save_sync_state(
            next_state, expected_subscription_ref=current.subscription_ref
        )
        await self._record_event(
            MailHubEventType.SUBSCRIPTION_CANCELLED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            state=saved,
            idempotency_key=f"subscription:{connection.connection_id}:{folder_ref}:{current.subscription_ref}:cancelled",
        )
        return saved

    async def renew_if_due(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        folder_ref: str = "INBOX",
        renewal_window: timedelta = timedelta(hours=24),
        desired_expiry: datetime | None = None,
        now: datetime | None = None,
    ) -> MailboxSyncState | None:
        """Renew a subscription only inside a bounded expiry window.

        The host remains responsible for provider-specific maximum TTL.  The
        default one-day target is valid for both the initial Gmail/Graph
        read-only activation contract and can be overridden by a scheduler
        that knows the provider's current policy.
        """

        if renewal_window <= timedelta(0) or renewal_window > timedelta(days=7):
            raise ValueError("subscription_renewal_window_invalid")
        current_time = _aware_now(now)
        current = await self.repository.get_sync_state(
            tenant_id=tenant_id, connection_id=connection.connection_id, folder_ref=folder_ref
        )
        if current is None or not current.subscription_ref:
            return None
        if current.subscription_status is SubscriptionStatus.CANCELLED:
            return current
        expires_at = current.subscription_expires_at
        if expires_at is not None and expires_at > current_time + renewal_window:
            return current
        target_expiry = desired_expiry or current_time + timedelta(days=1)
        request_key = (
            f"mailhub:subscription:renew:{connection.connection_id}:{folder_ref}:"
            f"{int(current_time.timestamp()) // 3600}"
        )
        try:
            return await self.ensure(
                tenant_id=tenant_id,
                subject_id=subject_id,
                connection=connection,
                callback_endpoint=current.subscription_callback_endpoint or "",
                desired_expiry=target_expiry,
                folder_ref=folder_ref,
                client_state_ref=current.subscription_client_state_ref,
                idempotency_key=request_key,
                now=current_time,
            )
        except Exception as exc:
            with suppress(Exception):
                await self.mark_failed(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    connection=connection,
                    folder_ref=folder_ref,
                    error_code=_error_code(exc),
                    now=current_time,
                )
            # Preserve the provider/scheduler failure; failure-state
            # persistence is best-effort and separately auditable.
            raise

    async def mark_expired(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        folder_ref: str = "INBOX",
        now: datetime | None = None,
    ) -> MailboxSyncState | None:
        current = await self.repository.get_sync_state(
            tenant_id=tenant_id, connection_id=connection.connection_id, folder_ref=folder_ref
        )
        current_time = _aware_now(now)
        if current is None or not current.subscription_ref:
            return current
        if (
            current.subscription_expires_at is not None
            and current.subscription_expires_at > current_time
        ):
            raise ValueError("subscription_not_expired")
        return await self._set_status(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            current=current,
            status=SubscriptionStatus.EXPIRED,
            folder_ref=folder_ref,
            now=current_time,
            event_type=MailHubEventType.SUBSCRIPTION_EXPIRED,
        )

    async def mark_failed(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        folder_ref: str = "INBOX",
        error_code: str = "subscription_renewal_failed",
        now: datetime | None = None,
    ) -> MailboxSyncState | None:
        current = await self.repository.get_sync_state(
            tenant_id=tenant_id, connection_id=connection.connection_id, folder_ref=folder_ref
        )
        if current is None or not current.subscription_ref:
            return current
        return await self._set_status(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            current=current,
            status=SubscriptionStatus.FAILED,
            folder_ref=folder_ref,
            now=_aware_now(now),
            event_type=MailHubEventType.SUBSCRIPTION_FAILED,
            error_code=_safe_error_code(error_code),
        )

    async def mark_renewal_required(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        folder_ref: str = "INBOX",
        error_code: str = "provider_lifecycle_requires_renewal",
        now: datetime | None = None,
    ) -> MailboxSyncState | None:
        """Fence a provider lifecycle signal into an immediately renewable lease."""

        current = await self.repository.get_sync_state(
            tenant_id=tenant_id, connection_id=connection.connection_id, folder_ref=folder_ref
        )
        if current is None or not current.subscription_ref:
            return current
        current_time = _aware_now(now)
        next_state = replace(
            current,
            subscription_status=SubscriptionStatus.RENEWAL_REQUIRED,
            # An explicit lifecycle removal/reauthorization signal must not
            # wait for a stale provider expiry before the scheduler retries.
            subscription_expires_at=current_time,
            updated_at=current_time,
        )
        saved = await self.repository.save_sync_state(
            next_state, expected_subscription_ref=current.subscription_ref
        )
        await self._record_event(
            MailHubEventType.SUBSCRIPTION_FAILED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            state=saved,
            idempotency_key=(
                f"subscription:{connection.connection_id}:{folder_ref}:"
                f"{current.subscription_ref}:renewal-required"
            ),
            error_code=_safe_error_code(error_code),
        )
        return saved

    async def _set_status(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        current: MailboxSyncState,
        status: SubscriptionStatus,
        folder_ref: str,
        now: datetime,
        event_type: MailHubEventType,
        error_code: str | None = None,
    ) -> MailboxSyncState:
        self._assert_connection(tenant_id, subject_id, connection, require_active=False)
        next_state = replace(current, subscription_status=status, updated_at=now)
        saved = await self.repository.save_sync_state(
            next_state, expected_subscription_ref=current.subscription_ref
        )
        await self._record_event(
            event_type,
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            state=saved,
            idempotency_key=f"subscription:{connection.connection_id}:{folder_ref}:{current.subscription_ref}:{status.value}",
            error_code=error_code,
        )
        return saved

    async def record_notification(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        subscription_ref: str,
        received_at: datetime,
        folder_ref: str = "INBOX",
        notification_id: str | None = None,
        change_kind: str | None = None,
        lifecycle_event: str | None = None,
        cursor_hint: str | None = None,
        trace_id: str | None = None,
    ) -> MailboxSyncState:
        """Attach a verified notification watermark to durable sync state.

        Signature/OIDC/clientState validation and receipt de-duplication happen
        before this method.  A mismatched subscription is rejected so a
        notification from another mailbox cannot advance the cursor state.
        """

        self._assert_connection(
            tenant_id,
            subject_id,
            connection,
            allow_degraded=True,
        )
        current = await self.repository.get_sync_state(
            tenant_id=tenant_id, connection_id=connection.connection_id, folder_ref=folder_ref
        )
        if current is None or current.subscription_ref != subscription_ref:
            raise ProviderFailureError("subscription_notification_mismatch")
        if current.subscription_status not in _ACTIVE_STATUSES:
            raise ProviderFailureError("subscription_notification_inactive")
        received = _aware_now(received_at)
        if current.watermark is not None and received < current.watermark:
            # Older, valid notifications are harmless but must not move the
            # watermark backwards.
            received = current.watermark
        next_state = replace(
            current,
            subscription_status=(
                SubscriptionStatus.ACTIVE
                if current.subscription_status in _ACTIVE_STATUSES
                else current.subscription_status
            ),
            watermark=received,
            updated_at=utc_now(),
        )
        saved = await self.repository.save_sync_state(
            next_state, expected_subscription_ref=current.subscription_ref
        )
        extra_metadata: dict[str, object] = {}
        if notification_id is not None:
            extra_metadata["notification_id_sha256"] = hashlib.sha256(
                notification_id.encode("utf-8")
            ).hexdigest()
        if change_kind is not None:
            extra_metadata["change_kind"] = _bounded_metadata(change_kind, "change_kind")
        if lifecycle_event is not None:
            extra_metadata["lifecycle_event"] = _bounded_metadata(
                lifecycle_event, "lifecycle_event"
            )
        if cursor_hint is not None:
            extra_metadata["cursor_hint_sha256"] = hashlib.sha256(
                cursor_hint.encode("utf-8")
            ).hexdigest()
        if trace_id is not None:
            extra_metadata["trace_id"] = _bounded_metadata(trace_id, "trace_id")
        await self._record_event(
            MailHubEventType.PROVIDER_NOTIFICATION_RECEIVED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            state=saved,
            idempotency_key=(
                f"provider-notification:{connection.connection_id}:"
                f"{subscription_ref}:{notification_id or received.isoformat()}"
            ),
            extra_metadata=extra_metadata,
        )
        return saved

    async def _record_event(
        self,
        event_type: MailHubEventType,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        state: MailboxSyncState,
        idempotency_key: str,
        error_code: str | None = None,
        extra_metadata: Mapping[str, object] | None = None,
    ) -> None:
        metadata: dict[str, object] = {
            "provider": connection.provider.value,
            "connection_id": str(connection.connection_id),
            "folder_ref": state.folder_ref,
            "subscription_status": state.subscription_status.value,
            "subscription_ref_sha256": hashlib.sha256(
                (state.subscription_ref or "").encode("utf-8")
            ).hexdigest(),
            "expires_at": (
                state.subscription_expires_at.isoformat()
                if state.subscription_expires_at is not None
                else None
            ),
            "subscription_provider_request_id": state.subscription_provider_request_id,
        }
        if error_code is not None:
            metadata["error_code"] = error_code
        if extra_metadata:
            metadata.update(extra_metadata)
        await self.repository.append_audit(
            {
                "event_type": event_type.value,
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "target_ref": str(connection.connection_id),
                **metadata,
            }
        )
        if self.event_publisher is not None:
            from mailhub.events import build_event

            try:
                await self.event_publisher.publish(
                    build_event(
                        event_type,
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        trace_id=f"subscription:{connection.connection_id}",
                        idempotency_key=idempotency_key,
                        data=metadata,
                        occurred_at=state.updated_at,
                    )
                )
            except Exception:
                # The durable sync state and audit record remain authoritative;
                # a host event outage must not drop the provider notification
                # or prevent its incremental sync job from being queued.
                with suppress(Exception):
                    await self.repository.append_audit(
                        {
                            "event_type": "mail.event.publish_failed",
                            "tenant_id": tenant_id,
                            "subject_id": subject_id,
                            "target_ref": str(connection.connection_id),
                            "published_event_type": event_type.value,
                            "idempotency_key": idempotency_key,
                        }
                    )

    @staticmethod
    def _assert_connection(
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        *,
        require_active: bool = True,
        allow_degraded: bool = False,
    ) -> None:
        if connection.tenant_id != tenant_id or connection.subject_id != subject_id:
            raise ProviderFailureError("subscription_scope_mismatch")
        if connection.provider not in {ProviderName.GMAIL, ProviderName.MICROSOFT_GRAPH}:
            raise ProviderFailureError("subscription_provider_unsupported")
        if require_active and connection.status is not ConnectionStatus.ACTIVE:
            if allow_degraded and connection.status is ConnectionStatus.DEGRADED:
                return
            raise ProviderFailureError("subscription_connection_not_active")
        if not require_active and connection.status not in {
            ConnectionStatus.ACTIVE,
            ConnectionStatus.REVOKED,
            ConnectionStatus.DELETING,
        }:
            raise ProviderFailureError("subscription_connection_not_cancellable")

    @staticmethod
    def _assert_lease(
        connection: MailboxConnection,
        callback_endpoint: str,
        lease: ProviderSubscriptionLease,
        now: datetime,
    ) -> None:
        if (
            lease.connection_id != connection.connection_id
            or lease.provider is not connection.provider
        ):
            raise ProviderFailureError("subscription_lease_identity_mismatch")
        if lease.callback_endpoint != callback_endpoint:
            raise ProviderFailureError("subscription_lease_callback_mismatch")
        if lease.expires_at <= now:
            raise ProviderFailureError("subscription_lease_expired")
        if lease.status not in {
            SubscriptionStatus.ACTIVE.value,
            SubscriptionStatus.RENEWAL_REQUIRED.value,
        }:
            raise ProviderFailureError("subscription_lease_status_invalid")

    @staticmethod
    def _state_from_lease(
        *,
        tenant_id: str,
        folder_ref: str,
        current: MailboxSyncState | None,
        lease: ProviderSubscriptionLease,
        callback_endpoint: str,
        client_state_ref: str | None,
        now: datetime,
    ) -> MailboxSyncState:
        return MailboxSyncState(
            tenant_id=tenant_id,
            connection_id=lease.connection_id,
            folder_ref=folder_ref,
            cursor_kind=current.cursor_kind if current else "provider",
            cursor_value=current.cursor_value if current else None,
            subscription_ref=lease.subscription_ref,
            subscription_status=SubscriptionStatus.ACTIVE,
            subscription_expires_at=lease.expires_at,
            subscription_callback_endpoint=callback_endpoint,
            subscription_client_state_ref=client_state_ref or lease.client_state_ref,
            subscription_provider_request_id=lease.provider_request_id,
            lease_owner=current.lease_owner if current else None,
            fencing_token=current.fencing_token if current else 0,
            status=current.status if current else "idle",
            watermark=current.watermark if current else None,
            updated_at=now,
        )


def _aware_now(value: datetime | None) -> datetime:
    current = value or utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("subscription_timestamp_timezone_missing")
    return current.astimezone(UTC)


def _optional_bounded(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ProviderFailureError("subscription_provider_request_id_invalid")
    return value.strip()


def _error_code(exc: Exception) -> str:
    value = getattr(exc, "code", None)
    if isinstance(value, str) and _SAFE_ERROR_CODE_RE.fullmatch(value.strip()):
        return value.strip()
    return type(exc).__name__[:200]


def _safe_error_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if _SAFE_ERROR_CODE_RE.fullmatch(normalized) else "subscription_failure"


def _bounded_metadata(value: str, field_name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"subscription_{field_name}_invalid")
    return value.strip()
