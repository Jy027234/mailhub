"""Verified, bounded Provider webhook ingress primitives."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_RECEIPT_ROUTE_FIELDS = {
    "subject_id": 200,
    "connection_id": 100,
    "folder_ref": 512,
    "subscription_ref": 1_000,
    "notification_id": 512,
}


@dataclass(frozen=True, slots=True)
class WebhookReceipt:
    provider: str
    event_id: str
    tenant_id: str
    body_sha256: str
    received_at: datetime
    verified: bool
    duplicate: bool
    status: str
    trace_id: str


def normalize_receipt_route_metadata(
    value: Mapping[str, object] | None,
) -> dict[str, str]:
    """Validate body-free facts used by an owner-scoped receipt replay."""

    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > len(_RECEIPT_ROUTE_FIELDS):
        raise ValueError("webhook_receipt_route_metadata_invalid")
    normalized: dict[str, str] = {}
    for key, maximum in _RECEIPT_ROUTE_FIELDS.items():
        raw = value.get(key)
        if raw is None:
            continue
        if (
            not isinstance(raw, str)
            or not raw.strip()
            or len(raw) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
        ):
            raise ValueError("webhook_receipt_route_metadata_invalid")
        normalized[key] = raw.strip()
    if any(key not in _RECEIPT_ROUTE_FIELDS for key in value):
        raise ValueError("webhook_receipt_route_metadata_invalid")
    return normalized


class WebhookIngress:
    """Validate signatures and deduplicate events before async processing."""

    def __init__(self, *, provider: str, signing_secret: bytes, max_body_bytes: int = 1_000_000):
        if not provider.strip() or not signing_secret:
            raise ValueError("webhook_provider_or_secret_missing")
        if not 1024 <= max_body_bytes <= 10_000_000:
            raise ValueError("webhook_body_limit_invalid")
        self.provider = provider
        self._signing_secret = bytes(signing_secret)
        self.max_body_bytes = max_body_bytes
        self._events: dict[tuple[str, str], datetime] = {}

    def receive(
        self,
        *,
        tenant_id: str,
        event_id: str,
        body: bytes,
        signature: str,
        trace_id: str,
        now: datetime | None = None,
    ) -> WebhookReceipt:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if (
            not tenant_id.strip()
            or not event_id.strip()
            or not trace_id.strip()
            or len(tenant_id) > 200
            or len(event_id) > 512
            or len(trace_id) > 200
        ):
            raise ValueError("webhook_identity_missing")
        if len(body) > self.max_body_bytes:
            raise ValueError("webhook_body_too_large")
        if len(signature) > 200 or any(ord(char) < 33 for char in signature):
            raise ValueError("webhook_signature_invalid")
        expected = hmac.new(self._signing_secret, body, hashlib.sha256).hexdigest()
        verified = hmac.compare_digest(expected, signature.removeprefix("sha256="))
        body_sha256 = hashlib.sha256(body).hexdigest()
        if not verified:
            return WebhookReceipt(
                provider=self.provider,
                event_id=event_id,
                tenant_id=tenant_id,
                body_sha256=body_sha256,
                received_at=current,
                verified=False,
                duplicate=False,
                status="quarantined",
                trace_id=trace_id,
            )
        self._events = {
            key: received
            for key, received in self._events.items()
            if received >= current - timedelta(hours=24)
        }
        key = (tenant_id, event_id)
        duplicate = key in self._events
        self._events.setdefault(key, current)
        return WebhookReceipt(
            provider=self.provider,
            event_id=event_id,
            tenant_id=tenant_id,
            body_sha256=body_sha256,
            received_at=current,
            verified=True,
            duplicate=duplicate,
            status="duplicate" if duplicate else "accepted",
            trace_id=trace_id,
        )


def verified_provider_receipt(
    *,
    provider: str,
    tenant_id: str,
    event_id: str,
    body: bytes,
    trace_id: str,
    max_body_bytes: int = 10_000_000,
    now: datetime | None = None,
) -> WebhookReceipt:
    """Build a receipt after the host verifier has authenticated a callback.

    Provider-specific authentication deliberately lives in the host
    ``ProviderNotificationVerifierPort``.  This helper does not accept a
    signature and must therefore only be called with its verified result; it
    still repeats all identity/size bounds before any durable write.
    """

    if not provider.strip() or len(provider) > 100:
        raise ValueError("webhook_provider_invalid")
    if (
        not tenant_id.strip()
        or not event_id.strip()
        or not trace_id.strip()
        or len(tenant_id) > 200
        or len(event_id) > 512
        or len(trace_id) > 200
    ):
        raise ValueError("webhook_identity_missing")
    if not 1024 <= max_body_bytes <= 10_000_000:
        raise ValueError("webhook_body_limit_invalid")
    if len(body) > max_body_bytes:
        raise ValueError("webhook_body_too_large")
    received_at = (now or datetime.now(UTC)).astimezone(UTC)
    return WebhookReceipt(
        provider=provider,
        event_id=event_id,
        tenant_id=tenant_id,
        body_sha256=hashlib.sha256(body).hexdigest(),
        received_at=received_at,
        verified=True,
        duplicate=False,
        status="accepted",
        trace_id=trace_id,
    )


def sign_webhook(*, secret: bytes, body: bytes) -> str:
    """Test/deployment helper; callers should keep the secret in a broker."""

    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def receipt_event(receipt: WebhookReceipt) -> Mapping[str, object]:
    return {
        "schema_version": "mailhub.provider_event.v1",
        "event_type": "mailhub.provider_event.v1",
        "provider": receipt.provider,
        "event_id": receipt.event_id,
        "tenant_id": receipt.tenant_id,
        "body_sha256": receipt.body_sha256,
        "received_at": receipt.received_at.isoformat(),
        "verified": receipt.verified,
        "duplicate": receipt.duplicate,
        "status": receipt.status,
        "trace_id": receipt.trace_id,
    }
