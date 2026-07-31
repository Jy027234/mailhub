from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "provider_preflight.py"
_SPEC = importlib.util.spec_from_file_location("mailhub_provider_preflight", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_report = cast(Any, _MODULE.build_report)
preflight_main = cast(Any, _MODULE.main)


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    names = (
        "MAILHUB_GMAIL_ENABLED",
        "MAILHUB_MICROSOFT_GRAPH_ENABLED",
        "MAILHUB_GMAIL_READ_ONLY",
        "MAILHUB_MICROSOFT_GRAPH_READ_ONLY",
        "MAILHUB_GMAIL_PUSH_ENABLED",
        "MAILHUB_MICROSOFT_GRAPH_PUSH_ENABLED",
        "MAILHUB_GMAIL_CLIENT_ID",
        "MAILHUB_GMAIL_CLIENT_SECRET",
        "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT",
        "MAILHUB_GMAIL_REDIRECT_URI",
        "MAILHUB_GMAIL_REDIRECT_URIS",
        "MAILHUB_GMAIL_SCOPES",
        "MAILHUB_GRAPH_CLIENT_ID",
        "MAILHUB_GRAPH_CLIENT_SECRET",
        "MAILHUB_GRAPH_AUTHORIZATION_ENDPOINT",
        "MAILHUB_GRAPH_AUTHORITY_TENANT",
        "MAILHUB_GRAPH_REDIRECT_URI",
        "MAILHUB_GRAPH_REDIRECT_URIS",
        "MAILHUB_GRAPH_SCOPES",
        "MAILHUB_OAUTH_STATE_SIGNING_SECRET",
        "MAILHUB_CREDENTIAL_BROKER_ENDPOINT",
        "MAILHUB_HOST_SERVICE_TOKEN",
        "MAILHUB_CREDENTIAL_ENCRYPTION_SECRET",
        "MAILHUB_HOST_IDENTITY_ENDPOINT",
        "MAILHUB_KMS_KEY_REF",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_provider_preflight_is_safe_and_green_when_providers_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)

    report = build_report(("gmail", "microsoft_graph"))

    assert report["ready"] is True
    assert report["readiness"] == "disabled"
    assert report["config_ready"] is False
    assert report["real_provider_evidence"] is False
    assert report["network_access"] is False
    assert all(
        item["enabled"] is False and item["readiness"] == "disabled" for item in report["providers"]
    )


def test_provider_preflight_checks_required_read_only_oauth_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_READ_ONLY", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_ID", "client-id")
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT", "https://accounts.google.com/o/oauth2/v2/auth"
    )
    monkeypatch.setenv("MAILHUB_GMAIL_REDIRECT_URI", "https://app.example.test/callback")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_SCOPES", "openid https://www.googleapis.com/auth/gmail.readonly"
    )
    monkeypatch.setenv("MAILHUB_OAUTH_STATE_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_BROKER_ENDPOINT", "https://broker.example.test")
    monkeypatch.setenv("MAILHUB_HOST_SERVICE_TOKEN", "h" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_ENCRYPTION_SECRET", "e" * 32)
    monkeypatch.setenv("MAILHUB_HOST_IDENTITY_ENDPOINT", "https://identity.example.test")
    monkeypatch.setenv("MAILHUB_KMS_KEY_REF", "kms/mailhub")

    report = build_report(("gmail",))
    item = report["providers"][0]
    assert isinstance(item, Mapping)
    assert report["ready"] is True
    assert report["readiness"] == "config_ready"
    assert report["config_ready"] is True
    assert item["ready"] is True
    assert item["readiness"] == "config_ready"
    assert item["real_provider_evidence"] is False
    assert item["config"]["client_id"] == "<set>"
    assert "client-id" not in str(report)
    assert "isolated_mailbox_access_not_evidenced" in item["warnings"]


def test_provider_preflight_rejects_write_mode_bad_endpoint_and_missing_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAILHUB_MICROSOFT_GRAPH_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_MICROSOFT_GRAPH_READ_ONLY", "false")
    monkeypatch.setenv("MAILHUB_GRAPH_CLIENT_ID", "client-id")
    monkeypatch.setenv("MAILHUB_GRAPH_AUTHORIZATION_ENDPOINT", "http://evil.example.test/auth")
    monkeypatch.setenv("MAILHUB_GRAPH_REDIRECT_URI", "https://app.example.test/callback")
    monkeypatch.setenv("MAILHUB_GRAPH_SCOPES", "User.Read")
    monkeypatch.setenv("MAILHUB_GRAPH_AUTHORITY_TENANT", "common")
    monkeypatch.setenv("MAILHUB_OAUTH_STATE_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_BROKER_ENDPOINT", "https://broker.example.test")
    monkeypatch.setenv("MAILHUB_HOST_IDENTITY_ENDPOINT", "https://identity.example.test")
    monkeypatch.setenv("MAILHUB_KMS_KEY_REF", "kms/mailhub")

    report = build_report(("microsoft_graph",))
    item = report["providers"][0]
    assert isinstance(item, Mapping)
    assert report["ready"] is False
    assert report["readiness"] == "blocked"
    assert item["ready"] is False
    assert item["readiness"] == "blocked"
    assert "read_only_gate_must_be_true_for_m2_m3" in item["errors"]
    assert "MAILHUB_GRAPH_SCOPES missing mail.read,offline_access" in item["missing"]


def test_provider_preflight_rejects_write_scopes_even_when_read_only_flag_is_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_READ_ONLY", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_ID", "client-id")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT", "https://accounts.google.com/o/oauth2/v2/auth"
    )
    monkeypatch.setenv("MAILHUB_GMAIL_REDIRECT_URI", "https://app.example.test/callback")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_SCOPES",
        "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send",
    )
    monkeypatch.setenv("MAILHUB_OAUTH_STATE_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_BROKER_ENDPOINT", "https://broker.example.test")
    monkeypatch.setenv("MAILHUB_HOST_IDENTITY_ENDPOINT", "https://identity.example.test")
    monkeypatch.setenv("MAILHUB_KMS_KEY_REF", "kms/mailhub")

    report = build_report(("gmail",))
    item = report["providers"][0]
    assert isinstance(item, Mapping)
    assert item["ready"] is False
    assert "write_scope_disallowed_for_read_only" in item["errors"]


def test_provider_preflight_rejects_ambiguous_oauth_endpoint_and_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_ID", "client-id")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT",
        "https://accounts.google.com:443/o/oauth2/v2/auth?tenant=unexpected",
    )
    monkeypatch.setenv("MAILHUB_GMAIL_REDIRECT_URI", "https://app.example.test/callback?next=evil")
    monkeypatch.setenv("MAILHUB_GMAIL_SCOPES", "https://www.googleapis.com/auth/gmail.readonly")
    monkeypatch.setenv("MAILHUB_OAUTH_STATE_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_BROKER_ENDPOINT", "https://broker.example.test")
    monkeypatch.setenv("MAILHUB_HOST_IDENTITY_ENDPOINT", "https://identity.example.test")
    monkeypatch.setenv("MAILHUB_KMS_KEY_REF", "kms/mailhub")

    report = build_report(("gmail",))
    item = report["providers"][0]
    assert isinstance(item, Mapping)
    assert item["ready"] is False
    assert "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT_host_or_tls_invalid" in item["errors"]
    assert "MAILHUB_GMAIL_REDIRECT_URI_must_be_https_or_localhost" in item["errors"]


def test_provider_preflight_validates_all_redirect_allowlist_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_ID", "client-id")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT", "https://accounts.google.com/o/oauth2/v2/auth"
    )
    monkeypatch.setenv("MAILHUB_GMAIL_REDIRECT_URI", "https://app.example.test/callback")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_REDIRECT_URIS",
        "https://app.example.test/callback https://evil.example.test/callback?next=mail",
    )
    monkeypatch.setenv("MAILHUB_GMAIL_SCOPES", "https://www.googleapis.com/auth/gmail.readonly")
    monkeypatch.setenv("MAILHUB_OAUTH_STATE_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_BROKER_ENDPOINT", "https://broker.example.test")
    monkeypatch.setenv("MAILHUB_HOST_IDENTITY_ENDPOINT", "https://identity.example.test")
    monkeypatch.setenv("MAILHUB_KMS_KEY_REF", "kms/mailhub")

    report = build_report(("gmail",))
    item = report["providers"][0]
    assert isinstance(item, Mapping)
    assert report["ready"] is False
    assert "MAILHUB_GMAIL_REDIRECT_URIS[1]_must_be_https_or_localhost" in item["errors"]


def test_provider_preflight_allows_loopback_development_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_ID", "client-id")
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT", "https://accounts.google.com/o/oauth2/v2/auth"
    )
    monkeypatch.setenv("MAILHUB_GMAIL_REDIRECT_URI", "http://127.0.0.1:5180/callback")
    monkeypatch.setenv("MAILHUB_GMAIL_REDIRECT_URIS", "http://127.0.0.1:5180/callback")
    monkeypatch.setenv("MAILHUB_GMAIL_SCOPES", "https://www.googleapis.com/auth/gmail.readonly")
    monkeypatch.setenv("MAILHUB_OAUTH_STATE_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_BROKER_ENDPOINT", "https://broker.example.test")
    monkeypatch.setenv("MAILHUB_HOST_SERVICE_TOKEN", "h" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_ENCRYPTION_SECRET", "e" * 32)
    monkeypatch.setenv("MAILHUB_HOST_IDENTITY_ENDPOINT", "https://identity.example.test")
    monkeypatch.setenv("MAILHUB_KMS_KEY_REF", "kms/mailhub")

    report = build_report(("gmail",))
    item = report["providers"][0]
    assert isinstance(item, Mapping)
    assert report["config_ready"] is True
    assert item["errors"] == []


def test_provider_preflight_keeps_push_in_a_later_activation_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_PUSH_ENABLED", "true")

    report = build_report(("gmail",))
    item = report["providers"][0]
    assert isinstance(item, Mapping)
    assert item["ready"] is False
    assert "MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT" in item["missing"]
    assert "MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT" in item["missing"]


def test_provider_preflight_accepts_push_with_host_subscription_and_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_PUSH_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_ID", "client-id")
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT", "https://accounts.google.com/o/oauth2/v2/auth"
    )
    monkeypatch.setenv("MAILHUB_GMAIL_REDIRECT_URI", "https://app.example.test/callback")
    monkeypatch.setenv("MAILHUB_GMAIL_SCOPES", "https://www.googleapis.com/auth/gmail.readonly")
    monkeypatch.setenv("MAILHUB_OAUTH_STATE_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_BROKER_ENDPOINT", "https://broker.example.test")
    monkeypatch.setenv("MAILHUB_HOST_SERVICE_TOKEN", "h" * 32)
    monkeypatch.setenv("MAILHUB_CREDENTIAL_ENCRYPTION_SECRET", "e" * 32)
    monkeypatch.setenv("MAILHUB_HOST_IDENTITY_ENDPOINT", "https://identity.example.test")
    monkeypatch.setenv("MAILHUB_KMS_KEY_REF", "kms/mailhub")
    monkeypatch.setenv(
        "MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT", "https://host.example.test/subscriptions"
    )
    monkeypatch.setenv(
        "MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT",
        "https://host.example.test/notification-verifier",
    )

    report = build_report(("gmail",))
    item = report["providers"][0]
    assert isinstance(item, Mapping)
    assert report["config_ready"] is True
    assert item["config"]["subscription_endpoint"] == "<set>"
    assert item["config"]["notification_verifier_endpoint"] == "<set>"


def test_provider_preflight_rejects_insecure_or_ambiguous_push_host_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_GMAIL_PUSH_ENABLED", "true")
    monkeypatch.setenv(
        "MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT", "http://host.example.test/subscriptions"
    )
    monkeypatch.setenv(
        "MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT",
        "https://host.example.test/verify?tenant=unexpected",
    )

    report = build_report(("gmail",))
    item = report["providers"][0]
    assert isinstance(item, Mapping)
    assert item["ready"] is False
    assert "MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT_must_be_https_or_loopback" in item["errors"]
    assert (
        "MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT_must_be_https_or_loopback"
        in item["errors"]
    )


def test_provider_preflight_require_config_ready_does_not_treat_disabled_as_green(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_provider_env(monkeypatch)

    assert preflight_main(["--provider", "gmail", "--json", "--require-config-ready"]) == 2
    output = capsys.readouterr().out
    assert '"config_ready": false' in output
