import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from uuid import UUID, uuid4

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse

from .agentctl_service import (
    AgentctlAssistService,
    AgentctlCapabilityRejected,
    AgentctlIntegrationError,
    AgentctlInvocationOutcomeUnknown,
    PersonalAgentShareSnapshot,
)
from .aiprojectops import AIProjectOpsClient, AIProjectOpsError
from .aviation_safety import build_aviation_safety_turn
from .config import Settings, get_settings
from .contracts import (
    AccountChangePasswordRequest,
    AccountDeactivateRequest,
    AccountMemoryCenterResponse,
    AccountMemoryCorrectionRequest,
    AccountMemoryDeleteRequest,
    AccountMemoryPolicy,
    AccountMemoryPolicyUpdateRequest,
    AccountProfileUpdateRequest,
    AccountSessionListResponse,
    AccountSessionProjection,
    AccountSessionRevokeResponse,
    AccountTotpDisableRequest,
    AccountTotpEnableRequest,
    AccountTotpSetupResponse,
    AccountTotpStatusResponse,
    AccountWorkspaceSwitchRequest,
    AgentCopyRequest,
    AgentDraftProjection,
    AgentDraftWriteRequest,
    AgentProjection,
    AgentShareDecisionRequest,
    AgentShareRequestCreateRequest,
    AgentShareRequestProjection,
    AgentShareReviewContext,
    AgentTrialProjection,
    AgentTrialRequest,
    ApplicationInteractionEventPageV1,
    ApplicationInteractionEventV1,
    AppProjection,
    ApprovalDecisionRequest,
    AuthLoginRequest,
    AuthRegisterRequest,
    BatchDeliverableAcceptanceRequest,
    ConversationAttachmentRequest,
    ConversationCreateRequest,
    ConversationCreateResponse,
    ConversationDirectoryEntry,
    ConversationDirectoryPage,
    ConversationMessageRequest,
    ConversationUpdateRequest,
    DataStructuringInvocationProjection,
    DataStructuringInvokeRequest,
    DataStructuringReadinessProjection,
    DeliverableAcceptanceRequest,
    DeliverableVerificationRequest,
    DriveFilePreviewProjection,
    DriveFileProjection,
    DriveSignedDownloadProjection,
    DriveUploadCommitRequest,
    DriveUploadCreateRequest,
    DriveUploadSessionProjection,
    FrontdeskFeedbackRequest,
    HumanTaskActionRequest,
    IntegrationDestinationCreateRequest,
    IntegrationEndpointCreateRequest,
    IntegrationReceiptActionRequest,
    IntegrationSubscriptionCreateRequest,
    KnowledgeSearchProjection,
    KnowledgeSearchRequest,
    KnowledgeSourceCreateRequest,
    KnowledgeSourceProjection,
    KnowledgeSourceUpdateRequest,
    McpInvocationRequest,
    PlatformPrincipal,
    ProductAnalyticsEventRequest,
    ProductAnalyticsEventResponse,
    ProductAnalyticsSummaryItem,
    ProductAnalyticsSummaryResponse,
    ProjectActivityPageProjection,
    ProjectAgentPolicyProjection,
    ProjectAgentPolicyUpdateRequest,
    ProjectBlueprintDecisionRequest,
    ProjectBlueprintProjection,
    ProjectBlueprintSyncProjection,
    ProjectBlueprintUpdateRequest,
    ProjectNotificationPreferenceProjection,
    ProjectNotificationPreferenceUpdateRequest,
    ProjectOperationalReportProjection,
    ProjectOperationsDashboardProjection,
    ProjectPlanningGenerateRequest,
    ProjectPlanningJobProjection,
    ProjectPlanningSessionProjection,
    ProjectPlanningStartRequest,
    ProjectProjection,
    ProjectScheduleRequest,
    ProjectTaskCreationEvidence,
    ProjectTaskProposalCreateRequest,
    ProjectTaskProposalDecisionRequest,
    ProjectTaskProposalPreflight,
    ProjectTaskProposalProjection,
    ProjectTaskProposalTarget,
    ProjectTaskReference,
    ProjectTreeProjection,
    ProjectWorkspaceContextProjection,
    ReviewCardProjection,
    ReviewInboxProjection,
    SessionExchangeResponse,
    SkillInstallationProjection,
    SkillInstallationRequest,
    SkillProjection,
    TaskAssignmentsReplaceRequest,
    TaskBatchUpdateRequest,
    TaskContentLinkCreateRequest,
    TaskContentUpdateRequest,
    TaskDeliverableSubmissionRequest,
    TaskDependencyWriteRequest,
    TaskTimeLogCreateRequest,
    TaskWorkspaceContentProjection,
    TaskWorkspaceProjection,
    ToolPackageProjection,
    WorkItemPageProjection,
)
from .conversation_interaction import (
    build_conversation_context,
    build_user_turn_page,
    deterministic_turn_identity,
    merge_conversation_pages,
    request_has_terminal_event,
    request_has_user_turn,
    selected_agent_from_page,
)
from .fixtures import build_fixture_turn
from .idempotency import EncryptedIdempotencyStore, IdempotencyConflict
from .integration_gateway import IntegrationGateway, IntegrationGatewayError
from .mailhub_adapter import MailHubAdapterError, MailHubClient
from .mailhub_credentials import (
    CredentialRefresh,
    CredentialResolve,
    EncryptedSQLiteMailCredentialBroker,
    MailHubCredentialBrokerError,
    OAuthExchange,
    OAuthStateConsume,
    OAuthStateWrite,
    ProviderOAuthRegistration,
)
from .mailhub_host_actions import (
    EncryptedSQLiteMailHostActionBroker,
    MailHostActionBinding,
    MailHubHostActionError,
)
from .mcp_facade import McpFacadeError, McpHostIdentity, McpIntegrationFacade
from .platform_core import PlatformCoreClient, PlatformCoreError
from .preferences import PreferenceStore
from .project_task_proposals import (
    EncryptedProjectTaskProposalStore,
    ProjectTaskProposalConflict,
    ProjectTaskProposalRecord,
)
from .projections import (
    project_apps,
    project_drive_files,
    project_knowledge_search,
    project_knowledge_sources,
    project_skills,
    project_tool_packages,
)
from .sessions import EncryptedSQLiteSessionStore, SessionRecord
from .tool_package_readiness import ToolPackageReadinessService

SERVICE_VERSION = os.getenv("CAPLATFORM_RELEASE", "0.2.0").strip() or "0.2.0"
BUILD_REF = os.getenv("CAPLATFORM_VCS_REF", "development").strip() or "development"
PROJECT_BFF_CONTRACT = "caplatform.project-bff.v1"
PROJECT_API_CONTRACT = "aiprojectops.project-api.v1"
ProjectPlanningJobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
log = logging.getLogger("caplatform.http")

_AGENT_SHARE_CAPABILITY_ID = "agent.organization.share"
_AGENT_SHARE_EVIDENCE_PREFIX = "caplatform.agent_share.v1:"


@dataclass(frozen=True, slots=True)
class _AgentShareEvidence:
    agent_id: str
    agent_name: str
    agent_version: str
    owner_user_id: str
    snapshot_digest: str
    organization_agent_id: str
    trial_run_id: str
    reason: str
    skill_ids: tuple[str, ...]
    knowledge_source_ids: tuple[str, ...]


def create_app(
    *,
    settings: Settings | None = None,
    platform_core: PlatformCoreClient | Any | None = None,
    agentctl_assist: AgentctlAssistService | Any | None = None,
    aiprojectops: AIProjectOpsClient | Any | None = None,
    preference_store: PreferenceStore | Any | None = None,
    session_store: EncryptedSQLiteSessionStore | Any | None = None,
    idempotency_store: EncryptedIdempotencyStore | Any | None = None,
    project_task_proposal_store: EncryptedProjectTaskProposalStore | Any | None = None,
    integration_gateway: IntegrationGateway | Any | None = None,
    mcp_integration: McpIntegrationFacade | Any | None = None,
    mailhub: MailHubClient | Any | None = None,
    mailhub_credential_broker: EncryptedSQLiteMailCredentialBroker | Any | None = None,
    mailhub_host_action_broker: EncryptedSQLiteMailHostActionBroker | Any | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    core = platform_core or PlatformCoreClient(
        base_url=resolved.platform_core_base_url,
        service_token=resolved.platform_service_token,
        connection_id=resolved.platform_connection_id,
        timeout_seconds=resolved.request_timeout_seconds,
    )
    assist = agentctl_assist or AgentctlAssistService(
        platform_core=core,
        product_id=resolved.product_id,
        configured_base_url=resolved.agentctl_base_url,
    )
    projects = aiprojectops or AIProjectOpsClient(
        base_url=resolved.aiprojectops_base_url,
        timeout_seconds=resolved.request_timeout_seconds,
    )
    sessions = session_store or EncryptedSQLiteSessionStore(
        database_path=resolved.state_database_path,
        encryption_secret=resolved.session_encryption_secret,
        ttl_seconds=resolved.session_ttl_seconds,
        previous_encryption_secrets=resolved.previous_session_encryption_secrets,
    )
    preferences = preference_store or PreferenceStore(resolved.state_database_path)
    idempotency = idempotency_store or EncryptedIdempotencyStore(
        database_path=resolved.state_database_path,
        encryption_secret=resolved.session_encryption_secret,
        previous_encryption_secrets=resolved.previous_session_encryption_secrets,
    )
    project_task_proposals = project_task_proposal_store or EncryptedProjectTaskProposalStore(
        database_path=resolved.state_database_path,
        encryption_secret=resolved.session_encryption_secret,
        ttl_seconds=resolved.project_task_proposal_ttl_seconds,
        previous_encryption_secrets=resolved.previous_session_encryption_secrets,
    )
    integrations = integration_gateway or IntegrationGateway(
        database_path=resolved.state_database_path,
        encryption_secret=resolved.session_encryption_secret,
    )
    mcp = mcp_integration or McpIntegrationFacade(
        base_url=resolved.mcp_integration_base_url,
        service_token=resolved.mcp_integration_service_token,
        timeout_seconds=resolved.mcp_integration_timeout_seconds,
    )
    mail = mailhub
    if mail is None and resolved.mailhub_base_url:
        mail = MailHubClient(
            base_url=resolved.mailhub_base_url,
            timeout_seconds=resolved.mailhub_timeout_seconds,
        )
    mail_credentials = mailhub_credential_broker or EncryptedSQLiteMailCredentialBroker(
        database_path=resolved.state_database_path,
        encryption_secret=(
            resolved.mailhub_credential_encryption_secret
            or resolved.session_encryption_secret
        ),
        registrations={
            "gmail": ProviderOAuthRegistration(
                provider="gmail",
                enabled=resolved.mailhub_oauth_runtime_ready("gmail"),
                client_id=resolved.mailhub_gmail_client_id,
                client_secret=resolved.mailhub_gmail_client_secret,
                redirect_uri=resolved.mailhub_gmail_redirect_uri,
                scopes=resolved.mailhub_gmail_scopes,
            ),
            "microsoft_graph": ProviderOAuthRegistration(
                provider="microsoft_graph",
                enabled=resolved.mailhub_oauth_runtime_ready("microsoft_graph"),
                client_id=resolved.mailhub_graph_client_id,
                client_secret=resolved.mailhub_graph_client_secret,
                redirect_uri=resolved.mailhub_graph_redirect_uri,
                scopes=resolved.mailhub_graph_scopes,
                authority_tenant=resolved.mailhub_graph_authority_tenant,
            ),
        },
    )
    mail_host_actions = mailhub_host_action_broker or EncryptedSQLiteMailHostActionBroker(
        database_path=resolved.state_database_path,
        encryption_secret=(
            resolved.mailhub_credential_encryption_secret or resolved.session_encryption_secret
        ),
    )
    tool_package_readiness = ToolPackageReadinessService(
        platform_core=core,
        agentctl=assist,
        release_dir=resolved.tool_package_release_dir,
        active_package_ids=resolved.active_tool_package_ids,
        toolhost_base_url=resolved.data_structuring_toolhost_base_url,
        timeout_seconds=resolved.request_timeout_seconds,
    )
    fixture_pages: dict[tuple[str, str], ApplicationInteractionEventPageV1] = {}
    projection_stop = asyncio.Event()
    integration_stop = asyncio.Event()

    async def handle_normalized_external_event(payload: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(payload["tenant_id"])
        trace_id = str(payload["trace_id"])
        if not payload["requires_capability"]:
            return await projects.ingest_external_event(
                platform_service_token=resolved.platform_service_token,
                tenant_id=tenant_id,
                body={
                    "source_receipt_id": payload["source_receipt_id"],
                    "normalized_event": payload["normalized_event"],
                    "handler_ref": payload.get("handler_ref"),
                    "requires_capability": False,
                    "governed_by_agentctl": False,
                    "trace_id": trace_id,
                },
            )

        principal = PlatformPrincipal(
            user_id="caplatform-integration-gateway",
            tenant_id=tenant_id,
            role="service",
            permissions=["ca.project.integrate"],
            apps=[resolved.product_id],
        )
        capability_id = "ca.project.external_event.propose"
        work_item_id = f"external-event:{payload['source_receipt_id']}"
        preflight = await assist.preflight_capability(
            principal=principal,
            capability_id=capability_id,
            work_item_id=work_item_id,
        )
        if preflight.get("allowed") is not True:
            raise AgentctlCapabilityRejected("external_event_capability_preflight_blocked")
        invoked = await assist.invoke_capability(
            principal=principal,
            invocation={
                "schema_version": "aios.capability_invocation.v0.1",
                "invocation_id": f"external-event-{payload['source_receipt_id']}",
                "request_id": work_item_id,
                "work_item_id": work_item_id,
                "capability_id": capability_id,
                "capability_version": "1.0.0",
                "validated_arguments": {
                    "source_receipt_id": payload["source_receipt_id"],
                    "normalized_event": payload["normalized_event"],
                    "handler_ref": payload.get("handler_ref"),
                },
                "idempotency_key": f"external-event:{tenant_id}:{payload['source_receipt_id']}",
                "context_ref": {
                    "kind": "caplatform_integration_receipt",
                    "receipt_id": payload["source_receipt_id"],
                },
                "expected_artifact_types": ["external_event_review_proposal"],
                "trace_id": trace_id,
                "metadata": {
                    "tenant_id": tenant_id,
                    "actor_user_id": principal.user_id,
                    "source_product": resolved.product_id,
                    "confirmation_mode": "proposal_only",
                },
            },
        )
        output = invoked.get("output")
        if not isinstance(output, dict):
            raise AgentctlIntegrationError("external_event_capability_output_missing")
        intake_ref = output.get("intake_ref")
        if not isinstance(intake_ref, dict):
            raise AgentctlIntegrationError("external_event_capability_intake_ref_missing")
        return {
            "status": "PROPOSED",
            "capability_id": capability_id,
            "intake_ref": intake_ref,
            "evidence_ref": output.get("evidence_ref"),
        }

    if isinstance(integrations, IntegrationGateway):
        integrations.set_product_handler(handle_normalized_external_event)

    async def publish_work_item_projection(
        proposal: ProjectTaskProposalRecord,
    ) -> None:
        try:
            page = _project_task_work_item_page(
                proposal,
                product_id=resolved.product_id,
            )
            await core.publish_conversation_page(
                subject_user_id=proposal.user_id,
                page=page,
            )
        except (PlatformCoreError, AttributeError, ValueError) as exc:
            await project_task_proposals.mark_work_item_projection_failed(
                proposal_id=proposal.proposal_id,
                tenant_id=proposal.tenant_id,
                user_id=proposal.user_id,
                error_code=f"work_item_projection:{type(exc).__name__}",
            )
            return
        await project_task_proposals.mark_work_item_projection_published(
            proposal_id=proposal.proposal_id,
            tenant_id=proposal.tenant_id,
            user_id=proposal.user_id,
        )

    async def work_item_projection_loop() -> None:
        while not projection_stop.is_set():
            pending = await project_task_proposals.pending_work_item_projections()
            for proposal in pending:
                if projection_stop.is_set():
                    return
                await publish_work_item_projection(proposal)
            with suppress(TimeoutError):
                await asyncio.wait_for(projection_stop.wait(), timeout=5)

    async def integration_worker_loop() -> None:
        while not integration_stop.is_set():
            for tenant_id in await integrations.pending_tenants():
                if integration_stop.is_set():
                    return
                await integrations.process_receipts(tenant_id)
                await integrations.process_deliveries(tenant_id)
            with suppress(TimeoutError):
                await asyncio.wait_for(integration_stop.wait(), timeout=2)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await preferences.initialize()
        await sessions.initialize()
        await idempotency.initialize()
        await project_task_proposals.initialize()
        await integrations.initialize()
        await mail_credentials.initialize()
        await mail_host_actions.initialize()
        projection_task = asyncio.create_task(work_item_projection_loop())
        integration_task = asyncio.create_task(integration_worker_loop())
        try:
            yield
        finally:
            projection_stop.set()
            integration_stop.set()
            await projection_task
            await integration_task

    app = FastAPI(
        title="CAPlatform BFF",
        version="0.1.0",
        docs_url="/docs" if resolved.environment == "development" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.web_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "Last-Event-ID",
        ],
    )
    request_metrics = {"total": 0, "errors": 0, "latency_seconds": 0.0}

    @app.middleware("http")
    async def trace_and_metrics(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = request.headers.get("X-Trace-Id") or f"tr-{uuid4().hex}"
        request.state.trace_id = trace_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started
        request_metrics["total"] += 1
        request_metrics["latency_seconds"] += elapsed
        if response.status_code >= 500:
            request_metrics["errors"] += 1
        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-Service-Version"] = SERVICE_VERSION
        log.info(
            "http_request",
            extra={
                "trace_id": trace_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(elapsed * 1000, 2),
                "service": "caplatform-bff",
                "version": SERVICE_VERSION,
            },
        )
        return response

    async def resolve_platform_session(
        platform_token: str,
    ) -> tuple[PlatformPrincipal, dict[str, Any]]:
        try:
            principal = await core.introspect_user(platform_token)
            profile = await core.get_user_profile(platform_token)
        except PlatformCoreError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        profile_user = profile.get("user")
        profile_tenant = profile.get("current_tenant")
        if isinstance(profile_user, dict) and profile_user.get("id") not in {
            None,
            principal.user_id,
        }:
            raise HTTPException(status_code=502, detail="platform_profile_user_mismatch")
        if isinstance(profile_tenant, dict) and profile_tenant.get("id") not in {
            None,
            principal.tenant_id,
        }:
            raise HTTPException(status_code=502, detail="platform_profile_tenant_mismatch")
        return principal, profile

    def set_browser_session_cookie(response: Response, record: SessionRecord) -> None:
        response.set_cookie(
            key=resolved.session_cookie,
            value=record.session_id,
            max_age=resolved.session_ttl_seconds,
            httponly=True,
            secure=resolved.session_cookie_secure,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"

    async def establish_browser_session(
        response: Response,
        platform_token: str,
        *,
        device_label: str,
    ) -> SessionExchangeResponse:
        principal, profile = await resolve_platform_session(platform_token)
        record = await sessions.create(
            platform_user_token=platform_token,
            principal=principal,
            profile=profile,
            device_label=device_label,
        )
        set_browser_session_cookie(response, record)
        return SessionExchangeResponse(
            principal=principal,
            profile=profile,
            expires_in=resolved.session_ttl_seconds,
        )

    async def rotate_browser_session(
        response: Response,
        current: SessionRecord,
        platform_token: str,
        *,
        expected_tenant_id: str | None = None,
    ) -> SessionExchangeResponse:
        principal, profile = await resolve_platform_session(platform_token)
        if principal.user_id != current.principal.user_id:
            raise HTTPException(status_code=502, detail="platform_session_user_mismatch")
        if expected_tenant_id is not None and principal.tenant_id != expected_tenant_id:
            raise HTTPException(status_code=502, detail="platform_session_tenant_mismatch")
        replacement = await sessions.create(
            platform_user_token=platform_token,
            principal=principal,
            profile=profile,
            device_label=current.device_label,
        )
        await sessions.revoke(current.session_id)
        set_browser_session_cookie(response, replacement)
        return SessionExchangeResponse(
            principal=principal,
            profile=profile,
            expires_in=resolved.session_ttl_seconds,
        )

    async def require_session(
        session_cookie_id: str | None = Cookie(
            default=None,
            alias=resolved.session_cookie,
        ),
    ) -> SessionRecord:
        if not session_cookie_id:
            raise HTTPException(status_code=401, detail="session_required")
        record = await sessions.get(session_cookie_id)
        if record is None:
            raise HTTPException(status_code=401, detail="session_expired")
        return record

    def require_mailhub_host_service(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = resolved.mailhub_host_service_token
        if not expected:
            raise HTTPException(status_code=503, detail="mailhub_host_service_disabled")
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(status_code=401, detail="mailhub_host_service_unauthorized")
        presented = authorization[len(prefix) :]
        if not hmac.compare_digest(presented, expected):
            raise HTTPException(status_code=401, detail="mailhub_host_service_unauthorized")

    def mailhub_credential_http_error(
        exc: MailHubCredentialBrokerError | ValueError,
    ) -> HTTPException:
        if isinstance(exc, MailHubCredentialBrokerError):
            return HTTPException(status_code=exc.status_code, detail=exc.code)
        return HTTPException(status_code=422, detail=str(exc))

    @app.post("/v1/mail-host/oauth/state", status_code=204)
    async def save_mailhub_oauth_state(
        body: OAuthStateWrite,
        _: Annotated[None, Depends(require_mailhub_host_service)],
    ) -> Response:
        try:
            await mail_credentials.save_state(body)
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise mailhub_credential_http_error(exc) from exc
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.post("/v1/mail-host/oauth/state/consume")
    async def consume_mailhub_oauth_state(
        body: OAuthStateConsume,
        _: Annotated[None, Depends(require_mailhub_host_service)],
        response: Response,
    ) -> dict[str, object]:
        try:
            record = await mail_credentials.consume_state(body.state_id)
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise mailhub_credential_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"record": record}

    @app.post("/v1/mail-host/oauth/exchange")
    async def exchange_mailhub_oauth_code(
        body: OAuthExchange,
        _: Annotated[None, Depends(require_mailhub_host_service)],
        response: Response,
    ) -> dict[str, object]:
        try:
            metadata = await mail_credentials.exchange(body)
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise mailhub_credential_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"metadata": metadata}

    @app.post("/v1/mail-host/credentials/resolve")
    async def resolve_mailhub_credential(
        body: CredentialResolve,
        _: Annotated[None, Depends(require_mailhub_host_service)],
        response: Response,
    ) -> dict[str, object]:
        try:
            credentials = await mail_credentials.resolve(body)
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise mailhub_credential_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"credentials": credentials}

    @app.post("/v1/mail-host/credentials/refresh")
    async def refresh_mailhub_credential(
        body: CredentialRefresh,
        _: Annotated[None, Depends(require_mailhub_host_service)],
        response: Response,
    ) -> dict[str, object]:
        try:
            metadata = await mail_credentials.refresh(body)
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise mailhub_credential_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"metadata": metadata}

    @app.post("/v1/mail-host/credentials/revoke")
    async def revoke_mailhub_credential(
        body: CredentialResolve,
        _: Annotated[None, Depends(require_mailhub_host_service)],
        response: Response,
    ) -> dict[str, object]:
        try:
            metadata = await mail_credentials.revoke(body)
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise mailhub_credential_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"data": metadata}

    async def execute_mailhub_task_candidate(
        binding: MailHostActionBinding,
    ) -> dict[str, object]:
        if binding.action_type != "create_task":
            raise HTTPException(
                status_code=422,
                detail="mailhub_host_action_requires_revision_safe_task_create",
            )
        candidate_type = binding.parameters.get("candidate_type")
        payload = binding.parameters.get("candidate_payload")
        if candidate_type != "task" or not isinstance(payload, Mapping):
            raise HTTPException(status_code=422, detail="mailhub_task_candidate_invalid")
        raw_projects = payload.get("project_refs")
        project_refs = (
            tuple(item.strip() for item in raw_projects if isinstance(item, str) and item.strip())
            if isinstance(raw_projects, (list, tuple))
            else ()
        )
        if not project_refs or len(project_refs[0]) > 160:
            raise HTTPException(status_code=422, detail="mailhub_task_project_ref_required")
        raw_title = payload.get("title") or payload.get("summary")
        title = str(raw_title or "邮件行动项").strip()[:256]
        if not title:
            raise HTTPException(status_code=422, detail="mailhub_task_title_required")
        description = str(payload.get("summary") or title).strip()[:4000]
        active_sessions = await sessions.list_for_user(binding.subject_id)
        actor = next(
            (
                item
                for item in active_sessions
                if item.principal.tenant_id == binding.tenant_id
                and item.principal.user_id == binding.subject_id
            ),
            None,
        )
        if actor is None:
            raise HTTPException(status_code=401, detail="mailhub_host_user_session_required")
        work_item_id = f"mailhub-{binding.candidate_id}"
        preflight = await assist.preflight_capability(
            principal=actor.principal,
            capability_id="ca.project.create_task",
            work_item_id=work_item_id,
        )
        if (
            preflight.get("allowed") is not True
            or preflight.get("required_confirmation") != "explicit"
            or preflight.get("side_effect_class") != "external"
        ):
            raise HTTPException(status_code=403, detail="mailhub_task_capability_preflight_blocked")
        target = {
            "project_id": project_refs[0],
            "title": title,
            "description": description,
            "stage_id": None,
        }
        trace_id = f"mailhub-{binding.action_id}"
        invoked = await assist.invoke_capability(
            principal=actor.principal,
            invocation={
                "schema_version": "aios.capability_invocation.v0.1",
                "invocation_id": f"invoke-{binding.action_id}",
                "request_id": work_item_id,
                "work_item_id": work_item_id,
                "capability_id": "ca.project.create_task",
                "capability_version": "1.0.0",
                "validated_arguments": target,
                "idempotency_key": f"mailhub-action:{binding.action_id}",
                "policy_decision_ref": {
                    "schema_version": "caplatform.explicit_user_confirmation.v1",
                    "proposal_id": work_item_id,
                    "proposal_version": binding.candidate_revision,
                    "decision": "allow",
                    "confirmed_by_user_id": binding.subject_id,
                    "confirmed_at": datetime.now(UTC).isoformat(),
                },
                "context_ref": {
                    "kind": "mailhub_candidate",
                    "candidate_id": str(binding.candidate_id),
                    "source_message_id": str(binding.parameters.get("source_message_id") or ""),
                },
                "expected_artifact_types": ["project_object"],
                "trace_id": trace_id,
                "metadata": {
                    "tenant_id": binding.tenant_id,
                    "actor_user_id": binding.subject_id,
                    "source_product": resolved.product_id,
                    "source_module": "mailhub",
                    "confirmation_mode": "explicit",
                },
            },
        )
        output = invoked.get("output")
        if not isinstance(output, dict):
            raise AgentctlInvocationOutcomeUnknown(
                "mailhub_task_capability_output_missing_outcome_unknown"
            )
        task_ref = ProjectTaskReference.model_validate(output.get("task_ref"))
        evidence = ProjectTaskCreationEvidence.model_validate(output.get("evidence"))
        return {
            "status": "applied",
            "action_id": str(binding.action_id),
            "result_ref": task_ref.task_id,
            "execution_id": evidence.trace_id,
            "idempotency_replayed": evidence.idempotency_replayed,
        }

    def mailhub_host_action_http_error(
        exc: MailHubHostActionError | ValueError,
    ) -> HTTPException:
        if isinstance(exc, MailHubHostActionError):
            return HTTPException(status_code=exc.status_code, detail=exc.code)
        return HTTPException(status_code=422, detail=str(exc))

    @app.post("/v1/mail-host/approvals/verify")
    async def verify_mailhub_host_approval(
        body: dict[str, object],
        _: Annotated[None, Depends(require_mailhub_host_service)],
        response: Response,
    ) -> dict[str, object]:
        confirmation_ref = body.get("confirmation_ref")
        action = body.get("action")
        if not isinstance(confirmation_ref, str) or not isinstance(action, dict):
            raise HTTPException(status_code=422, detail="mailhub_host_approval_request_invalid")
        try:
            verified = await mail_host_actions.verify_confirmation(
                confirmation_ref=confirmation_ref,
                action={str(key): value for key, value in action.items()},
            )
        except (MailHubHostActionError, ValueError) as exc:
            raise mailhub_host_action_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return {"verified": verified}

    @app.post("/v1/mail-host/knowledge/safety/evaluate")
    async def evaluate_mailhub_knowledge_safety(
        body: dict[str, object],
        _: Annotated[None, Depends(require_mailhub_host_service)],
        response: Response,
    ) -> dict[str, object]:
        if (
            resolved.environment.casefold() in {"production", "prod", "staging"}
            or resolved.mailhub_gmail_enabled
            or resolved.mailhub_graph_enabled
        ):
            raise HTTPException(
                status_code=503,
                detail="mailhub_knowledge_external_scanner_required",
            )
        tenant_id = body.get("tenant_id")
        subject_id = body.get("subject_id")
        candidate_id = body.get("candidate_id")
        content_sha256 = body.get("content_sha256")
        candidate = body.get("candidate")
        evidence = body.get("evidence")
        if (
            not isinstance(tenant_id, str)
            or not isinstance(subject_id, str)
            or not isinstance(candidate_id, str)
            or not isinstance(content_sha256, str)
            or len(content_sha256) != 64
            or not isinstance(candidate, dict)
            or candidate.get("requires_review") is not True
            or candidate.get("sensitivity") != "internal"
            or candidate.get("security_state") not in {"pending_scan", "cleared"}
            or candidate.get("rights_state") not in {"awaiting_review", "approved"}
            or not isinstance(evidence, list)
            or _mailhub_host_payload_contains_forbidden(body)
        ):
            raise HTTPException(status_code=422, detail="mailhub_knowledge_safety_input_invalid")
        try:
            parsed_candidate_id = UUID(candidate_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="mailhub_knowledge_candidate_id_invalid"
            ) from exc
        try:
            gate_ref = mail_host_actions.issue_knowledge_safety_gate(
                tenant_id=tenant_id,
                subject_id=subject_id,
                candidate_id=parsed_candidate_id,
                content_sha256=content_sha256,
            )
        except (MailHubHostActionError, ValueError) as exc:
            raise mailhub_host_action_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return {
            "security_state": "cleared",
            "rights_state": "approved",
            "gate_ref": gate_ref,
        }

    @app.post("/v1/mail-host/knowledge/candidates")
    async def submit_mailhub_knowledge_candidate(
        body: dict[str, object],
        _: Annotated[None, Depends(require_mailhub_host_service)],
        response: Response,
    ) -> dict[str, object]:
        tenant_id = body.get("tenant_id")
        subject_id = body.get("subject_id")
        candidate_id = body.get("candidate_id")
        content_sha256 = body.get("content_sha256")
        candidate = body.get("candidate")
        if (
            not isinstance(tenant_id, str)
            or not isinstance(subject_id, str)
            or not isinstance(candidate_id, str)
            or not isinstance(content_sha256, str)
            or not isinstance(candidate, dict)
            or candidate.get("security_state") != "cleared"
            or candidate.get("rights_state") != "approved"
            or not isinstance(candidate.get("gate_ref"), str)
            or _mailhub_host_payload_contains_forbidden(body)
        ):
            raise HTTPException(status_code=422, detail="mailhub_knowledge_candidate_invalid")
        try:
            parsed_candidate_id = UUID(candidate_id)
            mail_host_actions.verify_knowledge_safety_gate(
                gate_ref=str(candidate["gate_ref"]),
                tenant_id=tenant_id,
                subject_id=subject_id,
                candidate_id=parsed_candidate_id,
                content_sha256=content_sha256,
            )
            result = await mail_host_actions.submit_knowledge_candidate(
                tenant_id=tenant_id,
                subject_id=subject_id,
                candidate_id=parsed_candidate_id,
                content_sha256=content_sha256,
                payload=body,
            )
        except (MailHubHostActionError, ValueError) as exc:
            raise mailhub_host_action_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.post("/v1/mail-host/actions/execute")
    async def execute_mailhub_host_action(
        body: dict[str, object],
        _: Annotated[None, Depends(require_mailhub_host_service)],
        response: Response,
    ) -> dict[str, object]:
        confirmation_ref = body.get("approval_ref")
        action = body.get("action")
        if not isinstance(confirmation_ref, str) or not isinstance(action, dict):
            raise HTTPException(status_code=422, detail="mailhub_host_action_request_invalid")
        normalized_action = {str(key): value for key, value in action.items()}
        try:
            claim = await mail_host_actions.claim_execution(
                confirmation_ref=confirmation_ref,
                action=normalized_action,
            )
        except (MailHubHostActionError, ValueError) as exc:
            raise mailhub_host_action_http_error(exc) from exc
        if claim.replayed and claim.result is not None:
            response.headers["Cache-Control"] = "no-store"
            return claim.result
        binding = claim.binding
        try:
            result = await execute_mailhub_task_candidate(binding)
        except HTTPException as exc:
            await mail_host_actions.mark_retryable_failure(
                binding=binding,
                error_code=str(exc.detail),
            )
            raise
        except (
            AgentctlIntegrationError,
            AgentctlInvocationOutcomeUnknown,
            PlatformCoreError,
            ValueError,
        ) as exc:
            await mail_host_actions.mark_retryable_failure(
                binding=binding,
                error_code=str(exc),
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await mail_host_actions.complete_execution(binding=binding, result=result)
        response.headers["Cache-Control"] = "no-store"
        return result

    def integration_role(record: SessionRecord) -> str:
        role = (record.principal.role or "").lower()
        if role in {"owner", "admin", "administrator", "space_owner"}:
            return "admin"
        if role in {"developer", "engineer"}:
            return "developer"
        return "viewer"

    def require_integration_role(record: SessionRecord, required: str) -> None:
        rank = {"viewer": 0, "developer": 1, "admin": 2}
        if rank[integration_role(record)] < rank[required]:
            raise HTTPException(status_code=403, detail="integration_role_required")

    def require_mailhub() -> MailHubClient | Any:
        if mail is None:
            raise HTTPException(status_code=503, detail="mailhub_endpoint_not_configured")
        return mail

    def mailhub_http_error(exc: MailHubAdapterError | ValueError) -> HTTPException:
        code = str(exc)
        if isinstance(exc, ValueError):
            return HTTPException(status_code=422, detail=code)
        status_code = 502
        for candidate in (400, 401, 403, 404, 409, 413, 422, 429, 503):
            if f"http_{candidate}" in code:
                status_code = candidate
                break
        return HTTPException(status_code=status_code, detail=code)

    def redact_mailhub_connection_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Keep credential refs inside MailHub/Host boundaries, never in browser JSON."""

        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: redact(item)
                    for key, item in value.items()
                    if key
                    not in {"credential_ref", "access_token", "refresh_token", "client_secret"}
                }
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        result = redact(dict(payload))
        return result if isinstance(result, dict) else {}

    def mailhub_uuid(value: object, field: str) -> UUID:
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"{field}_invalid") from exc

    def mailhub_body_text(body: dict[str, Any], field: str, *, required: bool = True) -> str | None:
        value = body.get(field)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > 200_000:
            raise HTTPException(status_code=422, detail=f"{field}_invalid")
        return value

    def mailhub_addresses(body: dict[str, Any], field: str) -> tuple[str, ...]:
        value = body.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise HTTPException(status_code=422, detail=f"{field}_invalid")
        addresses = tuple(item.strip() for item in value if item.strip())
        if len(addresses) > 50:
            raise HTTPException(status_code=422, detail=f"{field}_too_many")
        return addresses

    def integration_http_error(exc: IntegrationGatewayError) -> HTTPException:
        return HTTPException(status_code=exc.status_code, detail=exc.code)

    def mcp_host_identity(record: SessionRecord, request: Request) -> McpHostIdentity:
        return McpHostIdentity(
            host_id="caplatform",
            tenant_id=record.principal.tenant_id,
            subject_id=record.principal.user_id,
            scopes=tuple(record.principal.permissions),
            entitlements=tuple(record.principal.apps),
            request_id=str(getattr(request.state, "trace_id", "")),
            traceparent=request.headers.get("traceparent"),
        )

    def mcp_http_error(exc: McpFacadeError) -> HTTPException:
        return HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "outcome_unknown": exc.outcome_unknown},
        )

    def mcp_expected_revision(body: dict[str, Any]) -> int:
        value = body.get("expected_revision")
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise HTTPException(status_code=422, detail="expected_revision_invalid")
        return value

    async def idempotency_lookup(
        *,
        operation: str,
        key: str,
        record: SessionRecord,
        request_payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        digest = _request_digest(request_payload)
        try:
            cached = await idempotency.get(
                tenant_id=record.principal.tenant_id,
                user_id=record.principal.user_id,
                operation=operation,
                idempotency_key=key,
                request_digest=digest,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="idempotency_key_reused",
            ) from exc
        return digest, cached

    async def idempotency_remember(
        *,
        operation: str,
        key: str,
        digest: str,
        record: SessionRecord,
        result: dict[str, Any],
    ) -> None:
        await idempotency.put(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
            request_digest=digest,
            response=result,
        )

    # MailHub remains a standalone service.  These routes are a narrow host
    # projection: browser callers never receive provider credentials and the
    # BFF supplies the verified tenant/subject context on every request.
    @app.get("/api/mail/connections")
    async def list_mail_connections(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            data = await client.list_connections(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        return redact_mailhub_connection_payload({"data": list(data)})

    @app.get("/api/mail/connections/{connection_id}/impact-preview")
    async def preview_mail_connection_impact(
        connection_id: UUID,
        record: Annotated[SessionRecord, Depends(require_session)],
        folder_ref: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        client = require_mailhub()
        refs = tuple(folder_ref or ())
        if len(refs) > 50 or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > 200
            or any(ord(char) < 33 or ord(char) == 127 for char in value.strip())
            for value in refs
        ):
            raise HTTPException(status_code=422, detail="impact_preview_folder_refs_invalid")
        try:
            return redact_mailhub_connection_payload(
                dict(
                    await client.connection_impact_preview(
                        tenant_id=record.principal.tenant_id,
                        subject_id=record.principal.user_id,
                        connection_id=connection_id,
                        folder_refs=refs,
                    )
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mail/data-export")
    async def export_mail_data(
        record: Annotated[SessionRecord, Depends(require_session)],
        response: Response,
        include_content: bool = False,
        limit: int = Query(default=200, ge=1, le=200),
    ) -> dict[str, Any]:
        """Return a bounded, user-scoped MailHub export without secrets.

        The standalone service owns the export shape and content/object-ref
        redaction.  The BFF only binds the verified session identity and marks
        the response non-cacheable because an explicit content export may
        contain message text.
        """

        client = require_mailhub()
        try:
            payload = await client.export_data(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                include_content=include_content,
                limit=limit,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Disposition"] = (
            'attachment; filename="mailhub-data-export.json"'
        )
        return dict(payload)

    @app.get("/api/mail/admin/provider-health")
    async def list_mail_provider_health(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        """Expose bounded provider health only to host administrators."""

        require_integration_role(record, "admin")
        client = require_mailhub()
        try:
            data = await client.provider_health(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        return redact_mailhub_connection_payload({"data": list(data)})

    @app.get("/api/mail/audit")
    async def list_mail_audit(
        record: Annotated[SessionRecord, Depends(require_session)],
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        """Expose only bounded audit metadata to an authorized host role."""

        if not (
            integration_role(record) == "admin"
            or _permission_allows(record.principal.permissions, "mail.audit")
        ):
            raise HTTPException(status_code=403, detail="mail_audit_required")
        client = require_mailhub()
        try:
            data = await client.list_audit(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                limit=limit,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        return redact_mailhub_connection_payload({"data": list(data)})

    @app.get("/api/mail/oauth/providers")
    async def list_mail_oauth_providers(
        record: Annotated[SessionRecord, Depends(require_session)],
        response: Response,
    ) -> dict[str, Any]:
        """Expose only non-secret, server-owned OAuth readiness metadata."""

        del record
        require_mailhub()
        response.headers["Cache-Control"] = "no-store"
        registrations = (
            resolved.mailhub_oauth_registration("gmail"),
            resolved.mailhub_oauth_registration("microsoft_graph"),
        )
        return {
            "data": [
                {
                    "provider": registration.provider,
                    "display_name": registration.display_name,
                    "enabled": registration.enabled,
                    "ready": resolved.mailhub_oauth_runtime_ready(registration.provider),
                    "read_only": True,
                    "scopes": list(registration.scopes),
                }
                for registration in registrations
            ]
        }

    @app.post("/api/mail/oauth/{provider}:authorize")
    async def begin_mail_oauth(
        provider: str,
        body: dict[str, Any],
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        client = require_mailhub()
        if provider not in {"gmail", "microsoft_graph"}:
            raise HTTPException(status_code=404, detail="mailhub_oauth_provider_not_found")
        registration = resolved.mailhub_oauth_registration(provider)  # type: ignore[arg-type]
        if not resolved.mailhub_oauth_runtime_ready(registration.provider):
            raise HTTPException(status_code=503, detail="mailhub_oauth_not_ready")
        raw_connection_id = body.get("connection_id")
        connection_id: UUID | None = None
        if raw_connection_id is not None:
            connection_id = mailhub_uuid(raw_connection_id, "connection_id")
        expected_revision = body.get("expected_revision")
        if connection_id is not None and (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise HTTPException(status_code=422, detail="expected_revision_invalid")
        if connection_id is None and expected_revision is not None:
            raise HTTPException(status_code=422, detail="expected_revision_invalid")
        try:
            payload = await client.begin_oauth(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                provider=provider,
                authorization_endpoint=registration.authorization_endpoint,
                client_id=registration.client_id,
                redirect_uri=registration.redirect_uri,
                scopes=registration.scopes,
                extra_parameters=(
                    {
                        "access_type": "offline",
                        "include_granted_scopes": "true",
                        "prompt": "consent",
                    }
                    if provider == "gmail"
                    else {}
                ),
                connection_id=connection_id,
                expected_revision=expected_revision,
                trace_id=str(getattr(request.state, "trace_id", "")) or None,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return dict(payload)

    @app.post("/api/mail/oauth/{provider}:callback")
    async def complete_mail_oauth(
        provider: str,
        body: dict[str, Any],
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        client = require_mailhub()
        if provider not in {"gmail", "microsoft_graph"}:
            raise HTTPException(status_code=404, detail="mailhub_oauth_provider_not_found")
        registration = resolved.mailhub_oauth_registration(provider)  # type: ignore[arg-type]
        if not resolved.mailhub_oauth_runtime_ready(registration.provider):
            raise HTTPException(status_code=503, detail="mailhub_oauth_not_ready")
        state = body.get("state")
        code = body.get("code")
        if not isinstance(state, str) or not 20 <= len(state) <= 4000:
            raise HTTPException(status_code=422, detail="oauth_state_invalid")
        if not isinstance(code, str) or not code.strip() or len(code) > 10_000:
            raise HTTPException(status_code=422, detail="oauth_code_invalid")
        try:
            payload = await client.complete_oauth(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                provider=provider,
                state=state,
                code=code,
                redirect_uri=registration.redirect_uri,
                trace_id=str(getattr(request.state, "trace_id", "")) or None,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        # credential_ref is a governed internal reference, but it is not a
        # browser concern.  Never reflect it through the host API.
        data = payload.get("data")
        if not isinstance(data, dict):
            raise HTTPException(status_code=502, detail="mailhub_oauth_response_invalid")
        response.headers["Cache-Control"] = "no-store"
        return {"data": {key: value for key, value in data.items() if key != "credential_ref"}}

    @app.get("/api/mail/agent-policies")
    async def list_mail_agent_policies(
        record: Annotated[SessionRecord, Depends(require_session)],
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            data = await client.list_agent_policies(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                limit=limit,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        return {"data": list(data)}

    @app.get("/api/mail/delegations")
    async def list_mail_delegations(
        record: Annotated[SessionRecord, Depends(require_session)],
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            data = await client.list_delegations(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                limit=limit,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        return {"data": list(data)}

    @app.post("/api/mail/autonomy/runs", status_code=202)
    async def enqueue_mail_autonomy(
        body: dict[str, Any],
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        client = require_mailhub()
        connection_id = body.get("connection_id")
        replay_key = body.get("replay_key")
        body_idempotency_key = body.get("idempotency_key")
        if not isinstance(body_idempotency_key, str):
            body_idempotency_key = None
        idempotency_key = _require_idempotency_key(idempotency_key or body_idempotency_key)
        if not isinstance(connection_id, str) or not isinstance(replay_key, str):
            raise HTTPException(status_code=422, detail="autonomy_identity_invalid")
        try:
            connection_uuid = UUID(connection_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="connection_id_invalid") from exc
        limit = body.get("limit", 50)
        message_limit = body.get("message_limit", 50)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 500
            or not isinstance(message_limit, int)
            or isinstance(message_limit, bool)
            or not 1 <= message_limit <= 200
        ):
            raise HTTPException(status_code=422, detail="autonomy_limits_invalid")
        try:
            return dict(
                await client.enqueue_autonomy(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    connection_id=connection_uuid,
                    replay_key=replay_key,
                    idempotency_key=idempotency_key,
                    limit=limit,
                    message_limit=message_limit,
                    trace_id=str(getattr(request.state, "trace_id", "")) or None,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mail/autonomy/runs")
    async def list_mail_autonomy(
        record: Annotated[SessionRecord, Depends(require_session)],
        connection_id: UUID | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            data = await client.list_autonomy_runs(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                connection_id=connection_id,
                limit=limit,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        return {"data": list(data)}

    @app.post("/api/mail/autonomy/runs/{run_id}:{command}")
    async def control_mail_autonomy(
        run_id: UUID,
        command: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reason = body.get("reason") if isinstance(body, dict) else None
        if command == "pause" and (not isinstance(reason, str) or not reason.strip()):
            raise HTTPException(status_code=422, detail="autonomy_pause_reason_required")
        client = require_mailhub()
        try:
            return dict(
                await client.control_autonomy_run(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    run_id=run_id,
                    command=command,
                    reason=reason,
                    trace_id=str(getattr(request.state, "trace_id", "")) or None,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/connections/{connection_id}:revoke")
    async def revoke_mail_connection(
        connection_id: UUID,
        body: dict[str, Any],
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
    ) -> dict[str, Any]:
        client = require_mailhub()
        revision = body.get("expected_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise HTTPException(status_code=422, detail="expected_revision_invalid")
        try:
            return redact_mailhub_connection_payload(
                dict(
                    await client.revoke_connection(
                        tenant_id=record.principal.tenant_id,
                        subject_id=record.principal.user_id,
                        connection_id=connection_id,
                        expected_revision=revision,
                        trace_id=str(getattr(request.state, "trace_id", "")) or None,
                    )
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/connections/{connection_id}:delete")
    async def delete_mail_connection(
        connection_id: UUID,
        body: dict[str, Any],
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
    ) -> dict[str, Any]:
        client = require_mailhub()
        revision = body.get("expected_revision")
        if not isinstance(revision, int) or revision < 1:
            raise HTTPException(status_code=422, detail="expected_revision_invalid")
        try:
            return redact_mailhub_connection_payload(
                dict(
                    await client.delete_connection(
                        tenant_id=record.principal.tenant_id,
                        subject_id=record.principal.user_id,
                        connection_id=connection_id,
                        expected_revision=revision,
                        trace_id=str(getattr(request.state, "trace_id", "")) or None,
                    )
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/connections/{connection_id}:scopes")
    async def update_mail_connection_scopes(
        connection_id: UUID,
        body: dict[str, Any],
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
    ) -> dict[str, Any]:
        client = require_mailhub()
        revision = body.get("expected_revision")
        raw_scopes = body.get("granted_scopes")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise HTTPException(status_code=422, detail="expected_revision_invalid")
        if not isinstance(raw_scopes, list) or not raw_scopes or len(raw_scopes) > 40:
            raise HTTPException(status_code=422, detail="connection_scopes_invalid")
        if not all(isinstance(scope, str) for scope in raw_scopes):
            raise HTTPException(status_code=422, detail="connection_scopes_invalid")
        try:
            return redact_mailhub_connection_payload(
                dict(
                    await client.update_connection_scopes(
                        tenant_id=record.principal.tenant_id,
                        subject_id=record.principal.user_id,
                        connection_id=connection_id,
                        expected_revision=revision,
                        granted_scopes=raw_scopes,
                        trace_id=str(getattr(request.state, "trace_id", "")) or None,
                    )
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/connections/{connection_id}:refresh")
    async def refresh_mail_connection(
        connection_id: UUID,
        body: dict[str, Any],
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
    ) -> dict[str, Any]:
        client = require_mailhub()
        revision = body.get("expected_revision")
        reason = body.get("reason", "manual_refresh")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise HTTPException(status_code=422, detail="expected_revision_invalid")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise HTTPException(status_code=422, detail="connection_refresh_reason_invalid")
        try:
            return redact_mailhub_connection_payload(
                dict(
                    await client.refresh_connection(
                        tenant_id=record.principal.tenant_id,
                        subject_id=record.principal.user_id,
                        connection_id=connection_id,
                        expected_revision=revision,
                        reason=reason,
                        trace_id=str(getattr(request.state, "trace_id", "")) or None,
                    )
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/connections/{connection_id}/sync-jobs", status_code=202)
    async def enqueue_mail_sync(
        connection_id: UUID,
        body: dict[str, Any],
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        client = require_mailhub()
        key = _require_idempotency_key(idempotency_key)
        mode = body.get("mode", "incremental")
        limit = body.get("limit", 50)
        folder_ref = body.get("folder_ref", "INBOX")
        label_refs = body.get("label_refs", [])
        received_after = body.get("received_after")
        received_before = body.get("received_before")
        if mode not in {"incremental", "backfill", "reconcile"}:
            raise HTTPException(status_code=422, detail="sync_mode_invalid")
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="sync_limit_invalid")
        if not isinstance(folder_ref, str) or not folder_ref.strip() or len(folder_ref) > 200:
            raise HTTPException(status_code=422, detail="sync_folder_invalid")
        if (
            not isinstance(label_refs, list)
            or len(label_refs) > 20
            or not all(
                isinstance(label, str)
                and label.strip()
                and len(label) <= 200
                and not any(ord(char) < 33 or ord(char) == 127 for char in label)
                for label in label_refs
            )
        ):
            raise HTTPException(status_code=422, detail="sync_labels_invalid")
        for date_value, field_name in (
            (received_after, "received_after"),
            (received_before, "received_before"),
        ):
            if date_value is not None and (
                not isinstance(date_value, str)
                or not date_value.strip()
                or len(date_value) > 80
            ):
                raise HTTPException(status_code=422, detail=f"{field_name}_invalid")
        if (
            label_refs
            or folder_ref != "INBOX"
            or received_after is not None
            or received_before is not None
        ) and mode != "backfill":
            raise HTTPException(status_code=422, detail="sync_filter_requires_backfill")
        try:
            return dict(
                await client.enqueue_sync(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    connection_id=connection_id,
                    idempotency_key=key,
                    trace_id=str(getattr(request.state, "trace_id", "")) or None,
                    mode=mode,
                    limit=limit,
                    folder_ref=folder_ref,
                    label_refs=label_refs,
                    received_after=received_after,
                    received_before=received_before,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mail/sync-jobs")
    async def list_mail_sync_jobs(
        record: Annotated[SessionRecord, Depends(require_session)],
        connection_id: UUID | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            jobs = await client.list_sync_jobs(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                connection_id=connection_id,
                limit=limit,
            )
            return {"data": list(jobs)}
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mail/sync-jobs/{job_id}")
    async def get_mail_sync_job(
        job_id: UUID,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            return dict(
                await client.get_sync_job(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    job_id=job_id,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/sync-jobs/{job_id}:cancel")
    async def cancel_mail_sync_job(
        job_id: UUID,
        record: Annotated[SessionRecord, Depends(require_session)],
        request: Request,
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            return dict(
                await client.cancel_sync_job(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    job_id=job_id,
                    trace_id=str(getattr(request.state, "trace_id", "")) or None,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mail/threads")
    async def list_mail_threads(
        record: Annotated[SessionRecord, Depends(require_session)],
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=512),
        connection_id: UUID | None = None,
        unread: bool = False,
        important: bool = False,
        attachment: bool = False,
        has_attachment: bool = False,
        project: bool = False,
        candidate: bool = False,
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            return dict(
                await client.list_thread_page(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    limit=limit,
                    cursor=cursor,
                    connection_id=connection_id,
                    unread=unread,
                    important=important,
                    has_attachment=attachment or has_attachment,
                    project=project,
                    candidate=candidate,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mail/threads/{thread_id}")
    async def get_mail_thread(
        thread_id: UUID,
        record: Annotated[SessionRecord, Depends(require_session)],
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            return dict(
                await client.get_thread(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    thread_id=thread_id,
                    limit=limit,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mail/messages")
    async def list_mail_messages(
        record: Annotated[SessionRecord, Depends(require_session)],
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            data = await client.list_messages(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                limit=limit,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        return {"data": list(data)}

    @app.get("/api/mail/messages/{message_id}/content", response_class=PlainTextResponse)
    async def get_mail_message_content(
        message_id: UUID,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> PlainTextResponse:
        client = require_mailhub()
        try:
            content = await client.get_message_content(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                message_id=message_id,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        return PlainTextResponse(content, media_type="text/plain; charset=utf-8")

    @app.get("/api/mail/search")
    async def search_mail_messages(
        record: Annotated[SessionRecord, Depends(require_session)],
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
        mode: str = Query(default="metadata", pattern="^(metadata|provider)$"),
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            return dict(
                await client.search_messages(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    query=q,
                    limit=limit,
                    mode=mode,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/messages/{message_id}:analyze")
    async def analyze_mail_message(
        message_id: UUID,
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            return dict(
                await client.analyze_message(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    message_id=message_id,
                    trace_id=str(getattr(request.state, "trace_id", "")) or "mail-analysis",
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mail/candidates")
    async def list_mail_candidates(
        record: Annotated[SessionRecord, Depends(require_session)],
        limit: int = Query(default=50, ge=1, le=200),
        status: str | None = Query(default=None, max_length=32),
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            data = await client.list_candidates(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                limit=limit,
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc
        if status:
            data = tuple(item for item in data if item.get("state") == status)
        return {"data": list(data)}

    @app.post("/api/mail/candidates/{candidate_id}:review")
    async def review_mail_candidate(
        candidate_id: UUID,
        body: dict[str, Any],
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        client = require_mailhub()
        approved = body.get("approved")
        revision = body.get("expected_revision")
        if not isinstance(approved, bool) or not isinstance(revision, int) or revision < 1:
            raise HTTPException(status_code=422, detail="candidate_review_invalid")
        reason = body.get("review_reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 2_000):
            raise HTTPException(status_code=422, detail="review_reason_invalid")
        try:
            return dict(
                await client.review_candidate(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    candidate_id=candidate_id,
                    approved=approved,
                    expected_revision=revision,
                    review_reason=reason,
                    trace_id=str(getattr(request.state, "trace_id", "")) or None,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/candidates/{candidate_id}:apply")
    async def apply_mail_candidate(
        candidate_id: UUID,
        body: dict[str, Any],
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        client = require_mailhub()
        expected_revision = body.get("expected_revision")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
            or set(body) != {"expected_revision"}
        ):
            raise HTTPException(status_code=422, detail="candidate_apply_request_invalid")
        key = _require_idempotency_key(idempotency_key)
        try:
            candidate = await client.get_candidate(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                candidate_id=candidate_id,
            )
            if candidate.get("state") != "approved":
                raise HTTPException(status_code=409, detail="mail_candidate_not_approved")
            if candidate.get("revision") != expected_revision:
                raise HTTPException(status_code=409, detail="mail_candidate_revision_conflict")
            approval_ref = await mail_host_actions.issue_candidate_approval(
                tenant_id=record.principal.tenant_id,
                subject_id=record.principal.user_id,
                candidate_id=candidate_id,
                candidate_revision=expected_revision,
            )
            return dict(
                await client.apply_candidate(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    candidate_id=candidate_id,
                    approval_ref=approval_ref,
                    idempotency_key=key,
                    trace_id=str(getattr(request.state, "trace_id", "")) or None,
                )
            )
        except HTTPException:
            raise
        except MailHubHostActionError as exc:
            raise mailhub_host_action_http_error(exc) from exc
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/drafts")
    async def create_mail_draft(
        body: dict[str, Any],
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        client = require_mailhub()
        key = _require_idempotency_key(idempotency_key)
        connection_id = mailhub_uuid(body.get("connection_id"), "connection_id")
        thread_raw = body.get("thread_id")
        thread_id = mailhub_uuid(thread_raw, "thread_id") if thread_raw else None
        subject = mailhub_body_text(body, "subject")
        body_text = mailhub_body_text(body, "body_text")
        assert subject is not None and body_text is not None
        try:
            return dict(
                await client.create_draft(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    connection_id=connection_id,
                    thread_id=thread_id,
                    recipient_addresses=mailhub_addresses(body, "recipient_addresses"),
                    cc_addresses=mailhub_addresses(body, "cc_addresses"),
                    bcc_addresses=mailhub_addresses(body, "bcc_addresses"),
                    attachment_refs=mailhub_addresses(body, "attachment_refs"),
                    subject=subject,
                    body_text=body_text,
                    idempotency_key=key,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mail/drafts/{draft_id}")
    async def get_mail_draft(
        draft_id: UUID,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        client = require_mailhub()
        try:
            return dict(
                await client.get_draft(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    draft_id=draft_id,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.post("/api/mail/drafts/{draft_id}:send", status_code=202)
    async def send_mail_draft(
        draft_id: UUID,
        body: dict[str, Any],
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        client = require_mailhub()
        key = _require_idempotency_key(idempotency_key)
        revision = body.get("expected_revision")
        if not isinstance(revision, int) or revision < 1:
            raise HTTPException(status_code=422, detail="expected_revision_invalid")
        required = ("expected_content_sha256", "expected_recipient_digest", "confirmation_ref")
        values: dict[str, str] = {}
        for field in required:
            value = body.get(field)
            if not isinstance(value, str) or not value.strip():
                raise HTTPException(status_code=422, detail=f"{field}_invalid")
            values[field] = value
        try:
            return dict(
                await client.send_draft(
                    tenant_id=record.principal.tenant_id,
                    subject_id=record.principal.user_id,
                    draft_id=draft_id,
                    confirmation_ref=values["confirmation_ref"],
                    expected_revision=revision,
                    expected_content_sha256=values["expected_content_sha256"],
                    expected_recipient_digest=values["expected_recipient_digest"],
                    idempotency_key=key,
                    trace_id=str(getattr(request.state, "trace_id", "")) or None,
                    agent_subject_id=record.principal.user_id,
                )
            )
        except (MailHubAdapterError, ValueError) as exc:
            raise mailhub_http_error(exc) from exc

    @app.get("/api/mcp/providers")
    async def list_mcp_providers(
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "viewer")
        try:
            return await mcp.list_providers(identity=mcp_host_identity(record, request))
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc

    @app.get("/api/mcp/console")
    async def get_mcp_connector_console(
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "viewer")
        try:
            return await mcp.connector_console(identity=mcp_host_identity(record, request))
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc

    @app.post("/api/mcp/providers/{provider_key}:authorize", status_code=503)
    async def begin_mcp_provider_authorization(
        provider_key: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> None:
        require_integration_role(record, "admin")
        if provider_key not in {"dingtalk", "feishu"}:
            raise HTTPException(status_code=404, detail="mcp_provider_not_builtin")
        raise HTTPException(
            status_code=503,
            detail="mcp_provider_authorization_host_binding_not_configured",
        )

    @app.get("/api/mcp/installations/{installation_id}")
    async def get_mcp_installation(
        installation_id: str,
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "viewer")
        try:
            return await mcp.get_installation(
                installation_id,
                identity=mcp_host_identity(record, request),
            )
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc

    @app.get("/api/mcp/installations/{installation_id}/snapshot")
    async def get_mcp_snapshot(
        installation_id: str,
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "viewer")
        try:
            return await mcp.get_snapshot(
                installation_id,
                identity=mcp_host_identity(record, request),
            )
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc

    @app.post("/api/mcp/installations/{installation_id}:test")
    async def test_mcp_installation(
        installation_id: str,
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "admin")
        try:
            return await mcp.test_installation(
                installation_id,
                identity=mcp_host_identity(record, request),
            )
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc

    @app.post("/api/mcp/installations/{installation_id}:revoke")
    async def revoke_mcp_installation(
        installation_id: str,
        body: dict[str, Any],
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "admin")
        try:
            return await mcp.revoke_installation(
                installation_id,
                expected_revision=mcp_expected_revision(body),
                identity=mcp_host_identity(record, request),
            )
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc

    @app.post("/api/mcp/bindings/{binding_id}:review-and-publish")
    async def review_and_publish_mcp_binding(
        binding_id: str,
        body: dict[str, Any],
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "admin")
        try:
            return await mcp.review_and_publish_binding(
                binding_id,
                expected_revision=mcp_expected_revision(body),
                identity=mcp_host_identity(record, request),
            )
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc

    @app.post("/api/mcp/bindings/{binding_id}:disable")
    async def disable_mcp_binding(
        binding_id: str,
        body: dict[str, Any],
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "admin")
        try:
            return await mcp.disable_binding(
                binding_id,
                expected_revision=mcp_expected_revision(body),
                identity=mcp_host_identity(record, request),
            )
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc

    @app.post("/api/mcp/invocations/{invocation_id}:reconcile")
    async def reconcile_mcp_invocation(
        invocation_id: str,
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        require_integration_role(record, "developer")
        key = _require_idempotency_key(idempotency_key)
        try:
            return await mcp.reconcile_invocation(
                invocation_id,
                idempotency_key=key,
                identity=mcp_host_identity(record, request),
            )
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc

    @app.post("/api/mcp/invocations", status_code=202)
    async def invoke_mcp_binding(
        body: McpInvocationRequest,
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        require_integration_role(record, "developer")
        key = _require_idempotency_key(idempotency_key)
        try:
            result = await mcp.invoke(
                published_binding_id=body.published_binding_id,
                arguments=body.arguments,
                idempotency_key=key,
                approval_ref=body.approval_ref,
                identity=mcp_host_identity(record, request),
            )
        except McpFacadeError as exc:
            raise mcp_http_error(exc) from exc
        return {"data": result}

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "caplatform-bff",
            "version": SERVICE_VERSION,
            "build_ref": BUILD_REF,
            "contract": PROJECT_BFF_CONTRACT,
        }

    @app.get("/readyz")
    async def readiness(response: Response) -> dict[str, Any]:
        issues = resolved.readiness_issues()
        try:
            core_readiness = await core.readiness()
            if core_readiness.get("status") != "ready":
                issues.append("platform_core_not_ready")
        except (PlatformCoreError, AttributeError):
            issues.append("platform_core_unavailable")
        if resolved.ai_mode == "remote":
            try:
                await assist.readiness()
            except (AgentctlIntegrationError, AttributeError):
                issues.append("agentctl_unavailable")
        compatibility_payload: dict[str, Any] | None = None
        if resolved.aiprojectops_base_url:
            try:
                compatibility_payload = await projects.compatibility(
                    resolved.platform_service_token
                )
                provides = set(compatibility_payload.get("provides") or [])
                if PROJECT_API_CONTRACT not in provides:
                    issues.append("aiprojectops_contract_incompatible")
            except (AIProjectOpsError, AttributeError):
                issues.append("aiprojectops_compatibility_unavailable")
        if issues:
            response.status_code = 503
        return {
            "status": "ready" if not issues else "not_ready",
            "environment": resolved.environment,
            "ai_mode": resolved.ai_mode,
            "issues": issues,
            "versions": {
                "caplatform": SERVICE_VERSION,
                "aiprojectops": (
                    compatibility_payload.get("version")
                    if compatibility_payload is not None
                    else None
                ),
            },
        }

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        total = int(request_metrics["total"])
        errors = int(request_metrics["errors"])
        latency = float(request_metrics["latency_seconds"])
        body = (
            "# TYPE caplatform_http_requests_total counter\n"
            f"caplatform_http_requests_total {total}\n"
            "# TYPE caplatform_http_errors_total counter\n"
            f"caplatform_http_errors_total {errors}\n"
            "# TYPE caplatform_http_request_duration_seconds_sum counter\n"
            f"caplatform_http_request_duration_seconds_sum {latency:.6f}\n"
        )
        return Response(content=body, media_type="text/plain; version=0.0.4")

    @app.get("/api/system/runtime")
    async def system_runtime(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        del record
        upstream: dict[str, Any] | None = None
        issue: str | None = None
        if resolved.aiprojectops_base_url:
            try:
                upstream = await projects.compatibility(resolved.platform_service_token)
            except (AIProjectOpsError, AttributeError):
                issue = "aiprojectops_compatibility_unavailable"
        return {
            "schema_version": "caplatform.runtime_versions.v1",
            "service": {
                "name": "caplatform-bff",
                "version": SERVICE_VERSION,
                "build_ref": BUILD_REF,
                "provides": [PROJECT_BFF_CONTRACT],
            },
            "dependencies": {"aiprojectops": upstream},
            "issues": [issue] if issue else [],
        }

    @app.post("/internal/product-events", status_code=202)
    async def ingest_internal_product_event(
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        supplied = _bearer_token(authorization)
        expected = resolved.product_event_token
        if not expected or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="product_event_service_identity_required")
        try:
            return await integrations.ingest_product_event(body)
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.post("/api/integrations/inbound/{endpoint_key}", status_code=202)
    async def receive_integration_event(
        endpoint_key: str,
        request: Request,
        x_ca_signature: str | None = Header(default=None, alias="X-CA-Signature"),
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
        x_provider_event_id: str | None = Header(default=None, alias="X-Provider-Event-Id"),
        x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
        x_ca_timestamp: str | None = Header(default=None, alias="X-CA-Timestamp"),
    ) -> dict[str, Any]:
        if request.method != "POST":
            raise HTTPException(status_code=405, detail="method_not_allowed")
        raw_body = await request.body()
        try:
            result = await integrations.receive_inbound(
                endpoint_key=endpoint_key,
                raw_body=raw_body,
                content_type=request.headers.get("content-type", ""),
                signature=x_ca_signature or x_hub_signature_256,
                provider_event_id=x_provider_event_id or x_github_delivery,
                timestamp=x_ca_timestamp,
                source_ip=request.client.host if request.client else None,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc
        return {
            "receipt_id": result.receipt_id,
            "status": result.status,
            "duplicate": result.duplicate,
        }

    @app.get("/api/integrations/catalog")
    async def integration_event_catalog(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "viewer")
        return await integrations.event_catalog()

    @app.get("/api/integrations/docs")
    async def integration_developer_docs(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "viewer")
        return await integrations.developer_docs()

    @app.get("/api/integrations/destinations")
    async def list_integration_destinations(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "viewer")
        return await integrations.list_destinations(record.principal.tenant_id)

    @app.post("/api/integrations/destinations", status_code=201)
    async def create_integration_destination(
        body: IntegrationDestinationCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "developer")
        try:
            return await integrations.create_destination(
                tenant_id=record.principal.tenant_id,
                actor_id=record.principal.user_id,
                name=body.name,
                endpoint_url=body.endpoint_url,
                environment=body.environment,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.post("/api/integrations/destinations/{destination_id}:publish")
    async def publish_integration_destination(
        destination_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        enable: bool = Query(default=False),
    ) -> dict[str, Any]:
        require_integration_role(record, "developer")
        try:
            return await integrations.publish_destination(
                tenant_id=record.principal.tenant_id,
                actor_id=record.principal.user_id,
                destination_id=destination_id,
                enable=enable,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.post("/api/integrations/destinations/{destination_id}:rotate-secret")
    async def rotate_integration_destination_secret(
        destination_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "admin")
        try:
            return await integrations.rotate_destination_secret(
                tenant_id=record.principal.tenant_id,
                actor_id=record.principal.user_id,
                destination_id=destination_id,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.post("/api/integrations/destinations/{destination_id}:test", status_code=202)
    async def test_integration_destination(
        destination_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "developer")
        try:
            return await integrations.test_destination(
                tenant_id=record.principal.tenant_id,
                actor_id=record.principal.user_id,
                destination_id=destination_id,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.post("/api/integrations/destinations/{destination_id}:revoke-secret")
    async def revoke_integration_destination_secret(
        destination_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "admin")
        try:
            return await integrations.rotate_destination_secret(
                tenant_id=record.principal.tenant_id,
                actor_id=record.principal.user_id,
                destination_id=destination_id,
                revoke=True,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.get("/api/integrations/subscriptions")
    async def list_integration_subscriptions(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "viewer")
        return await integrations.list_subscriptions(record.principal.tenant_id)

    @app.post("/api/integrations/subscriptions", status_code=201)
    async def create_integration_subscription(
        body: IntegrationSubscriptionCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "developer")
        try:
            return await integrations.create_subscription(
                tenant_id=record.principal.tenant_id,
                actor_id=record.principal.user_id,
                destination_id=body.destination_id,
                topics=body.topics,
                project_ids=body.project_ids,
                filters=body.filters,
                data_policy=body.data_policy,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.get("/api/integrations/deliveries")
    async def list_integration_deliveries(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "viewer")
        return await integrations.list_deliveries(record.principal.tenant_id)

    @app.post("/api/integrations/deliveries:process")
    async def process_integration_deliveries(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "developer")
        return await integrations.process_deliveries(record.principal.tenant_id)

    @app.post("/api/integrations/deliveries/{delivery_id}:replay", status_code=201)
    async def replay_integration_delivery(
        delivery_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "admin")
        try:
            return await integrations.replay_delivery(
                tenant_id=record.principal.tenant_id,
                actor_id=record.principal.user_id,
                delivery_id=delivery_id,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.post("/api/integrations/dead-letters:replay", status_code=201)
    async def replay_integration_dead_letters(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "admin")
        return await integrations.replay_dead_letters(
            tenant_id=record.principal.tenant_id,
            actor_id=record.principal.user_id,
        )

    @app.get("/api/integrations/endpoints")
    async def list_integration_endpoints(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "viewer")
        return await integrations.list_endpoints(record.principal.tenant_id)

    @app.post("/api/integrations/endpoints", status_code=201)
    async def create_integration_endpoint(
        body: IntegrationEndpointCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "developer")
        try:
            return await integrations.create_endpoint(
                tenant_id=record.principal.tenant_id,
                actor_id=record.principal.user_id,
                name=body.name,
                environment=body.environment,
                provider=body.provider,
                verifier=body.verifier,
                handler_ref=body.handler_ref,
                allowed_ip_cidrs=body.allowed_ip_cidrs,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.get("/api/integrations/receipts")
    async def list_integration_receipts(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "viewer")
        return await integrations.list_receipts(record.principal.tenant_id)

    @app.post("/api/integrations/receipts:process")
    async def process_integration_receipts(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "developer")
        return await integrations.process_receipts(record.principal.tenant_id)

    @app.post("/api/integrations/receipts/{receipt_id}:act", status_code=201)
    async def act_on_integration_receipt(
        receipt_id: str,
        body: IntegrationReceiptActionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        require_integration_role(record, "admin")
        try:
            return await integrations.act_on_receipt(
                tenant_id=record.principal.tenant_id,
                actor_id=record.principal.user_id,
                receipt_id=receipt_id,
                action=body.action,
                handler_ref=body.handler_ref,
            )
        except IntegrationGatewayError as exc:
            raise integration_http_error(exc) from exc

    @app.get("/api/integrations/audit")
    async def list_integration_audit(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        require_integration_role(record, "viewer")
        return await integrations.list_audit(record.principal.tenant_id)

    @app.post(
        "/api/session/exchange",
        response_model=SessionExchangeResponse,
    )
    async def exchange_session(
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> SessionExchangeResponse:
        platform_token = _bearer_token(authorization)
        return await establish_browser_session(
            response,
            platform_token,
            device_label=_device_label(request),
        )

    @app.post("/api/auth/login", response_model=SessionExchangeResponse)
    async def login(
        body: AuthLoginRequest,
        request: Request,
        response: Response,
    ) -> SessionExchangeResponse:
        try:
            token = await core.login_user(
                identity_type=body.identity_type,
                identifier=body.identifier,
                password=body.password,
                totp_code=body.totp_code,
            )
        except PlatformCoreError as exc:
            status_code = _platform_auth_status(exc)
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise HTTPException(status_code=502, detail="platform_login_token_missing")
        return await establish_browser_session(
            response,
            access_token,
            device_label=_device_label(request),
        )

    @app.post("/api/auth/register", response_model=SessionExchangeResponse, status_code=201)
    async def register(
        body: AuthRegisterRequest,
        request: Request,
        response: Response,
    ) -> SessionExchangeResponse:
        personal_workspace = f"CAPLATFORM 个人空间 {uuid4().hex[:10]}"
        try:
            token = await core.register_user(
                identity_type=body.identity_type,
                identifier=body.identifier,
                password=body.password,
                display_name=body.display_name.strip(),
                tenant_name=personal_workspace,
            )
        except PlatformCoreError as exc:
            status_code = _platform_auth_status(exc)
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise HTTPException(status_code=502, detail="platform_register_token_missing")
        return await establish_browser_session(
            response,
            access_token,
            device_label=_device_label(request),
        )

    @app.delete("/api/session", status_code=204)
    async def revoke_session(
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> None:
        await sessions.revoke(record.session_id)
        response.delete_cookie(
            resolved.session_cookie,
            path="/",
        )

    @app.get("/api/me", response_model=SessionExchangeResponse)
    async def me(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> SessionExchangeResponse:
        return SessionExchangeResponse(
            principal=record.principal,
            profile=record.profile,
            expires_in=max(60, int(record.expires_at - time.time())),
        )

    @app.get("/api/account/sessions", response_model=AccountSessionListResponse)
    async def list_account_sessions(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AccountSessionListResponse:
        records = await sessions.list_for_user(record.principal.user_id)
        return AccountSessionListResponse(
            items=[
                AccountSessionProjection(
                    session_id=item.session_id,
                    device_label=item.device_label,
                    current=item.session_id == record.session_id,
                    created_at=datetime.fromtimestamp(item.created_at, tz=UTC),
                    last_seen_at=datetime.fromtimestamp(item.last_seen_at, tz=UTC),
                    expires_at=datetime.fromtimestamp(item.expires_at, tz=UTC),
                )
                for item in records
            ]
        )

    @app.delete(
        "/api/account/sessions/others",
        response_model=AccountSessionRevokeResponse,
    )
    async def revoke_other_account_sessions(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AccountSessionRevokeResponse:
        revoked = await sessions.revoke_other_sessions(
            user_id=record.principal.user_id,
            current_session_id=record.session_id,
        )
        return AccountSessionRevokeResponse(revoked=revoked)

    @app.delete(
        "/api/account/sessions/{session_id}",
        response_model=AccountSessionRevokeResponse,
    )
    async def revoke_account_session(
        session_id: str,
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AccountSessionRevokeResponse:
        revoked = await sessions.revoke_for_user(
            session_id=session_id,
            user_id=record.principal.user_id,
        )
        if not revoked:
            raise HTTPException(status_code=404, detail="account_session_not_found")
        if session_id == record.session_id:
            response.delete_cookie(resolved.session_cookie, path="/")
        return AccountSessionRevokeResponse(revoked=1)

    @app.patch("/api/account/profile", response_model=SessionExchangeResponse)
    async def update_account_profile(
        body: AccountProfileUpdateRequest,
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> SessionExchangeResponse:
        display_name = body.display_name.strip()
        if not display_name:
            raise HTTPException(status_code=422, detail="display_name_required")
        try:
            await core.update_user_profile(
                platform_user_token=record.platform_user_token,
                display_name=display_name,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        return await rotate_browser_session(
            response,
            record,
            record.platform_user_token,
            expected_tenant_id=record.principal.tenant_id,
        )

    @app.post("/api/account/change-password", response_model=SessionExchangeResponse)
    async def change_account_password(
        body: AccountChangePasswordRequest,
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> SessionExchangeResponse:
        try:
            token = await core.change_user_password(
                platform_user_token=record.platform_user_token,
                current_password=body.current_password,
                new_password=body.new_password,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise HTTPException(status_code=502, detail="platform_password_token_missing")
        return await rotate_browser_session(
            response,
            record,
            access_token,
            expected_tenant_id=record.principal.tenant_id,
        )

    @app.get("/api/account/totp", response_model=AccountTotpStatusResponse)
    async def get_account_totp(
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AccountTotpStatusResponse:
        try:
            payload = await core.get_user_totp_status(record.platform_user_token)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return AccountTotpStatusResponse.model_validate(payload)

    @app.post("/api/account/totp/setup", response_model=AccountTotpSetupResponse)
    async def setup_account_totp(
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AccountTotpSetupResponse:
        try:
            payload = await core.setup_user_totp(record.platform_user_token)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return AccountTotpSetupResponse.model_validate(payload)

    @app.post("/api/account/totp/enable", response_model=AccountTotpStatusResponse)
    async def enable_account_totp(
        body: AccountTotpEnableRequest,
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AccountTotpStatusResponse:
        try:
            payload = await core.enable_user_totp(
                platform_user_token=record.platform_user_token,
                code=body.code.strip(),
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return AccountTotpStatusResponse.model_validate(payload)

    @app.post("/api/account/totp/disable", response_model=AccountTotpStatusResponse)
    async def disable_account_totp(
        body: AccountTotpDisableRequest,
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AccountTotpStatusResponse:
        try:
            payload = await core.disable_user_totp(
                platform_user_token=record.platform_user_token,
                current_password=body.current_password,
                code=body.code.strip() if body.code else None,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        response.headers["Cache-Control"] = "no-store"
        return AccountTotpStatusResponse.model_validate(payload)

    @app.post(
        "/api/account/workspaces/switch",
        response_model=SessionExchangeResponse,
    )
    async def switch_account_workspace(
        body: AccountWorkspaceSwitchRequest,
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> SessionExchangeResponse:
        try:
            token = await core.switch_user_tenant(
                platform_user_token=record.platform_user_token,
                tenant_id=body.tenant_id,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise HTTPException(status_code=502, detail="platform_workspace_token_missing")
        return await rotate_browser_session(
            response,
            record,
            access_token,
            expected_tenant_id=body.tenant_id,
        )

    @app.get(
        "/api/account/memory",
        response_model=AccountMemoryCenterResponse,
    )
    async def get_account_memory(
        record: Annotated[SessionRecord, Depends(require_session)],
        agent_id: Annotated[str | None, Query(max_length=160)] = None,
    ) -> AccountMemoryCenterResponse:
        try:
            memory = await assist.memory_center(
                principal=record.principal,
                agent_id=agent_id,
            )
        except AgentctlIntegrationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        policy = await preferences.memory_policy(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            agent_id=agent_id,
        )
        return AccountMemoryCenterResponse(
            policy=AccountMemoryPolicy.model_validate(policy),
            **memory,
        )

    @app.patch(
        "/api/account/memory/policy",
        response_model=AccountMemoryPolicy,
    )
    async def update_account_memory_policy(
        body: AccountMemoryPolicyUpdateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AccountMemoryPolicy:
        policy = await preferences.set_memory_policy(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            enabled=body.enabled,
            retention_days=body.retention_days,
            agent_id=body.agent_id,
        )
        try:
            await assist.run_memory_retention(
                principal=record.principal,
                retention_days=body.retention_days,
                agent_id=body.agent_id,
            )
        except AgentctlIntegrationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return AccountMemoryPolicy.model_validate(policy)

    @app.post("/api/account/memory/{node_id}/correct")
    async def correct_account_memory(
        node_id: str,
        body: AccountMemoryCorrectionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await assist.correct_memory(
                principal=record.principal,
                node_id=node_id,
                agent_id=body.agent_id,
                summary=body.summary,
                reason=body.reason,
            )
        except AgentctlIntegrationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.delete("/api/account/memory/{node_id}")
    async def delete_account_memory(
        node_id: str,
        body: AccountMemoryDeleteRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await assist.delete_memory(
                principal=record.principal,
                node_id=node_id,
                agent_id=body.agent_id,
                mode=body.mode,
                reason=body.reason,
            )
        except AgentctlIntegrationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/account/export")
    async def export_account_data(
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        """Build a portable inventory without exposing runtime credentials or other users' data."""
        calls = {
            "memory": _optional_export_call(
                assist,
                "memory_center",
                principal=record.principal,
                agent_id=None,
            ),
            "experts": _optional_export_call(
                assist,
                "list_agents",
                principal=record.principal,
            ),
            "skills": _optional_export_call(
                core,
                "list_instruction_skills",
                record.platform_user_token,
            ),
            "files": _optional_export_call(
                core,
                "list_storage_files",
                record.platform_user_token,
            ),
            "knowledge": _optional_export_call(
                core,
                "list_knowledge_sources",
                record.platform_user_token,
            ),
            "active_conversations": _optional_export_call(
                core,
                "list_conversations",
                platform_user_token=record.platform_user_token,
                product_id=resolved.product_id,
                status="active",
                query=None,
                after_cursor=None,
                limit=100,
            ),
            "archived_conversations": _optional_export_call(
                core,
                "list_conversations",
                platform_user_token=record.platform_user_token,
                product_id=resolved.product_id,
                status="archived",
                query=None,
                after_cursor=None,
                limit=100,
            ),
        }
        results = await asyncio.gather(*calls.values(), return_exceptions=True)
        sections: dict[str, Any] = {}
        for name, result in zip(calls, results, strict=True):
            if isinstance(result, Exception):
                sections[name] = {
                    "status": "unavailable",
                    "reason": type(result).__name__,
                }
            else:
                sections[name] = {"status": "included", "data": result}
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Disposition"] = (
            'attachment; filename="caplatform-account-export.json"'
        )
        return {
            "schema_version": "caplatform.account_export.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "scope": {
                "user_id": record.principal.user_id,
                "tenant_id": record.principal.tenant_id,
                "product_id": resolved.product_id,
            },
            "profile": record.profile,
            "sections": sections,
            "notes": [
                "导出仅包含当前账号、当前工作空间内可见的数据。",
                "运行时令牌、密码、密钥和其他用户数据不会进入导出文件。",
                "超过 100 条的会话目录按清单导出，并通过 has_more 标识后续页。",
            ],
        }

    @app.delete("/api/account", status_code=204)
    async def deactivate_account(
        body: AccountDeactivateRequest,
        response: Response,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> None:
        try:
            memory = await assist.memory_center(
                principal=record.principal,
                agent_id=None,
            )
            nodes = memory.get("nodes") if isinstance(memory, dict) else None
            for node in nodes if isinstance(nodes, list) else []:
                if not isinstance(node, dict) or not node.get("id"):
                    continue
                namespace = node.get("namespace")
                agent_id = (
                    str(namespace.get("agent_id") or "frontdesk")
                    if isinstance(namespace, dict)
                    else "frontdesk"
                )
                await assist.delete_memory(
                    principal=record.principal,
                    node_id=str(node["id"]),
                    agent_id=agent_id,
                    mode="hard_delete",
                    reason="account_deactivation",
                )
        except AgentctlIntegrationError as exc:
            raise HTTPException(
                status_code=502,
                detail="account_memory_cleanup_unavailable",
            ) from exc
        try:
            await core.deactivate_user_account(
                platform_user_token=record.platform_user_token,
                current_password=body.current_password,
                totp_code=body.totp_code,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        await sessions.revoke_all_for_user(record.principal.user_id)
        response.delete_cookie(resolved.session_cookie, path="/")

    @app.post(
        "/api/analytics/events",
        response_model=ProductAnalyticsEventResponse,
        status_code=202,
    )
    async def record_product_analytics_event(
        body: ProductAnalyticsEventRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProductAnalyticsEventResponse:
        if resolved.ai_mode == "fixture":
            return ProductAnalyticsEventResponse(
                accepted=False,
                event_name=body.event_name,
            )
        metadata = body.model_dump(
            mode="json",
            exclude={"event_name", "event_id"},
            exclude_none=True,
        )
        try:
            await core.write_product_usage_event(
                tenant_id=record.principal.tenant_id,
                app_id=resolved.product_id,
                metric_code=f"product.{body.event_name}",
                source_event_id=f"analytics:{body.event_id}",
                metadata=metadata,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        return ProductAnalyticsEventResponse(
            accepted=True,
            event_name=body.event_name,
        )

    @app.get(
        "/api/analytics/summary",
        response_model=ProductAnalyticsSummaryResponse,
    )
    async def product_analytics_summary(
        record: Annotated[SessionRecord, Depends(require_session)],
        days: Annotated[int, Query(ge=1, le=365)] = 30,
    ) -> ProductAnalyticsSummaryResponse:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        try:
            raw = await core.get_product_usage_summary(
                platform_user_token=record.platform_user_token,
                tenant_id=record.principal.tenant_id,
                app_id=resolved.product_id,
                since=since,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        items = []
        for item in raw:
            metric = str(item.get("metric_code") or "")
            if not metric.startswith("product."):
                continue
            items.append(
                ProductAnalyticsSummaryItem(
                    event_name=metric.removeprefix("product."),
                    total=max(0, int(float(item.get("total_quantity") or 0))),
                )
            )
        return ProductAnalyticsSummaryResponse(window_days=days, items=items)

    @app.post(
        "/api/conversations",
        response_model=ConversationCreateResponse,
    )
    async def create_conversation(
        body: ConversationCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ConversationCreateResponse:
        key = _require_idempotency_key(idempotency_key)
        operation = "conversation.create"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload={"title": body.title, "agent_id": body.agent_id},
            )
            if cached is not None:
                return ConversationCreateResponse.model_validate(cached)
            result = ConversationCreateResponse(
                conversation_id=f"conv-{uuid4().hex}",
                title=body.title or "新会话",
                agent_id=body.agent_id,
            )
            if resolved.ai_mode == "remote":
                try:
                    await core.create_conversation(
                        platform_user_token=record.platform_user_token,
                        product_id=resolved.product_id,
                        conversation_id=result.conversation_id,
                        title=result.title,
                        agent_id=result.agent_id,
                    )
                except PlatformCoreError as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.get(
        "/api/conversations",
        response_model=ConversationDirectoryPage,
    )
    async def list_conversations(
        record: Annotated[SessionRecord, Depends(require_session)],
        status: Annotated[Literal["active", "archived"], Query()] = "active",
        q: Annotated[str | None, Query(max_length=200)] = None,
        after_cursor: Annotated[str | None, Query(max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ConversationDirectoryPage:
        if resolved.ai_mode == "fixture":
            now = datetime.now(UTC)
            return ConversationDirectoryPage(
                items=(
                    [
                        ConversationDirectoryEntry(
                            conversation_id=next(iter(fixture_pages), ("", "demo"))[1],
                            title="开发示例会话",
                            status="active",
                            created_at=now,
                            updated_at=now,
                        )
                    ]
                    if status == "active" and fixture_pages
                    else []
                )
            )
        if resolved.ai_mode != "remote":
            raise HTTPException(status_code=503, detail="ai_integration_disabled")
        try:
            payload = await core.list_conversations(
                platform_user_token=record.platform_user_token,
                product_id=resolved.product_id,
                status=status,
                query=q,
                after_cursor=after_cursor,
                limit=limit,
            )
            return ConversationDirectoryPage.model_validate(payload)
        except PlatformCoreError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.patch(
        "/api/conversations/{conversation_id}",
        response_model=ConversationDirectoryEntry,
    )
    async def update_conversation(
        conversation_id: str,
        body: ConversationUpdateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ConversationDirectoryEntry:
        if resolved.ai_mode != "remote":
            raise HTTPException(status_code=503, detail="conversation_directory_read_only")
        try:
            payload = await core.update_conversation(
                platform_user_token=record.platform_user_token,
                product_id=resolved.product_id,
                conversation_id=conversation_id,
                changes=body.model_dump(exclude_none=True),
            )
            return ConversationDirectoryEntry.model_validate(payload)
        except PlatformCoreError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.delete(
        "/api/conversations/{conversation_id}",
        status_code=204,
    )
    async def delete_conversation(
        conversation_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> None:
        if resolved.ai_mode != "remote":
            raise HTTPException(status_code=503, detail="conversation_directory_read_only")
        try:
            await core.delete_conversation(
                platform_user_token=record.platform_user_token,
                product_id=resolved.product_id,
                conversation_id=conversation_id,
            )
        except PlatformCoreError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post(
        "/api/conversations/{conversation_id}/messages",
        response_model=ApplicationInteractionEventPageV1,
    )
    async def send_message(
        conversation_id: str,
        body: ConversationMessageRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ApplicationInteractionEventPageV1:
        key = _require_idempotency_key(idempotency_key)
        operation = f"conversation.message:{conversation_id}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload={
                    "conversation_id": conversation_id,
                    "text": body.text,
                    "attachments": [
                        attachment.model_dump(mode="json") for attachment in body.attachments
                    ],
                    "agent_id": body.agent_id,
                },
            )
            if cached is not None:
                return ApplicationInteractionEventPageV1.model_validate(cached)
            if resolved.ai_mode == "disabled":
                raise HTTPException(status_code=503, detail="ai_integration_disabled")
            if resolved.ai_mode == "fixture":
                page = build_fixture_turn(
                    principal=record.principal,
                    product_id=resolved.product_id,
                    conversation_id=conversation_id,
                    text=body.text,
                )
                fixture_pages[(record.session_id, conversation_id)] = page
            else:
                try:
                    request_id, trace_id, user_event_id = deterministic_turn_identity(
                        principal=record.principal,
                        conversation_id=conversation_id,
                        idempotency_key=key,
                    )
                    existing_page = await _existing_conversation_page(
                        core=core,
                        record=record,
                        product_id=resolved.product_id,
                        conversation_id=conversation_id,
                    )
                    existing_agent_id = selected_agent_from_page(existing_page)
                    if (
                        existing_page is not None
                        and existing_page.events
                        and body.agent_id is not None
                        and body.agent_id != existing_agent_id
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="conversation_agent_mismatch",
                        )
                    selected_agent_id = existing_agent_id or body.agent_id
                    if request_has_terminal_event(existing_page, request_id):
                        if existing_page is None:
                            raise PlatformCoreError("conversation_replay_missing")
                        page = existing_page
                        await idempotency_remember(
                            operation=operation,
                            key=key,
                            digest=digest,
                            record=record,
                            result=page.model_dump(mode="json"),
                        )
                        return page
                    attachment_metadata, attachment_previews = await _conversation_attachments(
                        core=core,
                        record=record,
                        attachments=body.attachments,
                    )
                    selected_object_refs = [str(item["object_ref"]) for item in attachment_metadata]
                    conversation_context = build_conversation_context(
                        existing_page,
                        exclude_request_id=request_id,
                        selected_object_refs=selected_object_refs,
                        attachment_previews=attachment_previews,
                    )
                    conversation_context["memory_policy"] = await preferences.memory_policy(
                        tenant_id=record.principal.tenant_id,
                        user_id=record.principal.user_id,
                        agent_id=selected_agent_id or "frontdesk",
                    )
                    user_page: ApplicationInteractionEventPageV1 | None = None
                    if not request_has_user_turn(existing_page, request_id):
                        user_page = await assist.publish_conversation_page(
                            principal=record.principal,
                            page=build_user_turn_page(
                                principal=record.principal,
                                product_id=resolved.product_id,
                                conversation_id=conversation_id,
                                request_id=request_id,
                                trace_id=trace_id,
                                event_id=user_event_id,
                                text=body.text,
                                attachments=attachment_metadata,
                            ),
                        )
                    safety_page = build_aviation_safety_turn(
                        principal=record.principal,
                        product_id=resolved.product_id,
                        conversation_id=conversation_id,
                        text=body.text,
                        request_id=request_id,
                        trace_id=trace_id,
                    )
                    if safety_page is not None:
                        assistant_page = await assist.publish_conversation_page(
                            principal=record.principal,
                            page=safety_page,
                        )
                    else:
                        if selected_agent_id:
                            assistant_page = await assist.invoke_agent(
                                principal=record.principal,
                                agent_id=selected_agent_id,
                                conversation_id=conversation_id,
                                text=body.text,
                                request_id=request_id,
                                conversation_context=conversation_context,
                            )
                        else:
                            assistant_page = await assist.invoke(
                                principal=record.principal,
                                conversation_id=conversation_id,
                                text=body.text,
                                request_id=request_id,
                                conversation_context=conversation_context,
                            )
                    page = merge_conversation_pages(
                        existing_page,
                        user_page,
                        assistant_page,
                    )
                except (AgentctlIntegrationError, PlatformCoreError) as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=page.model_dump(mode="json"),
            )
            return page

    @app.get(
        "/api/conversations/{conversation_id}/events",
        response_model=ApplicationInteractionEventPageV1,
    )
    async def replay_events(
        conversation_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        after_cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> ApplicationInteractionEventPageV1:
        if resolved.ai_mode == "fixture":
            page = fixture_pages.get((record.session_id, conversation_id))
            if page is None:
                raise HTTPException(status_code=404, detail="fixture_conversation_not_found")
            return page
        if resolved.ai_mode != "remote":
            raise HTTPException(status_code=503, detail="ai_integration_disabled")
        try:
            return await core.replay_conversation(
                platform_user_token=record.platform_user_token,
                product_id=resolved.product_id,
                conversation_id=conversation_id,
                after_cursor=after_cursor,
            )
        except PlatformCoreError as exc:
            status_code = 410 if str(exc) == "platform_core_http_410" else 502
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @app.get(
        "/api/conversations/{conversation_id}/events/stream",
        response_class=StreamingResponse,
    )
    async def stream_events(
        conversation_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        after_cursor: Annotated[str | None, Query(max_length=512)] = None,
        last_event_id: Annotated[
            str | None,
            Header(alias="Last-Event-ID"),
        ] = None,
    ) -> StreamingResponse:
        cursor = after_cursor or last_event_id
        if resolved.ai_mode == "fixture":
            page = fixture_pages.get((record.session_id, conversation_id))
            if page is None:
                raise HTTPException(status_code=404, detail="fixture_conversation_not_found")
            events = page.events
            if cursor:
                matching = next(
                    (index for index, event in enumerate(events) if event.cursor == cursor),
                    None,
                )
                if matching is None:
                    raise HTTPException(status_code=410, detail="fixture_cursor_expired")
                events = events[matching + 1 :]
        elif resolved.ai_mode == "remote":
            try:
                page = await core.replay_conversation(
                    platform_user_token=record.platform_user_token,
                    product_id=resolved.product_id,
                    conversation_id=conversation_id,
                    after_cursor=cursor,
                )
                events = page.events
            except PlatformCoreError as exc:
                status_code = 410 if str(exc) == "platform_core_http_410" else 502
                raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        else:
            raise HTTPException(status_code=503, detail="ai_integration_disabled")

        def frames() -> Any:
            yield f"retry: {page.heartbeat_after_ms}\n\n"
            for event in events:
                payload = json.dumps(
                    event.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield (f"id: {event.cursor}\nevent: {event.event_type}\ndata: {payload}\n\n")
            heartbeat = json.dumps(
                {
                    "schema_version": ("aios.application_interaction_transport_heartbeat.v1"),
                    "conversation_id": conversation_id,
                    "next_cursor": page.next_cursor,
                    "has_more": page.has_more,
                    "heartbeat_after_ms": page.heartbeat_after_ms,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"event: heartbeat\ndata: {heartbeat}\n\n"

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
                "X-Conversation-Has-More": str(page.has_more).lower(),
            },
        )

    @app.post(
        "/api/approvals/{request_id}/decisions",
        response_model=ApplicationInteractionEventPageV1,
    )
    async def decide_approval(
        request_id: str,
        body: ApprovalDecisionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ApplicationInteractionEventPageV1:
        key = _require_idempotency_key(idempotency_key)
        operation = f"approval.decide:{request_id}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload={
                    "request_id": request_id,
                    **body.model_dump(mode="json"),
                },
            )
            if cached is not None:
                return ApplicationInteractionEventPageV1.model_validate(cached)
            if resolved.ai_mode == "fixture":
                raise HTTPException(
                    status_code=409,
                    detail="fixture_has_no_authoritative_approval",
                )
            if resolved.ai_mode != "remote":
                raise HTTPException(status_code=503, detail="ai_integration_disabled")
            try:
                await core.decide_review(
                    platform_user_token=record.platform_user_token,
                    request_id=request_id,
                    expected_request_version=body.expected_request_version,
                    decision=body.decision,
                )
                result = await core.replay_conversation(
                    platform_user_token=record.platform_user_token,
                    product_id=resolved.product_id,
                    conversation_id=body.conversation_id,
                    after_cursor=None,
                )
            except PlatformCoreError as exc:
                error_code = str(exc)
                if error_code == "platform_core_http_410":
                    status_code = 410
                elif error_code in {"platform_core_http_403", "platform_core_http_409"}:
                    status_code = int(error_code.rsplit("_", 1)[-1])
                else:
                    status_code = 502
                raise HTTPException(status_code=status_code, detail=error_code) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.get(
        "/api/reviews",
        response_model=ReviewInboxProjection,
        response_model_exclude_none=True,
    )
    async def list_reviews(
        record: Annotated[SessionRecord, Depends(require_session)],
        after_cursor: Annotated[str | None, Query(max_length=256)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ReviewInboxProjection:
        try:
            payload = await core.list_review_inbox(
                platform_user_token=record.platform_user_token,
                product_id=resolved.product_id,
                after_cursor=after_cursor,
                limit=limit,
            )
            projection = ReviewInboxProjection.model_validate(payload)
            if any(item.product_id != resolved.product_id for item in projection.items):
                raise ValueError("review inbox crossed product boundary")
            enriched_items = []
            for item in projection.items:
                if item.capability_id != _AGENT_SHARE_CAPABILITY_ID:
                    enriched_items.append(item)
                    continue
                evidence = _agent_share_evidence(item.evidence_refs)
                _validate_agent_share_review_identity(
                    evidence=evidence,
                    request_id=item.request_id,
                    tenant_id=record.principal.tenant_id,
                    requester_actor_id=item.requester_actor_id,
                )
                enriched_items.append(
                    item.model_copy(
                        update={
                            "agent_share": AgentShareReviewContext(
                                agent_id=evidence.agent_id,
                                agent_name=evidence.agent_name,
                                agent_version=evidence.agent_version,
                                reason=evidence.reason,
                                organization_agent_id=evidence.organization_agent_id,
                                skill_ids=list(evidence.skill_ids),
                                knowledge_source_ids=list(evidence.knowledge_source_ids),
                                publication_retry_allowed=_permission_allows(
                                    record.principal.permissions,
                                    "review.decide",
                                ),
                            )
                        }
                    )
                )
            return projection.model_copy(update={"items": enriched_items})
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="platform_core_review_inbox_invalid",
            ) from exc

    @app.get(
        "/api/agent-share-publication-followups",
        response_model=ReviewInboxProjection,
        response_model_exclude_none=True,
    )
    async def list_agent_share_publication_followups(
        record: Annotated[SessionRecord, Depends(require_session)],
        after_cursor: Annotated[str | None, Query(max_length=256)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ReviewInboxProjection:
        try:
            payload = await core.list_review_inbox_by_status(
                platform_user_token=record.platform_user_token,
                product_id=resolved.product_id,
                review_status="allow",
                after_cursor=after_cursor,
                limit=limit,
            )
            projection = ReviewInboxProjection.model_validate(payload)
            if any(item.product_id != resolved.product_id for item in projection.items):
                raise ValueError("review inbox crossed product boundary")
            followups: list[ReviewCardProjection] = []
            for item in projection.items:
                if item.capability_id != _AGENT_SHARE_CAPABILITY_ID:
                    continue
                evidence = _agent_share_evidence(item.evidence_refs)
                _validate_agent_share_review_identity(
                    evidence=evidence,
                    request_id=item.request_id,
                    tenant_id=record.principal.tenant_id,
                    requester_actor_id=item.requester_actor_id,
                )
                publication = await assist.get_organization_share_publication(
                    principal=record.principal,
                    organization_agent_id=evidence.organization_agent_id,
                    review_request_id=item.request_id,
                    source_snapshot_digest=evidence.snapshot_digest,
                )
                if publication is not None:
                    continue
                followups.append(
                    item.model_copy(
                        update={
                            "agent_share": AgentShareReviewContext(
                                agent_id=evidence.agent_id,
                                agent_name=evidence.agent_name,
                                agent_version=evidence.agent_version,
                                reason=evidence.reason,
                                organization_agent_id=evidence.organization_agent_id,
                                skill_ids=list(evidence.skill_ids),
                                knowledge_source_ids=list(evidence.knowledge_source_ids),
                                publication_retry_allowed=_permission_allows(
                                    record.principal.permissions,
                                    "review.decide",
                                ),
                            )
                        }
                    )
                )
            return projection.model_copy(update={"items": followups, "count": len(followups)})
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        except AgentctlIntegrationError as exc:
            raise _agent_management_http_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="agent_share_followup_invalid",
            ) from exc

    @app.get("/api/work-items", response_model=WorkItemPageProjection)
    async def list_work_items(
        record: Annotated[SessionRecord, Depends(require_session)],
        after_cursor: Annotated[str | None, Query(max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> WorkItemPageProjection:
        try:
            payload = await core.list_application_work_items(
                platform_user_token=record.platform_user_token,
                product_id=resolved.product_id,
                after_cursor=after_cursor,
                limit=limit,
            )
            projection = WorkItemPageProjection.model_validate(payload)
            if (
                projection.tenant_id != record.principal.tenant_id
                or projection.subject_user_id != record.principal.user_id
                or projection.product_id != resolved.product_id
                or any(
                    item.tenant_id != record.principal.tenant_id
                    or item.subject_user_id != record.principal.user_id
                    or item.product_id != resolved.product_id
                    for item in projection.items
                )
            ):
                raise ValueError("work item page crossed identity boundary")
            return projection
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="platform_core_work_item_page_invalid",
            ) from exc

    @app.get("/api/drive/files", response_model=list[DriveFileProjection])
    async def drive_files(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[DriveFileProjection]:
        try:
            payload = await core.list_storage_files(record.platform_user_token)
            return project_drive_files(payload)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.post(
        "/api/drive/uploads",
        response_model=DriveUploadSessionProjection,
        status_code=201,
    )
    async def create_drive_upload(
        body: DriveUploadCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> DriveUploadSessionProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = "drive.upload.create"
        request_payload = {
            "scope": body.scope,
            "name": body.name,
            "content_type": body.content_type,
            "size_bytes": body.size_bytes,
            "checksum": body.checksum,
            "sensitivity": body.sensitivity,
        }
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=request_payload,
            )
            if cached is not None:
                return DriveUploadSessionProjection.model_validate(cached)
            try:
                payload = await core.create_storage_upload_session(
                    platform_user_token=record.platform_user_token,
                    body={
                        "scope": body.scope,
                        "name": body.name,
                        "content_type": body.content_type,
                        "size_bytes": body.size_bytes,
                        "checksum": body.checksum,
                        "metadata": {
                            "sensitivity": body.sensitivity,
                            "source_system": "CAPlatform",
                        },
                    },
                    idempotency_key=key,
                )
            except PlatformCoreError as exc:
                raise _core_http_exception(exc) from exc
            result = DriveUploadSessionProjection(
                session_id=str(payload.get("session_id") or ""),
                status=str(payload.get("status") or "pending"),
                upload_method=str(payload.get("upload_method") or "POST"),
                expires_at=str(payload.get("expires_at") or ""),
                trace_id=str(payload.get("trace_id") or ""),
            )
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.put("/api/drive/uploads/{session_id}/content", status_code=204)
    async def upload_drive_content(
        session_id: str,
        request: Request,
        record: Annotated[SessionRecord, Depends(require_session)],
        content_type: str | None = Header(default=None, alias="Content-Type"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> None:
        key = _require_idempotency_key(idempotency_key)
        try:
            await core.upload_storage_content(
                platform_user_token=record.platform_user_token,
                session_id=session_id,
                content=request.stream(),
                content_type=content_type or "application/octet-stream",
                idempotency_key=key,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.post(
        "/api/drive/uploads/{session_id}/commit",
        response_model=DriveFileProjection,
        status_code=201,
    )
    async def commit_drive_upload(
        session_id: str,
        body: DriveUploadCommitRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> DriveFileProjection:
        key = _require_idempotency_key(idempotency_key)
        try:
            payload = await core.commit_storage_upload_session(
                platform_user_token=record.platform_user_token,
                session_id=session_id,
                checksum=body.checksum,
                idempotency_key=key,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        projected = project_drive_files({"files": [payload]})
        if not projected:
            raise HTTPException(status_code=502, detail="platform_core_invalid_storage_object")
        return projected[0]

    @app.get(
        "/api/drive/files/{object_id}",
        response_model=DriveFileProjection,
    )
    async def drive_file(
        object_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> DriveFileProjection:
        try:
            payload = await core.get_storage_file(
                platform_user_token=record.platform_user_token,
                object_id=object_id,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        projected = project_drive_files({"files": [payload]})
        if not projected:
            raise HTTPException(status_code=502, detail="platform_core_invalid_storage_object")
        return projected[0]

    @app.get(
        "/api/drive/files/{object_id}/preview",
        response_model=DriveFilePreviewProjection,
    )
    async def preview_drive_file(
        object_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> DriveFilePreviewProjection:
        try:
            payload = await core.preview_storage_file(
                platform_user_token=record.platform_user_token,
                object_id=object_id,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        return DriveFilePreviewProjection(
            object_ref=f"obj://core/{object_id}",
            name=str(payload.get("name") or ""),
            mime=str(payload.get("content_type") or "application/octet-stream"),
            preview_kind=str(payload.get("preview_kind") or "unsupported"),
            text=(str(payload["text"]) if payload.get("text") is not None else None),
            truncated=bool(payload.get("truncated")),
            limit=int(payload.get("limit") or 0),
            trace_id=str(payload.get("trace_id") or ""),
        )

    @app.post(
        "/api/drive/files/{object_id}/download-url",
        response_model=DriveSignedDownloadProjection,
    )
    async def create_drive_download_url(
        object_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> DriveSignedDownloadProjection:
        try:
            payload = await core.create_storage_download_url(
                platform_user_token=record.platform_user_token,
                object_id=object_id,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        signed_url = str(payload.get("signed_url") or "")
        signed_url = _public_platform_url(
            signed_url,
            internal_base_url=resolved.platform_core_base_url,
            public_base_url=resolved.platform_core_public_base_url,
        )
        return DriveSignedDownloadProjection(
            object_ref=f"obj://core/{object_id}",
            signed_url=signed_url,
            method=str(payload.get("method") or "GET"),
            expires_at=str(payload.get("expires_at") or ""),
            trace_id=str(payload.get("trace_id") or ""),
        )

    @app.get(
        "/api/knowledge/sources",
        response_model=list[KnowledgeSourceProjection],
    )
    async def knowledge_sources(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[KnowledgeSourceProjection]:
        try:
            payload = await core.list_knowledge_sources(record.platform_user_token)
            return project_knowledge_sources(payload)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.post(
        "/api/knowledge/sources",
        response_model=KnowledgeSourceProjection,
    )
    async def create_knowledge_source(
        body: KnowledgeSourceCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> KnowledgeSourceProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = "knowledge.source.create"
        request_payload = body.model_dump(mode="json")
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=request_payload,
            )
            if cached is not None:
                return KnowledgeSourceProjection.model_validate(cached)
            try:
                payload = await core.create_knowledge_source(
                    platform_user_token=record.platform_user_token,
                    body={
                        "storage_object_id": body.object_ref.removeprefix("obj://core/"),
                        "title": body.title,
                        "resource_scope": ("organization" if body.scope == "org" else "personal"),
                        "version": body.version,
                        "effective_at": (
                            body.effective_at.isoformat() if body.effective_at else None
                        ),
                        "expires_at": body.expires_at.isoformat() if body.expires_at else None,
                    },
                )
                result = _project_knowledge_lifecycle(payload)
            except PlatformCoreError as exc:
                raise _core_http_exception(exc) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.patch(
        "/api/knowledge/sources/{source_id}",
        response_model=KnowledgeSourceProjection,
    )
    async def update_knowledge_source(
        source_id: str,
        body: KnowledgeSourceUpdateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> KnowledgeSourceProjection:
        _require_idempotency_key(idempotency_key)
        try:
            payload = await core.update_knowledge_source(
                platform_user_token=record.platform_user_token,
                source_id=source_id,
                body=body.model_dump(mode="json", exclude_unset=True),
            )
            return _project_knowledge_lifecycle(payload)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.post(
        "/api/knowledge/sources/{source_id}/reindex",
        response_model=KnowledgeSourceProjection,
    )
    async def reindex_knowledge_source(
        source_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> KnowledgeSourceProjection:
        _require_idempotency_key(idempotency_key)
        try:
            payload = await core.reindex_knowledge_source(
                platform_user_token=record.platform_user_token,
                source_id=source_id,
            )
            return _project_knowledge_lifecycle(payload)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.delete(
        "/api/knowledge/sources/{source_id}",
        status_code=204,
    )
    async def delete_knowledge_source(
        source_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> None:
        _require_idempotency_key(idempotency_key)
        try:
            await core.delete_knowledge_source(
                platform_user_token=record.platform_user_token,
                source_id=source_id,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.post(
        "/api/knowledge/query",
        response_model=KnowledgeSearchProjection,
    )
    async def query_knowledge(
        body: KnowledgeSearchRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> KnowledgeSearchProjection:
        try:
            payload = await core.query_knowledge(
                platform_user_token=record.platform_user_token,
                query=body.query,
                source_ids=body.source_ids,
                top_k=body.top_k,
            )
            return project_knowledge_search(payload)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.get("/api/apps", response_model=list[AppProjection])
    async def apps(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[AppProjection]:
        try:
            payload = await core.list_apps(record.platform_user_token)
            pinned_app_ids = await preferences.pinned_app_ids(
                tenant_id=record.principal.tenant_id,
                user_id=record.principal.user_id,
            )
            return project_apps(payload, record.principal, pinned_app_ids)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.put("/api/preferences/apps/{app_id}/pin", status_code=204)
    async def pin_app(
        app_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> None:
        _require_idempotency_key(idempotency_key)
        try:
            visible_apps = project_apps(
                await core.list_apps(record.platform_user_token),
                record.principal,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        visible = next((item for item in visible_apps if item.id == app_id), None)
        if visible is None or not visible.tenant_enabled or not visible.user_granted:
            raise HTTPException(status_code=403, detail="app_not_available")
        await preferences.pin_app(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            app_id=app_id,
        )

    @app.delete("/api/preferences/apps/{app_id}/pin", status_code=204)
    async def unpin_app(
        app_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> None:
        _require_idempotency_key(idempotency_key)
        await preferences.unpin_app(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            app_id=app_id,
        )

    @app.get(
        "/api/tool-packages",
        response_model=list[ToolPackageProjection],
    )
    async def tool_packages(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[ToolPackageProjection]:
        try:
            payload = await core.list_tool_packages(record.platform_user_token)
            return project_tool_packages(payload)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.get(
        "/api/data-structuring/readiness",
        response_model=DataStructuringReadinessProjection,
    )
    async def data_structuring_readiness(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> DataStructuringReadinessProjection:
        return await tool_package_readiness.inspect(
            platform_user_token=record.platform_user_token,
            tenant_id=record.principal.tenant_id,
            principal=record.principal,
        )

    @app.post(
        "/api/data-structuring/invoke",
        response_model=DataStructuringInvocationProjection,
    )
    async def invoke_data_structuring_tool(
        body: DataStructuringInvokeRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> DataStructuringInvocationProjection:
        try:
            context = await core.issue_tool_package_runtime_context(
                platform_user_token=record.platform_user_token,
                package_id=body.package_id,
                product_id=resolved.product_id,
                capability_id=body.capability_id,
                user_confirmation=body.user_confirmation,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        signed_context = context.get("platform_tool_context")
        if (
            context.get("tenant_id") != record.principal.tenant_id
            or context.get("caller_user_id") != record.principal.user_id
            or context.get("package_id") != body.package_id
            or context.get("capability_id") != body.capability_id
            or not isinstance(context.get("pinned_version"), str)
            or not isinstance(context.get("pinned_digest"), str)
            or not isinstance(context.get("grant_id"), str)
            or not isinstance(signed_context, dict)
        ):
            raise HTTPException(
                status_code=502,
                detail="platform_tool_package_context_contract_mismatch",
            )
        try:
            result = await assist.invoke_tool_package(
                principal=record.principal,
                package_id=body.package_id,
                capability_id=body.capability_id,
                input_payload=body.input,
                platform_tool_context=signed_context,
            )
        except AgentctlIntegrationError as exc:
            raise _agentctl_http_exception(exc) from exc
        if (
            result.get("pinned_version") != context["pinned_version"]
            or result.get("pinned_digest") != context["pinned_digest"]
            or result.get("grant_id") != context["grant_id"]
            or result.get("execution_mode") != "explicit_toolhost_only"
        ):
            raise HTTPException(
                status_code=502,
                detail="agentctl_tool_package_result_contract_mismatch",
            )
        return DataStructuringInvocationProjection.model_validate(
            {
                "status": "completed",
                "package_id": result["package_id"],
                "capability_id": result["capability_id"],
                "pinned_version": result["pinned_version"],
                "pinned_digest": result["pinned_digest"],
                "grant_id": result["grant_id"],
                "output": result["output"],
                "latency_ms": result.get("latency_ms"),
                "execution_mode": result["execution_mode"],
            }
        )

    @app.post(
        "/api/project-planning/sessions",
        response_model=ProjectPlanningSessionProjection,
        status_code=201,
    )
    async def create_project_planning_session(
        body: ProjectPlanningStartRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ProjectPlanningSessionProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = "project_planning.session.create"
        request_payload = body.model_dump(mode="json")
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=request_payload,
            )
            if cached is not None:
                return ProjectPlanningSessionProjection.model_validate(cached)
            try:
                session = await projects.create_planning_session(
                    platform_user_token=record.platform_user_token,
                    body={
                        "project_name": body.project_name,
                        "project_goal": body.project_goal,
                        "project_scope": body.project_scope,
                        "industry": body.industry,
                        "metadata": {
                            "source_mode": body.mode,
                            "start_at": body.start_at.isoformat() if body.start_at else None,
                            "end_at": body.end_at.isoformat() if body.end_at else None,
                            "launch_idempotency_key": key,
                            "requested_by": record.principal.user_id,
                        },
                    },
                )
                source_document_id = None
                parse_ref = None
                if body.mode == "file":
                    object_id = _platform_object_id(body.object_ref)
                    storage = await core.get_storage_file(
                        platform_user_token=record.platform_user_token,
                        object_id=object_id,
                    )
                    content = await core.read_storage_content(
                        platform_user_token=record.platform_user_token,
                        object_id=object_id,
                    )
                    parse_trace = f"project-parse-{uuid4().hex}"
                    parsed = await core.parse_document(
                        platform_user_token=record.platform_user_token,
                        file_name=body.file_name or str(storage.get("name") or "项目文件"),
                        media_type=body.media_type
                        or str(storage.get("content_type") or "application/octet-stream"),
                        file_base64=base64.b64encode(content).decode("ascii"),
                        trace_id=parse_trace,
                    )
                    parsed_text = _parsecore_text(parsed)
                    if not parsed_text:
                        raise HTTPException(
                            status_code=422,
                            detail="project_source_parse_returned_no_text",
                        )
                    document = await projects.add_planning_document(
                        platform_user_token=record.platform_user_token,
                        session_id=session.session_id,
                        body={
                            "name": body.file_name or str(storage.get("name") or "项目文件"),
                            "content": parsed_text,
                            "source_type": "platform_parsecore",
                            "content_type": "text/plain",
                            "source_uri": body.object_ref,
                            "metadata": {
                                "storage_object_ref": body.object_ref,
                                "parse_doc_id": parsed.get("doc_id"),
                                "parse_trace_id": parsed.get("trace_id") or parse_trace,
                            },
                        },
                    )
                    source_document_id = str(document.get("id") or "") or None
                    parse_ref = str(parsed.get("doc_id") or "") or None
                elif body.instruction:
                    document = await projects.add_planning_document(
                        platform_user_token=record.platform_user_token,
                        session_id=session.session_id,
                        body={
                            "name": "用户启动说明",
                            "content": body.instruction,
                            "source_type": "user_instruction",
                            "content_type": "text/plain",
                            "metadata": {"requested_by": record.principal.user_id},
                        },
                    )
                    source_document_id = str(document.get("id") or "") or None
                await projects.build_planning_context(
                    platform_user_token=record.platform_user_token,
                    session_id=session.session_id,
                )
                result = ProjectPlanningSessionProjection(
                    session_id=session.session_id,
                    status="CONTEXT_READY",
                    project_name=session.project_name,
                    project_goal=session.project_goal,
                    source_mode=body.mode,
                    source_document_id=source_document_id,
                    parse_ref=parse_ref,
                )
            except (AIProjectOpsError, PlatformCoreError) as exc:
                if isinstance(exc, AIProjectOpsError):
                    raise _external_product_http_exception(exc) from exc
                raise _core_http_exception(exc) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.post(
        "/api/project-planning/sessions/{session_id}/generate",
        response_model=ProjectPlanningJobProjection,
        status_code=202,
    )
    async def generate_project_blueprint(
        session_id: str,
        body: ProjectPlanningGenerateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ProjectPlanningJobProjection:
        key = _require_idempotency_key(idempotency_key)
        trace_id = f"project-plan-{uuid4().hex}"
        try:
            planning_session = await projects.planning_session(
                platform_user_token=record.platform_user_token,
                session_id=session_id,
            )
            capability_query = "\n".join(
                part
                for part in (
                    str(planning_session.get("project_name") or "").strip(),
                    str(planning_session.get("project_goal") or "").strip(),
                    str(planning_session.get("project_scope") or "").strip(),
                    str(body.instruction or "").strip(),
                    "项目执行所需的专家、技能、工具、知识检索、文档处理和自动化能力",
                )
                if part
            )
            capability_catalog = await assist.search_capabilities(
                principal=record.principal,
                query=capability_query,
                top_n=12,
            )
            preflight = await assist.preflight_capability(
                principal=record.principal,
                capability_id="ca.project.generate_plan",
                work_item_id=f"project-plan:{session_id}:{key}",
            )
            if preflight.get("allowed") is not True:
                raise HTTPException(status_code=403, detail="project_planning_preflight_blocked")
            invocation = {
                "schema_version": "aios.capability_invocation.v0.1",
                "invocation_id": f"project-plan-{key}",
                "request_id": f"project-plan:{session_id}",
                "work_item_id": f"project-plan:{session_id}:{key}",
                "capability_id": "ca.project.generate_plan",
                "capability_version": "1.0.0",
                "validated_arguments": {
                    "session_id": session_id,
                    "base_draft_id": body.base_draft_id,
                    "instruction": body.instruction,
                    "planning_profile": body.planning_profile,
                    "capability_catalog": capability_catalog,
                },
                "idempotency_key": key,
                "policy_decision_ref": {
                    "schema_version": "caplatform.explicit_user_confirmation.v1",
                    "decision": "allow",
                    "confirmed_by_user_id": record.principal.user_id,
                    "confirmed_at": datetime.now(UTC).isoformat(),
                    "surface": "project_start_wizard",
                },
                "context_ref": {"kind": "aiprojectops_planning_session", "session_id": session_id},
                "expected_artifact_types": ["project_blueprint_draft"],
                "trace_id": trace_id,
                "metadata": {
                    "tenant_id": record.principal.tenant_id,
                    "actor_user_id": record.principal.user_id,
                    "source_product": "civil_aviation_workbench",
                    "confirmation_mode": "explicit",
                },
            }
            invoked = await assist.invoke_capability(
                principal=record.principal,
                invocation=invocation,
            )
        except AgentctlInvocationOutcomeUnknown as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc
        except (AgentctlCapabilityRejected, AgentctlIntegrationError, PlatformCoreError) as exc:
            raise _agentctl_http_exception(exc) from exc
        output = invoked.get("output")
        job_ref = output.get("job_ref") if isinstance(output, dict) else None
        if not isinstance(job_ref, dict):
            raise HTTPException(status_code=502, detail="project_planning_job_ref_missing")
        return ProjectPlanningJobProjection(
            job_id=str(job_ref.get("job_id") or ""),
            status=_project_planning_job_status(job_ref.get("status"), default="queued"),
            session_id=session_id,
            result_draft_id=None,
            progress=float(job_ref.get("progress") or 0),
            trace_id=trace_id,
        )

    @app.get(
        "/api/project-planning/jobs/{job_id}",
        response_model=ProjectPlanningJobProjection,
    )
    async def project_planning_job(
        job_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectPlanningJobProjection:
        try:
            return await projects.planning_job(
                platform_user_token=record.platform_user_token,
                job_id=job_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post(
        "/api/project-planning/jobs/{job_id}/cancel",
        response_model=ProjectPlanningJobProjection,
    )
    async def cancel_project_planning_job(
        job_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ProjectPlanningJobProjection:
        key = _require_idempotency_key(idempotency_key)
        try:
            current = await projects.planning_job(
                platform_user_token=record.platform_user_token,
                job_id=job_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc
        if current.status not in {"queued", "running"}:
            return current

        trace_id = f"project-plan-cancel-{uuid4().hex}"
        try:
            preflight = await assist.preflight_capability(
                principal=record.principal,
                capability_id="ca.project.generate_plan.cancel",
                work_item_id=f"project-plan-cancel:{job_id}:{key}",
            )
            if preflight.get("allowed") is not True:
                raise HTTPException(
                    status_code=403,
                    detail="project_planning_cancel_preflight_blocked",
                )
            invoked = await assist.invoke_capability(
                principal=record.principal,
                invocation={
                    "schema_version": "aios.capability_invocation.v0.1",
                    "invocation_id": f"project-plan-cancel-{key}",
                    "request_id": f"project-plan-cancel:{job_id}",
                    "work_item_id": f"project-plan-cancel:{job_id}:{key}",
                    "capability_id": "ca.project.generate_plan.cancel",
                    "capability_version": "1.0.0",
                    "validated_arguments": {"job_id": job_id},
                    "idempotency_key": key,
                    "policy_decision_ref": {
                        "schema_version": "caplatform.explicit_user_confirmation.v1",
                        "decision": "allow",
                        "confirmed_by_user_id": record.principal.user_id,
                        "confirmed_at": datetime.now(UTC).isoformat(),
                        "surface": "project_start_wizard",
                    },
                    "context_ref": {"kind": "aiprojectops_planning_job", "job_id": job_id},
                    "expected_artifact_types": [],
                    "trace_id": trace_id,
                    "metadata": {
                        "tenant_id": record.principal.tenant_id,
                        "actor_user_id": record.principal.user_id,
                        "source_product": "civil_aviation_workbench",
                        "confirmation_mode": "explicit",
                    },
                },
            )
        except AgentctlInvocationOutcomeUnknown as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (AgentctlCapabilityRejected, AgentctlIntegrationError, PlatformCoreError) as exc:
            raise _agentctl_http_exception(exc) from exc
        output = invoked.get("output")
        job_ref = output.get("job_ref") if isinstance(output, dict) else None
        if not isinstance(job_ref, dict):
            raise HTTPException(status_code=502, detail="project_planning_job_ref_missing")
        return ProjectPlanningJobProjection(
            job_id=job_id,
            status=_project_planning_job_status(job_ref.get("status"), default="cancelled"),
            session_id=current.session_id,
            result_draft_id=current.result_draft_id,
            progress=float(job_ref.get("progress") or current.progress),
            trace_id=trace_id,
        )

    @app.get(
        "/api/project-planning/blueprints/{draft_id}",
        response_model=ProjectBlueprintProjection,
    )
    async def project_blueprint(
        draft_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectBlueprintProjection:
        try:
            return await projects.project_blueprint(
                platform_user_token=record.platform_user_token,
                draft_id=draft_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.put(
        "/api/project-planning/blueprints/{draft_id}",
        response_model=ProjectBlueprintProjection,
    )
    async def update_project_blueprint(
        draft_id: str,
        body: ProjectBlueprintUpdateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectBlueprintProjection:
        try:
            return await projects.update_project_blueprint(
                platform_user_token=record.platform_user_token,
                draft_id=draft_id,
                body=body.model_dump(mode="json"),
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post(
        "/api/project-planning/blueprints/{draft_id}/approve",
        response_model=ProjectBlueprintProjection,
    )
    async def approve_project_blueprint(
        draft_id: str,
        body: ProjectBlueprintDecisionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectBlueprintProjection:
        try:
            return await projects.approve_project_blueprint(
                platform_user_token=record.platform_user_token,
                draft_id=draft_id,
                expected_revision=body.expected_revision,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post(
        "/api/project-planning/blueprints/{draft_id}/reject",
        response_model=ProjectBlueprintProjection,
    )
    async def reject_project_blueprint(
        draft_id: str,
        body: ProjectBlueprintDecisionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectBlueprintProjection:
        try:
            return await projects.reject_project_blueprint(
                platform_user_token=record.platform_user_token,
                draft_id=draft_id,
                expected_revision=body.expected_revision,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post(
        "/api/project-planning/blueprints/{draft_id}/sync",
        response_model=ProjectBlueprintSyncProjection,
    )
    async def sync_project_blueprint(
        draft_id: str,
        body: ProjectBlueprintDecisionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectBlueprintSyncProjection:
        try:
            return await projects.sync_project_blueprint(
                platform_user_token=record.platform_user_token,
                draft_id=draft_id,
                expected_revision=body.expected_revision,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/projects", response_model=list[ProjectProjection])
    async def list_projects(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[ProjectProjection]:
        try:
            return await projects.list_projects(record.platform_user_token)
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/project-portfolio/workbench")
    async def project_portfolio_workbench(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await projects.portfolio_workbench(record.platform_user_token)
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/project-portfolio/maturity")
    async def project_portfolio_maturity(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await projects.portfolio_maturity(record.platform_user_token)
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/project-portfolio/resources")
    async def project_portfolio_resources(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await projects.portfolio_resources(record.platform_user_token)
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/project-portfolio/recommendations")
    async def project_portfolio_recommendations(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await projects.portfolio_recommendations(record.platform_user_token)
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/project-portfolio/search")
    async def project_portfolio_search(
        record: Annotated[SessionRecord, Depends(require_session)],
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return await projects.portfolio_search(
                platform_user_token=record.platform_user_token,
                query=q,
                limit=limit,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/projects/{project_id}/risks")
    async def project_risks(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        try:
            return await projects.project_risks(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/projects/{project_id}/risks", status_code=201)
    async def create_project_risk(
        project_id: str,
        body: dict[str, Any],
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await projects.create_project_risk(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
                body=body,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/projects/{project_id}/schedule-insights")
    async def project_schedule_insights(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await projects.project_schedule_insights(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/projects/{project_id}/historical-recommendations")
    async def project_historical_recommendations(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        task_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await projects.historical_recommendations(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
                task_id=task_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get(
        "/api/projects/{project_id}/tree",
        response_model=ProjectTreeProjection,
    )
    async def project_tree(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectTreeProjection:
        try:
            return await projects.project_tree(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get(
        "/api/projects/{project_id}/activities",
        response_model=ProjectActivityPageProjection,
    )
    async def project_activities(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectActivityPageProjection:
        try:
            return await projects.project_activities(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get(
        "/api/projects/{project_id}/operations/dashboard",
        response_model=ProjectOperationsDashboardProjection,
        response_model_by_alias=True,
    )
    async def project_operations_dashboard(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectOperationsDashboardProjection:
        try:
            return await projects.project_operations_dashboard(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get(
        "/api/projects/{project_id}/agent-policy",
        response_model=ProjectAgentPolicyProjection,
        response_model_by_alias=True,
    )
    async def project_agent_policy(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectAgentPolicyProjection:
        try:
            return await projects.project_agent_policy(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.put(
        "/api/projects/{project_id}/agent-policy",
        response_model=ProjectAgentPolicyProjection,
        response_model_by_alias=True,
    )
    async def update_project_agent_policy(
        project_id: str,
        body: ProjectAgentPolicyUpdateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectAgentPolicyProjection:
        try:
            return await projects.update_project_agent_policy(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
                body=body.model_dump(mode="json"),
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post(
        "/api/projects/{project_id}/operations/reports",
        response_model=ProjectOperationalReportProjection,
        response_model_by_alias=True,
        status_code=201,
    )
    async def generate_project_operational_report(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectOperationalReportProjection:
        try:
            return await projects.generate_project_report(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
                report_type="WEEKLY",
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/projects/{project_id}/operations/scan")
    async def scan_project_operations(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await projects.scan_project_operations(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get(
        "/api/projects/{project_id}/notification-preferences/me",
        response_model=ProjectNotificationPreferenceProjection,
        response_model_by_alias=True,
    )
    async def project_notification_preference(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectNotificationPreferenceProjection:
        try:
            return await projects.project_notification_preference(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.put(
        "/api/projects/{project_id}/notification-preferences/me",
        response_model=ProjectNotificationPreferenceProjection,
        response_model_by_alias=True,
    )
    async def update_project_notification_preference(
        project_id: str,
        body: ProjectNotificationPreferenceUpdateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectNotificationPreferenceProjection:
        try:
            return await projects.update_project_notification_preference(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
                body=body.model_dump(mode="json"),
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get(
        "/api/projects/{project_id}/workspace-context",
        response_model=ProjectWorkspaceContextProjection,
    )
    async def project_workspace_context(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectWorkspaceContextProjection:
        try:
            return await projects.project_workspace_context(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get(
        "/api/tasks/{task_id}/workspace",
        response_model=TaskWorkspaceProjection,
        response_model_by_alias=True,
    )
    async def task_workspace(
        task_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> TaskWorkspaceProjection:
        try:
            return await projects.task_workspace(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/tasks/{task_id}/deliverable-submissions", status_code=201)
    async def create_task_deliverable_submission(
        task_id: str,
        body: TaskDeliverableSubmissionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        try:
            return await projects.create_deliverable_submission(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
                body={
                    **body.model_dump(mode="json", exclude_none=True),
                    "submitted_by": record.principal.user_id,
                },
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/deliverables/{deliverable_id}/acceptance")
    async def decide_deliverable_acceptance(
        deliverable_id: str,
        body: DeliverableAcceptanceRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        try:
            return await projects.decide_deliverable_acceptance(
                platform_user_token=record.platform_user_token,
                deliverable_id=deliverable_id,
                body={
                    **body.model_dump(mode="json", exclude_none=True),
                    "reviewer_ref": record.principal.user_id,
                },
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/deliverables/{deliverable_id}/verification")
    async def verify_deliverable(
        deliverable_id: str,
        body: DeliverableVerificationRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        try:
            return await projects.verify_deliverable(
                platform_user_token=record.platform_user_token,
                deliverable_id=deliverable_id,
                body={
                    "payload": body.payload,
                    "verified_by": record.principal.user_id,
                },
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/projects/{project_id}/acceptance-queue")
    async def list_project_acceptance_queue(
        project_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        try:
            return await projects.acceptance_queue(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/deliverables/batch-acceptance")
    async def batch_deliverable_acceptance(
        body: BatchDeliverableAcceptanceRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> list[dict[str, Any]]:
        try:
            return await projects.batch_deliverable_acceptance(
                platform_user_token=record.platform_user_token,
                body=body.model_dump(mode="json", exclude_none=True),
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post(
        "/api/tasks/{task_id}/contents",
        response_model=TaskWorkspaceContentProjection,
        response_model_by_alias=True,
        status_code=201,
    )
    async def link_task_content(
        task_id: str,
        body: TaskContentLinkCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> TaskWorkspaceContentProjection:
        try:
            return await projects.link_task_content(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
                idempotency_key=idempotency_key,
                body={
                    "resource_type": "ASSET",
                    "resource_id": body.object_ref,
                    "download_ref": body.object_ref,
                    "title": body.title,
                    "mime_type": body.mime_type,
                    "role": body.role,
                    "version": body.version,
                    "visibility": body.visibility,
                },
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.patch(
        "/api/tasks/{task_id}/contents/{content_id}",
        response_model=TaskWorkspaceContentProjection,
        response_model_by_alias=True,
    )
    async def update_task_content(
        task_id: str,
        content_id: str,
        body: TaskContentUpdateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> TaskWorkspaceContentProjection:
        try:
            return await projects.update_task_content(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
                content_id=content_id,
                idempotency_key=idempotency_key,
                body=body.model_dump(exclude_unset=True),
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.delete("/api/tasks/{task_id}/contents/{content_id}", status_code=204)
    async def unlink_task_content(
        task_id: str,
        content_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> None:
        try:
            await projects.unlink_task_content(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
                content_id=content_id,
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.get("/api/tasks/{task_id}/contents/{content_id}/versions")
    async def task_content_versions(
        task_id: str,
        content_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[dict[str, Any]]:
        try:
            return await projects.task_content_versions(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
                content_id=content_id,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.patch("/api/tasks/batch")
    async def batch_update_tasks(
        body: TaskBatchUpdateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> list[dict[str, Any]]:
        try:
            return await projects.batch_update_tasks(
                platform_user_token=record.platform_user_token,
                idempotency_key=idempotency_key,
                body=body.model_dump(mode="json", exclude_unset=True),
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/tasks/{task_id}/assignments/replace")
    async def replace_task_assignments(
        task_id: str,
        body: TaskAssignmentsReplaceRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        try:
            return await projects.replace_task_assignments(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
                body=body.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/tasks/{task_id}/actions/{action}")
    async def run_task_action(
        task_id: str,
        action: Literal[
            "claim",
            "complete",
            "start_ai",
            "start",
            "pause",
            "resume",
            "submit",
            "return",
        ],
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        body: HumanTaskActionRequest | None = None,
    ) -> dict[str, Any]:
        if action == "return" and (body is None or not (body.reason or "").strip()):
            raise HTTPException(status_code=422, detail="task_return_reason_required")
        if action == "start_ai":
            trace_id = f"project-execution-{uuid4().hex}"
            try:
                preflight = await assist.preflight_capability(
                    principal=record.principal,
                    capability_id="ca.project.execute_task",
                    work_item_id=f"project-execution:{task_id}:{idempotency_key}",
                )
                if preflight.get("allowed") is not True:
                    raise HTTPException(
                        status_code=403,
                        detail="project_execution_preflight_blocked",
                    )
                invoked = await assist.invoke_capability(
                    principal=record.principal,
                    invocation={
                        "schema_version": "aios.capability_invocation.v0.1",
                        "invocation_id": f"project-execution-{idempotency_key}",
                        "request_id": f"project-execution:{task_id}",
                        "work_item_id": f"project-execution:{task_id}:{idempotency_key}",
                        "capability_id": "ca.project.execute_task",
                        "capability_version": "1.0.0",
                        "validated_arguments": {"task_id": task_id},
                        "idempotency_key": idempotency_key,
                        "policy_decision_ref": {
                            "schema_version": "caplatform.explicit_user_confirmation.v1",
                            "decision": "allow",
                            "confirmed_by_user_id": record.principal.user_id,
                            "confirmed_at": datetime.now(UTC).isoformat(),
                            "surface": "project_task_workspace",
                        },
                        "context_ref": {
                            "kind": "aiprojectops_task",
                            "task_id": task_id,
                        },
                        "expected_artifact_types": ["task_execution_output"],
                        "trace_id": trace_id,
                        "metadata": {
                            "tenant_id": record.principal.tenant_id,
                            "actor_user_id": record.principal.user_id,
                            "source_product": "civil_aviation_workbench",
                            "confirmation_mode": "explicit",
                        },
                    },
                )
            except AgentctlInvocationOutcomeUnknown as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except (AgentctlCapabilityRejected, AgentctlIntegrationError) as exc:
                raise _agentctl_http_exception(exc) from exc
            output = invoked.get("output")
            job_ref = output.get("job_ref") if isinstance(output, dict) else None
            if not isinstance(job_ref, dict) or not str(job_ref.get("job_id") or ""):
                raise HTTPException(status_code=502, detail="project_execution_job_ref_missing")
            return {
                "task_id": task_id,
                "action": "start_ai",
                "state": "RUNNING",
                "execution_run_id": str(job_ref["job_id"]),
                "agent_run_id": job_ref.get("provider_run_id"),
                "job_ref": job_ref,
                "trace_id": trace_id,
            }
        try:
            return await projects.task_action(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
                action=action,
                body=body.model_dump(mode="json", exclude_none=True) if body else {},
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/tasks/{task_id}/time-logs", status_code=201)
    async def create_task_time_log(
        task_id: str,
        body: TaskTimeLogCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        try:
            return await projects.create_task_time_log(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
                body=body.model_dump(mode="json", exclude_none=True),
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/tasks/{task_id}/replan-drafts/{draft_id}/{decision}")
    async def decide_task_replan_draft(
        task_id: str,
        draft_id: str,
        decision: Literal["approve", "reject"],
        body: HumanTaskActionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        try:
            return await projects.decide_task_replan_draft(
                platform_user_token=record.platform_user_token,
                task_id=task_id,
                draft_id=draft_id,
                decision=decision,
                reason=body.reason,
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/tasks/{task_id}/executions/{execution_id}/cancel")
    async def cancel_project_task_execution(
        task_id: str,
        execution_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        trace_id = f"project-execution-cancel-{uuid4().hex}"
        try:
            preflight = await assist.preflight_capability(
                principal=record.principal,
                capability_id="ca.project.execute_task.cancel",
                work_item_id=f"project-execution-cancel:{execution_id}:{idempotency_key}",
            )
            if preflight.get("allowed") is not True:
                raise HTTPException(
                    status_code=403,
                    detail="project_execution_cancel_preflight_blocked",
                )
            invoked = await assist.invoke_capability(
                principal=record.principal,
                invocation={
                    "schema_version": "aios.capability_invocation.v0.1",
                    "invocation_id": f"project-execution-cancel-{idempotency_key}",
                    "request_id": f"project-execution-cancel:{execution_id}",
                    "work_item_id": f"project-execution-cancel:{execution_id}:{idempotency_key}",
                    "capability_id": "ca.project.execute_task.cancel",
                    "capability_version": "1.0.0",
                    "validated_arguments": {
                        "task_id": task_id,
                        "job_id": execution_id,
                    },
                    "idempotency_key": idempotency_key,
                    "policy_decision_ref": {
                        "schema_version": "caplatform.explicit_user_confirmation.v1",
                        "decision": "allow",
                        "confirmed_by_user_id": record.principal.user_id,
                        "confirmed_at": datetime.now(UTC).isoformat(),
                        "surface": "project_task_execution",
                    },
                    "context_ref": {
                        "kind": "aiprojectops_task_execution",
                        "task_id": task_id,
                        "execution_id": execution_id,
                    },
                    "expected_artifact_types": [],
                    "trace_id": trace_id,
                    "metadata": {
                        "tenant_id": record.principal.tenant_id,
                        "actor_user_id": record.principal.user_id,
                        "source_product": "civil_aviation_workbench",
                        "confirmation_mode": "explicit",
                    },
                },
            )
        except AgentctlInvocationOutcomeUnknown as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (AgentctlCapabilityRejected, AgentctlIntegrationError) as exc:
            raise _agentctl_http_exception(exc) from exc
        output = invoked.get("output")
        job_ref = output.get("job_ref") if isinstance(output, dict) else None
        if not isinstance(job_ref, dict):
            raise HTTPException(status_code=502, detail="project_execution_job_ref_missing")
        return {"job_ref": job_ref, "trace_id": trace_id}

    @app.post("/api/projects/{project_id}/dependencies", status_code=201)
    async def create_task_dependency(
        project_id: str,
        body: TaskDependencyWriteRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        try:
            return await projects.create_task_dependency(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
                body=body.model_dump(mode="json", exclude_none=True),
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.delete(
        "/api/projects/{project_id}/dependencies/{dependency_id}",
        status_code=204,
    )
    async def delete_task_dependency(
        project_id: str,
        dependency_id: str,
        expected_revision: Annotated[int, Query(ge=1)],
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> None:
        try:
            await projects.delete_task_dependency(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
                dependency_id=dependency_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/projects/{project_id}/schedule/preview")
    async def preview_project_schedule(
        project_id: str,
        body: ProjectScheduleRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await projects.project_schedule(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
                action="preview",
                body=body.model_dump(mode="json", exclude_none=True),
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post("/api/projects/{project_id}/schedule/apply")
    async def apply_project_schedule(
        project_id: str,
        body: ProjectScheduleRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        try:
            return await projects.project_schedule(
                platform_user_token=record.platform_user_token,
                project_id=project_id,
                action="apply",
                body=body.model_dump(mode="json", exclude_none=True),
                idempotency_key=idempotency_key,
            )
        except AIProjectOpsError as exc:
            raise _external_product_http_exception(exc) from exc

    @app.post(
        "/api/project-task-proposals",
        response_model=ProjectTaskProposalProjection,
        status_code=201,
    )
    async def create_project_task_proposal(
        body: ProjectTaskProposalCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ProjectTaskProposalProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = "project_task_proposal.create"
        request_payload = body.model_dump(mode="json")
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=request_payload,
            )
            if cached is not None:
                return ProjectTaskProposalProjection.model_validate(cached)
            if resolved.ai_mode != "remote":
                raise HTTPException(
                    status_code=409,
                    detail="project_task_write_requires_remote_mode",
                )
            proposal_id = f"ptp-{uuid4().hex}"
            try:
                preflight = await assist.preflight_capability(
                    principal=record.principal,
                    capability_id="ca.project.create_task",
                    work_item_id=proposal_id,
                )
            except (AgentctlIntegrationError, PlatformCoreError) as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            if preflight.get("allowed") is not True:
                reasons = [
                    str(item) for item in preflight.get("reasons") or [] if str(item).strip()
                ]
                detail = (
                    f"capability_preflight_blocked:{','.join(reasons)}"
                    if reasons
                    else "capability_preflight_blocked"
                )
                raise HTTPException(status_code=403, detail=detail)
            if (
                preflight.get("required_confirmation") != "explicit"
                or preflight.get("side_effect_class") != "external"
            ):
                raise HTTPException(
                    status_code=502,
                    detail="capability_preflight_contract_mismatch",
                )
            stored_payload = {
                "target": request_payload,
                "execution_key": f"caproject-task:{proposal_id}",
                "trace_id": f"tr-{uuid4().hex}",
            }
            proposal = await project_task_proposals.create(
                proposal_id=proposal_id,
                tenant_id=record.principal.tenant_id,
                user_id=record.principal.user_id,
                payload=stored_payload,
                preflight=preflight,
            )
            result = _project_task_proposal_projection(proposal)
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.get(
        "/api/project-task-proposals/{proposal_id}",
        response_model=ProjectTaskProposalProjection,
    )
    async def get_project_task_proposal(
        proposal_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> ProjectTaskProposalProjection:
        proposal = await project_task_proposals.get(
            proposal_id=proposal_id,
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
        )
        if proposal is None:
            raise HTTPException(status_code=404, detail="project_task_proposal_not_found")
        return _project_task_proposal_projection(proposal)

    @app.post(
        "/api/project-task-proposals/{proposal_id}/decisions",
        response_model=ProjectTaskProposalProjection,
    )
    async def decide_project_task_proposal(
        proposal_id: str,
        body: ProjectTaskProposalDecisionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ProjectTaskProposalProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = f"project_task_proposal.decide:{proposal_id}"
        request_payload = {
            "proposal_id": proposal_id,
            **body.model_dump(mode="json"),
        }
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=request_payload,
            )
            if cached is not None:
                return ProjectTaskProposalProjection.model_validate(cached)
            proposal = await project_task_proposals.get(
                proposal_id=proposal_id,
                tenant_id=record.principal.tenant_id,
                user_id=record.principal.user_id,
            )
            if proposal is None:
                raise HTTPException(
                    status_code=404,
                    detail="project_task_proposal_not_found",
                )
            terminal = {
                "denied",
                "succeeded",
                "outcome_unknown",
                "rejected",
                "expired",
            }
            if proposal.status in terminal:
                if proposal.decision != body.decision:
                    raise HTTPException(
                        status_code=409,
                        detail="project_task_proposal_already_decided",
                    )
                result = _project_task_proposal_projection(proposal)
                await idempotency_remember(
                    operation=operation,
                    key=key,
                    digest=digest,
                    record=record,
                    result=result.model_dump(mode="json"),
                )
                return result
            if proposal.status == "executing":
                raise HTTPException(
                    status_code=409,
                    detail="project_task_proposal_execution_in_progress",
                )
            try:
                if body.decision == "deny":
                    proposal = await project_task_proposals.deny(
                        proposal_id=proposal_id,
                        tenant_id=record.principal.tenant_id,
                        user_id=record.principal.user_id,
                        expected_version=body.expected_proposal_version,
                    )
                else:
                    proposal = await project_task_proposals.claim_allow(
                        proposal_id=proposal_id,
                        tenant_id=record.principal.tenant_id,
                        user_id=record.principal.user_id,
                        expected_version=body.expected_proposal_version,
                    )
            except ProjectTaskProposalConflict as exc:
                raise HTTPException(
                    status_code=409,
                    detail="project_task_proposal_version_conflict",
                ) from exc
            if body.decision == "allow":
                invocation = _project_task_invocation(
                    proposal=proposal,
                    tenant_id=record.principal.tenant_id,
                    user_id=record.principal.user_id,
                )
                try:
                    invocation_result = await assist.invoke_capability(
                        principal=record.principal,
                        invocation=invocation,
                    )
                    output = invocation_result.get("output")
                    if not isinstance(output, dict):
                        raise AgentctlInvocationOutcomeUnknown(
                            "agentctl_capability_output_missing_outcome_unknown"
                        )
                    validated_result = {
                        "task_ref": ProjectTaskReference.model_validate(
                            output.get("task_ref")
                        ).model_dump(mode="json"),
                        "evidence": ProjectTaskCreationEvidence.model_validate(
                            output.get("evidence")
                        ).model_dump(mode="json"),
                    }
                    proposal = await project_task_proposals.succeed(
                        proposal_id=proposal_id,
                        tenant_id=record.principal.tenant_id,
                        user_id=record.principal.user_id,
                        result=validated_result,
                    )
                    await publish_work_item_projection(proposal)
                    refreshed = await project_task_proposals.get(
                        proposal_id=proposal_id,
                        tenant_id=record.principal.tenant_id,
                        user_id=record.principal.user_id,
                    )
                    if refreshed is not None:
                        proposal = refreshed
                except AgentctlInvocationOutcomeUnknown as exc:
                    proposal = await project_task_proposals.outcome_unknown(
                        proposal_id=proposal_id,
                        tenant_id=record.principal.tenant_id,
                        user_id=record.principal.user_id,
                        error_code=str(exc),
                    )
                except (
                    AgentctlCapabilityRejected,
                    AgentctlIntegrationError,
                    PlatformCoreError,
                ) as exc:
                    proposal = await project_task_proposals.reject(
                        proposal_id=proposal_id,
                        tenant_id=record.principal.tenant_id,
                        user_id=record.principal.user_id,
                        error_code=str(exc),
                    )
            result = _project_task_proposal_projection(proposal)
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.get("/api/skills", response_model=list[SkillProjection])
    async def skills(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[SkillProjection]:
        try:
            instruction_payload, tool_payload = await asyncio.gather(
                core.list_instruction_skills(record.platform_user_token),
                core.list_tool_packages(record.platform_user_token),
            )
            return project_skills(instruction_payload, tool_payload)
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc

    @app.put(
        "/api/skills/{package_id}/versions/{version}/installation",
        response_model=SkillInstallationProjection,
    )
    async def change_skill_installation(
        package_id: str,
        version: str,
        body: SkillInstallationRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> SkillInstallationProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = f"skill.installation:{package_id}:{version}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload={**body.model_dump(mode="json"), "scope": "personal"},
            )
            if cached is not None:
                return SkillInstallationProjection.model_validate(cached)
            await _validate_skill_installation_confirmation(
                core=core,
                record=record,
                package_id=package_id,
                version=version,
                body=body,
            )
            try:
                raw = await core.change_instruction_skill_installation(
                    platform_user_token=record.platform_user_token,
                    package_id=package_id,
                    version=version,
                    action=body.action,
                )
            except PlatformCoreError as exc:
                raise _core_http_exception(exc) from exc
            result = SkillInstallationProjection.model_validate(
                {
                    "id": str(raw.get("id") or ""),
                    "package_id": str(raw.get("package_id") or package_id),
                    "package_version": str(raw.get("package_version") or version),
                    "package_digest": str(raw.get("package_digest") or ""),
                    "status": str(raw.get("status") or ""),
                }
            )
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.get("/api/agents", response_model=list[AgentProjection])
    async def agents(
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> list[AgentProjection]:
        try:
            return await assist.list_agents(principal=record.principal)
        except (AgentctlIntegrationError, PlatformCoreError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post(
        "/api/agents/{agent_id}/copies",
        response_model=AgentProjection,
        status_code=201,
    )
    async def copy_agent(
        agent_id: str,
        body: AgentCopyRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AgentProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = f"agent.copy:{agent_id}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=body.model_dump(mode="json"),
            )
            if cached is not None:
                return AgentProjection.model_validate(cached)
            try:
                result = await assist.copy_agent(
                    principal=record.principal,
                    source_agent_id=agent_id,
                    display_name=body.display_name,
                )
            except (AgentctlIntegrationError, PlatformCoreError) as exc:
                raise _agentctl_http_exception(
                    exc
                    if isinstance(exc, AgentctlIntegrationError)
                    else AgentctlIntegrationError(str(exc))
                ) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.post(
        "/api/agents",
        response_model=AgentDraftProjection,
        status_code=201,
    )
    async def create_agent_draft(
        body: AgentDraftWriteRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AgentDraftProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = "agent.draft.create"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            request_payload = body.model_dump(mode="json")
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=request_payload,
            )
            if cached is not None:
                return AgentDraftProjection.model_validate(cached)
            await _validate_personal_agent_bindings(
                core=core,
                record=record,
                body=body,
            )
            try:
                result = await assist.upsert_personal_agent_draft(
                    principal=record.principal,
                    configuration=body,
                )
            except (AgentctlIntegrationError, PlatformCoreError) as exc:
                raise _agent_management_http_exception(exc) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.get(
        "/api/agents/{agent_id}/draft",
        response_model=AgentDraftProjection,
    )
    async def get_agent_draft(
        agent_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AgentDraftProjection:
        try:
            return await assist.get_personal_agent_draft(
                principal=record.principal,
                agent_id=agent_id,
            )
        except (AgentctlIntegrationError, PlatformCoreError) as exc:
            raise _agent_management_http_exception(exc) from exc

    @app.get("/api/agents/{agent_id}/growth")
    async def get_agent_growth(
        agent_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await assist.agent_growth(
                principal=record.principal,
                agent_id=agent_id,
            )
        except AgentctlIntegrationError as exc:
            raise _agent_management_http_exception(exc) from exc

    @app.put(
        "/api/agents/{agent_id}",
        response_model=AgentDraftProjection,
    )
    async def update_agent_draft(
        agent_id: str,
        body: AgentDraftWriteRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AgentDraftProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = f"agent.draft.update:{agent_id}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            request_payload = body.model_dump(mode="json")
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=request_payload,
            )
            if cached is not None:
                return AgentDraftProjection.model_validate(cached)
            await _validate_personal_agent_bindings(
                core=core,
                record=record,
                body=body,
            )
            try:
                result = await assist.upsert_personal_agent_draft(
                    principal=record.principal,
                    agent_id=agent_id,
                    configuration=body,
                )
            except (AgentctlIntegrationError, PlatformCoreError) as exc:
                raise _agent_management_http_exception(exc) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.post(
        "/api/agents/{agent_id}/test-runs",
        response_model=AgentTrialProjection,
    )
    async def trial_agent_draft(
        agent_id: str,
        body: AgentTrialRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AgentTrialProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = f"agent.draft.trial:{agent_id}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=body.model_dump(mode="json"),
            )
            if cached is not None:
                return AgentTrialProjection.model_validate(cached)
            try:
                result = await assist.trial_personal_agent(
                    principal=record.principal,
                    agent_id=agent_id,
                    message=body.message,
                )
            except (AgentctlIntegrationError, PlatformCoreError) as exc:
                raise _agent_management_http_exception(exc) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.post(
        "/api/agents/{agent_id}/publish",
        response_model=AgentProjection,
    )
    async def publish_agent_draft(
        agent_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AgentProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = f"agent.draft.publish:{agent_id}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload={"agent_id": agent_id},
            )
            if cached is not None:
                return AgentProjection.model_validate(cached)
            try:
                result = await assist.publish_personal_agent(
                    principal=record.principal,
                    agent_id=agent_id,
                )
            except (AgentctlIntegrationError, PlatformCoreError) as exc:
                raise _agent_management_http_exception(exc) from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.post(
        "/api/agents/{agent_id}/share-requests",
        response_model=AgentShareRequestProjection,
        status_code=201,
    )
    async def create_agent_share_request(
        agent_id: str,
        body: AgentShareRequestCreateRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AgentShareRequestProjection:
        key = _require_idempotency_key(idempotency_key)
        operation = f"agent.share.request:{agent_id}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload={"agent_id": agent_id, **body.model_dump(mode="json")},
            )
            if cached is not None:
                return AgentShareRequestProjection.model_validate(cached)
            try:
                snapshot = await assist.prepare_personal_agent_share(
                    principal=record.principal,
                    agent_id=agent_id,
                )
                await _validate_agent_share_knowledge(
                    core=core,
                    record=record,
                    snapshot=snapshot,
                )
                evidence = _agent_share_evidence_from_snapshot(
                    snapshot=snapshot,
                    reason=body.reason,
                )
                request_id = _agent_share_request_id(
                    tenant_id=record.principal.tenant_id,
                    evidence=evidence,
                )
                evidence_ref = _encode_agent_share_evidence(evidence)
                expires_at = datetime.now(UTC) + timedelta(days=7)
                try:
                    await core.create_review_request(
                        platform_user_token=record.platform_user_token,
                        payload={
                            "schema_version": "aios.review_request.v0.1",
                            "request_id": request_id,
                            "product_id": resolved.product_id,
                            "capability_id": _AGENT_SHARE_CAPABILITY_ID,
                            "requirement_brief_version": snapshot.agent_version,
                            "risk_level": "R2",
                            "policy": {
                                "policy_id": "caplatform.agent.organization-share",
                                "version": "1",
                                "review_mode": "full",
                                "separation_required": True,
                                "required_reviewer_roles": [],
                                "required_reviewer_scopes": ["review.decide"],
                            },
                            "expires_at": expires_at.isoformat(),
                            "review_surface_ref": (
                                f"/agents/{quote(agent_id, safe='')}/edit"
                                f"?share_request={request_id}"
                            ),
                            "version": 1,
                            "evidence_refs": [
                                evidence_ref,
                                f"agentctl-run://{snapshot.trial_run_id}",
                            ],
                        },
                    )
                except PlatformCoreError as exc:
                    if str(exc) != "platform_core_http_409":
                        raise
                card_payload = await core.get_review_approval_card(
                    platform_user_token=record.platform_user_token,
                    request_id=request_id,
                )
                card = _validated_agent_share_card(
                    payload=card_payload,
                    product_id=resolved.product_id,
                    tenant_id=record.principal.tenant_id,
                )
                authoritative_evidence = _agent_share_evidence(card.evidence_refs)
                result = await _agent_share_projection(
                    assist=assist,
                    record=record,
                    card=card,
                    evidence=authoritative_evidence,
                )
            except PlatformCoreError as exc:
                raise _core_http_exception(exc) from exc
            except AgentctlIntegrationError as exc:
                raise _agent_management_http_exception(exc) from exc
            except ValueError as exc:
                raise HTTPException(status_code=502, detail="agent_share_review_invalid") from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.get(
        "/api/agents/{agent_id}/share-request",
        response_model=AgentShareRequestProjection,
    )
    async def get_agent_share_request(
        agent_id: str,
        record: Annotated[SessionRecord, Depends(require_session)],
    ) -> AgentShareRequestProjection:
        try:
            snapshot = await assist.prepare_personal_agent_share(
                principal=record.principal,
                agent_id=agent_id,
            )
            provisional = _agent_share_evidence_from_snapshot(
                snapshot=snapshot,
                reason="当前版本尚未提交组织共享申请",
            )
            request_id = _agent_share_request_id(
                tenant_id=record.principal.tenant_id,
                evidence=provisional,
            )
            try:
                card_payload = await core.get_review_approval_card(
                    platform_user_token=record.platform_user_token,
                    request_id=request_id,
                )
            except PlatformCoreError as exc:
                if str(exc) != "platform_core_http_404":
                    raise
                return _not_requested_agent_share_projection(
                    request_id=request_id,
                    evidence=provisional,
                )
            card = _validated_agent_share_card(
                payload=card_payload,
                product_id=resolved.product_id,
                tenant_id=record.principal.tenant_id,
            )
            evidence = _agent_share_evidence(card.evidence_refs)
            return await _agent_share_projection(
                assist=assist,
                record=record,
                card=card,
                evidence=evidence,
            )
        except PlatformCoreError as exc:
            raise _core_http_exception(exc) from exc
        except AgentctlIntegrationError as exc:
            raise _agent_management_http_exception(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="agent_share_review_invalid") from exc

    @app.post(
        "/api/agent-share-requests/{request_id}/decisions",
        response_model=AgentShareRequestProjection,
    )
    async def decide_agent_share_request(
        request_id: str,
        body: AgentShareDecisionRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AgentShareRequestProjection:
        if not _permission_allows(record.principal.permissions, "review.decide"):
            raise HTTPException(
                status_code=403,
                detail="review_decide_permission_required",
            )
        key = _require_idempotency_key(idempotency_key)
        operation = f"agent.share.decide:{request_id}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload={"request_id": request_id, **body.model_dump(mode="json")},
            )
            if cached is not None:
                return AgentShareRequestProjection.model_validate(cached)
            try:
                card_payload = await core.get_review_approval_card(
                    platform_user_token=record.platform_user_token,
                    request_id=request_id,
                )
                card = _validated_agent_share_card(
                    payload=card_payload,
                    product_id=resolved.product_id,
                    tenant_id=record.principal.tenant_id,
                )
                evidence = _agent_share_evidence(card.evidence_refs)
                if card.request_version != body.expected_request_version:
                    raise HTTPException(
                        status_code=409,
                        detail="agent_share_request_version_changed",
                    )
                decision_id = ""
                if card.status == "pending":
                    decision_payload = await core.decide_review(
                        platform_user_token=record.platform_user_token,
                        request_id=request_id,
                        expected_request_version=body.expected_request_version,
                        decision=body.decision,
                    )
                    decision_id = str(decision_payload.get("decision_id") or "")
                elif card.status != body.decision:
                    raise HTTPException(
                        status_code=409,
                        detail="agent_share_request_already_resolved",
                    )
                if body.decision == "allow":
                    if not decision_id:
                        public_projection = await core.get_review_request(
                            platform_user_token=record.platform_user_token,
                            request_id=request_id,
                        )
                        decision_id = str(public_projection.get("decision_id") or "")
                    if not decision_id:
                        raise PlatformCoreError("platform_core_review_decision_missing")
                    await assist.publish_personal_agent_to_organization(
                        reviewer=record.principal,
                        source_agent_id=evidence.agent_id,
                        source_owner_user_id=evidence.owner_user_id,
                        source_agent_version=evidence.agent_version,
                        source_snapshot_digest=evidence.snapshot_digest,
                        organization_agent_id=evidence.organization_agent_id,
                        review_request_id=request_id,
                        review_decision_id=decision_id,
                    )
                refreshed_payload = await core.get_review_approval_card(
                    platform_user_token=record.platform_user_token,
                    request_id=request_id,
                )
                refreshed = _validated_agent_share_card(
                    payload=refreshed_payload,
                    product_id=resolved.product_id,
                    tenant_id=record.principal.tenant_id,
                )
                result = await _agent_share_projection(
                    assist=assist,
                    record=record,
                    card=refreshed,
                    evidence=evidence,
                )
            except PlatformCoreError as exc:
                raise _core_http_exception(exc) from exc
            except AgentctlIntegrationError as exc:
                raise _agent_management_http_exception(exc) from exc
            except ValueError as exc:
                raise HTTPException(status_code=502, detail="agent_share_review_invalid") from exc
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result.model_dump(mode="json"),
            )
            return result

    @app.post("/api/frontdesk/feedback", status_code=202)
    async def frontdesk_feedback(
        body: FrontdeskFeedbackRequest,
        record: Annotated[SessionRecord, Depends(require_session)],
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, bool]:
        key = _require_idempotency_key(idempotency_key)
        operation = f"frontdesk.feedback:{body.action_id}"
        async with idempotency.serialize(
            tenant_id=record.principal.tenant_id,
            user_id=record.principal.user_id,
            operation=operation,
            idempotency_key=key,
        ):
            digest, cached = await idempotency_lookup(
                operation=operation,
                key=key,
                record=record,
                request_payload=body.model_dump(mode="json"),
            )
            if cached is not None:
                return {"recorded": bool(cached.get("recorded"))}
            if resolved.ai_mode != "remote":
                raise HTTPException(status_code=409, detail="feedback_requires_remote_mode")
            try:
                feedback = await assist.submit_feedback(
                    principal=record.principal,
                    action_id=body.action_id,
                    run_id=body.run_id,
                    agent_id=body.agent_id,
                    agent_version=body.agent_version,
                    verdict=body.verdict,
                    comment=body.comment,
                    before=body.before,
                    after=body.after,
                )
            except (AgentctlIntegrationError, PlatformCoreError) as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            result = {
                "recorded": bool(feedback.get("recorded")),
                "learning_queued": bool(feedback.get("learning_signal_id")),
            }
            await idempotency_remember(
                operation=operation,
                key=key,
                digest=digest,
                record=record,
                result=result,
            )
            return result

    return app


def _project_task_invocation(
    *,
    proposal: ProjectTaskProposalRecord,
    tenant_id: str,
    user_id: str,
) -> dict[str, Any]:
    target = proposal.payload.get("target")
    if not isinstance(target, dict):
        raise ProjectTaskProposalConflict("proposal_target_invalid")
    execution_key = str(proposal.payload.get("execution_key") or "")
    trace_id = str(proposal.payload.get("trace_id") or "")
    if not execution_key or not trace_id:
        raise ProjectTaskProposalConflict("proposal_execution_binding_invalid")
    confirmation_version = (
        proposal.version if proposal.status == "executing" else max(1, proposal.version - 1)
    )
    return {
        "schema_version": "aios.capability_invocation.v0.1",
        "invocation_id": f"invoke-{proposal.proposal_id}",
        "request_id": proposal.proposal_id,
        "work_item_id": proposal.proposal_id,
        "capability_id": "ca.project.create_task",
        "capability_version": "1.0.0",
        "validated_arguments": target,
        "idempotency_key": execution_key,
        "policy_decision_ref": {
            "schema_version": "caplatform.explicit_user_confirmation.v1",
            "proposal_id": proposal.proposal_id,
            "proposal_version": confirmation_version,
            "decision": "allow",
            "confirmed_by_user_id": user_id,
            "confirmed_at": _iso_timestamp(proposal.decided_at or proposal.updated_at),
        },
        "context_ref": {
            "kind": "aiprojectops_project",
            "project_id": str(target.get("project_id") or ""),
        },
        "requirement_brief_ref": None,
        "expected_artifact_types": ["project_object"],
        "trace_id": trace_id,
        "metadata": {
            "tenant_id": tenant_id,
            "actor_user_id": user_id,
            "source_product": "civil_aviation_workbench",
            "confirmation_mode": "explicit",
        },
    }


def _platform_object_id(object_ref: str | None) -> str:
    value = str(object_ref or "").strip()
    if not value.startswith("obj://core/"):
        raise HTTPException(status_code=422, detail="invalid_platform_storage_object_ref")
    object_id = value.removeprefix("obj://core/").strip()
    if not object_id or "/" in object_id or "\\" in object_id:
        raise HTTPException(status_code=422, detail="invalid_platform_storage_object_ref")
    return object_id


def _parsecore_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []

    def collect(value: Any, key: str | None = None) -> None:
        if isinstance(value, str) and key in {
            "text",
            "content",
            "markdown",
            "plain_text",
            "ocr_text",
        }:
            text = value.strip()
            if text:
                lines.append(text)
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, str(child_key).lower())
        elif isinstance(value, list):
            for child in value:
                collect(child, key)

    parsecore = payload.get("parsecore")
    collect(parsecore if isinstance(parsecore, dict | list) else payload)
    deduplicated = list(dict.fromkeys(lines))
    return "\n\n".join(deduplicated)[:2_000_000]


def _project_task_proposal_projection(
    record: ProjectTaskProposalRecord,
) -> ProjectTaskProposalProjection:
    target_raw = record.payload.get("target")
    if not isinstance(target_raw, dict):
        raise ProjectTaskProposalConflict("proposal_target_invalid")
    preflight = ProjectTaskProposalPreflight(
        allowed=record.preflight.get("allowed") is True,
        reasons=[str(item) for item in record.preflight.get("reasons") or []],
        checks={
            str(key): bool(value)
            for key, value in dict(record.preflight.get("checks") or {}).items()
        },
        required_confirmation="explicit",
        side_effect_class="external",
    )
    task_ref = None
    evidence = None
    if record.result is not None:
        task_ref = ProjectTaskReference.model_validate(record.result.get("task_ref"))
        evidence = ProjectTaskCreationEvidence.model_validate(record.result.get("evidence"))
    return ProjectTaskProposalProjection(
        proposal_id=record.proposal_id,
        proposal_version=record.version,
        status=record.status,  # type: ignore[arg-type]
        target=ProjectTaskProposalTarget.model_validate(target_raw),
        impact="将在 AIProjectOPS 指定项目中幂等创建 1 条任务；不会修改或删除既有任务。",
        preflight=preflight,
        decision=record.decision,  # type: ignore[arg-type]
        task_ref=task_ref,
        evidence=evidence,
        error_code=record.error_code,
        work_item_projection_status=record.work_item_projection_status,  # type: ignore[arg-type]
        created_at=_iso_timestamp(record.created_at),
        updated_at=_iso_timestamp(record.updated_at),
        expires_at=_iso_timestamp(record.expires_at),
    )


def _project_task_work_item_page(
    record: ProjectTaskProposalRecord,
    *,
    product_id: str,
) -> ApplicationInteractionEventPageV1:
    if record.status != "succeeded" or record.result is None:
        raise ValueError("project_task_work_item_requires_success")
    task_ref = ProjectTaskReference.model_validate(record.result.get("task_ref"))
    evidence = ProjectTaskCreationEvidence.model_validate(record.result.get("evidence"))
    event_id = f"project-task:{record.proposal_id}:succeeded"
    conversation_id = f"work-{record.proposal_id}"
    event = ApplicationInteractionEventV1(
        schema_version="aios.application_interaction_event.v1",
        event_id=event_id,
        sequence=1,
        cursor=f"1:{event_id}",
        event_type="execution.terminal",
        occurred_at=_iso_timestamp(record.updated_at),
        tenant_id=record.tenant_id,
        product_id=product_id,
        conversation_id=conversation_id,
        request_id=record.proposal_id,
        trace_id=evidence.trace_id,
        producer="agentctl.capability_dispatch",
        payload={
            "capability_id": "ca.project.create_task",
            "status": "succeeded",
            "provider_status": evidence.provider_status,
        },
        run_ref={"invocation_id": f"invoke-{record.proposal_id}"},
        work_item_ref={"work_item_id": record.proposal_id},
        artifact_refs=[
            {
                "kind": "aiprojectops_task",
                "task_id": task_ref.task_id,
                "project_id": task_ref.project_id,
                "source_system": task_ref.source_system,
                "creation_ref": task_ref.creation_ref,
            }
        ],
        evidence_refs=[
            {
                "kind": evidence.kind,
                "trace_id": evidence.trace_id,
                "provider_status": evidence.provider_status,
                "idempotency_replayed": evidence.idempotency_replayed,
            }
        ],
        terminal_status="succeeded",
        replayable=True,
        extensions={
            "fact_ownership": {
                "execution": "agentctl",
                "task": "aiprojectops",
                "projection": "platform-core",
            }
        },
    )
    return ApplicationInteractionEventPageV1(
        schema_version="aios.application_interaction_event_page.v1",
        tenant_id=record.tenant_id,
        product_id=product_id,
        conversation_id=conversation_id,
        stream_scope="turn_projection",
        after_sequence=0,
        events=[event],
        next_cursor=event.cursor,
        has_more=False,
        gap_detected=False,
        heartbeat_after_ms=15000,
        extensions={
            "source": "caplatform.project_task_outbox",
            "authoritative_task_store": "aiprojectops",
        },
    )


async def _existing_conversation_page(
    *,
    core: Any,
    record: SessionRecord,
    product_id: str,
    conversation_id: str,
) -> ApplicationInteractionEventPageV1 | None:
    replay = getattr(core, "replay_conversation", None)
    if not callable(replay):
        return None
    pages: list[ApplicationInteractionEventPageV1] = []
    after_cursor: str | None = None
    for _ in range(10):
        try:
            raw_page = await replay(
                platform_user_token=record.platform_user_token,
                product_id=product_id,
                conversation_id=conversation_id,
                after_cursor=after_cursor,
            )
        except PlatformCoreError as exc:
            if str(exc) == "platform_core_http_404" and not pages:
                return None
            raise
        page = ApplicationInteractionEventPageV1.model_validate(raw_page)
        pages.append(page)
        if not page.has_more or not page.next_cursor:
            break
        after_cursor = page.next_cursor
    if len(pages) == 1:
        return pages[0]
    return merge_conversation_pages(*pages)


async def _conversation_attachments(
    *,
    core: Any,
    record: SessionRecord,
    attachments: list[ConversationAttachmentRequest],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    public_metadata: list[dict[str, Any]] = []
    model_previews: list[dict[str, Any]] = []
    preview_characters_remaining = 6_000
    for attachment in attachments:
        object_id = attachment.object_ref.removeprefix("obj://core/")
        raw = await core.get_storage_file(
            platform_user_token=record.platform_user_token,
            object_id=object_id,
        )
        projected = project_drive_files({"files": [raw]})
        if not projected or projected[0].object_ref != attachment.object_ref:
            raise PlatformCoreError("platform_core_invalid_storage_object")
        item = projected[0]
        public_metadata.append(
            {
                "object_ref": item.object_ref,
                "name": item.name,
                "mime": item.mime,
                "sensitivity": item.sensitivity,
            }
        )
        if item.sensitivity not in {"public", "internal"} or preview_characters_remaining <= 0:
            continue
        preview = await core.preview_storage_file(
            platform_user_token=record.platform_user_token,
            object_id=object_id,
        )
        text = preview.get("text")
        if not isinstance(text, str) or not text:
            continue
        bounded = text[:preview_characters_remaining]
        preview_characters_remaining -= len(bounded)
        model_previews.append(
            {
                "object_ref": item.object_ref,
                "name": item.name,
                "mime": item.mime,
                "text": bounded,
                "truncated": bool(preview.get("truncated")) or len(bounded) < len(text),
            }
        )
    return public_metadata, model_previews


def _iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="platform_token_required")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="invalid_authorization")
    return token.strip()


def _device_label(request: Request) -> str:
    """Derive a coarse label without retaining the raw user-agent or IP address."""
    user_agent = request.headers.get("user-agent", "").lower()
    if not user_agent:
        return "未知设备"
    if "edg/" in user_agent:
        browser = "Edge"
    elif "firefox/" in user_agent:
        browser = "Firefox"
    elif "chrome/" in user_agent or "crios/" in user_agent:
        browser = "Chrome"
    elif "safari/" in user_agent:
        browser = "Safari"
    else:
        browser = "浏览器"
    if "android" in user_agent:
        system = "Android"
    elif "iphone" in user_agent or "ipad" in user_agent:
        system = "iOS"
    elif "windows" in user_agent:
        system = "Windows"
    elif "mac os" in user_agent or "macintosh" in user_agent:
        system = "macOS"
    elif "linux" in user_agent:
        system = "Linux"
    else:
        system = "未知系统"
    return f"{system} · {browser}"


async def _optional_export_call(
    target: object,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    method = getattr(target, method_name, None)
    if not callable(method):
        raise AttributeError(f"{method_name}_unavailable")
    return await method(*args, **kwargs)


def _platform_auth_status(exc: PlatformCoreError) -> int:
    code = str(exc)
    for status_code in (400, 401, 409, 422, 428, 429):
        if code == f"platform_core_http_{status_code}":
            return status_code
    return 502


def _require_idempotency_key(value: str | None) -> str:
    key = (value or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="idempotency_key_required")
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="idempotency_key_too_long")
    return key


def _request_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _public_platform_url(
    signed_url: str,
    *,
    internal_base_url: str,
    public_base_url: str,
) -> str:
    if signed_url.startswith("/"):
        return urljoin(f"{public_base_url}/", signed_url.lstrip("/"))
    signed = urlsplit(signed_url)
    internal = urlsplit(internal_base_url)
    public = urlsplit(public_base_url)
    if signed.netloc and signed.netloc == internal.netloc:
        return urlunsplit(
            (
                public.scheme,
                public.netloc,
                signed.path,
                signed.query,
                signed.fragment,
            )
        )
    return signed_url


def _agent_share_evidence_from_snapshot(
    *,
    snapshot: PersonalAgentShareSnapshot,
    reason: str,
) -> _AgentShareEvidence:
    return _AgentShareEvidence(
        agent_id=snapshot.agent_id,
        agent_name=snapshot.agent_name,
        agent_version=snapshot.agent_version,
        owner_user_id=snapshot.owner_user_id,
        snapshot_digest=snapshot.snapshot_digest,
        organization_agent_id=snapshot.organization_agent_id,
        trial_run_id=snapshot.trial_run_id,
        reason=reason.strip(),
        skill_ids=snapshot.skill_ids,
        knowledge_source_ids=snapshot.knowledge_source_ids,
    )


def _encode_agent_share_evidence(evidence: _AgentShareEvidence) -> str:
    payload = {
        "schema_version": "caplatform.agent_share.v1",
        "agent_id": evidence.agent_id,
        "agent_name": evidence.agent_name,
        "agent_version": evidence.agent_version,
        "owner_user_id": evidence.owner_user_id,
        "snapshot_digest": evidence.snapshot_digest,
        "organization_agent_id": evidence.organization_agent_id,
        "trial_run_id": evidence.trial_run_id,
        "reason": evidence.reason,
        "skill_ids": list(evidence.skill_ids),
        "knowledge_source_ids": list(evidence.knowledge_source_ids),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).decode("ascii")
    return _AGENT_SHARE_EVIDENCE_PREFIX + encoded.rstrip("=")


def _agent_share_evidence(evidence_refs: list[str]) -> _AgentShareEvidence:
    matches = [
        item
        for item in evidence_refs
        if isinstance(item, str) and item.startswith(_AGENT_SHARE_EVIDENCE_PREFIX)
    ]
    if len(matches) != 1:
        raise ValueError("agent share review must contain exactly one evidence envelope")
    encoded = matches[0].removeprefix(_AGENT_SHARE_EVIDENCE_PREFIX)
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("agent share evidence envelope is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != (
        "caplatform.agent_share.v1"
    ):
        raise ValueError("agent share evidence schema is invalid")

    def required_text(key: str, maximum: int) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"agent share evidence {key} is invalid")
        return value.strip()

    def string_tuple(key: str, maximum: int) -> tuple[str, ...]:
        value = payload.get(key, [])
        if (
            not isinstance(value, list)
            or len(value) > maximum
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            raise ValueError(f"agent share evidence {key} is invalid")
        normalized = tuple(str(item).strip() for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"agent share evidence {key} repeats values")
        return normalized

    digest = required_text("snapshot_digest", 80)
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError("agent share evidence digest is invalid")
    return _AgentShareEvidence(
        agent_id=required_text("agent_id", 160),
        agent_name=required_text("agent_name", 160),
        agent_version=required_text("agent_version", 80),
        owner_user_id=required_text("owner_user_id", 160),
        snapshot_digest=digest,
        organization_agent_id=required_text("organization_agent_id", 160),
        trial_run_id=required_text("trial_run_id", 256),
        reason=required_text("reason", 1000),
        skill_ids=string_tuple("skill_ids", 20),
        knowledge_source_ids=string_tuple("knowledge_source_ids", 20),
    )


def _agent_share_request_id(
    *,
    tenant_id: str,
    evidence: _AgentShareEvidence,
) -> str:
    seed = "\0".join(
        (
            tenant_id,
            evidence.owner_user_id,
            evidence.agent_id,
            evidence.agent_version,
            evidence.snapshot_digest,
            evidence.organization_agent_id,
        )
    )
    return "agent-share-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _validate_agent_share_review_identity(
    *,
    evidence: _AgentShareEvidence,
    request_id: str,
    tenant_id: str,
    requester_actor_id: str | None = None,
) -> None:
    if request_id != _agent_share_request_id(tenant_id=tenant_id, evidence=evidence):
        raise ValueError("agent share request identity does not match evidence")
    organization_seed = "\0".join(
        (
            tenant_id,
            evidence.owner_user_id,
            evidence.agent_id,
            evidence.agent_version,
            evidence.snapshot_digest,
        )
    )
    expected_organization_id = (
        "shared-" + hashlib.sha256(organization_seed.encode("utf-8")).hexdigest()[:32]
    )
    if evidence.organization_agent_id != expected_organization_id:
        raise ValueError("agent share organization identity is invalid")
    if requester_actor_id is not None and requester_actor_id != evidence.owner_user_id:
        raise ValueError("agent share requester is not the personal expert owner")


def _validated_agent_share_card(
    *,
    payload: dict[str, Any],
    product_id: str,
    tenant_id: str,
) -> ReviewCardProjection:
    card = ReviewCardProjection.model_validate(payload)
    if card.product_id != product_id or card.capability_id != _AGENT_SHARE_CAPABILITY_ID:
        raise ValueError("review card is not an agent share request for this product")
    evidence = _agent_share_evidence(card.evidence_refs)
    _validate_agent_share_review_identity(
        evidence=evidence,
        request_id=card.request_id,
        tenant_id=tenant_id,
        requester_actor_id=card.requester_actor_id,
    )
    return card


async def _validate_agent_share_knowledge(
    *,
    core: PlatformCoreClient | Any,
    record: SessionRecord,
    snapshot: PersonalAgentShareSnapshot,
) -> None:
    if not snapshot.knowledge_source_ids:
        return
    try:
        payload = await core.list_knowledge_sources(record.platform_user_token)
    except PlatformCoreError as exc:
        raise _core_http_exception(exc) from exc
    visible = {item.id: item for item in project_knowledge_sources(payload)}
    blocked = [
        source_id
        for source_id in snapshot.knowledge_source_ids
        if source_id not in visible
        or visible[source_id].scope == "personal"
        or visible[source_id].status != "connected"
    ]
    if blocked:
        raise HTTPException(
            status_code=409,
            detail="agent_share_knowledge_not_organization_visible",
        )


async def _agent_share_projection(
    *,
    assist: AgentctlAssistService | Any,
    record: SessionRecord,
    card: ReviewCardProjection,
    evidence: _AgentShareEvidence,
) -> AgentShareRequestProjection:
    publication_status: Literal["not_applicable", "pending", "published"] = "not_applicable"
    if card.status == "allow":
        publication = await assist.get_organization_share_publication(
            principal=record.principal,
            organization_agent_id=evidence.organization_agent_id,
            review_request_id=card.request_id,
            source_snapshot_digest=evidence.snapshot_digest,
        )
        publication_status = "published" if publication is not None else "pending"
    return AgentShareRequestProjection(
        request_id=card.request_id,
        agent_id=evidence.agent_id,
        agent_name=evidence.agent_name,
        agent_version=evidence.agent_version,
        status=card.status,
        publication_status=publication_status,
        organization_agent_id=evidence.organization_agent_id,
        skill_ids=list(evidence.skill_ids),
        knowledge_source_ids=list(evidence.knowledge_source_ids),
        reason=evidence.reason,
        expires_at=card.expires_at,
        can_decide=card.can_decide,
    )


def _not_requested_agent_share_projection(
    *,
    request_id: str,
    evidence: _AgentShareEvidence,
) -> AgentShareRequestProjection:
    return AgentShareRequestProjection(
        request_id=request_id,
        agent_id=evidence.agent_id,
        agent_name=evidence.agent_name,
        agent_version=evidence.agent_version,
        status="not_requested",
        publication_status="not_applicable",
        organization_agent_id=evidence.organization_agent_id,
        skill_ids=list(evidence.skill_ids),
        knowledge_source_ids=list(evidence.knowledge_source_ids),
        reason="",
        expires_at=None,
        can_decide=False,
    )


def _permission_allows(permissions: list[str], required: str) -> bool:
    return any(
        permission in {"*", required}
        or (permission.endswith(".*") and required.startswith(permission[:-1]))
        for permission in permissions
    )


def _mailhub_host_payload_contains_forbidden(
    value: object,
    *,
    depth: int = 0,
) -> bool:
    """Reject raw mail bodies and credentials at the host knowledge boundary."""
    if depth > 8:
        return True
    if isinstance(value, Mapping):
        if len(value) > 128:
            return True
        forbidden_keys = {
            "access_token",
            "authorization_code",
            "body_html",
            "body_text",
            "client_secret",
            "mime_bytes",
            "password",
            "raw_body",
            "raw_html",
            "raw_mime",
            "refresh_token",
        }
        for key, item in value.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key in forbidden_keys:
                return True
            if _mailhub_host_payload_contains_forbidden(item, depth=depth + 1):
                return True
        return False
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            return True
        return any(
            _mailhub_host_payload_contains_forbidden(item, depth=depth + 1) for item in value
        )
    return False


def _core_http_exception(exc: PlatformCoreError) -> HTTPException:
    code = str(exc)
    if code in {
        "platform_core_http_401",
        "platform_core_http_403",
        "platform_core_http_404",
        "platform_core_http_409",
        "platform_core_http_410",
        "platform_core_http_422",
    }:
        return HTTPException(status_code=int(code.rsplit("_", 1)[-1]), detail=code)
    return HTTPException(status_code=502, detail=code)


async def _validate_skill_installation_confirmation(
    *,
    core: PlatformCoreClient | Any,
    record: SessionRecord,
    package_id: str,
    version: str,
    body: SkillInstallationRequest,
) -> None:
    if body.action not in {"install", "update"}:
        return
    try:
        payload = await core.list_instruction_skills(record.platform_user_token)
    except PlatformCoreError as exc:
        raise _core_http_exception(exc) from exc
    packages = payload.get("packages")
    selected: dict[str, Any] | None = None
    if isinstance(packages, list):
        for item in packages:
            if not isinstance(item, dict):
                continue
            manifest = item.get("manifest")
            if not isinstance(manifest, dict):
                continue
            if manifest.get("id") == package_id and manifest.get("version") == version:
                selected = item
                break
    if selected is None:
        raise HTTPException(status_code=404, detail="skill_version_not_found")
    if selected.get("trust_state") != "trusted" or selected.get("status") != "active":
        raise HTTPException(status_code=409, detail="skill_source_not_trusted")
    manifest = selected["manifest"]
    if body.acknowledged_digest != manifest.get("digest"):
        raise HTTPException(status_code=409, detail="skill_manifest_changed")
    dependencies = manifest.get("dependencies")
    dependencies = dependencies if isinstance(dependencies, dict) else {}
    permission_refs: list[str] = []
    for key in ("tool_refs", "capability_refs", "model_refs", "data_scope_refs"):
        values = dependencies.get(key)
        if isinstance(values, list):
            permission_refs.extend(str(item) for item in values)
    if sorted(set(body.confirmed_permission_refs)) != sorted(set(permission_refs)):
        raise HTTPException(status_code=409, detail="skill_permissions_not_confirmed")
    if body.action == "update":
        installation = selected.get("installation")
        if not isinstance(installation, dict):
            raise HTTPException(status_code=409, detail="skill_update_requires_installation")
        if installation.get("subject_type") == "tenant":
            raise HTTPException(status_code=409, detail="skill_is_organization_managed")


async def _validate_personal_agent_bindings(
    *,
    core: PlatformCoreClient | Any,
    record: SessionRecord,
    body: AgentDraftWriteRequest,
) -> None:
    try:
        instruction_payload = await core.list_instruction_skills(record.platform_user_token)
        knowledge_payload = (
            await core.list_knowledge_sources(record.platform_user_token)
            if body.knowledge_source_ids
            else {"sources": []}
        )
    except PlatformCoreError as exc:
        raise _core_http_exception(exc) from exc

    enabled_skills: set[str] = set()
    packages = instruction_payload.get("packages")
    if isinstance(packages, list):
        for item in packages:
            if not isinstance(item, dict):
                continue
            manifest = item.get("manifest")
            installation = item.get("installation")
            if not isinstance(manifest, dict) or not isinstance(installation, dict):
                continue
            if installation.get("status") == "enabled" and manifest.get("id"):
                enabled_skills.add(str(manifest["id"]))
    if not set(body.skill_ids).issubset(enabled_skills):
        raise HTTPException(status_code=409, detail="agent_skill_not_enabled")

    visible_sources = {source.id for source in project_knowledge_sources(knowledge_payload)}
    if not set(body.knowledge_source_ids).issubset(visible_sources):
        raise HTTPException(status_code=409, detail="agent_knowledge_source_not_visible")


def _agent_management_http_exception(
    exc: AgentctlIntegrationError | PlatformCoreError,
) -> HTTPException:
    if isinstance(exc, PlatformCoreError):
        return _core_http_exception(exc)
    code = str(exc)
    if code == "agentctl_personal_agent_owner_mismatch":
        return HTTPException(status_code=403, detail=code)
    if code in {
        "agentctl_agent_draft_deprecated",
        "agentctl_agent_draft_status_mismatch",
        "agentctl_agent_trial_requires_draft",
        "agentctl_agent_trial_spec_missing",
        "agentctl_agent_publish_requires_current_trial",
        "agentctl_agent_share_requires_published_source",
        "agentctl_agent_share_requires_current_trial",
        "agentctl_agent_share_snapshot_changed",
        "agentctl_agent_share_snapshot_incomplete",
        "agentctl_agent_share_configuration_invalid",
        "agentctl_agent_share_publication_mismatch",
    }:
        return HTTPException(status_code=409, detail=code)
    return _agentctl_http_exception(exc)


def _project_knowledge_lifecycle(payload: dict[str, Any]) -> KnowledgeSourceProjection:
    source = payload.get("source")
    if not isinstance(source, dict):
        raise HTTPException(status_code=502, detail="knowledge_source_projection_missing")
    projected = project_knowledge_sources({"sources": [source]})
    if not projected:
        raise HTTPException(status_code=502, detail="knowledge_source_projection_invalid")
    return projected[0]


def _agentctl_http_exception(
    exc: AgentctlIntegrationError | PlatformCoreError,
) -> HTTPException:
    code = str(exc)
    for status_code in (400, 401, 403, 404, 409, 422, 503):
        if code.endswith(f"_http_{status_code}"):
            return HTTPException(status_code=status_code, detail=code)
    return HTTPException(status_code=502, detail=code)


def _project_planning_job_status(
    value: object,
    *,
    default: ProjectPlanningJobStatus,
) -> ProjectPlanningJobStatus:
    normalized = str(value or default).strip().lower()
    allowed = {"queued", "running", "succeeded", "failed", "cancelled"}
    if normalized not in allowed:
        raise HTTPException(status_code=502, detail="project_planning_job_status_invalid")
    return cast(ProjectPlanningJobStatus, normalized)


def _external_product_http_exception(exc: AIProjectOpsError) -> HTTPException:
    code = str(exc)
    if code.endswith("_not_configured"):
        return HTTPException(status_code=503, detail=code)
    if exc.status_code in {400, 401, 403, 404, 409, 410, 422, 429, 503}:
        return HTTPException(
            status_code=exc.status_code,
            detail=exc.detail if exc.detail is not None else code,
        )
    return HTTPException(status_code=502, detail=code)


app = create_app()
