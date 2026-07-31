from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

_RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "run_provider_activation.py"
_RUNNER_SPEC = importlib.util.spec_from_file_location(
    "mailhub_provider_activation_revocation", _RUNNER_PATH
)
assert _RUNNER_SPEC is not None and _RUNNER_SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(_RUNNER)
ActivationClient = cast(Any, _RUNNER.MailHubActivationClient)
ActivationError = cast(Any, _RUNNER.ActivationError)

_COLLECTOR_PATH = Path(__file__).parents[1] / "scripts" / "collect_revocation_evidence.py"
_COLLECTOR_SPEC = importlib.util.spec_from_file_location(
    "mailhub_collect_revocation_evidence", _COLLECTOR_PATH
)
assert _COLLECTOR_SPEC is not None and _COLLECTOR_SPEC.loader is not None
_COLLECTOR = importlib.util.module_from_spec(_COLLECTOR_SPEC)
_COLLECTOR_SPEC.loader.exec_module(_COLLECTOR)
collector_main = cast(Any, _COLLECTOR.main)


def test_activation_client_builds_revocation_evidence_from_audit() -> None:
    connection_id = uuid4()
    events = [
        {"event_type": "mail.connection.credential_revoked", "target_ref": str(connection_id)},
        {"event_type": "mail.subscription.cancelled", "target_ref": str(connection_id)},
        {
            "event_type": "mail.sync.rejected_after_revoke",
            "target_ref": str(connection_id),
            "provider_requests_after_revoke": 0,
            "broker_requests_after_revoke": 0,
        },
        {"event_type": "mail.connection.revoked", "target_ref": str(connection_id)},
        {
            "event_type": "mail.connection.deleted",
            "target_ref": str(connection_id),
            "deletion_proof": {"object_refs_deleted": 2, "messages_deleted": 4},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mail/connections":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "connection_id": str(connection_id),
                            "provider": "gmail",
                            "status": "deleted",
                            "revision": 6,
                            "provider_account_id": "gmail-account-1",
                            "granted_scopes": [],
                        }
                    ]
                },
                request=request,
            )
        if request.url.path == "/v1/mail/audit":
            assert request.url.params["limit"] == "200"
            return httpx.Response(200, json={"data": events}, request=request)
        raise AssertionError(request.url)

    with ActivationClient(
        api_url="https://mailhub.example.test",
        tenant_id="tenant-1",
        subject_id="subject-1",
        trace_id="trace-1",
        transport=httpx.MockTransport(handler),
    ) as client:
        evidence = client.revocation_observation(
            connection_id=connection_id,
            provider="gmail",
            environment="test",
            run_id="run-1",
        )

    assert evidence["schema_version"] == "mailhub.provider_activation_revocation.v1"
    assert evidence["connection_status"] == "deleted"
    assert evidence["provider_requests_after_revoke"] == 0
    assert evidence["credential_broker_requests_after_revoke"] == 0
    assert "deletion_proof" not in json.dumps(evidence)


def test_revocation_observation_requires_zero_access_proof() -> None:
    connection_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mail/connections":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "connection_id": str(connection_id),
                            "provider": "gmail",
                            "status": "revoked",
                            "revision": 2,
                            "provider_account_id": "account-1",
                            "granted_scopes": [],
                        }
                    ]
                },
                request=request,
            )
        if request.url.path == "/v1/mail/audit":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "event_type": "mail.connection.credential_revoked",
                            "target_ref": str(connection_id),
                        }
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
        pytest.raises(ActivationError, match="revoked_audit_missing"),
    ):
        client.revocation_observation(
            connection_id=connection_id,
            provider="gmail",
            environment="test",
            run_id="run-1",
        )


def test_revocation_collector_network_gate_is_fail_closed(
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
    assert not (tmp_path / "revocation-negative.json").exists()
