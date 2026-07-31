from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from caplatform_bff.mailhub_adapter import MailHubClient


class _FakeClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, object] | None,
    ) -> httpx.Response:
        assert method == "GET"
        assert path == "https://mailhub.example.test/v1/mail/connections"
        assert headers["X-MailHub-Tenant"] == "tenant-1"
        assert headers["X-MailHub-Subject"] == "subject-1"
        assert json is None
        return httpx.Response(
            200,
            json={"data": [{"connection_id": "connection-1"}]},
            request=httpx.Request(method, "https://mailhub.example.test" + path),
        )


class _RecordingClient:
    calls: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, object] | None,
    ) -> httpx.Response:
        self.calls.append((method, path, headers, json))
        return httpx.Response(
            200,
            json={"data": {"status": "ok"}},
            request=httpx.Request(method, "https://mailhub.example.test" + path),
        )


def test_mailhub_adapter_rejects_lookalike_hosts() -> None:
    with pytest.raises(ValueError, match="mailhub_base_url_must_be_tls"):
        MailHubClient(base_url="http://localhost.evil.example.test")


def test_mailhub_adapter_forwards_scoped_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        MailHubClient(
            base_url="https://mailhub.example.test",
            timeout_seconds=2,
        ).list_connections(tenant_id="tenant-1", subject_id="subject-1")
    )
    assert result == ({"connection_id": "connection-1"},)


def test_mailhub_adapter_forwards_projection_impact_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    result = asyncio.run(
        MailHubClient(base_url="https://mailhub.example.test").connection_impact_preview(
            tenant_id="tenant-1",
            subject_id="subject-1",
            connection_id=UUID("00000000-0000-0000-0000-000000000001"),
            folder_refs=("INBOX", "项目/采购"),
        )
    )
    assert result == {"data": {"status": "ok"}}
    assert _RecordingClient.calls[0][1].endswith(
        "/v1/mail/connections/00000000-0000-0000-0000-000000000001/impact-preview?"
        "folder_ref=INBOX&folder_ref=%E9%A1%B9%E7%9B%AE%2F%E9%87%87%E8%B4%AD"
    )


def test_mailhub_adapter_forwards_provider_health(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    result = asyncio.run(
        MailHubClient(base_url="https://mailhub.example.test").provider_health(
            tenant_id="tenant-1", subject_id="subject-1"
        )
    )
    assert result == ()
    assert _RecordingClient.calls[0][0] == "GET"
    assert _RecordingClient.calls[0][1].endswith("/v1/mail/admin/provider-health")


def test_mailhub_adapter_forwards_bounded_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    result = asyncio.run(
        MailHubClient(base_url="https://mailhub.example.test").list_audit(
            tenant_id="tenant-1", subject_id="subject-1", limit=999
        )
    )
    assert result == ()
    assert _RecordingClient.calls[0][0] == "GET"
    assert _RecordingClient.calls[0][1].endswith("/v1/mail/audit?limit=200")


def test_mailhub_adapter_reads_exact_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    candidate_id = UUID("00000000-0000-0000-0000-000000000003")

    result = asyncio.run(
        MailHubClient(base_url="https://mailhub.example.test").get_candidate(
            tenant_id="tenant-1",
            subject_id="subject-1",
            candidate_id=candidate_id,
        )
    )

    assert result == {"status": "ok"}
    assert _RecordingClient.calls[0][0] == "GET"
    assert _RecordingClient.calls[0][1].endswith(f"/v1/mail/candidates/{candidate_id}")


def test_mailhub_adapter_forwards_host_configured_oauth_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    client = MailHubClient(base_url="https://mailhub.example.test")
    connection_id = UUID("00000000-0000-0000-0000-000000000001")

    asyncio.run(
        client.begin_oauth(
            tenant_id="tenant-1",
            subject_id="subject-1",
            provider="gmail",
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            client_id="client-1",
            redirect_uri="https://app.example.test/mail/oauth/gmail/callback",
            scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            extra_parameters={"access_type": "offline", "prompt": "consent"},
            connection_id=connection_id,
            expected_revision=3,
        )
    )
    asyncio.run(
        client.complete_oauth(
            tenant_id="tenant-1",
            subject_id="subject-1",
            provider="gmail",
            state="s" * 32,
            code="authorization-code",
            redirect_uri="https://app.example.test/mail/oauth/gmail/callback",
        )
    )

    assert _RecordingClient.calls[0][0] == "POST"
    assert _RecordingClient.calls[0][1].endswith("/v1/mail/oauth/gmail:authorize")
    assert _RecordingClient.calls[0][3] == {
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "client_id": "client-1",
        "redirect_uri": "https://app.example.test/mail/oauth/gmail/callback",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        "extra_parameters": {"access_type": "offline", "prompt": "consent"},
        "connection_id": str(connection_id),
        "expected_revision": 3,
    }
    assert _RecordingClient.calls[1][1].endswith("/v1/mail/oauth/gmail:callback")
    assert _RecordingClient.calls[1][3] == {
        "state": "s" * 32,
        "code": "authorization-code",
        "redirect_uri": "https://app.example.test/mail/oauth/gmail/callback",
    }


def test_mailhub_adapter_rejects_invalid_oauth_inputs() -> None:
    client = MailHubClient(base_url="https://mailhub.example.test")
    with pytest.raises(ValueError, match="mailhub_oauth_provider_invalid"):
        asyncio.run(
            client.begin_oauth(
                tenant_id="tenant-1",
                subject_id="subject-1",
                provider="imap",
                authorization_endpoint="https://accounts.google.com/auth",
                client_id="client-1",
                redirect_uri="https://app.example.test/callback",
                scopes=("mail.read",),
            )
        )
    with pytest.raises(ValueError, match="mailhub_oauth_extra_parameters_invalid"):
        asyncio.run(
            client.begin_oauth(
                tenant_id="tenant-1",
                subject_id="subject-1",
                provider="gmail",
                authorization_endpoint="https://accounts.google.com/auth",
                client_id="client-1",
                redirect_uri="https://app.example.test/callback",
                scopes=("https://www.googleapis.com/auth/gmail.readonly",),
                extra_parameters={"redirect_uri": "https://evil.example.test/callback"},
            )
        )
    with pytest.raises(ValueError, match="mailhub_oauth_expected_revision_invalid"):
        asyncio.run(
            client.begin_oauth(
                tenant_id="tenant-1",
                subject_id="subject-1",
                provider="gmail",
                authorization_endpoint="https://accounts.google.com/auth",
                client_id="client-1",
                redirect_uri="https://app.example.test/callback",
                scopes=("mail.read",),
                expected_revision=2,
            )
        )


def test_mailhub_adapter_forwards_revision_fenced_scope_narrowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    connection_id = UUID("00000000-0000-0000-0000-000000000001")
    result = asyncio.run(
        MailHubClient(base_url="https://mailhub.example.test").update_connection_scopes(
            tenant_id="tenant-1",
            subject_id="subject-1",
            connection_id=connection_id,
            expected_revision=3,
            granted_scopes=("mail.read", " mail.labels "),
        )
    )
    assert result == {"data": {"status": "ok"}}
    assert _RecordingClient.calls[0][0] == "POST"
    assert _RecordingClient.calls[0][1].endswith(f"/{connection_id}:scopes")
    assert _RecordingClient.calls[0][3] == {
        "expected_revision": 3,
        "granted_scopes": ["mail.read", "mail.labels"],
    }


def test_mailhub_adapter_forwards_host_credential_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    connection_id = UUID("00000000-0000-0000-0000-000000000001")
    result = asyncio.run(
        MailHubClient(base_url="https://mailhub.example.test").refresh_connection(
            tenant_id="tenant-1",
            subject_id="subject-1",
            connection_id=connection_id,
            expected_revision=4,
            reason="scheduled_refresh",
        )
    )
    assert result == {"data": {"status": "ok"}}
    assert _RecordingClient.calls[0][0] == "POST"
    assert _RecordingClient.calls[0][1].endswith(f"/{connection_id}:refresh")
    assert _RecordingClient.calls[0][3] == {
        "expected_revision": 4,
        "reason": "scheduled_refresh",
    }


def test_mailhub_adapter_forwards_explicit_connection_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    connection_id = UUID("00000000-0000-0000-0000-000000000001")
    result = asyncio.run(
        MailHubClient(base_url="https://mailhub.example.test").revoke_connection(
            tenant_id="tenant-1",
            subject_id="subject-1",
            connection_id=connection_id,
            expected_revision=5,
            trace_id="trace-revoke",
        )
    )
    assert result == {"data": {"status": "ok"}}
    assert _RecordingClient.calls[0][0] == "POST"
    assert _RecordingClient.calls[0][1].endswith(f"/{connection_id}:revoke")
    assert _RecordingClient.calls[0][2]["X-Trace-Id"] == "trace-revoke"
    assert _RecordingClient.calls[0][3] == {"expected_revision": 5}


def test_mailhub_adapter_forwards_lifecycle_and_rule_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    client = MailHubClient(base_url="https://mailhub.example.test")
    connection_id = UUID("00000000-0000-0000-0000-000000000001")
    rule_id = UUID("00000000-0000-0000-0000-000000000002")
    message_id = UUID("00000000-0000-0000-0000-000000000003")
    run_id = UUID("00000000-0000-0000-0000-000000000004")

    asyncio.run(
        client.delete_connection(
            tenant_id="tenant-1",
            subject_id="subject-1",
            connection_id=connection_id,
            expected_revision=2,
        )
    )
    asyncio.run(
        client.export_data(
            tenant_id="tenant-1", subject_id="subject-1", include_content=True, limit=999
        )
    )
    asyncio.run(client.list_agent_policies(tenant_id="tenant-1", subject_id="subject-1", limit=999))
    asyncio.run(client.list_delegations(tenant_id="tenant-1", subject_id="subject-1", limit=999))
    asyncio.run(
        client.enqueue_autonomy(
            tenant_id="tenant-1",
            subject_id="subject-1",
            connection_id=connection_id,
            replay_key="cycle-1",
            idempotency_key="idem-1",
        )
    )
    asyncio.run(client.list_autonomy_runs(tenant_id="tenant-1", subject_id="subject-1", limit=999))
    asyncio.run(
        client.control_autonomy_run(
            tenant_id="tenant-1",
            subject_id="subject-1",
            run_id=run_id,
            command="pause",
            reason="owner_pause",
        )
    )
    asyncio.run(
        client.apply_candidate(
            tenant_id="tenant-1",
            subject_id="subject-1",
            candidate_id=message_id,
            approval_ref="approval-1",
            idempotency_key="candidate-apply-1",
        )
    )
    asyncio.run(
        client.execute_rule(
            tenant_id="tenant-1",
            subject_id="subject-1",
            rule_id=rule_id,
            message_ids=(message_id,),
            dry_run=True,
        )
    )
    asyncio.run(
        client.list_rule_executions(
            tenant_id="tenant-1", subject_id="subject-1", rule_id=rule_id, limit=999
        )
    )

    assert [call[0] for call in _RecordingClient.calls] == [
        "POST",
        "GET",
        "GET",
        "GET",
        "POST",
        "GET",
        "POST",
        "POST",
        "POST",
        "GET",
    ]
    assert _RecordingClient.calls[0][1].endswith(f"/{connection_id}:delete")
    assert _RecordingClient.calls[0][3] == {"expected_revision": 2}
    assert "include_content=true&limit=200" in _RecordingClient.calls[1][1]
    assert _RecordingClient.calls[2][1].endswith("/agent-policies?limit=200")
    assert _RecordingClient.calls[3][1].endswith("/delegations?limit=200")
    assert _RecordingClient.calls[4][1].endswith("/autonomy/runs")
    assert _RecordingClient.calls[4][3] == {
        "connection_id": str(connection_id),
        "replay_key": "cycle-1",
        "limit": 50,
        "message_limit": 50,
        "run_inline": False,
    }
    assert _RecordingClient.calls[5][1].endswith("/autonomy/runs?limit=200")
    assert _RecordingClient.calls[6][1].endswith(f"/{run_id}:pause")
    assert _RecordingClient.calls[6][3] == {"reason": "owner_pause"}
    assert _RecordingClient.calls[7][1].endswith(f"/{message_id}:apply")
    assert _RecordingClient.calls[7][2]["Idempotency-Key"] == "candidate-apply-1"
    assert _RecordingClient.calls[7][3] == {"approval_ref": "approval-1"}
    assert _RecordingClient.calls[8][1].endswith(f"/{rule_id}:execute")
    assert _RecordingClient.calls[8][3] == {
        "message_ids": [str(message_id)],
        "dry_run": True,
        "policy_id": None,
        "grant_id": None,
    }
    assert _RecordingClient.calls[9][1].endswith(f"/{rule_id}/executions?limit=500")


def test_mailhub_adapter_forwards_scope_bound_thread_page_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    client = MailHubClient(base_url="https://mailhub.example.test")
    connection_id = UUID("00000000-0000-0000-0000-000000000001")
    result = asyncio.run(
        client.list_thread_page(
            tenant_id="tenant-1",
            subject_id="subject-1",
            limit=20,
            cursor="opaque-cursor",
            connection_id=connection_id,
            unread=True,
            important=True,
            has_attachment=True,
            project=True,
            candidate=True,
        )
    )
    assert result == {"data": {"status": "ok"}}
    path = _RecordingClient.calls[-1][1]
    assert "/v1/mail/threads?limit=20" in path
    assert "cursor=opaque-cursor" in path
    assert f"connection_id={connection_id}" in path
    for key in ("unread", "important", "attachment", "project", "candidate"):
        assert f"{key}=true" in path


def test_mailhub_adapter_forwards_durable_sync_job_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    client = MailHubClient(base_url="https://mailhub.example.test")
    connection_id = UUID("00000000-0000-0000-0000-000000000001")
    job_id = UUID("00000000-0000-0000-0000-000000000002")
    asyncio.run(
        client.list_sync_jobs(
            tenant_id="tenant-1",
            subject_id="subject-1",
            connection_id=connection_id,
            limit=999,
        )
    )
    asyncio.run(client.get_sync_job(tenant_id="tenant-1", subject_id="subject-1", job_id=job_id))
    asyncio.run(
        client.cancel_sync_job(
            tenant_id="tenant-1",
            subject_id="subject-1",
            job_id=job_id,
            trace_id="trace-sync-cancel",
        )
    )
    assert _RecordingClient.calls[0][0] == "GET"
    assert _RecordingClient.calls[0][1].endswith(
        f"/sync-jobs?limit=200&connection_id={connection_id}"
    )
    assert _RecordingClient.calls[1][1].endswith(f"/sync-jobs/{job_id}")
    assert _RecordingClient.calls[2][0] == "POST"
    assert _RecordingClient.calls[2][1].endswith(f"/sync-jobs/{job_id}:cancel")
    assert _RecordingClient.calls[2][2]["X-Trace-Id"] == "trace-sync-cancel"


def test_mailhub_adapter_normalizes_bounded_sync_dates_and_rejects_naive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    client = MailHubClient(base_url="https://mailhub.example.test")
    connection_id = UUID("00000000-0000-0000-0000-000000000001")
    asyncio.run(
        client.enqueue_sync(
            tenant_id="tenant-1",
            subject_id="subject-1",
            connection_id=connection_id,
            mode="backfill",
            folder_ref="INBOX",
            received_after="2026-07-28T08:00:00+08:00",
            received_before="2026-07-29T08:00:00+08:00",
            idempotency_key="sync-1",
        )
    )
    body = _RecordingClient.calls[0][3]
    assert body is not None
    assert body["received_after"] == "2026-07-28T00:00:00Z"
    assert body["received_before"] == "2026-07-29T00:00:00Z"
    with pytest.raises(ValueError, match="must_be_aware"):
        asyncio.run(
            client.enqueue_sync(
                tenant_id="tenant-1",
                subject_id="subject-1",
                connection_id=connection_id,
                mode="backfill",
                received_after="2026-07-28T00:00:00",
                idempotency_key="sync-2",
            )
        )
