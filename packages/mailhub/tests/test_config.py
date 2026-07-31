import pytest

from mailhub.config import MailHubSettings


def test_production_configuration_fails_closed_without_external_controls() -> None:
    report = MailHubSettings(environment="production").validate_for_runtime()
    assert report.ready is False
    assert "database_url_postgresql_asyncpg" in report.missing
    assert "kms_key_ref" in report.missing
    assert "telemetry_endpoint" in report.missing
    assert "quota_endpoint" in report.missing
    with pytest.raises(RuntimeError, match="mailhub_runtime_blocked"):
        MailHubSettings(environment="production").require_ready()


def test_development_configuration_is_read_only_by_default() -> None:
    settings = MailHubSettings()
    report = settings.validate_for_runtime()
    assert report.ready is True
    assert report.outbound_enabled is False
    assert settings.gmail_enabled is False
    assert settings.microsoft_graph_enabled is False
    assert settings.gmail_read_only is True
    assert settings.microsoft_graph_read_only is True
    assert settings.gmail_push_enabled is False
    assert settings.microsoft_graph_push_enabled is False


def test_outbound_requires_production_mode() -> None:
    report = MailHubSettings(outbound_enabled=True).validate_for_runtime()
    assert report.ready is False
    assert "outbound_requires_production_mode" in report.missing


def test_host_ai_endpoint_is_loaded_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILHUB_AI_EXECUTION_ENDPOINT", "https://ai.example.test")
    assert MailHubSettings.from_env().ai_execution_endpoint == "https://ai.example.test"


def test_agent_memory_endpoint_is_explicitly_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILHUB_AGENT_MEMORY_ENDPOINT", "https://memory.example.test")
    assert MailHubSettings.from_env().agent_memory_endpoint == "https://memory.example.test"


def test_event_publisher_endpoint_is_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_EVENT_PUBLISHER_ENDPOINT", "https://events.example.test")
    assert MailHubSettings.from_env().event_publisher_endpoint == "https://events.example.test"


def test_telemetry_and_quota_endpoints_are_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_TELEMETRY_ENDPOINT", "https://telemetry.example.test")
    monkeypatch.setenv("MAILHUB_QUOTA_ENDPOINT", "https://quota.example.test")

    settings = MailHubSettings.from_env()

    assert settings.telemetry_endpoint == "https://telemetry.example.test"
    assert settings.quota_endpoint == "https://quota.example.test"


def test_provider_subscription_endpoint_is_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT", "https://subscriptions.example.test"
    )

    assert (
        MailHubSettings.from_env().provider_subscription_endpoint
        == "https://subscriptions.example.test"
    )


def test_provider_notification_verifier_endpoint_is_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT", "https://notifications.example.test"
    )

    assert (
        MailHubSettings.from_env().provider_notification_verifier_endpoint
        == "https://notifications.example.test"
    )


def test_scheduled_read_only_production_does_not_require_push_endpoint() -> None:
    settings = MailHubSettings(environment="production")
    assert "provider_subscription_endpoint" not in settings.validate_for_runtime().missing
    assert "provider_notification_verifier_endpoint" not in settings.validate_for_runtime().missing


def test_production_credential_broker_requires_host_service_token() -> None:
    settings = MailHubSettings(
        environment="production",
        credential_broker_endpoint="https://host.example.test",
    )

    assert "host_service_token" in settings.validate_for_runtime().missing


def test_push_enabled_production_requires_both_host_provider_endpoints() -> None:
    settings = MailHubSettings(environment="production", gmail_push_enabled=True)
    missing = settings.validate_for_runtime().missing
    assert "provider_subscription_endpoint" in missing
    assert "provider_notification_verifier_endpoint" in missing


def test_push_enabled_production_rejects_unsafe_host_provider_endpoints() -> None:
    settings = MailHubSettings(
        environment="production",
        gmail_push_enabled=True,
        provider_subscription_endpoint="http://host.example.test/subscriptions",
        provider_notification_verifier_endpoint="https://host.example.test/verify?tenant=unexpected",
    )
    missing = settings.validate_for_runtime().missing
    assert "provider_subscription_endpoint_invalid" in missing
    assert "provider_notification_verifier_endpoint_invalid" in missing


def test_kill_switch_endpoint_is_loaded_and_required_for_enabled_production() -> None:
    settings = MailHubSettings(environment="production", outbound_enabled=True)
    report = settings.validate_for_runtime()
    assert "kill_switch_endpoint" in report.missing
    configured = MailHubSettings(
        environment="production",
        outbound_enabled=True,
        kill_switch_endpoint="https://ops.example.test",
    )
    assert configured.kill_switch_endpoint == "https://ops.example.test"


def test_real_provider_switches_are_explicitly_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILHUB_GMAIL_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_MICROSOFT_GRAPH_ENABLED", "1")

    settings = MailHubSettings.from_env()

    assert settings.gmail_enabled is True
    assert settings.microsoft_graph_enabled is True


def test_real_provider_write_and_push_gates_are_loaded_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_GMAIL_READ_ONLY", "false")
    monkeypatch.setenv("MAILHUB_MICROSOFT_GRAPH_READ_ONLY", "0")
    monkeypatch.setenv("MAILHUB_GMAIL_PUSH_ENABLED", "true")
    monkeypatch.setenv("MAILHUB_MICROSOFT_GRAPH_PUSH_ENABLED", "1")

    settings = MailHubSettings.from_env()

    assert settings.gmail_read_only is False
    assert settings.microsoft_graph_read_only is False
    assert settings.gmail_push_enabled is True
    assert settings.microsoft_graph_push_enabled is True


def test_oauth_registration_values_are_loaded_without_exposing_signing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_OAUTH_STATE_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("MAILHUB_GMAIL_CLIENT_ID", "gmail-client")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT", "https://accounts.google.com/o/oauth2/v2/auth"
    )
    monkeypatch.setenv("MAILHUB_GMAIL_REDIRECT_URIS", "https://app.example.test/callback")
    monkeypatch.setenv(
        "MAILHUB_GMAIL_SCOPES", "openid https://www.googleapis.com/auth/gmail.readonly"
    )

    settings = MailHubSettings.from_env()

    assert settings.gmail_client_id == "gmail-client"
    assert settings.gmail_redirect_uris == ("https://app.example.test/callback",)
    assert settings.oauth_state_signing_secret is not None
    assert "s" * 32 not in repr(settings)


def test_worker_lease_seconds_are_loaded_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILHUB_JOB_LEASE_SECONDS", "45")
    assert MailHubSettings.from_env().job_lease_seconds == 45

    monkeypatch.setenv("MAILHUB_JOB_LEASE_SECONDS", "3601")
    with pytest.raises(ValueError):
        MailHubSettings.from_env()


def test_versioned_ai_policy_is_loaded_and_bounded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAILHUB_AI_POLICY_VERSION", "mail-ai-policy-test-v2")
    monkeypatch.setenv("MAILHUB_AI_MAX_INPUT_CHARS", "1200")
    monkeypatch.setenv("MAILHUB_AI_MAX_ESTIMATED_TOKENS", "250")
    monkeypatch.setenv("MAILHUB_AI_ACTION_MIN_CONFIDENCE", "0.8")

    settings = MailHubSettings.from_env()
    policy = settings.analysis_policy()

    assert policy.policy_version == "mail-ai-policy-test-v2"
    assert policy.max_input_chars == 1200
    assert policy.max_estimated_tokens == 250
    assert policy.action_min_confidence == 0.8


def test_invalid_ai_budget_environment_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILHUB_AI_MAX_ACTIONS", "not-an-integer")

    with pytest.raises(ValueError, match="MAILHUB_AI_MAX_ACTIONS_invalid_integer"):
        MailHubSettings.from_env()
