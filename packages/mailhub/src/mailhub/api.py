"""Versioned HTTP contract for MailHub."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated, Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from mailhub.config import MailHubSettings
from mailhub.connectors.http_providers import GmailConnector, MicrosoftGraphConnector
from mailhub.connectors.imap_smtp import ImapSmtpConnector
from mailhub.connectors.sandbox import SandboxConnector
from mailhub.domain import (
    ActionType,
    AutomationLevel,
    CandidateState,
    CandidateType,
    ConnectionStatus,
    DelegationGrant,
    DeliveryStatus,
    MailAgentPolicy,
    ProviderName,
    digest_recipient_headers,
)
from mailhub.errors import DegradedError, MailHubError
from mailhub.hosts.http import (
    HttpAgentMemoryAdapter,
    HttpAiExecutionAdapter,
    HttpApprovalAdapter,
    HttpCredentialBrokerAdapter,
    HttpEventPublisherAdapter,
    HttpHostActionAdapter,
    HttpHostIdentityAdapter,
    HttpKillSwitchAdapter,
    HttpKnowledgeLifecycleAdapter,
    HttpKnowledgeSafetyAdapter,
    HttpKnowledgeSink,
    HttpOAuthCallbackAdapter,
    HttpOAuthStateStore,
    HttpObjectStoreAdapter,
    HttpProviderNotificationVerifierAdapter,
    HttpProviderSubscriptionAdapter,
    HttpQuotaAdapter,
    HttpTelemetryAdapter,
)
from mailhub.notifications import (
    ProviderLifecycleEvent,
    classify_provider_lifecycle,
)
from mailhub.oauth import (
    OAuthAuthorizationService,
    OAuthCallbackPort,
    OAuthProvider,
    validate_granted_scopes,
)
from mailhub.ports import (
    CredentialRefreshPort,
    CredentialRevocationPort,
    ProviderConnector,
    ProviderNotificationVerifierPort,
)
from mailhub.rules import (
    MailRule,
    RuleCondition,
    RuleField,
    RuleOperator,
    summarize_rule_evaluations,
)
from mailhub.service import InMemoryApprovalPort, MailService, StaticCredentialBroker
from mailhub.storage import InMemoryMailRepository, RepositoryConflictError
from mailhub.webhook import WebhookIngress, receipt_event, verified_provider_receipt


class Context(BaseModel):
    model_config = ConfigDict(frozen=True)
    tenant_id: str = Field(min_length=1, max_length=200)
    subject_id: str = Field(min_length=1, max_length=200)


async def request_context(
    x_mailhub_tenant: Annotated[str | None, Header(alias="X-MailHub-Tenant")] = None,
    x_mailhub_subject: Annotated[str | None, Header(alias="X-MailHub-Subject")] = None,
) -> Context:
    if not x_mailhub_tenant or not x_mailhub_subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "host_identity_context_required",
                "message": "Host identity headers are required",
            },
        )
    return Context(tenant_id=x_mailhub_tenant, subject_id=x_mailhub_subject)


class ConnectionCreateRequest(BaseModel):
    provider: ProviderName
    email_address: str = Field(min_length=3, max_length=320)
    credential_ref: str = Field(min_length=1, max_length=512)
    granted_scopes: tuple[str, ...] = Field(default=(), max_length=40)
    provider_account_id: str | None = Field(default=None, min_length=1, max_length=512)
    provider_tenant_id: str | None = Field(default=None, min_length=1, max_length=512)
    credential_version: int = Field(default=1, ge=1, le=2_147_483_647)


class ConnectionRevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class ConnectionRefreshRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    reason: str = Field(default="manual_refresh", min_length=1, max_length=500)


class ConnectionScopesUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    granted_scopes: tuple[str, ...] = Field(min_length=1, max_length=40)


class PolicyCreateRequest(BaseModel):
    owner_subject_id: str | None = Field(default=None, min_length=1, max_length=200)
    allowed_connection_ids: frozenset[UUID] = frozenset()
    allowed_folder_refs: frozenset[str] = frozenset()
    allowed_actions: frozenset[ActionType] = frozenset()
    allowed_domains: frozenset[str] = frozenset()
    allowed_data_classes: frozenset[str] = frozenset({"public", "internal"})
    thread_only: bool = True
    allowed_automation_level: AutomationLevel = AutomationLevel.L1_RECOMMEND
    max_per_hour: int = Field(default=0, ge=0, le=10_000)
    max_per_day: int = Field(default=0, ge=0, le=100_000)
    valid_days: int = Field(default=30, ge=1, le=365)


class GrantCreateRequest(BaseModel):
    policy_id: UUID
    agent_subject_id: str = Field(min_length=1, max_length=200)
    capability_ids: frozenset[ActionType]
    valid_days: int = Field(default=7, ge=1, le=90)


class DraftCreateRequest(BaseModel):
    connection_id: UUID
    thread_id: UUID | None = None
    recipient_addresses: tuple[str, ...] = Field(min_length=1, max_length=50)
    cc_addresses: tuple[str, ...] = Field(default=(), max_length=50)
    bcc_addresses: tuple[str, ...] = Field(default=(), max_length=50)
    attachment_refs: tuple[str, ...] = Field(default=(), max_length=20)
    subject: str = Field(min_length=1, max_length=1000)
    body_text: str = Field(min_length=1, max_length=200_000)


class DraftUpdateRequest(DraftCreateRequest):
    expected_revision: int = Field(ge=1)


class DraftSendRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_content_sha256: str = Field(min_length=64, max_length=64)
    expected_recipient_digest: str = Field(min_length=64, max_length=64)
    confirmation_ref: str | None = Field(default=None, max_length=512)
    policy_id: UUID | None = None
    grant_id: UUID | None = None
    agent_subject_id: str | None = Field(default=None, max_length=200)


class CandidateReviewRequest(BaseModel):
    approved: bool
    expected_revision: int = Field(ge=1)
    review_reason: str | None = Field(default=None, min_length=1, max_length=500)


class CandidateApplyRequest(BaseModel):
    approval_ref: str = Field(min_length=1, max_length=512)


class CandidateRevokeRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class OutcomeReconcileRequest(BaseModel):
    status: DeliveryStatus
    provider_message_ref: str | None = Field(default=None, max_length=1000)
    provider_request_id: str | None = Field(default=None, max_length=1000)
    error_code: str | None = Field(default=None, max_length=200)
    next_attempt_at: datetime | None = None


class SyncJobCreateRequest(BaseModel):
    mode: str = Field(default="incremental", pattern="^(incremental|backfill|reconcile)$")
    limit: int = Field(default=50, ge=1, le=500)
    folder_ref: str = Field(default="INBOX", min_length=1, max_length=200)
    label_refs: tuple[str, ...] = Field(default=(), max_length=20)
    received_after: datetime | None = None
    received_before: datetime | None = None
    run_inline: bool = False


class SubscriptionEnsureRequest(BaseModel):
    callback_endpoint: str = Field(min_length=1, max_length=2000)
    desired_expiry: datetime
    folder_ref: str = Field(default="INBOX", min_length=1, max_length=200)
    client_state_ref: str | None = Field(default=None, min_length=1, max_length=512)


class SubscriptionRenewRequest(BaseModel):
    folder_ref: str = Field(default="INBOX", min_length=1, max_length=200)
    renewal_window_hours: int = Field(default=24, ge=1, le=168)
    desired_expiry: datetime | None = None


class SubscriptionCancelRequest(BaseModel):
    folder_ref: str = Field(default="INBOX", min_length=1, max_length=200)


class WebhookReceiptReplayRequest(BaseModel):
    expected_body_sha256: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=500)


class AutonomyRunCreateRequest(BaseModel):
    connection_id: UUID
    limit: int = Field(default=50, ge=1, le=500)
    message_limit: int = Field(default=50, ge=1, le=200)
    replay_key: str | None = Field(default=None, min_length=1, max_length=200)
    run_inline: bool = False


class AutonomyPauseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class OAuthBeginRequest(BaseModel):
    authorization_endpoint: str = Field(min_length=10, max_length=2000)
    client_id: str = Field(min_length=1, max_length=512)
    redirect_uri: str = Field(min_length=10, max_length=2000)
    scopes: tuple[str, ...] = Field(min_length=1, max_length=20)
    extra_parameters: dict[str, str] = Field(default_factory=dict, max_length=20)
    connection_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class OAuthCallbackRequest(BaseModel):
    state: str = Field(min_length=20, max_length=4000)
    code: str = Field(min_length=1, max_length=10_000)
    redirect_uri: str = Field(min_length=10, max_length=2000)


class RuleConditionRequest(BaseModel):
    field: RuleField
    operator: RuleOperator
    value: str | tuple[str, ...] | bool


class RuleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    conditions: tuple[RuleConditionRequest, ...] = Field(min_length=1, max_length=20)
    action_type: ActionType
    action_params: dict[str, object] = Field(default_factory=dict, max_length=20)
    # L3A is explicit in the rule contract, but execution still requires the
    # independent feature flag plus policy/delegation intersection.
    automation_level: AutomationLevel = AutomationLevel.L2_REVIEW_QUEUE
    max_per_hour: int = Field(default=0, ge=0, le=10_000)
    max_per_day: int = Field(default=0, ge=0, le=100_000)
    valid_days: int = Field(default=30, ge=1, le=365)


class RuleRevisionRequest(BaseModel):
    expected_version: int = Field(ge=1)


class RuleSimulationRequest(BaseModel):
    message_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)


class RuleExecuteRequest(BaseModel):
    message_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)
    policy_id: UUID | None = None
    grant_id: UUID | None = None
    dry_run: bool = True


def create_app(
    service: MailService | None = None,
    *,
    runtime_settings: MailHubSettings | None = None,
    oauth_service: OAuthAuthorizationService | None = None,
    oauth_callback_port: OAuthCallbackPort | None = None,
    webhook_ingresses: Mapping[str, WebhookIngress] | None = None,
    provider_notification_verifier: ProviderNotificationVerifierPort | None = None,
) -> FastAPI:
    """Build an app with explicit dependency injection for tests and hosts."""

    default_graph = service is None
    settings = runtime_settings or MailHubSettings.from_env()
    if default_graph:
        production_like = settings.environment.casefold() in {"production", "prod", "staging"}
        if production_like:
            settings.require_ready()
            # A production process must inject the PostgreSQL repository,
            # encrypted object store and host ports.  Never silently boot the
            # in-memory/sandbox dependency graph just because environment
            # variables happen to be present.
            raise RuntimeError("mailhub_production_dependency_injection_required")
        repository = InMemoryMailRepository()
        sandbox = SandboxConnector()
        connectors: dict[ProviderName, ProviderConnector] = {ProviderName.SANDBOX: sandbox}
        if settings.gmail_enabled:
            connectors[ProviderName.GMAIL] = GmailConnector(
                read_only=settings.gmail_read_only,
                push_enabled=settings.gmail_push_enabled,
            )
        if settings.microsoft_graph_enabled:
            connectors[ProviderName.MICROSOFT_GRAPH] = MicrosoftGraphConnector(
                read_only=settings.microsoft_graph_read_only,
                push_enabled=settings.microsoft_graph_push_enabled,
            )
        if oauth_service is None and oauth_callback_port is None:
            oauth_service, oauth_callback_port = _build_default_oauth(settings)
        imap_host = os.environ.get("MAILHUB_IMAP_HOST")
        smtp_host = os.environ.get("MAILHUB_SMTP_HOST")
        if imap_host and smtp_host:
            connectors[ProviderName.IMAP_SMTP] = ImapSmtpConnector(
                imap_host=imap_host,
                smtp_host=smtp_host,
                send_enabled=os.environ.get("MAILHUB_SMTP_SEND_ENABLED", "false").casefold()
                in {"1", "true", "yes", "on"},
            )
        credential_broker = (
            HttpCredentialBrokerAdapter(
                base_url=settings.credential_broker_endpoint,
                headers=_host_service_headers(settings),
            )
            if settings.credential_broker_endpoint
            else StaticCredentialBroker()
        )
        if (
            provider_notification_verifier is None
            and settings.provider_notification_verifier_endpoint
        ):
            provider_notification_verifier = HttpProviderNotificationVerifierAdapter(
                base_url=settings.provider_notification_verifier_endpoint
            )
        service = MailService(
            repository,
            connectors=connectors,
            credential_broker=credential_broker,
            credential_refresh=(
                cast(CredentialRefreshPort, credential_broker)
                if hasattr(credential_broker, "refresh")
                else None
            ),
            credential_revocation=(
                cast(CredentialRevocationPort, credential_broker)
                if hasattr(credential_broker, "revoke")
                else None
            ),
            approval_port=(
                HttpApprovalAdapter(base_url=settings.approval_endpoint)
                if settings.approval_endpoint
                else InMemoryApprovalPort()
            ),
            host_identity=(
                HttpHostIdentityAdapter(base_url=settings.host_identity_endpoint)
                if settings.host_identity_endpoint
                else None
            ),
            object_store=(
                HttpObjectStoreAdapter(base_url=settings.object_store_endpoint)
                if settings.object_store_endpoint
                else None
            ),
            host_action_port=(
                HttpHostActionAdapter(base_url=settings.host_action_endpoint)
                if settings.host_action_endpoint
                else None
            ),
            knowledge_sink=(
                HttpKnowledgeSink(base_url=settings.knowledge_endpoint)
                if settings.knowledge_endpoint
                else None
            ),
            knowledge_lifecycle=(
                HttpKnowledgeLifecycleAdapter(base_url=settings.knowledge_endpoint)
                if settings.knowledge_endpoint
                else None
            ),
            agent_memory=(
                HttpAgentMemoryAdapter(base_url=settings.agent_memory_endpoint)
                if settings.agent_memory_endpoint
                else None
            ),
            knowledge_safety=(
                HttpKnowledgeSafetyAdapter(base_url=settings.knowledge_endpoint)
                if settings.knowledge_endpoint
                else None
            ),
            ai_execution=(
                HttpAiExecutionAdapter(base_url=settings.ai_execution_endpoint)
                if settings.ai_execution_endpoint
                else None
            ),
            telemetry=(
                HttpTelemetryAdapter(base_url=settings.telemetry_endpoint)
                if settings.telemetry_endpoint
                else None
            ),
            event_publisher=(
                HttpEventPublisherAdapter(base_url=settings.event_publisher_endpoint)
                if settings.event_publisher_endpoint
                else None
            ),
            quota_port=(
                HttpQuotaAdapter(base_url=settings.quota_endpoint)
                if settings.quota_endpoint
                else None
            ),
            provider_subscription_port=(
                HttpProviderSubscriptionAdapter(base_url=settings.provider_subscription_endpoint)
                if settings.provider_subscription_endpoint
                else None
            ),
            kill_switch=(
                HttpKillSwitchAdapter(base_url=settings.kill_switch_endpoint)
                if settings.kill_switch_endpoint
                else None
            ),
            worker_lease_ttl=timedelta(seconds=settings.job_lease_seconds),
            intelligence_policy=settings.analysis_policy(),
            outbound_enabled=settings.outbound_enabled,
            rule_automation_enabled=settings.rule_automation_enabled,
        )
    elif settings.environment.casefold() in {"production", "prod", "staging"}:
        settings.require_ready()
    assert service is not None
    if settings.environment.casefold() in {"production", "prod", "staging"}:
        missing_dependencies = [
            name
            for name, value in (
                ("credential_broker", service.credential_broker),
                ("object_store", service.object_store),
                ("host_identity", service.host_identity),
                ("event_publisher", service.event_publisher),
            )
            if value is None
        ]
        if service.outbound_enabled and service.approval_port is None:
            missing_dependencies.append("approval_port")
        if (
            service.outbound_enabled or service.rule_automation_enabled
        ) and service.kill_switch is None:
            missing_dependencies.append("kill_switch")
        if missing_dependencies:
            raise RuntimeError(
                "mailhub_production_dependency_missing:" + ",".join(missing_dependencies)
            )
    app = FastAPI(title="MailHub", version="0.1.0")
    app.state.mail_service = service

    async def authorized_context(
        request: Request,
        x_mailhub_tenant: Annotated[str | None, Header(alias="X-MailHub-Tenant")] = None,
        x_mailhub_subject: Annotated[str | None, Header(alias="X-MailHub-Subject")] = None,
    ) -> Context:
        context = await request_context(x_mailhub_tenant, x_mailhub_subject)
        identity_port = service.host_identity
        if identity_port is not None:
            capability = "mail.read" if request.method == "GET" else "mail.write"
            if ":send" in request.url.path:
                capability = "mail.send"
            elif "/admin/" in request.url.path:
                capability = "mail.admin"
            elif "/webhooks/receipts" in request.url.path:
                # Receipt bodies are metadata-only, but their event IDs and
                # tenant/provider status remain audit evidence. Do not expose
                # them through ordinary mailbox-read entitlement.
                capability = "mail.audit"
            elif request.url.path.endswith("/audit"):
                capability = "mail.audit"
            elif (
                ":apply" in request.url.path
                or ":execute" in request.url.path
                or "/agent-policies" in request.url.path
                or "/delegations/" in request.url.path
            ):
                capability = "mail.action.apply"
            allowed = await identity_port.authorize(
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                capability=capability,
            )
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"code": "host_identity_denied"},
                )
        return context

    # Existing route declarations use request_context; override them centrally
    # so a production host can enforce its signed identity/entitlement port.
    app.dependency_overrides[request_context] = authorized_context

    @app.exception_handler(MailHubError)
    async def mailhub_error_handler(_request: Request, exc: MailHubError) -> JSONResponse:
        detail = exc.detail()
        code = detail.code
        if code in {"authorization_denied", "policy_denied"}:
            response_status = 403
        elif code == "not_found":
            response_status = 404
        elif code == "rate_limited":
            response_status = 429
        elif code in {"provider_failure", "degraded", "outcome_unknown"}:
            response_status = 503
        elif code in {"conflict", "approval_required"}:
            response_status = 409
        elif code == "validation_error":
            response_status = 422
        else:
            response_status = 400
        return _json_response(
            status_code=response_status,
            payload={
                "error": {
                    "code": detail.code,
                    "message": detail.message,
                    "retryable": detail.retryable,
                    "outcome_unknown": detail.outcome_unknown,
                    "details": detail.details,
                }
            },
        )

    @app.exception_handler(RepositoryConflictError)
    async def repository_conflict_handler(
        _request: Request, exc: RepositoryConflictError
    ) -> JSONResponse:
        # Repository adapters expose stable conflict codes only; SQL/driver
        # details must never leak through the public HTTP contract.
        message = str(exc) or "repository_conflict"
        is_outcome_unknown = message == "operation_outcome_unknown_reconciliation_required"
        response_status = (
            503 if is_outcome_unknown else 404 if message.endswith("_not_found") else 409
        )
        return _json_response(
            status_code=response_status,
            payload={
                "error": {
                    "code": (
                        "outcome_unknown"
                        if is_outcome_unknown
                        else "not_found"
                        if response_status == 404
                        else "conflict"
                    ),
                    "message": message,
                    "retryable": response_status == 409 and not is_outcome_unknown,
                    "outcome_unknown": is_outcome_unknown,
                    "details": {},
                }
            },
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return _json_response(
            status_code=422,
            payload={
                "error": {
                    "code": "validation_error",
                    "message": str(exc) or "validation_error",
                    "retryable": False,
                    "outcome_unknown": False,
                    "details": {},
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "service": "mailhub", "outbound_enabled": service.outbound_enabled}

    @app.get("/health/ready")
    async def readiness() -> dict[str, object]:
        """Report whether the injected graph is safe for the advertised mode."""

        if default_graph:
            report = settings.validate_for_runtime()
            readiness_missing = list(report.missing)
            real_provider_enabled = settings.gmail_enabled or settings.microsoft_graph_enabled
            if real_provider_enabled:
                # The development graph is intentionally in-memory and cannot
                # be presented as a real Provider runtime.  A controlled M2/M3
                # run must inject the Host identity, broker, encrypted object
                # store and OAuth callback boundary; otherwise surface a
                # durable degraded signal instead of the generic sandbox one.
                readiness_missing.append("real_provider_requires_injected_runtime")
                if oauth_service is None or oauth_callback_port is None:
                    readiness_missing.append("oauth_host_boundary")
                for field_name in (
                    "credential_broker_endpoint",
                    "host_identity_endpoint",
                    "object_store_endpoint",
                    "kms_key_ref",
                ):
                    if not getattr(settings, field_name):
                        readiness_missing.append(field_name)
                if settings.gmail_push_enabled or settings.microsoft_graph_push_enabled:
                    if not settings.provider_subscription_endpoint:
                        readiness_missing.append("provider_subscription_endpoint")
                    if not settings.provider_notification_verifier_endpoint:
                        readiness_missing.append("provider_notification_verifier_endpoint")
            readiness_missing = list(dict.fromkeys(readiness_missing))
            return {
                "status": (
                    "sandbox"
                    if report.ready and settings.allow_sandbox and not real_provider_enabled
                    else ("ready" if not readiness_missing else "degraded")
                ),
                "environment": report.environment,
                "outbound_enabled": report.outbound_enabled,
                "missing": readiness_missing,
            }
        non_sandbox = any(provider is not ProviderName.SANDBOX for provider in service.connectors)
        missing: list[str] = []
        if non_sandbox and service.object_store is None:
            missing.append("object_store")
        if non_sandbox and service.credential_broker is None:
            missing.append("credential_broker")
        if non_sandbox and service.credential_refresh is None:
            missing.append("credential_refresh")
        if service.outbound_enabled and service.approval_port is None:
            missing.append("approval_port")
        if (
            service.outbound_enabled or service.rule_automation_enabled
        ) and service.kill_switch is None:
            missing.append("kill_switch")
        if service.outbound_enabled and service.host_identity is None:
            missing.append("host_identity_port")
        return {
            "status": "sandbox"
            if not non_sandbox and not service.outbound_enabled
            else ("ready" if not missing else "degraded"),
            "environment": "injected",
            "outbound_enabled": service.outbound_enabled,
            "missing": missing,
        }

    @app.get("/v1/mail/providers/capabilities")
    async def provider_capabilities() -> dict[str, object]:
        return {
            "data": [
                {
                    "provider": provider.value,
                    "capabilities": _serialize(connector.capabilities),
                }
                for provider, connector in service.connectors.items()
            ]
        }

    @app.get("/v1/mail/admin/provider-health")
    async def provider_health(
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        return {
            "data": list(
                await service.provider_health(
                    tenant_id=context.tenant_id, subject_id=context.subject_id
                )
            )
        }

    @app.get("/v1/mail/audit")
    async def list_audit_events(
        limit: int = Query(default=100, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        return {
            "data": [
                _serialize(item)
                for item in await service.repository.list_audit_events(
                    tenant_id=context.tenant_id,
                    subject_id=context.subject_id,
                    limit=limit,
                )
            ]
        }

    @app.post("/v1/mail/oauth/{provider}:authorize")
    async def begin_oauth(
        provider: OAuthProvider,
        body: OAuthBeginRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        if oauth_service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "oauth_authorization_unconfigured"},
            )
        if body.connection_id is not None:
            if body.expected_revision is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "oauth_expected_revision_required"},
                )
            existing = await service.repository.get_connection(
                tenant_id=context.tenant_id, connection_id=body.connection_id
            )
            if existing is None or existing.subject_id != context.subject_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"code": "connection_not_found"},
                )
            if existing.provider.value != provider.value:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "oauth_provider_binding_mismatch"},
                )
            if existing.revision != body.expected_revision:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "connection_revision_conflict"},
                )
            if existing.status in {ConnectionStatus.DELETING, ConnectionStatus.DELETED}:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "connection_not_reauthorizable"},
                )
        try:
            request = await oauth_service.begin(
                provider=provider,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                authorization_endpoint=body.authorization_endpoint,
                client_id=body.client_id,
                redirect_uri=body.redirect_uri,
                scopes=body.scopes,
                extra_parameters=body.extra_parameters,
                connection_id=body.connection_id,
                expected_revision=body.expected_revision,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": str(exc)},
            ) from exc
        # The verifier remains in the one-time server-side state record.  Only
        # the challenge and opaque state are returned to the browser.
        response_data: dict[str, object] = {
            "provider": request.provider.value,
            "authorization_url": request.authorization_url,
            "state": request.state,
            "code_challenge": request.code_challenge,
            "expires_at": request.expires_at.astimezone(UTC).isoformat(),
        }
        if request.connection_id is not None:
            response_data["connection_id"] = str(request.connection_id)
        if request.expected_revision is not None:
            response_data["expected_revision"] = request.expected_revision
        return {"data": response_data}

    @app.post("/v1/mail/oauth/{provider}:callback")
    async def complete_oauth(
        provider: OAuthProvider,
        body: OAuthCallbackRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        if oauth_service is None or oauth_callback_port is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "oauth_exchange_unconfigured"},
            )
        # Keep only a digest of the opaque state id so a Host audit reader can
        # correlate the successful callback with a deliberate replay test.
        # The signed state, authorization code and PKCE verifier never enter
        # the audit ledger.
        oauth_flow_ref: str | None = None
        with suppress(ValueError):
            oauth_flow_ref = oauth_service.state_observation_ref(body.state)
        try:
            callback = await oauth_service.consume(
                provider=provider,
                state=body.state,
                redirect_uri=body.redirect_uri,
            )
            if callback.tenant_id != context.tenant_id or callback.subject_id != context.subject_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"code": "oauth_callback_scope_denied"},
                )
            metadata = await oauth_callback_port.exchange(context=callback, code=body.code)
            email_address = metadata.get("email_address")
            credential_ref = metadata.get("credential_ref")
            if not isinstance(email_address, str) or not isinstance(credential_ref, str):
                raise ValueError("oauth_exchange_metadata_invalid")
            raw_scopes = metadata.get("granted_scopes", ())
            granted_scopes = validate_granted_scopes(
                provider,
                raw_scopes,
                callback.requested_scopes,
            )
            provider_account_id = _optional_oauth_identity(
                metadata.get("provider_account_id"), "provider_account_id"
            )
            provider_tenant_id = _optional_oauth_identity(
                metadata.get("provider_tenant_id"), "provider_tenant_id"
            )
            credential_version = _oauth_credential_version(metadata.get("credential_version"))
            if callback.connection_id is not None:
                if callback.expected_revision is None:
                    raise ValueError("oauth_state_binding_mismatch")
                connection = await service.reauthorize_connection(
                    tenant_id=callback.tenant_id,
                    subject_id=callback.subject_id,
                    connection_id=callback.connection_id,
                    expected_revision=callback.expected_revision,
                    email_address=email_address,
                    credential_ref=credential_ref,
                    granted_scopes=granted_scopes,
                    provider_account_id=provider_account_id,
                    provider_tenant_id=provider_tenant_id,
                    credential_version=credential_version,
                )
            else:
                connection = await service.create_connection(
                    tenant_id=callback.tenant_id,
                    subject_id=callback.subject_id,
                    provider=ProviderName(provider.value),
                    email_address=email_address,
                    credential_ref=credential_ref,
                    granted_scopes=granted_scopes,
                    provider_account_id=provider_account_id,
                    provider_tenant_id=provider_tenant_id,
                    credential_version=credential_version or 1,
                )
                connection = await service.activate_connection(
                    tenant_id=callback.tenant_id,
                    subject_id=callback.subject_id,
                    connection_id=connection.connection_id,
                    expected_revision=connection.revision,
                )
            await service.repository.append_audit(
                service._audit(
                    "mail.oauth.completed",
                    tenant_id=callback.tenant_id,
                    subject_id=callback.subject_id,
                    target_ref=str(connection.connection_id),
                    provider=provider.value,
                    oauth_flow_ref_sha256=oauth_flow_ref,
                    requested_scopes=list(callback.requested_scopes),
                    granted_scopes=list(granted_scopes),
                    scope_subset_verified=True,
                    state_replay_rejected=False,
                    redirect_allowlist_verified=True,
                    pkce_verified=True,
                    **service._connection_identity_fields(connection),
                )
            )
        except HTTPException:
            raise
        except ValueError as exc:
            if oauth_flow_ref is not None:
                with suppress(Exception):
                    await service.repository.append_audit(
                        service._audit(
                            "mail.oauth.callback_rejected",
                            tenant_id=context.tenant_id,
                            subject_id=context.subject_id,
                            target_ref=f"oauth:{oauth_flow_ref}",
                            provider=provider.value,
                            oauth_flow_ref_sha256=oauth_flow_ref,
                            reason=str(exc),
                        )
                    )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": str(exc)},
            ) from exc
        return {
            "data": {
                "connection": _serialize(connection),
                "provider": provider.value,
                "credential_ref": credential_ref,
            }
        }

    @app.post("/v1/mail/webhooks/{provider}", status_code=202)
    async def receive_provider_webhook(
        provider: str,
        request: Request,
        x_mailhub_tenant: Annotated[str | None, Header(alias="X-MailHub-Tenant")] = None,
        x_mailhub_event_id: Annotated[str | None, Header(alias="X-MailHub-Event-Id")] = None,
        x_mailhub_signature: Annotated[str | None, Header(alias="X-MailHub-Signature")] = None,
        x_trace_id: Annotated[str | None, Header(alias="X-Trace-Id")] = None,
    ) -> dict[str, object]:
        """Fast receipt endpoint; provider-specific processing is asynchronous."""

        ingress = (webhook_ingresses or {}).get(provider)
        if ingress is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "webhook_provider_unknown"},
            )
        if request.headers.get("content-type", "").split(";", 1)[0].casefold() not in {
            "application/json",
            "application/octet-stream",
        }:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={"code": "webhook_content_type_unsupported"},
            )
        if not x_mailhub_tenant or not x_mailhub_event_id or not x_mailhub_signature:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "webhook_headers_required"},
            )
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "webhook_content_length_invalid"},
                ) from exc
            if declared_length < 0 or declared_length > ingress.max_body_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail={"code": "webhook_body_too_large"},
                )
        body = await request.body()
        try:
            receipt = ingress.receive(
                tenant_id=x_mailhub_tenant,
                event_id=x_mailhub_event_id,
                body=body,
                signature=x_mailhub_signature,
                trace_id=x_trace_id or "webhook-unattributed",
            )
        except ValueError as exc:
            error_status = (
                status.HTTP_413_CONTENT_TOO_LARGE
                if str(exc) == "webhook_body_too_large"
                else status.HTTP_400_BAD_REQUEST
            )
            raise HTTPException(
                status_code=error_status,
                detail={"code": str(exc)},
            ) from exc
        receipt_data = receipt_event(receipt)
        receipt_store = service.repository
        persisted = True
        if hasattr(receipt_store, "save_webhook_receipt"):
            persisted = await receipt_store.save_webhook_receipt(receipt_data)
        # The in-process ingress cache is only an optimization.  The durable
        # provider/tenant/event uniqueness constraint is authoritative across
        # replicas, so surface its duplicate result in the HTTP receipt too.
        duplicate = receipt.duplicate or (receipt.verified and not persisted)
        return {
            "data": {
                "event_id": receipt.event_id,
                "status": "duplicate" if duplicate else receipt.status,
                "duplicate": duplicate,
                "verified": receipt.verified,
                "body_sha256": receipt.body_sha256,
            }
        }

    @app.post("/v1/mail/provider-notifications/{provider}", status_code=202)
    async def receive_verified_provider_notification(
        provider: str,
        request: Request,
        x_trace_id: Annotated[str | None, Header(alias="X-Trace-Id")] = None,
    ) -> dict[str, object]:
        """Accept a host-verified Gmail/Graph callback and queue sync work.

        The public callback never supplies tenant or connection identifiers.
        The injected host verifier authenticates the provider request and
        returns scoped, body-free ``ProviderNotificationDelivery`` values.
        Receipt persistence is authoritative for deduplication; all provider
        I/O remains in the durable sync worker.
        """

        try:
            provider_name = ProviderName(provider)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "provider_notification_provider_unknown"},
            ) from exc
        if provider_name not in {ProviderName.GMAIL, ProviderName.MICROSOFT_GRAPH}:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "provider_notification_provider_unknown"},
            )
        verifier = provider_notification_verifier
        if verifier is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "provider_notification_verifier_unconfigured"},
            )
        content_type = request.headers.get("content-type", "").split(";", 1)[0].casefold()
        if content_type != "application/json":
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={"code": "provider_notification_content_type_unsupported"},
            )
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > 10_000_000:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail={"code": "provider_notification_body_too_large"},
                    )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "provider_notification_content_length_invalid"},
                ) from exc
        body = await request.body()
        if len(body) > 10_000_000:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "provider_notification_body_too_large"},
            )
        trace_id = x_trace_id or f"provider-notification:{provider_name.value}"
        request_started = monotonic()
        try:
            deliveries = await verifier.verify_and_route(
                provider=provider_name,
                body=body,
                headers=dict(request.headers),
            )
        except MailHubError as exc:
            # A host verifier outage or authorization failure must not expose
            # provider/identity details through the public callback.  Keep the
            # endpoint fail closed while making the failure retryable by the
            # operator/scheduler rather than treating it as an accepted route.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "provider_notification_verifier_unavailable"},
            ) from exc
        except (TypeError, ValueError) as exc:
            # Malformed or untrusted provider input is rejected without
            # persisting a receipt or returning host routing details.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "provider_notification_verification_failed"},
            ) from exc
        receipt_store = service.repository
        if not hasattr(receipt_store, "save_webhook_receipt"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "webhook_receipt_store_unconfigured"},
            )
        results: list[dict[str, object]] = []
        accepted = 0
        duplicates = 0
        for delivery in deliveries:
            notification = delivery.notification
            if notification.provider is not provider_name:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail={"code": "provider_notification_provider_mismatch"},
                )
            receipt = verified_provider_receipt(
                provider=provider_name.value,
                tenant_id=delivery.tenant_id,
                event_id=notification.notification_id,
                body=body,
                trace_id=trace_id,
                max_body_bytes=10_000_000,
                now=notification.received_at,
            )
            receipt_payload = dict(receipt_event(receipt))
            # Replay needs only owner-scoped, body-free routing facts.  Do not
            # persist the callback body, provider cursor or client state.
            receipt_payload["route_metadata"] = {
                "subject_id": delivery.subject_id,
                "connection_id": str(delivery.connection_id),
                "folder_ref": delivery.folder_ref,
                "subscription_ref": notification.subscription_ref,
                "notification_id": notification.notification_id,
            }
            persisted = await receipt_store.save_webhook_receipt(receipt_payload)
            if not persisted:
                duplicates += 1
                with suppress(Exception):
                    await service.repository.append_audit(
                        service._audit(
                            "mail.provider.notification.duplicate",
                            tenant_id=delivery.tenant_id,
                            subject_id=delivery.subject_id,
                            target_ref=str(delivery.connection_id),
                            provider=provider_name.value,
                            notification_id_sha256=hashlib.sha256(
                                notification.notification_id.encode("utf-8")
                            ).hexdigest(),
                            duplicate=True,
                            trace_id=trace_id,
                        )
                    )
                with suppress(Exception):
                    await service.repository.append_audit(
                        service._audit(
                            "mail.provider.notification.ack",
                            tenant_id=delivery.tenant_id,
                            subject_id=delivery.subject_id,
                            target_ref=str(delivery.connection_id),
                            provider=provider_name.value,
                            notification_id_sha256=hashlib.sha256(
                                notification.notification_id.encode("utf-8")
                            ).hexdigest(),
                            ack_latency_ms=max(1, int((monotonic() - request_started) * 1000)),
                            status="duplicate",
                            duplicate=True,
                            trace_id=trace_id,
                        )
                    )
                results.append(
                    {
                        "notification_id": notification.notification_id,
                        "status": "duplicate",
                        "duplicate": True,
                    }
                )
                continue
            accepted += 1
            result: dict[str, object] = {
                "notification_id": notification.notification_id,
                "status": "accepted",
                "duplicate": False,
            }
            try:
                state = await service.record_provider_notification(
                    tenant_id=delivery.tenant_id,
                    subject_id=delivery.subject_id,
                    connection_id=delivery.connection_id,
                    subscription_ref=notification.subscription_ref,
                    received_at=notification.received_at,
                    folder_ref=delivery.folder_ref,
                    notification_id=notification.notification_id,
                    change_kind=notification.change_kind.value,
                    lifecycle_event=notification.lifecycle_event,
                    cursor_hint=notification.cursor_hint,
                    trace_id=trace_id,
                )
                result.update({"sync_state": _serialize_sync_state(state)})
                lifecycle = classify_provider_lifecycle(notification.lifecycle_event)
                if lifecycle is ProviderLifecycleEvent.REAUTHORIZATION_REQUIRED:
                    # Do not create a queued job that can only fail after the
                    # connection has been fenced.  Reauthorization must be an
                    # explicit owner action before any new Provider I/O.
                    result.update(
                        {
                            "sync_job_status": "blocked",
                            "sync_job_error_code": "connection_reauthorization_required",
                        }
                    )
                else:
                    job = await service.enqueue_sync_job(
                        tenant_id=delivery.tenant_id,
                        subject_id=delivery.subject_id,
                        connection_id=delivery.connection_id,
                        mode=(
                            "reconcile"
                            if lifecycle
                            in {
                                ProviderLifecycleEvent.SUBSCRIPTION_REMOVED,
                                ProviderLifecycleEvent.MISSED,
                            }
                            else "incremental"
                        ),
                        limit=50,
                        idempotency_key=f"provider-notification:{notification.notification_id}",
                        trace_id=trace_id,
                    )
                    result.update(
                        {
                            "sync_job_ref": str(job.job_id),
                            "sync_job_status": job.status.value,
                        }
                    )
            except Exception as exc:
                # The receipt is already durable and the callback must not be
                # retried forever.  Persist a bounded route failure for the
                # scheduler/reconciliation operator instead.
                await service.repository.append_audit(
                    {
                        "event_type": "mail.provider.notification.route_failed",
                        "tenant_id": delivery.tenant_id,
                        "subject_id": delivery.subject_id,
                        "target_ref": str(delivery.connection_id),
                        "notification_id_sha256": hashlib.sha256(
                            notification.notification_id.encode("utf-8")
                        ).hexdigest(),
                        "error_code": type(exc).__name__[:200],
                        "trace_id": trace_id,
                    }
                )
                result.update(
                    {
                        "status": "accepted_route_failed",
                        "error_code": type(exc).__name__[:200],
                    }
                )
            with suppress(Exception):
                await service.repository.append_audit(
                    service._audit(
                        "mail.provider.notification.ack",
                        tenant_id=delivery.tenant_id,
                        subject_id=delivery.subject_id,
                        target_ref=str(delivery.connection_id),
                        provider=provider_name.value,
                        notification_id_sha256=hashlib.sha256(
                            notification.notification_id.encode("utf-8")
                        ).hexdigest(),
                        ack_latency_ms=max(1, int((monotonic() - request_started) * 1000)),
                        status=str(result.get("status", "accepted")),
                        duplicate=False,
                        trace_id=trace_id,
                    )
                )
            results.append(result)
        return {
            "data": {
                "provider": provider_name.value,
                "accepted": accepted,
                "duplicates": duplicates,
                "notifications": results,
            }
        }

    @app.get("/v1/mail/webhooks/receipts")
    async def list_webhook_receipts(
        limit: int = Query(default=100, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        receipt_store = service.repository
        if not hasattr(receipt_store, "list_webhook_receipts"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "webhook_receipt_store_unconfigured"},
            )
        receipts = await receipt_store.list_webhook_receipts(
            tenant_id=context.tenant_id, limit=limit
        )
        return {"data": [_serialize(receipt) for receipt in receipts]}

    @app.post("/v1/mail/webhooks/receipts/{provider}/{event_id}:replay", status_code=202)
    async def replay_webhook_receipt(
        provider: str,
        event_id: str,
        body: WebhookReceiptReplayRequest,
        x_idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_trace_id: Annotated[str | None, Header(alias="X-Trace-Id")] = None,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        """Requeue a verified provider receipt without mutating its evidence.

        Only the body-free route saved by the Host verifier may be replayed.
        Legacy HMAC receipts and receipts without owner routing remain
        immutable/queryable but cannot be turned into provider work.
        """

        try:
            provider_name = ProviderName(provider)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "webhook_receipt_provider_unknown"},
            ) from exc
        if provider_name not in {ProviderName.GMAIL, ProviderName.MICROSOFT_GRAPH}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "webhook_receipt_replay_provider_unsupported"},
            )
        if not 1 <= len(event_id) <= 512 or any(
            ord(char) < 32 or ord(char) == 127 for char in event_id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "webhook_receipt_event_id_invalid"},
            )
        if not x_idempotency_key or not x_idempotency_key.strip() or len(x_idempotency_key) > 300:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "webhook_receipt_replay_idempotency_required"},
            )
        receipt_store = service.repository
        if not hasattr(receipt_store, "get_webhook_receipt"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "webhook_receipt_store_unconfigured"},
            )
        receipt = await receipt_store.get_webhook_receipt(
            tenant_id=context.tenant_id,
            provider=provider_name.value,
            event_id=event_id,
        )
        if receipt is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "webhook_receipt_not_found"},
            )
        if receipt.get("verified") is not True:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "webhook_receipt_not_verified"},
            )
        if receipt.get("body_sha256") != body.expected_body_sha256:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "webhook_receipt_body_digest_mismatch"},
            )
        raw_route = receipt.get("route_metadata")
        if not isinstance(raw_route, Mapping):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "webhook_receipt_route_unavailable"},
            )
        route = dict(raw_route)
        route_subject = route.get("subject_id")
        route_connection = route.get("connection_id")
        route_folder = route.get("folder_ref")
        route_subscription = route.get("subscription_ref")
        route_notification = route.get("notification_id")
        if route_subject != context.subject_id or route_notification != event_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "webhook_receipt_route_scope_denied"},
            )
        if not all(
            isinstance(value, str) and value.strip()
            for value in (route_connection, route_folder, route_subscription)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "webhook_receipt_route_invalid"},
            )
        try:
            connection_id = UUID(str(route_connection))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "webhook_receipt_route_invalid"},
            ) from exc
        replay_key = f"webhook-replay:{provider_name.value}:{event_id}:{x_idempotency_key.strip()}"
        job = await service.enqueue_sync_job(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            mode="reconcile",
            limit=50,
            idempotency_key=replay_key,
            trace_id=x_trace_id or f"webhook-replay:{event_id}",
        )
        await service.repository.append_audit(
            {
                "event_type": "mail.provider.receipt.replayed",
                "tenant_id": context.tenant_id,
                "subject_id": context.subject_id,
                "target_ref": str(job.job_id),
                "provider": provider_name.value,
                "event_id_sha256": hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
                "body_sha256": body.expected_body_sha256,
                "connection_id": str(connection_id),
                "replay_key": replay_key,
                "reason": body.reason,
                "trace_id": x_trace_id or f"webhook-replay:{event_id}",
            }
        )
        return {
            "data": {
                "provider": provider_name.value,
                "event_id": event_id,
                "job_ref": str(job.job_id),
                "status": job.status.value,
                "mode": job.mode,
                "replayed": True,
            }
        }

    @app.post("/v1/mail/connections", status_code=201)
    async def create_connection(
        body: ConnectionCreateRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        connection = await service.create_connection(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            provider=body.provider,
            email_address=body.email_address,
            credential_ref=body.credential_ref,
            granted_scopes=body.granted_scopes,
            provider_account_id=body.provider_account_id,
            provider_tenant_id=body.provider_tenant_id,
            credential_version=body.credential_version,
        )
        return {"data": _serialize(connection)}

    @app.get("/v1/mail/connections")
    async def list_connections(context: Context = Depends(request_context)) -> dict[str, object]:
        connections = await service.list_connections(
            tenant_id=context.tenant_id, subject_id=context.subject_id
        )
        return {"data": [_serialize(connection) for connection in connections]}

    @app.get("/v1/mail/connections/{connection_id}/sync-state")
    async def list_sync_states(
        connection_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        states = await service.list_sync_states(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
        )
        return {"data": [_serialize_sync_state(state) for state in states]}

    @app.get("/v1/mail/connections/{connection_id}/impact-preview")
    async def connection_impact_preview(
        connection_id: UUID,
        folder_ref: tuple[str, ...] = Query(default=()),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        return {
            "data": await service.connection_impact_preview(
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                connection_id=connection_id,
                requested_folder_refs=folder_ref,
            )
        }

    @app.post("/v1/mail/connections/{connection_id}/subscription:ensure")
    async def ensure_provider_subscription(
        connection_id: UUID,
        body: SubscriptionEnsureRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        if idempotency_key is None or not idempotency_key.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "subscription_idempotency_key_required"},
            )
        state = await service.ensure_provider_subscription(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            callback_endpoint=body.callback_endpoint,
            desired_expiry=body.desired_expiry,
            folder_ref=body.folder_ref,
            client_state_ref=body.client_state_ref,
            idempotency_key=idempotency_key,
        )
        return {"data": _serialize_sync_state(state)}

    @app.post("/v1/mail/connections/{connection_id}/subscription:renew")
    async def renew_provider_subscription(
        connection_id: UUID,
        body: SubscriptionRenewRequest | None = None,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        request = body or SubscriptionRenewRequest()
        state = await service.renew_provider_subscription_if_due(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            folder_ref=request.folder_ref,
            renewal_window=timedelta(hours=request.renewal_window_hours),
            desired_expiry=request.desired_expiry,
        )
        return {"data": _serialize_sync_state(state)}

    @app.post("/v1/mail/connections/{connection_id}/subscription:cancel")
    async def cancel_provider_subscription(
        connection_id: UUID,
        body: SubscriptionCancelRequest | None = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        if idempotency_key is None or not idempotency_key.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "subscription_idempotency_key_required"},
            )
        request = body or SubscriptionCancelRequest()
        state = await service.cancel_provider_subscription(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            folder_ref=request.folder_ref,
            request_id=idempotency_key,
        )
        return {"data": _serialize_sync_state(state)}

    @app.post("/v1/mail/connections/{connection_id}:activate")
    async def activate_connection(
        connection_id: UUID,
        body: ConnectionRevisionRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        connection = await service.activate_connection(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            expected_revision=body.expected_revision,
        )
        return {"data": _serialize(connection)}

    @app.post("/v1/mail/connections/{connection_id}:scopes")
    async def update_connection_scopes(
        connection_id: UUID,
        body: ConnectionScopesUpdateRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        connection = await service.update_connection_scopes(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            expected_revision=body.expected_revision,
            granted_scopes=body.granted_scopes,
        )
        return {"data": _serialize(connection)}

    @app.post("/v1/mail/connections/{connection_id}:refresh")
    async def refresh_connection(
        connection_id: UUID,
        body: ConnectionRefreshRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        connection = await service.refresh_connection(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            expected_revision=body.expected_revision,
            reason=body.reason,
        )
        return {"data": _serialize(connection)}

    @app.post("/v1/mail/connections/{connection_id}:revoke")
    async def revoke_connection(
        connection_id: UUID,
        body: ConnectionRevisionRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        connection = await service.revoke_connection(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            expected_revision=body.expected_revision,
        )
        return {"data": _serialize(connection)}

    @app.post("/v1/mail/connections/{connection_id}:delete")
    async def delete_connection(
        connection_id: UUID,
        body: ConnectionRevisionRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        connection = await service.delete_connection(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            expected_revision=body.expected_revision,
        )
        return {"data": _serialize(connection)}

    @app.get("/v1/mail/data-export")
    async def export_mail_data(
        include_content: bool = False,
        limit: int = Query(default=200, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        payload = await service.export_subject_data(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            include_content=include_content,
            limit=limit,
        )
        return {"data": _serialize(payload)}

    @app.post("/v1/mail/connections/{connection_id}/sync-jobs")
    async def sync_connection(
        connection_id: UUID,
        body: SyncJobCreateRequest | None = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        trace_id: Annotated[str | None, Header(alias="X-Trace-Id")] = None,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        request = body or SyncJobCreateRequest()
        if request.run_inline and settings.environment.casefold() in {
            "production",
            "prod",
            "staging",
        }:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "sync_inline_disabled_in_production"},
            )
        job = await service.enqueue_sync_job(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            mode=request.mode,
            limit=request.limit,
            folder_ref=request.folder_ref,
            label_refs=request.label_refs,
            received_after=request.received_after,
            received_before=request.received_before,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
        if request.run_inline:
            job = await service.run_sync_job(
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                job_id=job.job_id,
                worker_id=f"api:{context.subject_id}",
            )
        return {
            "data": {
                "job_ref": str(job.job_id),
                "status": job.status.value,
                "async_mode": "inline" if request.run_inline else "job",
                **cast(dict[str, object], _serialize(job)),
            }
        }

    @app.get("/v1/mail/sync-jobs")
    async def list_sync_jobs(
        connection_id: UUID | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        jobs = await service.list_sync_jobs(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            limit=limit,
        )
        return {"data": [_serialize(job) for job in jobs]}

    @app.get("/v1/mail/sync-jobs/{job_id}")
    async def get_sync_job(
        job_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        job = await service.get_sync_job(
            tenant_id=context.tenant_id, subject_id=context.subject_id, job_id=job_id
        )
        return {"data": _serialize(job)}

    @app.post("/v1/mail/sync-jobs/{job_id}:run")
    async def run_sync_job(
        job_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        job = await service.run_sync_job(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            job_id=job_id,
            worker_id=f"api:{context.subject_id}",
        )
        return {"data": _serialize(job)}

    @app.post("/v1/mail/sync-jobs/{job_id}:cancel")
    async def cancel_sync_job(
        job_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        job = await service.cancel_sync_job(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            job_id=job_id,
        )
        return {"data": _serialize(job)}

    @app.post("/v1/mail/autonomy/runs", status_code=202)
    async def enqueue_autonomy_run(
        body: AutonomyRunCreateRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        trace_id: Annotated[str | None, Header(alias="X-Trace-Id")] = None,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        if body.run_inline and settings.environment.casefold() in {
            "production",
            "prod",
            "staging",
        }:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "autonomy_inline_disabled_in_production"},
            )
        run = await service.enqueue_autonomy_run(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=body.connection_id,
            limit=body.limit,
            message_limit=body.message_limit,
            replay_key=body.replay_key or idempotency_key,
            trace_id=trace_id,
        )
        if body.run_inline and run.status.value == "queued":
            run = await service.run_autonomy_job(
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                run_id=run.run_id,
                worker_id=f"api-autonomy:{context.subject_id}",
            )
        return {
            "data": {
                "job_ref": str(run.run_id),
                "status": run.status.value,
                "async_mode": "inline" if body.run_inline else "job",
                **cast(dict[str, object], _serialize(run)),
            }
        }

    @app.get("/v1/mail/autonomy/runs")
    async def list_autonomy_runs(
        connection_id: UUID | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        runs = await service.list_autonomy_runs(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=connection_id,
            limit=limit,
        )
        return {"data": [_serialize(run) for run in runs]}

    @app.get("/v1/mail/autonomy/runs/{run_id}")
    async def get_autonomy_run(
        run_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        run = await service.get_autonomy_run(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            run_id=run_id,
        )
        return {"data": _serialize(run)}

    @app.post("/v1/mail/autonomy/runs/{run_id}:run")
    async def run_autonomy_run(
        run_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        run = await service.run_autonomy_job(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            run_id=run_id,
            worker_id=f"api-autonomy:{context.subject_id}",
        )
        return {"data": _serialize(run)}

    @app.post("/v1/mail/autonomy/runs/{run_id}:pause")
    async def pause_autonomy_run(
        run_id: UUID,
        body: AutonomyPauseRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        run = await service.pause_autonomy_run(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            run_id=run_id,
            reason=body.reason,
        )
        return {"data": _serialize(run)}

    @app.post("/v1/mail/autonomy/runs/{run_id}:resume")
    async def resume_autonomy_run(
        run_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        run = await service.resume_autonomy_run(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            run_id=run_id,
        )
        return {"data": _serialize(run)}

    @app.post("/v1/mail/autonomy/runs/{run_id}:cancel")
    async def cancel_autonomy_run(
        run_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        run = await service.cancel_autonomy_run(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            run_id=run_id,
        )
        return {"data": _serialize(run)}

    @app.get("/v1/mail/threads")
    async def list_threads(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=512),
        connection_id: UUID | None = None,
        unread: bool = False,
        important: bool = False,
        has_attachment: bool = Query(default=False, alias="attachment"),
        has_attachment_alias: bool = Query(default=False, alias="has_attachment"),
        project: bool = False,
        candidate: bool = False,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        attachment_filter = has_attachment or has_attachment_alias
        page = await service.list_thread_page(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            limit=limit,
            cursor=cursor,
            connection_id=connection_id,
            unread=unread,
            important=important,
            has_attachment=attachment_filter,
            project=project,
            candidate=candidate,
        )
        return {
            "data": [_serialize(thread) for thread in page.items],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
            "filters": {
                "connection_id": str(connection_id) if connection_id is not None else None,
                "unread": unread,
                "important": important,
                "attachment": attachment_filter,
                "project": project,
                "candidate": candidate,
            },
        }

    @app.get("/v1/mail/threads/{thread_id}")
    async def get_thread_detail(
        thread_id: UUID,
        limit: int = Query(default=200, ge=1, le=500),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        thread, messages = await service.get_thread_detail(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            thread_id=thread_id,
            limit=limit,
        )
        return {
            "data": {
                "thread": _serialize(thread),
                "messages": [_message_summary(message) for message in messages],
                "content_policy": "plain_text_endpoint_only",
            }
        }

    @app.get("/v1/mail/messages")
    async def list_messages(
        limit: int = 50,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        messages = await service.list_messages(
            tenant_id=context.tenant_id, subject_id=context.subject_id, limit=limit
        )
        return {"data": [_message_summary(message) for message in messages]}

    @app.get("/v1/mail/messages/{message_id}/content", response_class=PlainTextResponse)
    async def get_message_content(
        message_id: UUID,
        context: Context = Depends(request_context),
    ) -> PlainTextResponse:
        content = await service.get_message_content(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            message_id=message_id,
        )
        return PlainTextResponse(content, media_type="text/plain; charset=utf-8")

    @app.get("/v1/mail/search")
    async def search_messages(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
        mode: str = Query(default="metadata", pattern="^(metadata|provider)$"),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        if mode == "provider":
            # Provider search is deliberately not inferred from a connector's
            # advertised capability.  A connector must expose a separately
            # verified, scope-bound search contract before this mode is enabled.
            raise DegradedError(
                "provider_search_unavailable",
                details={
                    "requested_mode": "provider",
                    "available_mode": "metadata",
                    "reason": "provider_search_contract_not_enabled",
                },
            )
        messages = await service.search_messages(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            query=q,
            limit=limit,
        )
        return {
            "data": [_message_summary(message) for message in messages],
            "mode": "metadata",
            "complete": False,
            "coverage": [
                "subject",
                "sender",
                "recipient_headers",
                "provider_message_ref",
                "labels",
            ],
            "incomplete_reason": "projection_metadata_only",
        }

    @app.post("/v1/mail/messages/{message_id}:analyze")
    async def analyze_message(
        message_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        result = await service.analyze_message(
            tenant_id=context.tenant_id, subject_id=context.subject_id, message_id=message_id
        )
        return {"data": _serialize(result)}

    @app.get("/v1/mail/candidates")
    async def list_candidates(
        status: CandidateState | None = None,
        candidate_type: CandidateType | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        candidates = await service.list_candidates(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            status=status,
            candidate_type=candidate_type,
            limit=limit,
        )
        return {
            "data": [_serialize(candidate) for candidate in candidates],
            "filters": {
                "candidate_type": candidate_type.value if candidate_type is not None else None,
                "status": status.value if status is not None else None,
            },
        }

    async def _list_typed_candidates(
        candidate_type: CandidateType,
        *,
        status: CandidateState | None,
        limit: int,
        context: Context,
    ) -> dict[str, object]:
        candidates = await service.list_candidates(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            status=status,
            candidate_type=candidate_type,
            limit=limit,
        )
        return {
            "data": [_serialize(candidate) for candidate in candidates],
            "filters": {
                "candidate_type": candidate_type.value,
                "status": status.value if status is not None else None,
            },
        }

    @app.get("/v1/mail/candidates/projects")
    async def list_project_candidates(
        status: CandidateState | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        return await _list_typed_candidates(
            CandidateType.PROJECT, status=status, limit=limit, context=context
        )

    @app.get("/v1/mail/candidates/knowledge")
    async def list_knowledge_candidates(
        status: CandidateState | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        return await _list_typed_candidates(
            CandidateType.KNOWLEDGE, status=status, limit=limit, context=context
        )

    @app.get("/v1/mail/candidates/tasks")
    async def list_task_candidates(
        status: CandidateState | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        return await _list_typed_candidates(
            CandidateType.TASK, status=status, limit=limit, context=context
        )

    @app.get("/v1/mail/candidates/{candidate_id}")
    async def get_candidate_detail(
        candidate_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        candidate = await service.get_candidate(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            candidate_id=candidate_id,
        )
        return {"data": _serialize(candidate)}

    @app.post("/v1/mail/candidates/{candidate_id}:revoke")
    async def revoke_candidate(
        candidate_id: UUID,
        body: CandidateRevokeRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        candidate = await service.revoke_candidate(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            candidate_id=candidate_id,
            expected_revision=body.expected_revision,
            reason=body.reason,
        )
        return {"data": _serialize(candidate)}

    @app.post("/v1/mail/candidates/{candidate_id}:review")
    async def review_candidate(
        candidate_id: UUID,
        body: CandidateReviewRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        candidate = await service.review_candidate(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            candidate_id=candidate_id,
            approved=body.approved,
            expected_revision=body.expected_revision,
            review_reason=body.review_reason,
        )
        return {"data": _serialize(candidate)}

    @app.post("/v1/mail/candidates/{candidate_id}:apply")
    async def apply_candidate(
        candidate_id: UUID,
        body: CandidateApplyRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        result = await service.apply_candidate(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            candidate_id=candidate_id,
            approval_ref=body.approval_ref,
        )
        return {"data": _serialize(result)}

    @app.post("/v1/mail/agent-policies", status_code=201)
    async def create_policy(
        body: PolicyCreateRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        policy = MailAgentPolicy(
            policy_id=uuid4(),
            tenant_id=context.tenant_id,
            owner_subject_id=body.owner_subject_id or context.subject_id,
            allowed_connection_ids=body.allowed_connection_ids,
            allowed_folder_refs=body.allowed_folder_refs,
            allowed_actions=body.allowed_actions,
            allowed_domains=body.allowed_domains,
            allowed_data_classes=body.allowed_data_classes,
            thread_only=body.thread_only,
            allowed_automation_level=body.allowed_automation_level,
            max_per_hour=body.max_per_hour,
            max_per_day=body.max_per_day,
            valid_from=datetime.now(UTC),
            valid_until=datetime.now(UTC) + timedelta(days=body.valid_days),
        )
        if policy.owner_subject_id != context.subject_id:
            raise HTTPException(status_code=403, detail={"code": "policy_owner_mismatch"})
        return {"data": _serialize(await service.create_policy(policy))}

    @app.get("/v1/mail/agent-policies")
    async def list_agent_policies(
        limit: int = Query(default=50, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        policies = await service.list_policies(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            limit=limit,
        )
        return {"data": [_serialize(policy) for policy in policies]}

    @app.post("/v1/mail/delegations", status_code=201)
    async def create_grant(
        body: GrantCreateRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        grant = DelegationGrant(
            grant_id=uuid4(),
            tenant_id=context.tenant_id,
            policy_id=body.policy_id,
            agent_subject_id=body.agent_subject_id,
            granted_by_subject_id=context.subject_id,
            capability_ids=body.capability_ids,
            granted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=body.valid_days),
        )
        return {"data": _serialize(await service.create_grant(grant))}

    @app.get("/v1/mail/delegations")
    async def list_delegations(
        limit: int = Query(default=50, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        grants = await service.list_grants(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            limit=limit,
        )
        return {"data": [_serialize(grant) for grant in grants]}

    @app.post("/v1/mail/delegations/{grant_id}:revoke")
    async def revoke_delegation(
        grant_id: UUID,
        body: ConnectionRevisionRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        grant = await service.revoke_grant(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            grant_id=grant_id,
            expected_revision=body.expected_revision,
        )
        return {"data": _serialize(grant)}

    @app.post("/v1/mail/agent-policies/{policy_id}:disable")
    async def disable_agent_policy(
        policy_id: UUID,
        body: ConnectionRevisionRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        policy = await service.disable_policy(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            policy_id=policy_id,
            expected_revision=body.expected_revision,
        )
        return {"data": _serialize(policy)}

    @app.post("/v1/mail/rules", status_code=201)
    async def create_rule(
        body: RuleCreateRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        try:
            rule = MailRule(
                rule_id=uuid4(),
                tenant_id=context.tenant_id,
                owner_subject_id=context.subject_id,
                name=body.name,
                conditions=tuple(
                    RuleCondition(
                        field=item.field,
                        operator=item.operator,
                        value=item.value,
                    )
                    for item in body.conditions
                ),
                action_type=body.action_type,
                action_params=body.action_params,
                automation_level=body.automation_level,
                max_per_hour=body.max_per_hour,
                max_per_day=body.max_per_day,
                valid_from=datetime.now(UTC),
                valid_until=datetime.now(UTC) + timedelta(days=body.valid_days),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": str(exc)},
            ) from exc
        return {"data": _serialize(await service.create_rule(rule))}

    @app.get("/v1/mail/rules")
    async def list_rules(
        limit: int = Query(default=50, ge=1, le=200),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        rules = await service.list_rules(
            tenant_id=context.tenant_id, subject_id=context.subject_id, limit=limit
        )
        return {"data": [_serialize(rule) for rule in rules]}

    @app.post("/v1/mail/rules/{rule_id}:publish")
    async def publish_rule(
        rule_id: UUID,
        body: RuleRevisionRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        rule = await service.repository.get_rule(tenant_id=context.tenant_id, rule_id=rule_id)
        if rule is None or rule.owner_subject_id != context.subject_id:
            raise HTTPException(status_code=404, detail={"code": "rule_not_found"})
        if rule.version != body.expected_version:
            raise HTTPException(status_code=409, detail={"code": "rule_version_conflict"})
        published = replace(
            rule, enabled=True, version=rule.version + 1, updated_at=datetime.now(UTC)
        )
        return {"data": _serialize(await service.create_rule(published))}

    @app.post("/v1/mail/rules/{rule_id}:pause")
    async def pause_rule(
        rule_id: UUID,
        body: RuleRevisionRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        rule = await service.repository.get_rule(tenant_id=context.tenant_id, rule_id=rule_id)
        if rule is None or rule.owner_subject_id != context.subject_id:
            raise HTTPException(status_code=404, detail={"code": "rule_not_found"})
        if rule.version != body.expected_version:
            raise HTTPException(status_code=409, detail={"code": "rule_version_conflict"})
        paused = replace(
            rule, enabled=False, version=rule.version + 1, updated_at=datetime.now(UTC)
        )
        return {"data": _serialize(await service.create_rule(paused))}

    @app.post("/v1/mail/rules:simulate")
    async def simulate_rules(
        body: RuleSimulationRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        evaluations = await service.simulate_rules(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            message_ids=body.message_ids,
        )
        return {
            "data": [_serialize(item) for item in evaluations],
            "summary": _serialize(summarize_rule_evaluations(evaluations)),
            "dry_run": True,
        }

    @app.post("/v1/mail/rules/{rule_id}:execute")
    async def execute_rule(
        rule_id: UUID,
        body: RuleExecuteRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        executions = await service.execute_rule(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            rule_id=rule_id,
            message_ids=body.message_ids,
            policy_id=body.policy_id,
            grant_id=body.grant_id,
            dry_run=body.dry_run,
        )
        return {
            "data": [_serialize(item) for item in executions],
            "dry_run": body.dry_run,
        }

    @app.get("/v1/mail/rules/{rule_id}/executions")
    async def list_rule_executions(
        rule_id: UUID,
        limit: int = Query(default=100, ge=1, le=500),
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        executions = await service.list_rule_executions(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            rule_id=rule_id,
            limit=limit,
        )
        return {"data": [_serialize(item) for item in executions]}

    @app.post("/v1/mail/drafts", status_code=201)
    async def create_draft(
        body: DraftCreateRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        draft = await service.create_draft(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            connection_id=body.connection_id,
            thread_id=body.thread_id,
            recipient_addresses=body.recipient_addresses,
            cc_addresses=body.cc_addresses,
            bcc_addresses=body.bcc_addresses,
            attachment_refs=body.attachment_refs,
            subject=body.subject,
            body_text=body.body_text,
            idempotency_key=idempotency_key,
        )
        payload = cast(dict[str, object], _serialize(draft))
        payload["recipient_digest"] = digest_recipient_headers(
            draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
        )
        return {"data": payload}

    @app.put("/v1/mail/drafts/{draft_id}")
    async def update_draft(
        draft_id: UUID,
        body: DraftUpdateRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        draft = await service.update_draft(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            connection_id=body.connection_id,
            thread_id=body.thread_id,
            recipient_addresses=body.recipient_addresses,
            cc_addresses=body.cc_addresses,
            bcc_addresses=body.bcc_addresses,
            attachment_refs=body.attachment_refs,
            subject=body.subject,
            body_text=body.body_text,
        )
        payload = cast(dict[str, object], _serialize(draft))
        payload["recipient_digest"] = digest_recipient_headers(
            draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
        )
        return {"data": payload}

    @app.get("/v1/mail/drafts/{draft_id}")
    async def get_draft(
        draft_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        draft = await service.get_draft(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            draft_id=draft_id,
        )
        payload = cast(dict[str, object], _serialize(draft))
        payload["recipient_digest"] = digest_recipient_headers(
            draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
        )
        return {"data": payload}

    @app.post("/v1/mail/drafts/{draft_id}:send")
    async def queue_draft_send(
        draft_id: UUID,
        body: DraftSendRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        result = await service.queue_draft_send(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            expected_content_sha256=body.expected_content_sha256,
            expected_recipient_digest=body.expected_recipient_digest,
            confirmation_ref=body.confirmation_ref,
            policy_id=body.policy_id,
            grant_id=body.grant_id,
            agent_subject_id=body.agent_subject_id,
            idempotency_key=idempotency_key,
        )
        return {"data": result}

    @app.get("/v1/mail/actions/{operation_id}")
    async def get_operation(
        operation_id: UUID,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        operation = await service.get_operation(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            operation_id=operation_id,
        )
        return {"data": _serialize(operation)}

    @app.post("/v1/mail/actions/{operation_id}:reconcile")
    async def reconcile_operation(
        operation_id: UUID,
        body: OutcomeReconcileRequest,
        context: Context = Depends(request_context),
    ) -> dict[str, object]:
        operation = await service.reconcile_outcome_unknown(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            operation_id=operation_id,
            status=body.status,
            provider_message_ref=body.provider_message_ref,
            provider_request_id=body.provider_request_id,
            error_code=body.error_code,
            next_attempt_at=body.next_attempt_at,
        )
        return {"data": _serialize(operation)}

    return app


def _build_default_oauth(
    settings: MailHubSettings,
) -> tuple[OAuthAuthorizationService | None, OAuthCallbackPort | None]:
    """Build OAuth host adapters only when a complete non-secret registration exists.

    Missing or malformed activation configuration intentionally leaves OAuth
    unavailable (the endpoint returns a bounded 503); the offline preflight
    command is the operator-facing diagnostic. Production still injects a
    shared state store/callback port explicitly rather than using the local
    in-memory repository graph.
    """

    if not (settings.gmail_enabled or settings.microsoft_graph_enabled):
        return None, None
    if settings.oauth_state_signing_secret is None or not settings.credential_broker_endpoint:
        return None, None
    registrations = (
        (
            settings.gmail_enabled,
            OAuthProvider.GMAIL,
            settings.gmail_read_only,
            settings.gmail_client_id,
            settings.gmail_authorization_endpoint,
            settings.gmail_redirect_uris,
            settings.gmail_scopes,
            None,
        ),
        (
            settings.microsoft_graph_enabled,
            OAuthProvider.MICROSOFT_GRAPH,
            settings.microsoft_graph_read_only,
            settings.graph_client_id,
            settings.graph_authorization_endpoint,
            settings.graph_redirect_uris,
            settings.graph_scopes,
            settings.graph_authority_tenant,
        ),
    )
    allowed_redirects: dict[OAuthProvider, tuple[str, ...]] = {}
    allowed_endpoints: dict[OAuthProvider, tuple[str, ...]] = {}
    allowed_clients: dict[OAuthProvider, tuple[str, ...]] = {}
    allowed_scopes: dict[OAuthProvider, tuple[str, ...]] = {}
    for (
        enabled,
        provider,
        read_only,
        client_id,
        endpoint,
        redirects,
        scopes,
        graph_authority_tenant,
    ) in registrations:
        if not enabled:
            continue
        if (
            not read_only
            or not client_id
            or not endpoint
            or not redirects
            or not scopes
            or not _read_only_oauth_scopes_allowed(provider, scopes)
            or not _oauth_registration_urls_allowed(
                provider, endpoint, redirects, graph_authority_tenant
            )
        ):
            return None, None
        allowed_redirects[provider] = redirects
        allowed_endpoints[provider] = (endpoint,)
        allowed_clients[provider] = (client_id,)
        allowed_scopes[provider] = scopes
    try:
        authorization = OAuthAuthorizationService(
            signing_secret=settings.oauth_state_signing_secret.get_secret_value().encode("utf-8"),
            state_store=HttpOAuthStateStore(
                base_url=settings.credential_broker_endpoint,
                headers=_host_service_headers(settings),
            ),
            allowed_redirect_uris=allowed_redirects,
            allowed_authorization_endpoints=allowed_endpoints,
            allowed_client_ids=allowed_clients,
            allowed_scopes=allowed_scopes,
        )
        callback: OAuthCallbackPort = HttpOAuthCallbackAdapter(
            base_url=settings.credential_broker_endpoint,
            headers=_host_service_headers(settings),
        )
    except ValueError:
        return None, None
    return authorization, callback


def _host_service_headers(settings: MailHubSettings) -> dict[str, str]:
    """Build the non-persisted service authentication header for Host Ports."""

    if settings.host_service_token is None:
        return {}
    token = settings.host_service_token.get_secret_value()
    if len(token) < 32 or any(ord(char) < 33 or ord(char) == 127 for char in token):
        raise ValueError("mailhub_host_service_token_invalid")
    return {"Authorization": f"Bearer {token}"}


_READ_ONLY_OAUTH_REQUIRED_SCOPES: dict[OAuthProvider, frozenset[str]] = {
    OAuthProvider.GMAIL: frozenset({"https://www.googleapis.com/auth/gmail.readonly"}),
    OAuthProvider.MICROSOFT_GRAPH: frozenset({"mail.read", "offline_access"}),
}
_READ_ONLY_OAUTH_WRITE_SCOPE_MARKERS: dict[OAuthProvider, tuple[str, ...]] = {
    OAuthProvider.GMAIL: (
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.settings.",
    ),
    OAuthProvider.MICROSOFT_GRAPH: (
        "mail.send",
        "mail.readwrite",
        "mail.manage",
        "mail.fullaccessasuser",
    ),
}
_OAUTH_AUTHORIZATION_HOSTS: dict[OAuthProvider, frozenset[str]] = {
    OAuthProvider.GMAIL: frozenset({"accounts.google.com"}),
    OAuthProvider.MICROSOFT_GRAPH: frozenset({"login.microsoftonline.com"}),
}
_GRAPH_AUTHORITY_TENANT_RE = re.compile(r"(?:common|organizations|consumers|[0-9a-fA-F-]{8,64})")


def _read_only_oauth_scopes_allowed(provider: OAuthProvider, scopes: tuple[str, ...]) -> bool:
    """Keep the default host registration read-only even if preflight is skipped."""

    normalized = frozenset(scope.strip().casefold() for scope in scopes if scope.strip())
    required = _READ_ONLY_OAUTH_REQUIRED_SCOPES[provider]
    forbidden = _READ_ONLY_OAUTH_WRITE_SCOPE_MARKERS[provider]
    return required.issubset(normalized) and not any(
        scope == marker or scope.startswith(marker) for scope in normalized for marker in forbidden
    )


def _oauth_registration_urls_allowed(
    provider: OAuthProvider,
    endpoint: str | None,
    redirects: tuple[str, ...],
    graph_authority_tenant: str | None,
) -> bool:
    """Keep standalone defaults aligned with the BFF/preflight URL contract."""

    if endpoint is None or any(ord(char) < 33 or ord(char) == 127 for char in endpoint):
        return False
    try:
        parsed_endpoint = urlsplit(endpoint)
        endpoint_port = parsed_endpoint.port
    except ValueError:
        return False
    if (
        parsed_endpoint.scheme != "https"
        or (parsed_endpoint.hostname or "").casefold() not in _OAUTH_AUTHORIZATION_HOSTS[provider]
        or not parsed_endpoint.path
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or endpoint_port is not None
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        return False
    if provider is OAuthProvider.MICROSOFT_GRAPH:
        if (
            graph_authority_tenant is None
            or _GRAPH_AUTHORITY_TENANT_RE.fullmatch(graph_authority_tenant) is None
        ):
            return False
        segments = tuple(segment for segment in parsed_endpoint.path.split("/") if segment)
        if not segments or segments[0] != graph_authority_tenant:
            return False
    for redirect in redirects:
        if any(ord(char) < 33 or ord(char) == 127 for char in redirect):
            return False
        try:
            parsed_redirect = urlsplit(redirect)
            redirect_port = parsed_redirect.port
        except ValueError:
            return False
        hostname = (parsed_redirect.hostname or "").casefold()
        local_http = parsed_redirect.scheme == "http" and hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if (
            not (parsed_redirect.scheme == "https" or local_http)
            or not hostname
            or not parsed_redirect.path
            or parsed_redirect.username is not None
            or parsed_redirect.password is not None
            or redirect_port is not None
            or parsed_redirect.query
            or parsed_redirect.fragment
        ):
            return False
    return True


def _optional_oauth_identity(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"oauth_exchange_{field_name}_invalid")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError(f"oauth_exchange_{field_name}_invalid")
    return value.strip()


def _oauth_credential_version(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        raise ValueError("oauth_exchange_credential_version_invalid")
    return value


def _serialize(value: object) -> object:
    if is_dataclass(value):
        return _serialize(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_serialize(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _serialize_sync_state(value: object) -> dict[str, object]:
    """Serialize sync state without exposing host-owned client state."""

    serialized = _serialize(value)
    if not isinstance(serialized, dict):
        raise TypeError("sync_state_serialization_invalid")
    client_state_ref = serialized.pop("subscription_client_state_ref", None)
    serialized["subscription_client_state_ref_present"] = bool(client_state_ref)
    return serialized


def _message_summary(message: object) -> dict[str, object]:
    """Serialize message metadata without exposing governed object refs."""

    serialized = _serialize(message)
    if not isinstance(serialized, dict):
        raise TypeError("message_serialization_invalid")
    serialized.pop("body_text", None)
    serialized.pop("body_object_ref", None)
    serialized["content_available"] = bool(
        getattr(message, "body_text", None) or getattr(message, "body_object_ref", None)
    )
    return serialized


def _json_response(*, status_code: int, payload: dict[str, object]) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload)
