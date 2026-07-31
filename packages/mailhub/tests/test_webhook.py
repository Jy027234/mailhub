import pytest

from mailhub.webhook import (
    WebhookIngress,
    normalize_receipt_route_metadata,
    receipt_event,
    sign_webhook,
)


def test_webhook_verification_and_deduplication_are_bounded() -> None:
    ingress = WebhookIngress(provider="gmail", signing_secret=b"test-secret")
    body = b'{"historyId":"123"}'
    signature = sign_webhook(secret=b"test-secret", body=body)
    first = ingress.receive(
        tenant_id="tenant-1",
        event_id="event-1",
        body=body,
        signature=signature,
        trace_id="trace-1",
    )
    second = ingress.receive(
        tenant_id="tenant-1",
        event_id="event-1",
        body=body,
        signature=signature,
        trace_id="trace-2",
    )
    assert first.status == "accepted"
    assert second.status == "duplicate"
    assert receipt_event(first)["body_sha256"] == first.body_sha256


def test_webhook_invalid_signature_is_quarantined_without_processing() -> None:
    ingress = WebhookIngress(provider="graph", signing_secret=b"test-secret")
    receipt = ingress.receive(
        tenant_id="tenant-1",
        event_id="event-1",
        body=b"untrusted",
        signature="sha256=bad",
        trace_id="trace-1",
    )
    assert receipt.verified is False
    assert receipt.status == "quarantined"


def test_receipt_route_metadata_is_bounded_and_body_free() -> None:
    route = normalize_receipt_route_metadata(
        {
            "subject_id": "subject-1",
            "connection_id": "connection-1",
            "folder_ref": "INBOX",
            "subscription_ref": "watch-1",
            "notification_id": "event-1",
        }
    )
    assert route["subject_id"] == "subject-1"
    assert "raw_body" not in route


@pytest.mark.parametrize(
    "value",
    [
        {"raw_body": "should-not-cross"},
        {"subject_id": "line\nfeed"},
        {"connection_id": ""},
    ],
)
def test_receipt_route_metadata_rejects_unknown_or_unsafe_values(
    value: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="webhook_receipt_route_metadata_invalid"):
        normalize_receipt_route_metadata(value)
