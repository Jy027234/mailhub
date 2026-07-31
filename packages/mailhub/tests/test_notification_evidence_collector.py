from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

_RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "run_provider_activation.py"
_RUNNER_SPEC = importlib.util.spec_from_file_location(
    "mailhub_provider_activation_notifications", _RUNNER_PATH
)
assert _RUNNER_SPEC is not None and _RUNNER_SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(_RUNNER)
ActivationClient = cast(Any, _RUNNER.MailHubActivationClient)
ActivationError = cast(Any, _RUNNER.ActivationError)

_COLLECTOR_PATH = Path(__file__).parents[1] / "scripts" / "collect_notification_evidence.py"
_COLLECTOR_SPEC = importlib.util.spec_from_file_location(
    "mailhub_collect_notification_evidence", _COLLECTOR_PATH
)
assert _COLLECTOR_SPEC is not None and _COLLECTOR_SPEC.loader is not None
_COLLECTOR = importlib.util.module_from_spec(_COLLECTOR_SPEC)
_COLLECTOR_SPEC.loader.exec_module(_COLLECTOR)
collector_main = cast(Any, _COLLECTOR.main)


def test_activation_client_builds_notification_evidence_from_owner_observations() -> None:
    connection_id = uuid4()
    started = datetime.now(UTC) - timedelta(days=8)
    renewed = started + timedelta(days=1)
    duplicate = started + timedelta(days=6)
    reconciled = started + timedelta(days=7)
    connection_payload = {
        "connection_id": str(connection_id),
        "provider": "gmail",
        "status": "active",
        "revision": 4,
        "email_address": "private@example.test",
        "provider_account_id": "gmail-account-1",
        "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }
    audit_events = [
        {
            "event_type": "mail.subscription.renewed",
            "target_ref": str(connection_id),
            "provider": "gmail",
            "occurred_at": renewed.isoformat(),
        },
        {
            "event_type": "mail.provider.notification.duplicate",
            "target_ref": str(connection_id),
            "provider": "gmail",
            "duplicate": True,
            "occurred_at": duplicate.isoformat(),
        },
        {
            "event_type": "mail.provider.notification.ack",
            "target_ref": str(connection_id),
            "provider": "gmail",
            "ack_latency_ms": 12,
            "occurred_at": duplicate.isoformat(),
        },
        {
            "event_type": "mail.sync.completed",
            "target_ref": str(connection_id),
            "mode": "reconcile",
            "occurred_at": reconciled.isoformat(),
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mail/connections":
            return httpx.Response(200, json={"data": [connection_payload]}, request=request)
        if request.url.path.endswith("/sync-state"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "connection_id": str(connection_id),
                            "folder_ref": "INBOX",
                            "subscription_ref": "watch-opaque",
                            "subscription_status": "active",
                            "watermark": started.isoformat(),
                        }
                    ]
                },
                request=request,
            )
        if request.url.path == "/v1/mail/webhooks/receipts":
            assert request.url.params["limit"] == "200"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "provider": "gmail",
                            "event_id": "notification-1",
                            "verified": True,
                            "received_at": started.isoformat(),
                            "route_metadata": {"connection_id": str(connection_id)},
                        }
                    ]
                },
                request=request,
            )
        if request.url.path == "/v1/mail/audit":
            assert request.url.params["limit"] == "200"
            # Exercise the SQL-backed metadata envelope normalization path.
            return httpx.Response(
                200,
                json={"data": [{"metadata": event} for event in audit_events]},
                request=request,
            )
        raise AssertionError(request.url)

    with ActivationClient(
        api_url="https://mailhub.example.test",
        tenant_id="tenant-1",
        subject_id="subject-1",
        trace_id="trace-1",
        transport=httpx.MockTransport(handler),
    ) as client:
        evidence = client.notification_observation(
            connection_id=connection_id,
            provider="gmail",
            environment="test",
            run_id="run-1",
        )

    assert evidence["schema_version"] == "mailhub.provider_activation_notifications.v1"
    assert evidence["receipt_count"] == 1
    assert evidence["duplicate_count"] == 1
    assert evidence["renewal_count"] == 1
    assert evidence["ack_p95_ms"] == 12
    assert evidence["reconciliation_verified"] is True
    assert evidence["credentials_observed"] is False
    assert "watch-opaque" not in json.dumps(evidence)


def test_notification_observation_rejects_short_window() -> None:
    connection_id = uuid4()
    now = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mail/connections":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "connection_id": str(connection_id),
                            "provider": "gmail",
                            "status": "active",
                            "revision": 1,
                            "provider_account_id": "account-1",
                            "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                        }
                    ]
                },
                request=request,
            )
        if request.url.path.endswith("/sync-state"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "connection_id": str(connection_id),
                            "folder_ref": "INBOX",
                            "subscription_ref": "watch-1",
                            "subscription_status": "active",
                            "watermark": now.isoformat(),
                        }
                    ]
                },
                request=request,
            )
        if request.url.path == "/v1/mail/webhooks/receipts":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "provider": "gmail",
                            "verified": True,
                            "received_at": now.isoformat(),
                            "route_metadata": {"connection_id": str(connection_id)},
                        }
                    ]
                },
                request=request,
            )
        if request.url.path == "/v1/mail/audit":
            common = {
                "target_ref": str(connection_id),
                "provider": "gmail",
                "occurred_at": now.isoformat(),
            }
            return httpx.Response(
                200,
                json={
                    "data": [
                        {**common, "event_type": "mail.subscription.renewed"},
                        {
                            **common,
                            "event_type": "mail.provider.notification.duplicate",
                            "duplicate": True,
                        },
                        {
                            **common,
                            "event_type": "mail.provider.notification.ack",
                            "ack_latency_ms": 1,
                        },
                        {
                            **common,
                            "event_type": "mail.sync.completed",
                            "mode": "reconcile",
                        },
                    ]
                },
                request=request,
            )
        raise AssertionError(request.url)

    with (
        ActivationClient(
            api_url="https://mailhub.example.test",
            tenant_id="tenant-1",
            subject_id="subject-1",
            trace_id="trace-1",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ActivationError, match="observation_window_too_short"),
    ):
        client.notification_observation(
            connection_id=connection_id,
            provider="gmail",
            environment="test",
            run_id="run-1",
        )


def test_notification_collector_writes_only_observed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", "true")
    monkeypatch.setattr(
        _COLLECTOR,
        "_offline_preflight",
        lambda provider: {
            "schema_version": "mailhub.provider_preflight.v1",
            "provider": provider,
            "network_access": False,
            "real_provider_evidence": False,
            "config_ready": True,
        },
    )

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def readiness(self) -> dict[str, object]:
            return {"status": "ready"}

        def notification_observation(self, **kwargs: object) -> dict[str, object]:
            return {
                "schema_version": "mailhub.provider_activation_notifications.v1",
                "network_access": True,
                "status": "succeeded",
                "credentials_observed": False,
                "provider": kwargs["provider"],
                "environment": kwargs["environment"],
                "run_id": kwargs["run_id"],
                "account_identity_sha256": "a" * 64,
                "subscription_active": True,
                "renewal_count": 1,
                "receipt_count": 1,
                "duplicate_count": 1,
                "ack_p95_ms": 2,
                "observation_started_at": "2026-07-20T00:00:00Z",
                "observation_finished_at": "2026-07-27T00:00:00Z",
                "dedupe_verified": True,
                "reconciliation_verified": True,
                "watermark_sha256": "b" * 64,
            }

    evidence_dir = tmp_path / "gmail" / "test" / "run-1"
    result = collector_main(
        [
            "--provider",
            "gmail",
            "--environment",
            "test",
            "--api-url",
            "https://mailhub.example.test",
            "--tenant-id",
            "tenant-1",
            "--subject-id",
            "subject-1",
            "--connection-id",
            str(uuid4()),
            "--run-id",
            "run-1",
            "--evidence-dir",
            str(evidence_dir),
            "--confirm-real-provider",
        ],
        client_factory=FakeClient,
    )

    assert result == 0
    payload = json.loads((evidence_dir / "webhook-receipts.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "mailhub.provider_activation_notifications.v1"
    assert payload["credentials_observed"] is False


def test_notification_collector_keeps_network_gate_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", raising=False)
    result = collector_main(
        [
            "--provider",
            "gmail",
            "--api-url",
            "https://mailhub.example.test",
            "--tenant-id",
            "tenant-1",
            "--subject-id",
            "subject-1",
            "--connection-id",
            str(uuid4()),
            "--run-id",
            "run-gate",
            "--evidence-dir",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert not (tmp_path / "webhook-receipts.json").exists()
