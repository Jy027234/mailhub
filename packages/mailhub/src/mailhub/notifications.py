"""Bounded Gmail Pub/Sub and Microsoft Graph notification contracts.

Provider-specific bearer/JWT validation and subscription ownership remain in
the host. These parsers accept only an already authenticated Gmail push or a
Graph payload whose clientState matches the host-held value, then return
metadata suitable for receipt deduplication and a follow-up sync job. They do
not hydrate mail content or treat a notification as proof that synchronization
completed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from mailhub.domain import ProviderName

_MAX_GMAIL_DATA_BYTES = 16_384
_MAX_GRAPH_NOTIFICATIONS = 100


class ProviderNotificationError(ValueError):
    """Raised when a provider notification cannot be trusted or normalized."""


class NotificationChangeKind(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    LIFECYCLE = "lifecycle"


class ProviderLifecycleEvent(StrEnum):
    """Known Graph lifecycle signals that require durable host action."""

    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    SUBSCRIPTION_REMOVED = "subscription_removed"
    MISSED = "missed"


def classify_provider_lifecycle(value: str | None) -> ProviderLifecycleEvent | None:
    """Normalize known provider lifecycle spellings without trusting unknown values."""

    if value is None or not value.strip():
        return None
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return {
        "reauthorizationrequired": ProviderLifecycleEvent.REAUTHORIZATION_REQUIRED,
        "subscriptionremoved": ProviderLifecycleEvent.SUBSCRIPTION_REMOVED,
        "missed": ProviderLifecycleEvent.MISSED,
    }.get(normalized)


@dataclass(frozen=True, slots=True)
class ProviderNotification:
    provider: ProviderName
    notification_id: str
    subscription_ref: str
    resource_ref: str
    change_kind: NotificationChangeKind
    received_at: datetime
    cursor_hint: str | None = None
    lifecycle_event: str | None = None

    def __post_init__(self) -> None:
        _text(self.notification_id, "notification_id", 512)
        _text(self.subscription_ref, "subscription_ref", 1000)
        _text(self.resource_ref, "resource_ref", 2000)
        if self.cursor_hint is not None:
            _text(self.cursor_hint, "cursor_hint", 200)
        if self.lifecycle_event is not None:
            _text(self.lifecycle_event, "lifecycle_event", 200)
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ProviderNotificationError("notification_received_at_naive")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))

    def metadata(self) -> dict[str, object]:
        """Return a body-free, secret-free event payload."""

        return {
            "provider": self.provider.value,
            "notification_id": self.notification_id,
            "subscription_ref": self.subscription_ref,
            "resource_sha256": hashlib.sha256(self.resource_ref.encode("utf-8")).hexdigest(),
            "change_kind": self.change_kind.value,
            "cursor_hint": self.cursor_hint,
            "lifecycle_event": self.lifecycle_event,
            "received_at": self.received_at.isoformat(),
        }


def parse_gmail_pubsub_notification(
    payload: Mapping[str, object],
    *,
    oidc_verified: bool,
    expected_subscription: str | None = None,
    now: datetime | None = None,
) -> ProviderNotification:
    """Parse a Gmail Pub/Sub push after host OIDC verification.

    Gmail's push data contains only an account address and a historyId. The
    account address is intentionally reduced to a digest in the normalized
    resource ref; the sync worker resolves the actual connection by its
    subscription/account mapping.
    """

    if oidc_verified is not True:
        raise ProviderNotificationError("gmail_pubsub_oidc_verification_required")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ProviderNotificationError("gmail_pubsub_message_missing")
    notification_id = _text_value(message.get("messageId"), "notification_id", 512)
    encoded = _text_value(message.get("data"), "data", 30_000)
    if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded) is None:
        raise ProviderNotificationError("gmail_pubsub_data_encoding_invalid")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ProviderNotificationError("gmail_pubsub_data_invalid") from exc
    if not raw or len(raw) > _MAX_GMAIL_DATA_BYTES:
        raise ProviderNotificationError("gmail_pubsub_data_too_large")
    try:
        decoded: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderNotificationError("gmail_pubsub_data_json_invalid") from exc
    if not isinstance(decoded, dict):
        raise ProviderNotificationError("gmail_pubsub_data_payload_invalid")
    email_address = _text_value(decoded.get("emailAddress"), "email_address", 320).casefold()
    history_id = _text_value(decoded.get("historyId"), "history_id", 40)
    if not history_id.isdigit():
        raise ProviderNotificationError("gmail_history_id_invalid")
    subscription_ref = _text_value(payload.get("subscription"), "subscription_ref", 1000)
    if expected_subscription is not None and not hmac.compare_digest(
        subscription_ref, expected_subscription
    ):
        raise ProviderNotificationError("gmail_subscription_mismatch")
    account_digest = hashlib.sha256(email_address.encode("utf-8")).hexdigest()
    return ProviderNotification(
        provider=ProviderName.GMAIL,
        notification_id=notification_id,
        subscription_ref=subscription_ref,
        resource_ref=f"gmail-account:{account_digest}",
        change_kind=NotificationChangeKind.UPDATED,
        received_at=(now or datetime.now(UTC)).astimezone(UTC),
        cursor_hint=history_id,
    )


def parse_graph_change_notifications(
    payload: Mapping[str, object],
    *,
    expected_client_state: str,
    now: datetime | None = None,
) -> tuple[ProviderNotification, ...]:
    """Parse Graph change/lifecycle notifications with constant-time clientState binding."""

    _text(expected_client_state, "expected_client_state", 512)
    raw_items = payload.get("value")
    if not isinstance(raw_items, list) or not raw_items:
        raise ProviderNotificationError("graph_notification_value_missing")
    if len(raw_items) > _MAX_GRAPH_NOTIFICATIONS:
        raise ProviderNotificationError("graph_notification_batch_too_large")
    received_at = (now or datetime.now(UTC)).astimezone(UTC)
    result: list[ProviderNotification] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ProviderNotificationError("graph_notification_item_invalid")
        subscription_ref = _text_value(raw.get("subscriptionId"), "subscription_ref", 512)
        client_state = _text_value(raw.get("clientState"), "client_state", 512)
        if not hmac.compare_digest(client_state, expected_client_state):
            raise ProviderNotificationError("graph_client_state_mismatch")
        lifecycle = raw.get("lifecycleEvent")
        lifecycle_event = (
            _text_value(lifecycle, "lifecycle_event", 200) if lifecycle is not None else None
        )
        resource_value = raw.get("resource")
        if lifecycle_event is not None and resource_value is None:
            # Graph lifecycle notifications are about the subscription state,
            # not a changed mail resource.  The official payload may omit
            # `resource`; keep a stable, body-free ref for reconciliation.
            resource_ref = f"subscription:{subscription_ref}"
        else:
            resource_ref = _graph_resource(
                resource_value, allow_subscription=lifecycle_event is not None
            )
        change_value = raw.get("changeType")
        change_kind = _graph_change_kind(change_value, lifecycle_event)
        resource_data = raw.get("resourceData")
        resource_id = (
            _text_value(resource_data.get("id"), "resource_id", 512)
            if isinstance(resource_data, dict) and resource_data.get("id") is not None
            else None
        )
        identity = "|".join(
            item
            for item in (subscription_ref, resource_ref, resource_id, change_kind.value)
            if item
        )
        notification_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        result.append(
            ProviderNotification(
                provider=ProviderName.MICROSOFT_GRAPH,
                notification_id=notification_id,
                subscription_ref=subscription_ref,
                resource_ref=resource_ref,
                change_kind=change_kind,
                received_at=received_at,
                lifecycle_event=lifecycle_event,
            )
        )
    return tuple(result)


def _graph_change_kind(value: object, lifecycle_event: str | None) -> NotificationChangeKind:
    if lifecycle_event is not None:
        return NotificationChangeKind.LIFECYCLE
    if not isinstance(value, str):
        raise ProviderNotificationError("graph_change_type_missing")
    normalized = value.casefold()
    try:
        return NotificationChangeKind(normalized)
    except ValueError as exc:
        raise ProviderNotificationError("graph_change_type_invalid") from exc


def _graph_resource(value: object, *, allow_subscription: bool = False) -> str:
    resource = _text_value(value, "resource_ref", 2000)
    parsed = urlsplit(resource)
    if parsed.scheme:
        if parsed.scheme != "https" or parsed.hostname not in {
            "graph.microsoft.com",
            "graph.microsoft.us",
            "dod-graph.microsoft.us",
            "microsoftgraph.chinacloudapi.cn",
        }:
            raise ProviderNotificationError("graph_resource_host_invalid")
        if parsed.fragment or parsed.query:
            raise ProviderNotificationError("graph_resource_query_invalid")
    elif not resource.startswith("/"):
        raise ProviderNotificationError("graph_resource_path_invalid")
    if (
        "/messages" not in resource
        and "/mailFolders" not in resource
        and not (allow_subscription and "/subscriptions/" in resource)
    ):
        raise ProviderNotificationError("graph_resource_mail_path_invalid")
    return resource


def _text_value(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProviderNotificationError(f"notification_{field_name}_invalid")
    _text(value, field_name, maximum)
    return value


def _text(value: str, field_name: str, maximum: int) -> None:
    if not value.strip() or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ProviderNotificationError(f"notification_{field_name}_invalid")
