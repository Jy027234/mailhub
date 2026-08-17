"""Fail-closed runtime configuration for MailHub deployments."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from mailhub.intelligence import AnalysisPolicy


class MailHubSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str = Field(default="development", min_length=1, max_length=32)
    database_url: str | None = None
    object_store_endpoint: str | None = None
    kms_key_ref: str | None = None
    av_scanner_endpoint: str | None = None
    dlp_endpoint: str | None = None
    credential_broker_endpoint: str | None = None
    host_service_token: SecretStr | None = None
    host_identity_endpoint: str | None = None
    approval_endpoint: str | None = None
    ai_execution_endpoint: str | None = None
    host_action_endpoint: str | None = None
    knowledge_endpoint: str | None = None
    agent_memory_endpoint: str | None = None
    telemetry_endpoint: str | None = None
    event_publisher_endpoint: str | None = None
    quota_endpoint: str | None = None
    provider_subscription_endpoint: str | None = None
    provider_notification_verifier_endpoint: str | None = None
    kill_switch_endpoint: str | None = None
    oauth_state_signing_secret: SecretStr | None = None
    # Optional client->API bearer token.  When set, every /v1/mail/* request
    # must present it (constant-time compare) in addition to the host identity
    # headers; unset preserves the host-identity-only mode for controlled
    # local graphs.  This token never replaces the Host Identity port.
    api_auth_token: SecretStr | None = None
    gmail_client_id: str | None = None
    gmail_authorization_endpoint: str | None = None
    gmail_redirect_uris: tuple[str, ...] = ()
    gmail_scopes: tuple[str, ...] = ()
    graph_client_id: str | None = None
    graph_authorization_endpoint: str | None = None
    graph_authority_tenant: str | None = None
    graph_redirect_uris: tuple[str, ...] = ()
    graph_scopes: tuple[str, ...] = ()
    ai_policy_version: str = Field(default="mail-ai-policy-v1", min_length=1, max_length=100)
    ai_prompt_version: str = Field(default="rules-prompt-v1", min_length=1, max_length=100)
    ai_parser_version: str = Field(default="rules-parser-v1", min_length=1, max_length=100)
    ai_calibration_version: str = Field(
        default="evidence-calibration-v1", min_length=1, max_length=100
    )
    ai_mode: str = Field(default="rules", min_length=1, max_length=40)
    ai_max_input_chars: int = Field(default=200_000, ge=1_000, le=2_000_000)
    ai_max_evidence: int = Field(default=100, ge=1, le=1_000)
    ai_max_actions: int = Field(default=20, ge=1, le=100)
    ai_max_estimated_tokens: int = Field(default=50_000, ge=100, le=500_000)
    ai_chars_per_token: float = Field(default=4.0, ge=1.0, le=10.0)
    ai_action_min_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    ai_knowledge_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    ai_low_confidence_floor: float = Field(default=0.35, ge=0.0, le=1.0)
    outbound_enabled: bool = False
    rule_automation_enabled: bool = False
    job_lease_seconds: int = Field(default=300, ge=1, le=3600)
    allow_sandbox: bool = True
    # Real Provider adapters are intentionally opt-in.  The adapters are
    # available for a controlled M2/M3 preflight, while read-only and push
    # gates remain fail-closed until their corresponding evidence exists.
    gmail_enabled: bool = False
    microsoft_graph_enabled: bool = False
    gmail_read_only: bool = True
    microsoft_graph_read_only: bool = True
    gmail_push_enabled: bool = False
    microsoft_graph_push_enabled: bool = False
    # IMAP/SMTP is an application-password/XOAUTH2 transport and never uses the
    # Gmail/Graph OAuth registration above.  Read-only IMAP sync is the B4
    # fallback slice; SMTP send stays disabled until an explicit operator gate.
    imap_enabled: bool = False
    imap_host: str | None = None
    smtp_host: str | None = None
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    imap_folder: str = Field(default="INBOX", min_length=1, max_length=200)
    smtp_send_enabled: bool = False

    @classmethod
    def from_env(cls) -> MailHubSettings:
        return cls(
            environment=os.getenv("MAILHUB_ENV", "development"),
            database_url=os.getenv("MAILHUB_DATABASE_URL"),
            object_store_endpoint=os.getenv("MAILHUB_OBJECT_STORE_ENDPOINT"),
            kms_key_ref=os.getenv("MAILHUB_KMS_KEY_REF"),
            av_scanner_endpoint=os.getenv("MAILHUB_AV_SCANNER_ENDPOINT"),
            dlp_endpoint=os.getenv("MAILHUB_DLP_ENDPOINT"),
            credential_broker_endpoint=os.getenv("MAILHUB_CREDENTIAL_BROKER_ENDPOINT"),
            host_service_token=(
                SecretStr(value) if (value := os.getenv("MAILHUB_HOST_SERVICE_TOKEN")) else None
            ),
            host_identity_endpoint=os.getenv("MAILHUB_HOST_IDENTITY_ENDPOINT"),
            approval_endpoint=os.getenv("MAILHUB_APPROVAL_ENDPOINT"),
            ai_execution_endpoint=os.getenv("MAILHUB_AI_EXECUTION_ENDPOINT"),
            host_action_endpoint=os.getenv("MAILHUB_HOST_ACTION_ENDPOINT"),
            knowledge_endpoint=os.getenv("MAILHUB_KNOWLEDGE_ENDPOINT"),
            agent_memory_endpoint=os.getenv("MAILHUB_AGENT_MEMORY_ENDPOINT"),
            telemetry_endpoint=os.getenv("MAILHUB_TELEMETRY_ENDPOINT"),
            event_publisher_endpoint=os.getenv("MAILHUB_EVENT_PUBLISHER_ENDPOINT"),
            quota_endpoint=os.getenv("MAILHUB_QUOTA_ENDPOINT"),
            provider_subscription_endpoint=os.getenv("MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT"),
            provider_notification_verifier_endpoint=os.getenv(
                "MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT"
            ),
            kill_switch_endpoint=os.getenv("MAILHUB_KILL_SWITCH_ENDPOINT"),
            oauth_state_signing_secret=_env_secret("MAILHUB_OAUTH_STATE_SIGNING_SECRET"),
            api_auth_token=_env_secret("MAILHUB_API_AUTH_TOKEN"),
            gmail_client_id=os.getenv("MAILHUB_GMAIL_CLIENT_ID"),
            gmail_authorization_endpoint=os.getenv("MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT"),
            gmail_redirect_uris=_env_tuple(
                "MAILHUB_GMAIL_REDIRECT_URIS", fallback="MAILHUB_GMAIL_REDIRECT_URI"
            ),
            gmail_scopes=_env_tuple("MAILHUB_GMAIL_SCOPES"),
            graph_client_id=os.getenv("MAILHUB_GRAPH_CLIENT_ID"),
            graph_authorization_endpoint=os.getenv("MAILHUB_GRAPH_AUTHORIZATION_ENDPOINT"),
            graph_authority_tenant=os.getenv("MAILHUB_GRAPH_AUTHORITY_TENANT"),
            graph_redirect_uris=_env_tuple(
                "MAILHUB_GRAPH_REDIRECT_URIS", fallback="MAILHUB_GRAPH_REDIRECT_URI"
            ),
            graph_scopes=_env_tuple("MAILHUB_GRAPH_SCOPES"),
            ai_policy_version=os.getenv("MAILHUB_AI_POLICY_VERSION", "mail-ai-policy-v1"),
            ai_prompt_version=os.getenv("MAILHUB_AI_PROMPT_VERSION", "rules-prompt-v1"),
            ai_parser_version=os.getenv("MAILHUB_AI_PARSER_VERSION", "rules-parser-v1"),
            ai_calibration_version=os.getenv(
                "MAILHUB_AI_CALIBRATION_VERSION", "evidence-calibration-v1"
            ),
            ai_mode=os.getenv("MAILHUB_AI_MODE", "rules"),
            ai_max_input_chars=_env_int("MAILHUB_AI_MAX_INPUT_CHARS", 200_000),
            ai_max_evidence=_env_int("MAILHUB_AI_MAX_EVIDENCE", 100),
            ai_max_actions=_env_int("MAILHUB_AI_MAX_ACTIONS", 20),
            ai_max_estimated_tokens=_env_int("MAILHUB_AI_MAX_ESTIMATED_TOKENS", 50_000),
            ai_chars_per_token=_env_float("MAILHUB_AI_CHARS_PER_TOKEN", 4.0),
            ai_action_min_confidence=_env_float("MAILHUB_AI_ACTION_MIN_CONFIDENCE", 0.65),
            ai_knowledge_min_confidence=_env_float("MAILHUB_AI_KNOWLEDGE_MIN_CONFIDENCE", 0.75),
            ai_low_confidence_floor=_env_float("MAILHUB_AI_LOW_CONFIDENCE_FLOOR", 0.35),
            outbound_enabled=_env_bool("MAILHUB_OUTBOUND_ENABLED", default=False),
            rule_automation_enabled=_env_bool("MAILHUB_RULE_AUTOMATION_ENABLED", default=False),
            job_lease_seconds=_env_int("MAILHUB_JOB_LEASE_SECONDS", 300),
            allow_sandbox=_env_bool("MAILHUB_ALLOW_SANDBOX", default=True),
            gmail_enabled=_env_bool("MAILHUB_GMAIL_ENABLED", default=False),
            microsoft_graph_enabled=_env_bool("MAILHUB_MICROSOFT_GRAPH_ENABLED", default=False),
            gmail_read_only=_env_bool("MAILHUB_GMAIL_READ_ONLY", default=True),
            microsoft_graph_read_only=_env_bool("MAILHUB_MICROSOFT_GRAPH_READ_ONLY", default=True),
            gmail_push_enabled=_env_bool("MAILHUB_GMAIL_PUSH_ENABLED", default=False),
            microsoft_graph_push_enabled=_env_bool(
                "MAILHUB_MICROSOFT_GRAPH_PUSH_ENABLED", default=False
            ),
            imap_enabled=_env_bool("MAILHUB_IMAP_ENABLED", default=False),
            imap_host=os.getenv("MAILHUB_IMAP_HOST"),
            smtp_host=os.getenv("MAILHUB_SMTP_HOST"),
            imap_port=_env_int("MAILHUB_IMAP_PORT", 993),
            smtp_port=_env_int("MAILHUB_SMTP_PORT", 465),
            imap_folder=os.getenv("MAILHUB_IMAP_FOLDER", "INBOX"),
            smtp_send_enabled=_env_bool("MAILHUB_SMTP_SEND_ENABLED", default=False),
        )

    def analysis_policy(self) -> AnalysisPolicy:
        """Return the bounded, versioned analysis policy for this deployment."""

        return AnalysisPolicy(
            policy_version=self.ai_policy_version,
            prompt_version=self.ai_prompt_version,
            parser_version=self.ai_parser_version,
            calibration_version=self.ai_calibration_version,
            mode=self.ai_mode,
            max_input_chars=self.ai_max_input_chars,
            max_evidence=self.ai_max_evidence,
            max_actions=self.ai_max_actions,
            max_estimated_tokens=self.ai_max_estimated_tokens,
            chars_per_token=self.ai_chars_per_token,
            action_min_confidence=self.ai_action_min_confidence,
            knowledge_min_confidence=self.ai_knowledge_min_confidence,
            low_confidence_floor=self.ai_low_confidence_floor,
        )

    def missing_production_requirements(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.database_url or not self.database_url.startswith("postgresql+asyncpg://"):
            missing.append("database_url_postgresql_asyncpg")
        for field_name in (
            "object_store_endpoint",
            "kms_key_ref",
            "av_scanner_endpoint",
            "dlp_endpoint",
            "credential_broker_endpoint",
            "host_identity_endpoint",
            "approval_endpoint",
            "ai_execution_endpoint",
            "host_action_endpoint",
            "knowledge_endpoint",
            "telemetry_endpoint",
            "event_publisher_endpoint",
            "quota_endpoint",
        ):
            if not getattr(self, field_name):
                missing.append(field_name)
        if self.credential_broker_endpoint and self.host_service_token is None:
            missing.append("host_service_token")
        if self.allow_sandbox:
            missing.append("sandbox_disabled")
        if (
            self.outbound_enabled or self.rule_automation_enabled
        ) and not self.kill_switch_endpoint:
            missing.append("kill_switch_endpoint")
        if self.gmail_push_enabled or self.microsoft_graph_push_enabled:
            if not self.provider_subscription_endpoint:
                missing.append("provider_subscription_endpoint")
            elif not _host_endpoint_ok(self.provider_subscription_endpoint):
                missing.append("provider_subscription_endpoint_invalid")
            if not self.provider_notification_verifier_endpoint:
                missing.append("provider_notification_verifier_endpoint")
            elif not _host_endpoint_ok(self.provider_notification_verifier_endpoint):
                missing.append("provider_notification_verifier_endpoint_invalid")
        return tuple(missing)

    def validate_for_runtime(self) -> RuntimeConfigReport:
        production_like = self.environment.casefold() in {"production", "prod", "staging"}
        missing = self.missing_production_requirements() if production_like else ()
        if self.outbound_enabled and not production_like:
            missing = (*missing, "outbound_requires_production_mode")
        return RuntimeConfigReport(
            environment=self.environment,
            ready=not missing,
            outbound_enabled=self.outbound_enabled,
            missing=tuple(dict.fromkeys(missing)),
        )

    def require_ready(self) -> None:
        report = self.validate_for_runtime()
        if not report.ready:
            raise RuntimeError("mailhub_runtime_blocked:" + ",".join(report.missing))


@dataclass(frozen=True, slots=True)
class RuntimeConfigReport:
    environment: str
    ready: bool
    outbound_enabled: bool
    missing: tuple[str, ...]


def _host_endpoint_ok(value: str) -> bool:
    """Keep injected Host Port base URLs on the same fail-closed contract as A0."""

    if not value or any(ord(char) < 33 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return False
    local_http = parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
    return (
        bool(hostname)
        and (parsed.scheme == "https" or local_http)
        and parsed.username is None
        and parsed.password is None
        and (port is None or 1 <= port <= 65535)
        and not parsed.query
        and not parsed.fragment
    )


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}_invalid_boolean")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name}_invalid_integer") from exc


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name}_invalid_number") from exc


def _env_tuple(name: str, *, fallback: str | None = None) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None and fallback is not None:
        value = os.getenv(fallback)
    if not value:
        return ()
    return tuple(dict.fromkeys(item for item in value.replace(",", " ").split() if item))


def _env_secret(name: str) -> SecretStr | None:
    value = os.getenv(name)
    return SecretStr(value) if value else None
