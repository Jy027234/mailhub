"""Dependency-free observability boundary for MailHub.

Deployments may bridge this port to OpenTelemetry. The core emits bounded
identifiers, hashes, status and error codes; raw MIME, tokens and message
bodies are redacted before persistence or export.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from threading import Lock
from types import TracebackType
from typing import Protocol, cast
from uuid import UUID, uuid4


class TelemetryPort(Protocol):
    async def record(self, *, name: str, fields: Mapping[str, object]) -> None: ...


class SpanPort(Protocol):
    """Small OpenTelemetry span surface used by the optional bridge."""

    def __enter__(self) -> SpanPort: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def set_attributes(self, attributes: Mapping[str, object]) -> object: ...

    def add_event(self, name: str, attributes: Mapping[str, object] | None = None) -> object: ...


class TracerPort(Protocol):
    def start_as_current_span(self, name: str) -> AbstractContextManager[SpanPort]: ...


class CounterPort(Protocol):
    def add(self, amount: int = 1, attributes: Mapping[str, object] | None = None) -> None: ...


class MeterPort(Protocol):
    def create_counter(
        self,
        name: str,
        *,
        unit: str | None = None,
        description: str | None = None,
    ) -> CounterPort: ...


@dataclass(frozen=True, slots=True)
class TelemetryRecord:
    """Bounded, redacted telemetry item used by tests and local development.

    The record intentionally contains no body, address, credential or provider
    response.  Production deployments can translate the same contract to
    OpenTelemetry spans/metrics without making MailHub depend on an exporter.
    """

    name: str
    fields: Mapping[str, object]
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    record_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        normalized_name = self.name.strip()
        if not normalized_name or len(normalized_name) > 200:
            raise ValueError("telemetry_name_invalid")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("telemetry_timestamp_not_aware")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "recorded_at", self.recorded_at.astimezone(UTC))


class InMemoryTelemetryPort:
    """Bounded telemetry recorder for contract tests and sandbox operation.

    It is deliberately not presented as a production exporter.  A fixed-size
    ring prevents an unhealthy provider or worker from exhausting process
    memory while still making event assertions straightforward.
    """

    def __init__(self, *, max_records: int = 1_000) -> None:
        if not 1 <= max_records <= 100_000:
            raise ValueError("telemetry_max_records_invalid")
        self.max_records = max_records
        self.records: list[TelemetryRecord] = []
        self._lock = Lock()

    async def record(self, *, name: str, fields: Mapping[str, object]) -> None:
        if not isinstance(fields, Mapping) or len(fields) > 50:
            raise ValueError("telemetry_fields_invalid")
        safe = redact_event(fields)
        record = TelemetryRecord(name=name, fields=safe)
        with self._lock:
            self.records.append(record)
            if len(self.records) > self.max_records:
                del self.records[: len(self.records) - self.max_records]

    def snapshot(self) -> tuple[TelemetryRecord, ...]:
        with self._lock:
            return tuple(self.records)


class StructuredRedactingLogger:
    """Structured-log bridge with the same redaction contract as telemetry.

    The adapter writes a stable ``mailhub_event`` extra field so a host JSON
    formatter can emit structured logs without requiring a logging framework
    dependency in the core package.  The human-readable message contains no
    event payload, and both the payload and nested values are bounded.
    """

    def __init__(self, logger: logging.Logger, *, level: int = logging.INFO) -> None:
        self.logger = logger
        self.level = level

    async def record(self, *, name: str, fields: Mapping[str, object]) -> None:
        _validate_telemetry_fields(fields)
        normalized_name = _normalize_name(name)
        payload = {
            "event": normalized_name,
            "recorded_at": datetime.now(UTC).isoformat(),
            "fields": redact_event(fields),
        }
        self.logger.log(
            self.level,
            "mailhub_event",
            extra={"mailhub_event": payload},
        )


class OpenTelemetryTelemetryPort:
    """Optional OpenTelemetry trace/metric bridge.

    ``opentelemetry-api`` is intentionally optional.  Hosts that install the
    ``mailhub[observability]`` extra get a real tracer/meter; tests and custom
    hosts can inject the tiny protocol surfaces above.  The bridge emits only
    redacted, bounded span attributes and low-cardinality metric labels.
    """

    def __init__(
        self,
        *,
        service_name: str = "mailhub",
        tracer: TracerPort | None = None,
        meter: MeterPort | None = None,
    ) -> None:
        self.service_name = _normalize_name(service_name)
        if tracer is None or meter is None:
            try:
                metrics_module = import_module("opentelemetry.metrics")
                trace_module = import_module("opentelemetry.trace")
            except ImportError as exc:  # pragma: no cover - depends on host extra
                raise RuntimeError("opentelemetry_api_required") from exc
            get_tracer = cast(Callable[[str], TracerPort], getattr(trace_module, "get_tracer"))  # noqa: B009
            get_meter = cast(Callable[[str], MeterPort], getattr(metrics_module, "get_meter"))  # noqa: B009
            tracer = tracer or get_tracer(self.service_name)
            meter = meter or get_meter(self.service_name)
        self.tracer = tracer
        self.events_counter = meter.create_counter(
            "mailhub.events",
            unit="{event}",
            description="MailHub bounded telemetry events",
        )

    async def record(self, *, name: str, fields: Mapping[str, object]) -> None:
        _validate_telemetry_fields(fields)
        normalized_name = _normalize_name(name)
        safe = redact_event(fields)
        attributes = _otel_attributes(safe)
        with self.tracer.start_as_current_span(f"mailhub.{normalized_name}") as span:
            span.set_attributes(attributes)
            span.add_event("mailhub.telemetry", {"mailhub.event": normalized_name})
        metric_attributes: dict[str, str | int | float | bool] = {"event": normalized_name}
        for key in ("operation", "provider", "status", "error_code", "mode"):
            value = safe.get(key)
            if isinstance(value, (str, int, float, bool)):
                metric_attributes[key] = value
        self.events_counter.add(1, attributes=metric_attributes)


_REDACT_KEYS = {
    "access_token",
    "refresh_token",
    "password",
    "client_secret",
    "authorization",
    "raw_mime",
    "body_text",
    "body",
    "content",
    "text",
    "original",
    "html",
    "attachment_bytes",
}


def redact_event(event: Mapping[str, object]) -> dict[str, object]:
    """Return an audit-safe, bounded copy of an event mapping.

    The depth and cardinality caps are part of the security contract: a
    malformed provider payload must not turn telemetry into an unbounded
    recursive serializer or smuggle body-shaped data under a nested key.
    """

    def sanitize(key: str, value: object, *, depth: int = 0) -> object:
        normalized = key.casefold()
        if normalized in _REDACT_KEYS or any(
            marker in normalized
            for marker in (
                "token",
                "secret",
                "credential",
                "raw_",
                "mime",
                "html",
                "original_text",
                "prompt",
            )
        ):
            return "[REDACTED]"
        if depth >= 5:
            return "[TRUNCATED]"
        if isinstance(value, Mapping):
            return {
                str(child_key): sanitize(str(child_key), child_value, depth=depth + 1)
                for child_key, child_value in list(value.items())[:100]
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [sanitize(key, child, depth=depth + 1) for child in list(value)[:100]]
        if isinstance(value, bytes):
            return "[REDACTED]"
        if isinstance(value, str):
            return value[:2000]
        return value

    return {str(key): sanitize(str(key), value) for key, value in event.items()}


def _validate_telemetry_fields(fields: Mapping[str, object]) -> None:
    if not isinstance(fields, Mapping) or len(fields) > 50:
        raise ValueError("telemetry_fields_invalid")


def _normalize_name(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("telemetry_name_invalid")
    return normalized


def _otel_attributes(fields: Mapping[str, object]) -> dict[str, str | int | float | bool]:
    attributes: dict[str, str | int | float | bool] = {}
    for key, value in list(fields.items())[:50]:
        normalized_key = str(key)[:100]
        if isinstance(value, (str, int, float, bool)):
            attributes[normalized_key] = value if not isinstance(value, str) else value[:500]
            continue
        try:
            encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            encoded = str(value)
        attributes[normalized_key] = encoded[:500]
    return attributes
