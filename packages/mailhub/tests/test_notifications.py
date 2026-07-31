from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from mailhub.domain import ProviderName
from mailhub.notifications import (
    NotificationChangeKind,
    ProviderLifecycleEvent,
    ProviderNotificationError,
    classify_provider_lifecycle,
    parse_gmail_pubsub_notification,
    parse_graph_change_notifications,
)


def test_provider_lifecycle_classification_is_bounded_and_unknown_is_metadata_only() -> None:
    assert (
        classify_provider_lifecycle("reauthorizationRequired")
        is ProviderLifecycleEvent.REAUTHORIZATION_REQUIRED
    )
    assert (
        classify_provider_lifecycle("subscription_removed")
        is ProviderLifecycleEvent.SUBSCRIPTION_REMOVED
    )
    assert classify_provider_lifecycle("missed") is ProviderLifecycleEvent.MISSED
    assert classify_provider_lifecycle("futureProviderSignal") is None
    assert classify_provider_lifecycle(None) is None


def _gmail_payload(*, history_id: str = "123") -> dict[str, object]:
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"emailAddress": "User@Example.test", "historyId": history_id},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    return {
        "subscription": "projects/project/subscriptions/mailhub-gmail-test",
        "message": {"messageId": "pubsub-message-1", "data": encoded},
    }


def test_gmail_pubsub_is_oidc_bound_and_reduces_account_to_digest() -> None:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    payload = _gmail_payload()
    with pytest.raises(ProviderNotificationError, match="oidc_verification_required"):
        parse_gmail_pubsub_notification(payload, oidc_verified=False, now=now)

    notification = parse_gmail_pubsub_notification(
        payload,
        oidc_verified=True,
        expected_subscription="projects/project/subscriptions/mailhub-gmail-test",
        now=now,
    )
    assert notification.provider is ProviderName.GMAIL
    assert notification.change_kind is NotificationChangeKind.UPDATED
    assert notification.cursor_hint == "123"
    assert notification.resource_ref.startswith("gmail-account:")
    metadata = notification.metadata()
    assert metadata["resource_sha256"]
    assert "User@Example.test" not in str(metadata)


def test_gmail_pubsub_rejects_subscription_mismatch_and_invalid_history() -> None:
    payload = _gmail_payload(history_id="not-a-history-id")
    with pytest.raises(ProviderNotificationError, match="history_id_invalid"):
        parse_gmail_pubsub_notification(payload, oidc_verified=True)
    with pytest.raises(ProviderNotificationError, match="subscription_mismatch"):
        parse_gmail_pubsub_notification(
            _gmail_payload(),
            oidc_verified=True,
            expected_subscription="projects/project/subscriptions/other",
        )


def test_gmail_pubsub_rejects_non_base64url_data() -> None:
    payload = _gmail_payload()
    payload["message"] = {"messageId": "pubsub-message-1", "data": "%%%"}
    with pytest.raises(ProviderNotificationError, match="data_encoding_invalid"):
        parse_gmail_pubsub_notification(payload, oidc_verified=True)


def test_graph_change_notifications_bind_client_state_and_support_lifecycle() -> None:
    payload = {
        "value": [
            {
                "subscriptionId": "subscription-1",
                "clientState": "state-1",
                "changeType": "updated",
                "resource": "/users/user-1/mailFolders/inbox/messages/message-1",
                "resourceData": {"id": "message-1"},
            },
            {
                "subscriptionId": "subscription-1",
                "clientState": "state-1",
                "lifecycleEvent": "reauthorizationRequired",
            },
        ]
    }
    notifications = parse_graph_change_notifications(
        payload, expected_client_state="state-1", now=datetime(2026, 7, 29, tzinfo=UTC)
    )
    assert len(notifications) == 2
    assert notifications[0].change_kind is NotificationChangeKind.UPDATED
    assert notifications[1].change_kind is NotificationChangeKind.LIFECYCLE
    assert notifications[1].lifecycle_event == "reauthorizationRequired"
    assert notifications[1].resource_ref == "subscription:subscription-1"
    assert notifications[0].metadata()["resource_sha256"]
    assert "state-1" not in str(notifications[0].metadata())


def test_graph_change_notifications_reject_state_and_resource_confusion() -> None:
    payload = {
        "value": [
            {
                "subscriptionId": "subscription-1",
                "clientState": "wrong",
                "changeType": "created",
                "resource": "https://evil.example.test/users/user/messages/message",
            }
        ]
    }
    with pytest.raises(ProviderNotificationError, match="client_state_mismatch"):
        parse_graph_change_notifications(payload, expected_client_state="state-1")

    invalid_resource_payload = {
        "value": [
            {
                "subscriptionId": "subscription-1",
                "clientState": "state-1",
                "changeType": "created",
                "resource": "https://evil.example.test/users/user/messages/message",
            }
        ]
    }
    with pytest.raises(ProviderNotificationError, match="resource_host_invalid"):
        parse_graph_change_notifications(invalid_resource_payload, expected_client_state="state-1")
