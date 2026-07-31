from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

AiMode = Literal["disabled", "fixture", "remote"]
DEFAULT_SESSION_ENCRYPTION_SECRET = "development-only-session-encryption-secret-change-me"
_READ_ONLY_REQUIRED_SCOPES = {
    "gmail": {"https://www.googleapis.com/auth/gmail.readonly"},
    "microsoft_graph": {"mail.read", "offline_access"},
}
_READ_ONLY_WRITE_SCOPE_MARKERS = {
    "gmail": (
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.settings.",
    ),
    "microsoft_graph": ("mail.send", "mail.readwrite", "mail.manage", "mail.fullaccessasuser"),
}


@dataclass(frozen=True, slots=True)
class MailHubOAuthRegistration:
    """Non-secret OAuth registration metadata owned by the host BFF.

    Client secrets, authorization codes, refresh tokens and credential refs
    never belong in this projection.  The BFF uses the metadata to construct
    the MailHub OAuth request; the browser only receives a provider URL.
    """

    provider: Literal["gmail", "microsoft_graph"]
    display_name: str
    enabled: bool
    client_id: str
    authorization_endpoint: str
    redirect_uri: str
    scopes: tuple[str, ...]
    authority_tenant: str | None = None

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return False
        if (
            not self.client_id
            or len(self.client_id) > 512
            or any(ord(char) < 33 or ord(char) == 127 for char in self.client_id)
        ):
            return False
        if not self.scopes or len(self.scopes) > 20:
            return False
        if any(
            not scope
            or len(scope) > 200
            or any(ord(char) < 33 or ord(char) == 127 for char in scope)
            for scope in self.scopes
        ):
            return False
        if self.provider == "microsoft_graph":
            tenant = self.authority_tenant or ""
            # The first controlled CAPlatform Beta binds one isolated Entra
            # tenant. Multi-tenant/common consent is a later security review.
            tenant_pattern = (
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            )
            if re.fullmatch(tenant_pattern, tenant) is None:
                return False
        else:
            tenant = None
        normalized_scopes = tuple(scope.casefold() for scope in self.scopes)
        if not _READ_ONLY_REQUIRED_SCOPES[self.provider].issubset(normalized_scopes):
            return False
        if any(
            any(
                scope == marker or scope.startswith(marker)
                for marker in _READ_ONLY_WRITE_SCOPE_MARKERS[self.provider]
            )
            for scope in normalized_scopes
        ):
            return False
        if any(
            ord(char) < 33 or ord(char) == 127
            for char in (self.authorization_endpoint + self.redirect_uri)
        ):
            return False
        try:
            endpoint = urlsplit(self.authorization_endpoint)
            endpoint_port = endpoint.port
            redirect = urlsplit(self.redirect_uri)
            _ = redirect.port  # force malformed-port rejection
        except ValueError:
            return False
        expected_host = (
            "accounts.google.com" if self.provider == "gmail" else "login.microsoftonline.com"
        )
        if (
            endpoint.scheme != "https"
            or (endpoint.hostname or "").casefold() != expected_host
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint_port is not None
            or endpoint.query
            or endpoint.fragment
            or not endpoint.path
        ):
            return False
        if self.provider == "microsoft_graph":
            path_segments = tuple(segment for segment in endpoint.path.split("/") if segment)
            if not path_segments or path_segments[0] != tenant:
                return False
        local_http = redirect.scheme == "http" and redirect.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        return not (
            not redirect.hostname
            or (redirect.scheme != "https" and not local_http)
            or redirect.username is not None
            or redirect.password is not None
            or redirect.fragment
            or redirect.query
            or not redirect.path
        )


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(item.strip().rstrip("/") for item in re.split(r"[,\s]+", value) if item.strip())


def _secret_list_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _secret_is_insecure(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        len(normalized) < 32
        or normalized == DEFAULT_SESSION_ENCRYPTION_SECRET
        or "replace-with" in normalized
        or "change-me" in normalized
    )


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    ai_mode: AiMode = "disabled"
    product_id: str = "civil_aviation_workbench"
    web_origins: tuple[str, ...] = ("http://127.0.0.1:5180", "http://localhost:5180")
    platform_core_base_url: str = "http://127.0.0.1:8010"
    platform_core_public_base_url: str = "http://127.0.0.1:8010"
    platform_connection_id: str = ""
    platform_service_token: str = ""
    product_event_token: str = ""
    agentctl_base_url: str = "http://127.0.0.1:8765"
    mcp_integration_base_url: str = ""
    mcp_integration_service_token: str = ""
    mcp_integration_timeout_seconds: float = 10.0
    aiprojectops_base_url: str = ""
    mailhub_base_url: str = ""
    mailhub_timeout_seconds: float = 15.0
    mailhub_host_service_token: str = field(default="", repr=False)
    mailhub_credential_encryption_secret: str = field(default="", repr=False)
    mailhub_gmail_enabled: bool = False
    mailhub_gmail_client_id: str = ""
    mailhub_gmail_client_secret: str = field(default="", repr=False)
    mailhub_gmail_authorization_endpoint: str = "https://accounts.google.com/o/oauth2/v2/auth"
    mailhub_gmail_redirect_uri: str = ""
    mailhub_gmail_scopes: tuple[str, ...] = ()
    mailhub_graph_enabled: bool = False
    mailhub_graph_client_id: str = ""
    mailhub_graph_client_secret: str = field(default="", repr=False)
    mailhub_graph_authorization_endpoint: str = (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    )
    mailhub_graph_redirect_uri: str = ""
    mailhub_graph_scopes: tuple[str, ...] = ()
    mailhub_graph_authority_tenant: str = "common"
    data_structuring_toolhost_base_url: str = ""
    tool_package_release_dir: str = ""
    active_tool_package_ids: tuple[str, ...] = ()
    session_cookie: str = "caplatform_session"
    session_ttl_seconds: int = 3600
    session_cookie_secure: bool = False
    session_encryption_secret: str = DEFAULT_SESSION_ENCRYPTION_SECRET
    previous_session_encryption_secrets: tuple[str, ...] = ()
    request_timeout_seconds: float = 10.0
    state_database_path: str = ".state/caplatform.sqlite3"
    project_task_proposal_ttl_seconds: int = 86_400

    @classmethod
    def from_env(cls) -> Settings:
        raw_mode = os.getenv("CAPLATFORM_AI_MODE", "disabled").strip().lower()
        if raw_mode not in {"disabled", "fixture", "remote"}:
            raise ValueError("CAPLATFORM_AI_MODE must be disabled, fixture, or remote")
        return cls(
            environment=os.getenv("CAPLATFORM_ENVIRONMENT", "development").strip(),
            ai_mode=raw_mode,  # type: ignore[arg-type]
            product_id=os.getenv("CAPLATFORM_PRODUCT_ID", "civil_aviation_workbench").strip(),
            web_origins=_csv_env(
                "CAPLATFORM_WEB_ORIGINS",
                ("http://127.0.0.1:5180", "http://localhost:5180"),
            ),
            platform_core_base_url=os.getenv(
                "PLATFORM_CORE_BASE_URL", "http://127.0.0.1:8010"
            ).rstrip("/"),
            platform_core_public_base_url=os.getenv(
                "PLATFORM_CORE_PUBLIC_BASE_URL",
                os.getenv("PLATFORM_CORE_BASE_URL", "http://127.0.0.1:8010"),
            ).rstrip("/"),
            platform_connection_id=os.getenv("PLATFORM_CONNECTION_ID", "").strip(),
            platform_service_token=os.getenv("PLATFORM_SERVICE_TOKEN", "").strip(),
            product_event_token=os.getenv("CAPLATFORM_PRODUCT_EVENT_TOKEN", "").strip(),
            agentctl_base_url=os.getenv("AGENTCTL_BASE_URL", "http://127.0.0.1:8765").rstrip("/"),
            mcp_integration_base_url=os.getenv("MCP_INTEGRATION_BASE_URL", "").rstrip("/"),
            mcp_integration_service_token=os.getenv("MCP_INTEGRATION_SERVICE_TOKEN", "").strip(),
            mcp_integration_timeout_seconds=float(
                os.getenv("MCP_INTEGRATION_TIMEOUT_SECONDS", "10")
            ),
            aiprojectops_base_url=os.getenv("AIPROJECTOPS_BASE_URL", "").rstrip("/"),
            mailhub_base_url=os.getenv("MAILHUB_BASE_URL", "").rstrip("/"),
            mailhub_timeout_seconds=float(os.getenv("MAILHUB_TIMEOUT_SECONDS", "15")),
            mailhub_host_service_token=os.getenv("MAILHUB_HOST_SERVICE_TOKEN", "").strip(),
            mailhub_credential_encryption_secret=os.getenv(
                "MAILHUB_CREDENTIAL_ENCRYPTION_SECRET", ""
            ).strip(),
            mailhub_gmail_enabled=_bool_env("MAILHUB_GMAIL_ENABLED", False),
            mailhub_gmail_client_id=os.getenv("MAILHUB_GMAIL_CLIENT_ID", "").strip(),
            mailhub_gmail_client_secret=os.getenv("MAILHUB_GMAIL_CLIENT_SECRET", "").strip(),
            mailhub_gmail_authorization_endpoint=os.getenv(
                "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT",
                "https://accounts.google.com/o/oauth2/v2/auth",
            ).strip(),
            mailhub_gmail_redirect_uri=os.getenv("MAILHUB_GMAIL_REDIRECT_URI", "").strip(),
            mailhub_gmail_scopes=_csv_env("MAILHUB_GMAIL_SCOPES", ()),
            mailhub_graph_enabled=_bool_env("MAILHUB_MICROSOFT_GRAPH_ENABLED", False),
            mailhub_graph_client_id=os.getenv("MAILHUB_GRAPH_CLIENT_ID", "").strip(),
            mailhub_graph_client_secret=os.getenv("MAILHUB_GRAPH_CLIENT_SECRET", "").strip(),
            mailhub_graph_authorization_endpoint=os.getenv(
                "MAILHUB_GRAPH_AUTHORIZATION_ENDPOINT",
                "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            ).strip(),
            mailhub_graph_redirect_uri=os.getenv("MAILHUB_GRAPH_REDIRECT_URI", "").strip(),
            mailhub_graph_scopes=_csv_env("MAILHUB_GRAPH_SCOPES", ()),
            mailhub_graph_authority_tenant=os.getenv(
                "MAILHUB_GRAPH_AUTHORITY_TENANT", "common"
            ).strip(),
            data_structuring_toolhost_base_url=os.getenv(
                "DATA_STRUCTURING_TOOLHOST_BASE_URL", ""
            ).rstrip("/"),
            tool_package_release_dir=os.getenv("CAPLATFORM_TOOL_PACKAGE_RELEASE_DIR", "").strip(),
            active_tool_package_ids=_csv_env("CAPLATFORM_ACTIVE_TOOL_PACKAGE_IDS", ()),
            session_cookie=os.getenv("CAPLATFORM_SESSION_COOKIE", "caplatform_session").strip(),
            session_ttl_seconds=int(os.getenv("CAPLATFORM_SESSION_TTL_SECONDS", "3600")),
            session_cookie_secure=_bool_env("CAPLATFORM_SESSION_COOKIE_SECURE", False),
            session_encryption_secret=os.getenv(
                "CAPLATFORM_SESSION_ENCRYPTION_SECRET",
                DEFAULT_SESSION_ENCRYPTION_SECRET,
            ).strip(),
            previous_session_encryption_secrets=_secret_list_env(
                "CAPLATFORM_SESSION_ENCRYPTION_SECRET_PREVIOUS"
            ),
            request_timeout_seconds=float(os.getenv("CAPLATFORM_REQUEST_TIMEOUT_SECONDS", "10")),
            state_database_path=os.getenv(
                "CAPLATFORM_STATE_DATABASE_PATH",
                ".state/caplatform.sqlite3",
            ).strip(),
            project_task_proposal_ttl_seconds=int(
                os.getenv("CAPLATFORM_PROJECT_TASK_PROPOSAL_TTL_SECONDS", "86400")
            ),
        )

    def readiness_issues(self) -> list[str]:
        issues: list[str] = []
        if not self.product_id:
            issues.append("product_id_missing")
        if self.session_ttl_seconds < 60:
            issues.append("session_ttl_too_short")
        if self.environment != "development" and _secret_is_insecure(
            self.session_encryption_secret
        ):
            issues.append("session_encryption_secret_insecure")
        if not self.state_database_path:
            issues.append("state_database_path_missing")
        if not 0.1 <= self.mailhub_timeout_seconds <= 120:
            issues.append("mailhub_timeout_invalid")
        for registration, issue_name in (
            (self.mailhub_oauth_registration("gmail"), "mailhub_gmail_oauth_incomplete"),
            (self.mailhub_oauth_registration("microsoft_graph"), "mailhub_graph_oauth_incomplete"),
        ):
            if registration.enabled and not registration.ready:
                issues.append(issue_name)
        real_mail_enabled = self.mailhub_gmail_enabled or self.mailhub_graph_enabled
        if real_mail_enabled:
            if _secret_is_insecure(self.mailhub_host_service_token):
                issues.append("mailhub_host_service_token_insecure")
            if _secret_is_insecure(self.mailhub_credential_encryption_secret):
                issues.append("mailhub_credential_encryption_secret_insecure")
        if self.mailhub_gmail_enabled and not self.mailhub_gmail_client_secret:
            issues.append("mailhub_gmail_client_secret_missing")
        if self.mailhub_graph_enabled and not self.mailhub_graph_client_secret:
            issues.append("mailhub_graph_client_secret_missing")
        if self.mailhub_base_url:
            endpoint = urlsplit(self.mailhub_base_url)
            invalid_origin = (
                not endpoint.hostname
                or bool(endpoint.username)
                or bool(endpoint.password)
                or bool(endpoint.query)
                or bool(endpoint.fragment)
                or endpoint.path not in {"", "/"}
            )
            is_local_http = endpoint.scheme == "http" and endpoint.hostname in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
            if invalid_origin:
                issues.append("mailhub_url_invalid")
            elif endpoint.scheme != "https" and not is_local_http:
                issues.append("mailhub_url_not_tls")
        if not 0 < self.mcp_integration_timeout_seconds <= 300:
            issues.append("mcp_integration_timeout_invalid")
        if self.mcp_integration_base_url and not self.mcp_integration_service_token:
            issues.append("mcp_integration_service_token_missing")
        if self.mcp_integration_service_token and not self.mcp_integration_base_url:
            issues.append("mcp_integration_base_url_missing")
        if self.mcp_integration_base_url:
            endpoint = urlsplit(self.mcp_integration_base_url)
            invalid_origin = (
                not endpoint.hostname
                or bool(endpoint.username)
                or bool(endpoint.password)
                or bool(endpoint.query)
                or bool(endpoint.fragment)
                or endpoint.path not in {"", "/"}
            )
            is_local_http = endpoint.scheme == "http" and endpoint.hostname in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
            if invalid_origin:
                issues.append("mcp_integration_url_invalid")
            elif endpoint.scheme != "https" and not is_local_http:
                issues.append("mcp_integration_url_not_tls")
        if self.project_task_proposal_ttl_seconds < 300:
            issues.append("project_task_proposal_ttl_too_short")
        if self.environment != "development" and self.ai_mode == "fixture":
            issues.append("fixture_mode_forbidden_outside_development")
        if self.ai_mode == "remote":
            required = {
                "platform_core_base_url": self.platform_core_base_url,
                "platform_connection_id": self.platform_connection_id,
                "platform_service_token": self.platform_service_token,
                "agentctl_base_url": self.agentctl_base_url,
            }
            issues.extend(f"{name}_missing" for name, value in required.items() if not value)
        if self.environment != "development" and not self.product_event_token:
            issues.append("product_event_token_missing")
        return issues

    def mailhub_oauth_registration(
        self, provider: Literal["gmail", "microsoft_graph"]
    ) -> MailHubOAuthRegistration:
        if provider == "gmail":
            return MailHubOAuthRegistration(
                provider="gmail",
                display_name="Gmail",
                enabled=self.mailhub_gmail_enabled,
                client_id=self.mailhub_gmail_client_id,
                authorization_endpoint=self.mailhub_gmail_authorization_endpoint,
                redirect_uri=self.mailhub_gmail_redirect_uri,
                scopes=self.mailhub_gmail_scopes,
            )
        return MailHubOAuthRegistration(
            provider="microsoft_graph",
            display_name="Microsoft Graph",
            enabled=self.mailhub_graph_enabled,
            client_id=self.mailhub_graph_client_id,
            authorization_endpoint=self.mailhub_graph_authorization_endpoint,
            redirect_uri=self.mailhub_graph_redirect_uri,
            scopes=self.mailhub_graph_scopes,
            authority_tenant=self.mailhub_graph_authority_tenant,
        )

    def mailhub_oauth_runtime_ready(
        self, provider: Literal["gmail", "microsoft_graph"]
    ) -> bool:
        """Return true only when the complete host-side OAuth boundary is usable."""

        registration = self.mailhub_oauth_registration(provider)
        client_secret = (
            self.mailhub_gmail_client_secret
            if provider == "gmail"
            else self.mailhub_graph_client_secret
        )
        return bool(
            registration.ready
            and client_secret
            and not _secret_is_insecure(self.mailhub_host_service_token)
            and not _secret_is_insecure(self.mailhub_credential_encryption_secret)
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
