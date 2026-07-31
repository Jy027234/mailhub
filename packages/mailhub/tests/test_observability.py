import logging
from collections.abc import Mapping
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any

import pytest

from mailhub.observability import (
    InMemoryTelemetryPort,
    OpenTelemetryTelemetryPort,
    StructuredRedactingLogger,
    redact_event,
)
from mailhub.storage import InMemoryMailRepository


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.events: list[tuple[str, Mapping[str, object] | None]] = []

    def __enter__(self) -> "_Span":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        self.attributes.update(attributes)

    def add_event(self, name: str, attributes: Mapping[str, object] | None = None) -> None:
        self.events.append((name, attributes))


class _Tracer:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.spans: list[_Span] = []

    def start_as_current_span(self, name: str) -> AbstractContextManager[_Span]:
        self.names.append(name)
        span = _Span()
        self.spans.append(span)
        return span


class _Counter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, Mapping[str, object] | None]] = []

    def add(self, amount: int = 1, attributes: Mapping[str, object] | None = None) -> None:
        self.calls.append((amount, attributes))


class _Meter:
    def __init__(self) -> None:
        self.counter = _Counter()

    def create_counter(
        self, name: str, *, unit: str | None = None, description: str | None = None
    ) -> _Counter:
        del name, unit, description
        return self.counter


def test_audit_redaction_removes_secret_and_body_fields() -> None:
    safe = redact_event(
        {
            "tenant_id": "tenant-1",
            "target_ref": "message-1",
            "body_text": "private body",
            "access_token": "secret-token",
            "content_sha256": "a" * 64,
            "nested": {"password": "pw"},
        }
    )
    assert safe["body_text"] == "[REDACTED]"
    assert safe["access_token"] == "[REDACTED]"
    assert safe["nested"] == {"password": "[REDACTED]"}
    assert safe["content_sha256"] == "a" * 64


def test_redaction_bounds_nested_provider_payloads() -> None:
    nested: object = {"value": "safe"}
    for _ in range(8):
        nested = {"nested": nested}
    safe = redact_event(
        {
            "payload": nested,
            "nested_mime": "raw mime",
            "attachment_bytes": b"private",
        }
    )
    assert "[TRUNCATED]" in str(safe["payload"])
    assert safe["nested_mime"] == "[REDACTED]"
    assert safe["attachment_bytes"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_in_memory_audit_persists_only_redacted_event() -> None:
    repository = InMemoryMailRepository()
    await repository.append_audit(
        {
            "tenant_id": "tenant-1",
            "subject_id": "user-1",
            "event_type": "test",
            "target_ref": "target-1",
            "body": "not stored",
            "refresh_token": "not stored",
        }
    )
    assert repository.audits[0]["body"] == "[REDACTED]"
    assert repository.audits[0]["refresh_token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_telemetry_recorder_is_bounded_and_redacted() -> None:
    telemetry = InMemoryTelemetryPort(max_records=2)
    await telemetry.record(
        name="mail.test",
        fields={"tenant_id": "tenant-1", "body_text": "private", "attempt": 1},
    )
    await telemetry.record(name="mail.test.2", fields={"access_token": "secret"})
    await telemetry.record(name="mail.test.3", fields={"status": "ok"})

    records = telemetry.snapshot()
    assert len(records) == 2
    assert records[0].name == "mail.test.2"
    assert records[0].fields["access_token"] == "[REDACTED]"
    assert records[1].fields["status"] == "ok"


@pytest.mark.asyncio
async def test_structured_logger_and_opentelemetry_bridge_redact_payloads(caplog: Any) -> None:
    logger = logging.getLogger("mailhub.test")
    adapter = StructuredRedactingLogger(logger)
    with caplog.at_level(logging.INFO, logger="mailhub.test"):
        await adapter.record(
            name="mail.send",
            fields={"status": "accepted", "body_text": "private", "access_token": "secret"},
        )
    record = caplog.records[-1]
    assert record.getMessage() == "mailhub_event"
    assert record.mailhub_event["fields"]["body_text"] == "[REDACTED]"
    assert record.mailhub_event["fields"]["access_token"] == "[REDACTED]"

    tracer = _Tracer()
    meter = _Meter()
    telemetry = OpenTelemetryTelemetryPort(tracer=tracer, meter=meter)
    await telemetry.record(
        name="mail.send",
        fields={"status": "accepted", "body_text": "private", "provider": "sandbox"},
    )
    assert tracer.names == ["mailhub.mail.send"]
    assert tracer.spans[0].attributes["body_text"] == "[REDACTED]"
    assert tracer.spans[0].attributes["provider"] == "sandbox"
    assert meter.counter.calls == [
        (1, {"event": "mail.send", "provider": "sandbox", "status": "accepted"})
    ]
