import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from mailhub.domain import DeliveryStatus, MailMessageProjection, MailOutboxOperation, digest_text
from mailhub.events import (
    SUPPORTED_EVENT_TYPES,
    EventEnvelope,
    MailHubEventType,
    action_operation_event,
    build_event,
    message_observed_event,
    validate_event_payload,
)


def test_event_envelope_is_bounded_and_redacts_content_shaped_fields() -> None:
    envelope = EventEnvelope(
        event_type="mail.test.v1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        data={"body_text": "never emitted", "content_sha256": "a" * 64},
    )
    payload = envelope.to_dict()
    assert payload["schema_version"] == "mailhub.event_envelope.v1"
    assert payload["data"] == {
        "body_text": "[REDACTED]",
        "content_sha256": "a" * 64,
    }
    with pytest.raises(ValueError, match="event_trace_invalid"):
        EventEnvelope(event_type="mail.test.v1", tenant_id="tenant-1", trace_id="", data={})
    with pytest.raises(ValueError, match="event_occurred_at_naive"):
        EventEnvelope(
            event_type="mail.test.v1",
            tenant_id="tenant-1",
            trace_id="trace-1",
            data={},
            occurred_at=datetime(2026, 1, 1),
        )


def test_message_and_operation_events_match_schema_fields() -> None:
    body = "bounded"
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-1",
        connection_id=uuid4(),
        thread_id=uuid4(),
        provider_message_ref="provider-1",
        internet_message_id=None,
        sender_address="sender@example.test",
        recipient_addresses=("user@example.test",),
        subject="Update",
        received_at=datetime.now(UTC),
        body_text=body,
        content_sha256=digest_text(body),
    )
    observed = message_observed_event(message, trace_id="trace-1")
    assert observed["event_type"] == "mailhub.message_observed.v1"
    assert observed["data"]["message_id"] == str(message.message_id)  # type: ignore[index]

    operation = MailOutboxOperation(
        operation_id=uuid4(),
        tenant_id="tenant-1",
        subject_id="user-1",
        draft_id=uuid4(),
        connection_id=message.connection_id,
        idempotency_key="send:key-1",
        status=DeliveryStatus.OUTCOME_UNKNOWN,
    )
    event = action_operation_event(operation, trace_id="trace-1")
    assert event["data"]["outcome_unknown"] is True  # type: ignore[index]


def test_registered_business_event_is_routable_and_body_free() -> None:
    payload = build_event(
        MailHubEventType.CANDIDATE_APPLIED,
        tenant_id="tenant-1",
        subject_id="user-1",
        trace_id="trace-1",
        idempotency_key="candidate:1:apply:2",
        data={
            "candidate_id": str(uuid4()),
            "candidate_revision": 2,
            "result_ref": "project-task:123",
            "body_text": "must never leave the bounded event contract",
        },
    )
    assert payload["event_type"] == "mail.candidate.applied"
    assert payload["data"]["body_text"] == "[REDACTED]"  # type: ignore[index]
    validate_event_payload(payload)


def test_unregistered_event_and_malformed_payload_fail_closed() -> None:
    with pytest.raises(ValueError, match="event_type_not_registered"):
        build_event("mail.typo.event", tenant_id="tenant-1", trace_id="trace-1", data={})
    with pytest.raises(ValueError, match="event_schema_version_invalid"):
        validate_event_payload(
            {
                "schema_version": "mailhub.event_envelope.v2",
                "event_id": str(uuid4()),
                "event_type": "mail.sync.started",
                "occurred_at": datetime.now(UTC).isoformat(),
                "trace_id": "trace-1",
                "tenant_id": "tenant-1",
                "data": {},
            }
        )
    with pytest.raises(ValueError, match="event_data_not_redacted"):
        validate_event_payload(
            {
                "schema_version": "mailhub.event_envelope.v1",
                "event_id": str(uuid4()),
                "event_type": "mail.sync.started",
                "occurred_at": datetime.now(UTC).isoformat(),
                "trace_id": "trace-1",
                "tenant_id": "tenant-1",
                "data": {"body_text": "raw body must not cross the host boundary"},
            }
        )


def test_business_event_registry_schema_matches_runtime() -> None:
    schema = json.loads(
        Path(__file__)
        .parents[1]
        .joinpath("schemas", "events", "mailhub.business_event_types.v1.json")
        .read_text(encoding="utf-8")
    )
    assert set(schema["enum"]) == {item.value for item in MailHubEventType}
    assert set(schema["enum"]).issubset(SUPPORTED_EVENT_TYPES)
