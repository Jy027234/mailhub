from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "run_provider_activation.py"
_SPEC = importlib.util.spec_from_file_location("mailhub_provider_activation", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
ActivationClient = cast(Any, _MODULE.MailHubActivationClient)
ActivationError = cast(Any, _MODULE.ActivationError)
poll_job = cast(Any, _MODULE._poll_job)
run_main = cast(Any, _MODULE.main)
build_sync_filter = cast(Any, _MODULE._sync_filter)


def test_activation_client_rejects_non_tls_remote_api() -> None:
    with pytest.raises(ActivationError, match="api_url_must_be_https"):
        ActivationClient(
            api_url="http://mailhub.example.test",
            tenant_id="tenant-1",
            subject_id="subject-1",
            trace_id="trace-1",
        )


def test_activation_client_rejects_secret_shaped_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"access_token": "must-not-cross"}},
            request=request,
        )

    with (
        ActivationClient(
            api_url="https://mailhub.example.test",
            tenant_id="tenant-1",
            subject_id="subject-1",
            trace_id="trace-1",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ActivationError, match="response_invalid"),
    ):
        client._request("GET", "/v1/mail/connections")


def test_activation_client_rejects_sandbox_readiness() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "sandbox", "environment": "development", "missing": []},
            request=request,
        )

    with (
        ActivationClient(
            api_url="https://mailhub.example.test",
            tenant_id="tenant-1",
            subject_id="subject-1",
            trace_id="trace-1",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ActivationError, match="runtime_not_ready:sandbox"),
    ):
        client.readiness()


def test_activation_client_rejects_ready_runtime_with_missing_dependencies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "environment": "injected",
                "missing": ["credential_broker_endpoint"],
            },
            request=request,
        )

    with (
        ActivationClient(
            api_url="https://mailhub.example.test",
            tenant_id="tenant-1",
            subject_id="subject-1",
            trace_id="trace-1",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ActivationError, match="runtime_not_ready:missing_dependencies"),
    ):
        client.readiness()


def test_activation_backfill_filter_is_normalized_and_fail_closed() -> None:
    body, summary = build_sync_filter(
        mode="backfill",
        folder_ref="INBOX",
        label_refs=["project", "RFQ"],
        received_after="2026-07-01T08:00:00+08:00",
        received_before="2026-07-08T00:00:00Z",
    )
    assert body == {
        "folder_ref": "INBOX",
        "label_refs": ["project", "RFQ"],
        "received_after": "2026-07-01T00:00:00Z",
        "received_before": "2026-07-08T00:00:00Z",
    }
    assert summary["bounded"] is True
    assert summary["label_count"] == 2
    assert summary["label_refs_sha256"]
    with pytest.raises(ActivationError, match="filter_requires_backfill"):
        build_sync_filter(
            mode="incremental",
            folder_ref="INBOX",
            label_refs=["project"],
            received_after=None,
            received_before=None,
        )
    with pytest.raises(ActivationError, match="received_range_invalid"):
        build_sync_filter(
            mode="backfill",
            folder_ref="INBOX",
            label_refs=[],
            received_after="2026-07-08T00:00:00Z",
            received_before="2026-07-01T00:00:00Z",
        )


def test_activation_client_hashes_cursor_and_keeps_scope_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_id = uuid4()
    job_id = uuid4()
    calls: list[httpx.Request] = []
    job_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal job_calls
        calls.append(request)
        if request.url.path == "/v1/mail/connections":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "connection_id": str(connection_id),
                            "provider": "gmail",
                            "status": "active",
                            "revision": 3,
                            "email_address": "private@example.test",
                            "provider_account_id": "gmail-account-1",
                            "granted_scopes": ["gmail.readonly"],
                        }
                    ]
                },
                request=request,
            )
        if request.url.path.endswith("/sync-jobs"):
            return httpx.Response(
                200,
                json={"data": {"job_ref": str(job_id), "status": "queued"}},
                request=request,
            )
        if request.url.path == f"/v1/mail/sync-jobs/{job_id}":
            job_calls += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "job_id": str(job_id),
                        "status": "succeeded",
                        "fetched_count": 2,
                        "saved_count": 2,
                        "duplicate_count": 0,
                        "deleted_count": 1,
                        "cursor_before": "https://graph.microsoft.com/delta?$skiptoken=private",
                        "cursor_after": "https://graph.microsoft.com/delta?$deltatoken=private",
                        "provider_request_id": "provider-request-1",
                    }
                },
                request=request,
            )
        return httpx.Response(404, json={"error": {"code": "not_found"}}, request=request)

    with ActivationClient(
        api_url="https://mailhub.example.test",
        tenant_id="tenant-1",
        subject_id="subject-1",
        trace_id="trace-1",
        transport=httpx.MockTransport(handler),
    ) as client:
        connection = client.connection(connection_id=connection_id, provider="gmail")
        initial = client.enqueue_sync(
            connection_id=connection_id,
            mode="incremental",
            limit=10,
            idempotency_key="activation-sync-1",
        )
        completed = poll_job(
            client, job_ref=str(initial["job_ref"]), timeout_seconds=1, interval_seconds=0.1
        )

    assert connection == {
        "connection_id": str(connection_id),
        "provider": "gmail",
        "status": "active",
        "revision": 3,
        "scope_count": 1,
        "identity": {
            "account_identity_sha256": _MODULE._hash_text("gmail-account-1"),
            "email_identity_sha256": _MODULE._hash_text("private@example.test"),
        },
    }
    assert completed["status"] == "succeeded"
    assert completed["deleted_count"] == 1
    assert completed["cursor_after_sha256"]
    assert "private" not in str(completed)
    assert job_calls == 1
    assert calls[0].headers["X-MailHub-Tenant"] == "tenant-1"
    assert calls[0].headers["X-MailHub-Subject"] == "subject-1"


def test_activation_client_builds_oauth_evidence_only_from_audit_ledger() -> None:
    connection_id = uuid4()
    flow_ref = "a" * 64
    events = [
        {
            "event_type": "mail.connection.credential_revoked",
            "target_ref": str(connection_id),
            "provider": "gmail",
        },
        {
            "event_type": "mail.connection.credentials.refreshed",
            "target_ref": str(connection_id),
            "provider": "gmail",
        },
        {
            "event_type": "mail.oauth.callback_rejected",
            "target_ref": f"oauth:{flow_ref}",
            "provider": "gmail",
            "oauth_flow_ref_sha256": flow_ref,
            "reason": "oauth_state_replayed_or_missing",
        },
        {
            "event_type": "mail.oauth.completed",
            "target_ref": str(connection_id),
            "provider": "gmail",
            "oauth_flow_ref_sha256": flow_ref,
            "requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "scope_subset_verified": True,
            "redirect_allowlist_verified": True,
            "pkce_verified": True,
            "provider_account_id_sha256": "b" * 64,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/mail/audit"
        assert request.url.params["limit"] == "200"
        return httpx.Response(200, json={"data": events}, request=request)

    with ActivationClient(
        api_url="https://mailhub.example.test",
        tenant_id="tenant-1",
        subject_id="subject-1",
        trace_id="trace-1",
        transport=httpx.MockTransport(handler),
    ) as client:
        evidence = client.oauth_observation(
            connection_id=connection_id,
            provider="gmail",
            environment="test",
            run_id="run-1",
        )

    assert evidence["schema_version"] == "mailhub.provider_activation_oauth.v1"
    assert evidence["credentials_observed"] is False
    assert evidence["state_replay_rejected"] is True
    assert evidence["account_identity_sha256"] == "b" * 64
    serialized = json.dumps(evidence).casefold()
    assert "credential_ref" not in serialized
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized


def test_activation_client_rejects_oauth_audit_fixture_without_replay() -> None:
    connection_id = uuid4()
    flow_ref = "a" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "event_type": "mail.oauth.completed",
                        "target_ref": str(connection_id),
                        "provider": "gmail",
                        "oauth_flow_ref_sha256": flow_ref,
                        "requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                        "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                        "scope_subset_verified": True,
                        "redirect_allowlist_verified": True,
                        "pkce_verified": True,
                        "provider_account_id_sha256": "b" * 64,
                    }
                ]
            },
            request=request,
        )

    with (
        ActivationClient(
            api_url="https://mailhub.example.test",
            tenant_id="tenant-1",
            subject_id="subject-1",
            trace_id="trace-1",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ActivationError, match="replay_rejection_missing"),
    ):
        client.oauth_observation(
            connection_id=connection_id,
            provider="gmail",
            environment="test",
            run_id="run-1",
        )


def test_activation_client_rejects_malformed_audit_event() -> None:
    connection_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["not-an-audit-event"]}, request=request)

    with (
        ActivationClient(
            api_url="https://mailhub.example.test",
            tenant_id="tenant-1",
            subject_id="subject-1",
            trace_id="trace-1",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ActivationError, match="audit_event_invalid"),
    ):
        client.oauth_observation(
            connection_id=connection_id,
            provider="gmail",
            environment="test",
            run_id="run-1",
        )


def test_activation_main_writes_real_observation_shape_without_raw_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = uuid4()
    job_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(
                200,
                json={"status": "ready", "environment": "injected", "missing": []},
                request=request,
            )
        if request.url.path == "/v1/mail/connections":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "connection_id": str(connection_id),
                            "provider": "microsoft_graph",
                            "status": "active",
                            "revision": 2,
                            "email_address": "mailbox@example.test",
                            "provider_account_id": "graph-account-1",
                            "provider_tenant_id": "tenant-graph-1",
                            "granted_scopes": ["Mail.Read", "offline_access"],
                        }
                    ]
                },
                request=request,
            )
        if request.url.path.endswith("/sync-jobs"):
            return httpx.Response(
                200,
                json={"data": {"job_ref": str(job_id), "status": "queued"}},
                request=request,
            )
        if request.url.path == f"/v1/mail/sync-jobs/{job_id}":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "succeeded",
                        "fetched_count": 1,
                        "saved_count": 1,
                        "duplicate_count": 0,
                        "deleted_count": 0,
                        "cursor_after": "delta-secret-like-value",
                        "provider_request_id": "request-1",
                    }
                },
                request=request,
            )
        return httpx.Response(404, json={"error": {"code": "not_found"}}, request=request)

    def factory(**kwargs: object) -> Any:
        return ActivationClient(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        _MODULE,
        "_offline_preflight",
        lambda provider: {
            "schema_version": "mailhub.provider_preflight.v1",
            "network_access": False,
            "real_provider_evidence": False,
            "config_ready": True,
            "ready": True,
            "readiness": "config_ready",
            "providers": [
                {
                    "provider": provider,
                    "enabled": True,
                    "ready": True,
                    "readiness": "config_ready",
                    "real_provider_evidence": False,
                    "missing": [],
                    "errors": [],
                    "config": {
                        "enabled": "true",
                        "read_only": "true",
                        "push_enabled": "false",
                    },
                }
            ],
        },
    )

    monkeypatch.setenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", "true")
    evidence_root = tmp_path / "evidence"
    result = run_main(
        [
            "--provider",
            "microsoft_graph",
            "--api-url",
            "https://mailhub.example.test",
            "--tenant-id",
            "tenant-1",
            "--subject-id",
            "subject-1",
            "--connection-id",
            str(connection_id),
            "--mode",
            "backfill",
            "--folder-ref",
            "INBOX",
            "--label-ref",
            "project",
            "--received-after",
            "2026-07-01T00:00:00Z",
            "--received-before",
            "2026-07-08T00:00:00Z",
            "--run-id",
            "run-1",
            "--evidence-root",
            str(evidence_root),
            "--confirm-real-provider",
        ],
        client_factory=factory,
    )

    assert result == 0
    evidence_path = (
        evidence_root / "microsoft_graph" / "test" / "run-1" / "sync-reconciliation.json"
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["evidence_kind"] == "real_api_observation"
    assert payload["network_access"] is True
    assert payload["real_provider_evidence"] is True
    assert payload["credentials_observed"] is False
    assert payload["connection"]["identity"]["tenant_identity_sha256"]
    assert payload["readiness"] == {
        "status": "ready",
        "environment": "injected",
        "missing_count": 0,
    }
    assert payload["status"] == "succeeded"
    assert payload["replay"]["same_job_ref"] is True
    assert payload["sync_filter"]["bounded"] is True
    assert payload["sync_filter"]["label_count"] == 1
    preflight = json.loads((evidence_path.parent / "preflight.json").read_text(encoding="utf-8"))
    assert preflight["config_ready"] is True
    assert preflight["network_access"] is False
    assert "mailbox@example.test" not in evidence_path.read_text(encoding="utf-8")
    assert "delta-secret-like-value" not in evidence_path.read_text(encoding="utf-8")


def test_activation_main_requires_both_network_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", raising=False)
    result = run_main(
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
            "--evidence-root",
            str(tmp_path),
        ]
    )
    assert result == 2
    payload = json.loads(
        (tmp_path / "gmail" / "test" / "run-gate" / "sync-reconciliation.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["status"] == "failed"
    assert payload["evidence_kind"] == "activation_gate_failure"
    assert payload["network_access"] is False
    assert payload["error_code"] == "activation_confirmation_required"
    test_output = (tmp_path / "gmail" / "test" / "run-gate" / "test-output.txt").read_text(
        encoding="utf-8"
    )
    assert "schema_version=mailhub.provider_activation_test_output.v1" in test_output
    assert "provider=gmail" in test_output
    assert "environment=test" in test_output
    assert "run_id=run-gate" in test_output
    assert "network_access=false" in test_output
    assert "status=failed" in test_output
    assert "credentials_observed=false" in test_output
    assert "exit_code=2" in test_output


def test_activation_main_refuses_network_when_offline_preflight_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", "true")
    monkeypatch.setattr(
        _MODULE,
        "_offline_preflight",
        lambda _provider: (_ for _ in ()).throw(ActivationError("activation_preflight_not_ready")),
    )

    evidence_root = tmp_path / "evidence"
    result = run_main(
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
            "run-preflight-gate",
            "--evidence-root",
            str(evidence_root),
            "--confirm-real-provider",
        ]
    )

    assert result == 2
    evidence_path = (
        evidence_root / "gmail" / "test" / "run-preflight-gate" / "sync-reconciliation.json"
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["error_code"] == "activation_preflight_not_ready"
    assert payload["network_access"] is False
    assert not (evidence_path.parent / "preflight.json").exists()


def test_activation_main_never_records_sandbox_as_real_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "sandbox", "environment": "development", "missing": []},
            request=request,
        )

    def factory(**kwargs: object) -> Any:
        return ActivationClient(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        _MODULE,
        "_offline_preflight",
        lambda provider: {
            "schema_version": "mailhub.provider_preflight.v1",
            "network_access": False,
            "real_provider_evidence": False,
            "config_ready": True,
            "ready": True,
            "readiness": "config_ready",
            "providers": [],
        },
    )

    monkeypatch.setenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", "true")
    evidence_root = tmp_path / "evidence"
    result = run_main(
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
            "run-sandbox",
            "--evidence-root",
            str(evidence_root),
            "--confirm-real-provider",
        ],
        client_factory=factory,
    )

    assert result == 2
    payload = json.loads(
        (evidence_root / "gmail" / "test" / "run-sandbox" / "sync-reconciliation.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["error_code"] == "activation_runtime_not_ready:sandbox"
    assert payload["evidence_kind"] == "activation_gate_failure"
    assert payload["network_access"] is True
    assert payload["connection"] is None
