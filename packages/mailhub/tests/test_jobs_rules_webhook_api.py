from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from mailhub.api import create_app
from mailhub.config import MailHubSettings
from mailhub.domain import ActionType, MailMessageProjection, digest_text
from mailhub.oauth import (
    InMemoryOAuthStateStore,
    OAuthAuthorizationService,
    OAuthCallbackContext,
    OAuthProvider,
)
from mailhub.rules import (
    MailRule,
    RuleCondition,
    RuleEvaluation,
    RuleField,
    RuleOperator,
    evaluate_rule,
    summarize_rule_evaluations,
)
from mailhub.webhook import WebhookIngress, sign_webhook


def test_sync_request_is_durable_and_idempotent() -> None:
    client = TestClient(create_app())
    headers = {
        "X-MailHub-Tenant": "tenant-jobs",
        "X-MailHub-Subject": "subject-jobs",
        "Idempotency-Key": "sync-once",
    }
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "jobs@example.test",
            "credential_ref": "sandbox-ref",
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["data"]["connection_id"]
    first = client.post(
        f"/v1/mail/connections/{connection_id}/sync-jobs",
        headers=headers,
        json={"mode": "incremental", "limit": 10},
    )
    second = client.post(
        f"/v1/mail/connections/{connection_id}/sync-jobs",
        headers=headers,
        json={"mode": "incremental", "limit": 10},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["job_ref"] == second.json()["data"]["job_ref"]
    assert first.json()["data"]["status"] == "queued"
    inline = client.post(
        f"/v1/mail/connections/{connection_id}/sync-jobs",
        headers={**headers, "Idempotency-Key": "sync-inline"},
        json={"run_inline": True},
    )
    assert inline.status_code == 200
    assert inline.json()["data"]["status"] == "succeeded"


def test_backfill_request_persists_bounded_filter_contract() -> None:
    client = TestClient(create_app())
    headers = {
        "X-MailHub-Tenant": "tenant-backfill",
        "X-MailHub-Subject": "subject-backfill",
        "Idempotency-Key": "backfill-once",
    }
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "backfill@example.test",
            "credential_ref": "sandbox-ref",
        },
    )
    assert created.status_code == 201
    connection_id = created.json()["data"]["connection_id"]
    response = client.post(
        f"/v1/mail/connections/{connection_id}/sync-jobs",
        headers=headers,
        json={
            "mode": "backfill",
            "limit": 25,
            "folder_ref": "INBOX",
            "label_refs": ["project", "important", "project"],
            "received_after": "2026-06-01T00:00:00Z",
            "received_before": "2026-08-01T00:00:00Z",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "backfill"
    assert data["folder_ref"] == "INBOX"
    assert data["label_refs"] == ["project", "important"]
    assert data["received_after"].startswith("2026-06-01T00:00:00")
    assert data["received_before"].startswith("2026-08-01T00:00:00")


def test_backfill_filter_cannot_be_attached_to_incremental_job() -> None:
    client = TestClient(create_app())
    headers = {
        "X-MailHub-Tenant": "tenant-backfill-negative",
        "X-MailHub-Subject": "subject-backfill-negative",
        "Idempotency-Key": "backfill-negative",
    }
    created = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "backfill-negative@example.test",
            "credential_ref": "sandbox-ref",
        },
    )
    connection_id = created.json()["data"]["connection_id"]
    response = client.post(
        f"/v1/mail/connections/{connection_id}/sync-jobs",
        headers=headers,
        json={"mode": "incremental", "label_refs": ["project"]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["message"] == "sync_filter_requires_backfill"


def test_draft_create_uses_idempotency_key() -> None:
    client = TestClient(create_app())
    headers = {
        "X-MailHub-Tenant": "tenant-draft",
        "X-MailHub-Subject": "subject-draft",
        "Idempotency-Key": "draft-once",
    }
    connection = client.post(
        "/v1/mail/connections",
        headers=headers,
        json={
            "provider": "sandbox",
            "email_address": "user@example.test",
            "credential_ref": "sandbox-ref",
        },
    )
    assert connection.status_code == 201
    body = {
        "connection_id": connection.json()["data"]["connection_id"],
        "recipient_addresses": ["recipient@example.test"],
        "subject": "Review",
        "body_text": "Please review.",
    }
    first = client.post("/v1/mail/drafts", headers=headers, json=body)
    second = client.post("/v1/mail/drafts", headers=headers, json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["draft_id"] == second.json()["data"]["draft_id"]


def test_rule_evaluator_is_declarative_and_low_risk() -> None:
    rule = MailRule(
        rule_id=uuid4(),
        tenant_id="tenant-rules",
        owner_subject_id="subject-rules",
        name="label invoices",
        conditions=(RuleCondition(RuleField.SUBJECT, RuleOperator.CONTAINS, "invoice"),),
        action_type=ActionType.LABEL,
    )
    message = MailMessageProjection(
        message_id=uuid4(),
        tenant_id="tenant-rules",
        connection_id=uuid4(),
        thread_id=uuid4(),
        provider_message_ref="provider-1",
        internet_message_id=None,
        sender_address="billing@example.test",
        recipient_addresses=("user@example.test",),
        subject="Invoice ready",
        received_at=datetime.now(UTC),
        body_text="safe",
        body_object_ref=None,
        content_sha256=digest_text("safe"),
    )
    # The evaluator only consumes the projection; this assertion covers the
    # disabled-by-default publish boundary without invoking provider I/O.
    assert evaluate_rule(rule, message).matched is False


def test_rule_simulation_summary_reports_expected_hits_without_side_effects() -> None:
    evaluation = RuleEvaluation(uuid4(), 1, True, "matched_dry_run")
    summary = summarize_rule_evaluations((evaluation,))
    assert summary.evaluated_count == 1
    assert summary.matched_count == 1
    assert summary.dry_run is True


def test_webhook_receipt_is_verified_and_deduplicated() -> None:
    secret = b"s" * 32
    ingress = WebhookIngress(provider="gmail", signing_secret=secret)
    client = TestClient(create_app(webhook_ingresses={"gmail": ingress}))
    body = b'{"historyId":"42"}'
    headers = {
        "Content-Type": "application/json",
        "X-MailHub-Tenant": "tenant-webhook",
        "X-MailHub-Event-Id": "event-1",
        "X-MailHub-Signature": sign_webhook(secret=secret, body=body),
        "X-Trace-Id": "trace-1",
    }
    first = client.post("/v1/mail/webhooks/gmail", headers=headers, content=body)
    # Simulate a second API replica with a cold process-local dedupe cache. The
    # repository's unique receipt key must still make the response a duplicate.
    ingress._events.clear()  # noqa: SLF001 - deliberate replica simulation
    second = client.post("/v1/mail/webhooks/gmail", headers=headers, content=body)
    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["status"] == "accepted"
    assert second.json()["data"]["status"] == "duplicate"


def test_webhook_rejects_declared_oversize_before_body_processing() -> None:
    ingress = WebhookIngress(provider="gmail", signing_secret=b"s" * 32)
    client = TestClient(create_app(webhook_ingresses={"gmail": ingress}))
    response = client.post(
        "/v1/mail/webhooks/gmail",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(ingress.max_body_bytes + 1),
            "X-MailHub-Tenant": "tenant-webhook-limit",
            "X-MailHub-Event-Id": "event-limit",
            "X-MailHub-Signature": "sha256=" + "0" * 64,
        },
        content=b"{}",
    )
    assert response.status_code == 413


def test_oauth_api_keeps_verifier_server_side_and_hands_off_credential_ref() -> None:
    class Callback:
        async def exchange(
            self, *, context: OAuthCallbackContext, code: str
        ) -> Mapping[str, object]:
            assert code == "provider-code"
            assert context.provider is OAuthProvider.GMAIL
            return {
                "email_address": "oauth@example.test",
                "credential_ref": "secret-manager://credential-1",
                "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "provider_account_id": "google-account-1",
                "credential_version": 2,
            }

    oauth = OAuthAuthorizationService(
        signing_secret=b"o" * 32,
        state_store=InMemoryOAuthStateStore(),
        allowed_redirect_uris={OAuthProvider.GMAIL: ("https://app.example.test/oauth/callback",)},
    )
    # OAuth callback contract tests explicitly opt into the controlled
    # preflight provider switch; they do not claim real provider evidence.
    # switch; this remains a fake callback and never contacts Gmail.
    client = TestClient(
        create_app(
            oauth_service=oauth,
            oauth_callback_port=Callback(),
            runtime_settings=MailHubSettings(gmail_enabled=True),
        )
    )
    headers = {"X-MailHub-Tenant": "tenant-oauth", "X-MailHub-Subject": "subject-oauth"}
    begun = client.post(
        "/v1/mail/oauth/gmail:authorize",
        headers=headers,
        json={
            "authorization_endpoint": "https://accounts.example.test/authorize",
            "client_id": "client-1",
            "redirect_uri": "https://app.example.test/oauth/callback",
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        },
    )
    assert begun.status_code == 200
    assert "code_verifier" not in begun.json()["data"]
    state = begun.json()["data"]["state"]
    completed = client.post(
        "/v1/mail/oauth/gmail:callback",
        headers=headers,
        json={
            "state": state,
            "code": "provider-code",
            "redirect_uri": "https://app.example.test/oauth/callback",
        },
    )
    assert completed.status_code == 200
    assert completed.json()["data"]["connection"]["status"] == "active"
    assert completed.json()["data"]["connection"]["provider_account_id"] == "google-account-1"
    assert completed.json()["data"]["connection"]["credential_version"] == 2
    connection = completed.json()["data"]["connection"]
    connection_id = connection["connection_id"]
    reauth_begun = client.post(
        "/v1/mail/oauth/gmail:authorize",
        headers=headers,
        json={
            "authorization_endpoint": "https://accounts.example.test/authorize",
            "client_id": "client-1",
            "redirect_uri": "https://app.example.test/oauth/callback",
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "connection_id": connection_id,
            "expected_revision": connection["revision"],
        },
    )
    assert reauth_begun.status_code == 200
    assert reauth_begun.json()["data"]["connection_id"] == connection_id
    reauth_completed = client.post(
        "/v1/mail/oauth/gmail:callback",
        headers=headers,
        json={
            "state": reauth_begun.json()["data"]["state"],
            "code": "provider-code",
            "redirect_uri": "https://app.example.test/oauth/callback",
        },
    )
    assert reauth_completed.status_code == 200
    assert reauth_completed.json()["data"]["connection"]["connection_id"] == connection_id
    assert reauth_completed.json()["data"]["connection"]["revision"] == connection["revision"] + 1
