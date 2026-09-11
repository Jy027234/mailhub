"""Application use cases for MailHub."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from mailhub.domain import (
    ActionType,
    AgentActionContext,
    AgentActionRequest,
    AutomationLevel,
    AutonomyRunStatus,
    CandidateState,
    CandidateType,
    ConnectionStatus,
    DelegationGrant,
    DeliveryStatus,
    MailActionCandidate,
    MailAgentPolicy,
    MailAutonomyRun,
    MailboxConnection,
    MailboxSyncState,
    MailDraft,
    MailMessageProjection,
    MailOutboxOperation,
    MailSyncJob,
    MailThread,
    MailThreadPage,
    MailThreadSummary,
    PolicyDecision,
    ProviderName,
    SubscriptionStatus,
    SyncJobStatus,
    digest_recipient_headers,
    digest_text,
    ensure_addresses,
    evaluate_policy,
    utc_now,
)
from mailhub.errors import (
    ApprovalRequiredError,
    AuthorizationError,
    ConflictError,
    KillSwitchError,
    MailHubError,
    NotFoundError,
    OutcomeUnknownError,
    PolicyDeniedError,
    ProviderFailureError,
    RateLimitedError,
    SyncCancelledError,
)
from mailhub.events import MailHubEventType, build_event
from mailhub.intelligence import (
    AI_RESULT_SCHEMA,
    DEFAULT_ANALYSIS_POLICY,
    AnalysisPolicy,
    IntelligenceResult,
    analyze_message,
    merge_ai_result,
)
from mailhub.memory import ApprovedKnowledgeReference
from mailhub.notifications import (
    ProviderLifecycleEvent,
    classify_provider_lifecycle,
)
from mailhub.oauth import OAuthProvider, validate_granted_scopes
from mailhub.observability import TelemetryPort, redact_event
from mailhub.pagination import (
    decode_thread_cursor,
    encode_thread_cursor,
    thread_page_scope_digest,
)
from mailhub.ports import (
    AgentMemoryPort,
    AiExecutionPort,
    ApprovalPort,
    CredentialBrokerPort,
    CredentialRefreshPort,
    CredentialRevocationPort,
    EventPublisherPort,
    HostActionPort,
    HostIdentityPort,
    KillSwitchPort,
    KnowledgeLifecyclePort,
    KnowledgeSafetyPort,
    KnowledgeSinkPort,
    MailRepository,
    ObjectStorePort,
    ProviderConnector,
    ProviderSendRequest,
    ProviderSubscriptionPort,
    ProviderSyncFilter,
)
from mailhub.quota import QuotaLease, QuotaLimits, QuotaPort
from mailhub.rules import (
    MailRule,
    RuleEvaluation,
    RuleExecution,
    RuleExecutionStatus,
    evaluate_rule,
    simulate_rules,
)
from mailhub.subscriptions import MailSubscriptionCoordinator

_SYNC_RETRY_MAX_ATTEMPTS = 5
_SYNC_RETRY_BASE_SECONDS = 30
_SYNC_RETRY_MAX_SECONDS = 3600


class InMemoryApprovalPort(ApprovalPort):
    """Explicit local approval adapter; production must use the host authority."""

    def __init__(self) -> None:
        self._approved: set[str] = set()

    def add_confirmation(self, confirmation_ref: str) -> None:
        """Seed a local test confirmation; production uses the host approval API."""

        if not confirmation_ref.strip():
            raise ValueError("confirmation_ref_required")
        self._approved.add(confirmation_ref)

    async def require_confirmation(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        action: AgentActionRequest,
        decision: PolicyDecision,
    ) -> str:
        reference = f"local-confirmation:{action.action_id}"
        self._approved.add(reference)
        return reference

    async def verify_confirmation(
        self,
        *,
        confirmation_ref: str,
        action: AgentActionRequest,
        approver_subject_id: str | None = None,
        revalidation: bool = False,
    ) -> bool:
        # A single-principal double cannot separate the requester from the
        # approver, so the identity is accepted and deliberately not enforced.
        # The conformance kit is expected to report separation of duties as
        # unproven for this port rather than as satisfied.
        del approver_subject_id, revalidation
        return (
            confirmation_ref in self._approved
            or confirmation_ref == f"local-confirmation:{action.action_id}"
        )


class StaticCredentialBroker(CredentialBrokerPort, CredentialRefreshPort, CredentialRevocationPort):
    """Test-only credential broker; values must never be persisted by MailHub."""

    def __init__(self, credentials: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self._credentials = {key: dict(value) for key, value in (credentials or {}).items()}

    async def resolve(
        self, *, credential_ref: str, tenant_id: str, subject_id: str
    ) -> Mapping[str, str]:
        del tenant_id, subject_id
        credential = self._credentials.get(credential_ref)
        if credential is None:
            raise AuthorizationError("credential_ref_unavailable")
        return dict(credential)

    async def revoke(
        self, *, credential_ref: str, tenant_id: str, subject_id: str
    ) -> Mapping[str, object]:
        del tenant_id, subject_id
        existed = credential_ref in self._credentials
        self._credentials.pop(credential_ref, None)
        return {
            "status": "revoked" if existed else "already_revoked",
        }

    async def refresh(
        self,
        *,
        credential_ref: str,
        tenant_id: str,
        subject_id: str,
        reason: str,
    ) -> Mapping[str, str]:
        del tenant_id, subject_id, reason
        credential = self._credentials.get(credential_ref)
        if credential is None:
            raise AuthorizationError("credential_ref_unavailable")
        allowed = {
            "credential_ref",
            "email_address",
            "provider_account_id",
            "provider_tenant_id",
            "credential_version",
        }
        return {key: value for key, value in credential.items() if key in allowed}


class MailService:
    """Coordinates provider-neutral mail use cases and durable send operations."""

    def __init__(
        self,
        repository: MailRepository,
        *,
        connectors: Mapping[ProviderName, ProviderConnector],
        credential_broker: CredentialBrokerPort | None = None,
        credential_refresh: CredentialRefreshPort | None = None,
        credential_revocation: CredentialRevocationPort | None = None,
        approval_port: ApprovalPort | None = None,
        host_action_port: HostActionPort | None = None,
        host_identity: HostIdentityPort | None = None,
        knowledge_sink: KnowledgeSinkPort | None = None,
        knowledge_lifecycle: KnowledgeLifecyclePort | None = None,
        agent_memory: AgentMemoryPort | None = None,
        knowledge_safety: KnowledgeSafetyPort | None = None,
        kill_switch: KillSwitchPort | None = None,
        object_store: ObjectStorePort | None = None,
        ai_execution: AiExecutionPort | None = None,
        telemetry: TelemetryPort | None = None,
        event_publisher: EventPublisherPort | None = None,
        quota_port: QuotaPort | None = None,
        provider_subscription_port: ProviderSubscriptionPort | None = None,
        quota_limits: QuotaLimits = QuotaLimits(),
        outbound_enabled: bool = False,
        rule_automation_enabled: bool = False,
        message_content_ttl: timedelta = timedelta(days=30),
        draft_content_ttl: timedelta = timedelta(hours=24),
        outbox_lease_ttl: timedelta = timedelta(minutes=5),
        worker_lease_ttl: timedelta = timedelta(minutes=5),
        intelligence_policy: AnalysisPolicy = DEFAULT_ANALYSIS_POLICY,
    ) -> None:
        if not 0 < message_content_ttl.total_seconds() <= timedelta(days=365).total_seconds():
            raise ValueError("message_content_ttl_invalid")
        if not 0 < draft_content_ttl.total_seconds() <= timedelta(days=30).total_seconds():
            raise ValueError("draft_content_ttl_invalid")
        if not 1 <= outbox_lease_ttl.total_seconds() <= timedelta(hours=1).total_seconds():
            raise ValueError("outbox_lease_ttl_invalid")
        if not 1 <= worker_lease_ttl.total_seconds() <= timedelta(hours=1).total_seconds():
            raise ValueError("worker_lease_ttl_invalid")
        self.intelligence_policy = intelligence_policy
        self.repository = repository
        self.connectors = dict(connectors)
        self.credential_broker = credential_broker
        self.credential_refresh = credential_refresh
        self.credential_revocation = credential_revocation
        self.approval_port = approval_port
        self.host_action_port = host_action_port
        self.host_identity = host_identity
        self.knowledge_sink = knowledge_sink
        self.knowledge_lifecycle = knowledge_lifecycle
        self.agent_memory = agent_memory
        self.knowledge_safety = knowledge_safety
        self.kill_switch = kill_switch
        self.object_store = object_store
        self.ai_execution = ai_execution
        self.telemetry = telemetry
        self.event_publisher = event_publisher
        self.quota_port = quota_port
        self.provider_subscription_port = provider_subscription_port
        self.quota_limits = quota_limits
        self.outbound_enabled = outbound_enabled
        self.rule_automation_enabled = rule_automation_enabled
        self.message_content_ttl = message_content_ttl
        self.draft_content_ttl = draft_content_ttl
        self.outbox_lease_ttl = outbox_lease_ttl
        self.worker_lease_ttl = worker_lease_ttl

    async def create_connection(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        provider: ProviderName,
        email_address: str,
        credential_ref: str,
        granted_scopes: tuple[str, ...] = (),
        provider_account_id: str | None = None,
        provider_tenant_id: str | None = None,
        credential_version: int = 1,
    ) -> MailboxConnection:
        _validate_provider_scope_input(provider, granted_scopes, require_read_only=False)
        connection = MailboxConnection(
            connection_id=uuid4(),
            tenant_id=tenant_id,
            subject_id=subject_id,
            provider=provider,
            email_address=email_address,
            credential_ref=credential_ref,
            granted_scopes=granted_scopes,
            provider_account_id=provider_account_id,
            provider_tenant_id=provider_tenant_id,
            credential_version=credential_version,
            status=(
                ConnectionStatus.ACTIVE
                if provider is ProviderName.SANDBOX
                else ConnectionStatus.PENDING_AUTHORIZATION
            ),
        )
        if provider not in self.connectors:
            raise ProviderFailureError("provider_connector_unavailable")
        await self.repository.save_connection(connection)
        await self.repository.append_audit(
            self._audit(
                "mail.connection.created",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection.connection_id),
                provider=provider.value,
                **self._connection_identity_fields(connection),
            )
        )
        if connection.status is ConnectionStatus.ACTIVE:
            await self._publish_event(
                MailHubEventType.CONNECTION_AUTHORIZED,
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection.connection_id),
                idempotency_key=f"connection:{connection.connection_id}:authorized:{connection.revision}",
                data={
                    "provider": provider.value,
                    "status": connection.status.value,
                    "scope_count": len(connection.granted_scopes),
                },
                occurred_at=connection.updated_at,
            )
        return connection

    async def list_connections(
        self, *, tenant_id: str, subject_id: str
    ) -> tuple[MailboxConnection, ...]:
        return await self.repository.list_connections(tenant_id=tenant_id, subject_id=subject_id)

    async def list_sync_states(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID
    ) -> tuple[MailboxSyncState, ...]:
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        return await self.repository.list_sync_states(
            tenant_id=tenant_id, connection_id=connection_id
        )

    async def connection_impact_preview(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        requested_folder_refs: tuple[str, ...] = (),
    ) -> Mapping[str, object]:
        """Return a bounded, projection-only impact preview for scope changes.

        The preview is intentionally read-only and never calls a Provider.  It
        gives the host enough information to show what a revoke/delete or
        folder-scope change would affect while explicitly separating durable
        MailHub projections from unknown remote mailbox state.  Counts are
        bounded by repository contracts and no body/object/credential value is
        returned.
        """

        connection = await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        states = await self.repository.list_sync_states(
            tenant_id=tenant_id, connection_id=connection_id
        )
        current_folders = _normalize_impact_folder_refs(
            tuple(state.folder_ref for state in states) or ("INBOX",)
        )
        requested_folders = _normalize_impact_folder_refs(requested_folder_refs or current_folders)
        cleanup_refs = await self.repository.list_connection_cleanup_refs(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        message_ids = set(cleanup_refs.message_ids)
        threads = tuple(
            thread
            for thread in await self.repository.list_threads(
                tenant_id=tenant_id, subject_id=subject_id, limit=200
            )
            if thread.connection_id == connection_id
        )
        candidates = tuple(
            candidate
            for candidate in await self.repository.list_candidates(
                tenant_id=tenant_id, subject_id=subject_id, status=None, limit=200
            )
            if candidate.message_id in message_ids
        )
        drafts = await self.repository.list_drafts(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            limit=200,
        )
        operations = await self.repository.list_operations(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            limit=200,
        )
        jobs = await self.repository.list_sync_jobs(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            limit=200,
        )
        project_hints: set[str] = set()
        for candidate in candidates:
            payload = candidate.payload
            values: list[object] = [payload.get("project_hint"), payload.get("project_ref")]
            target = payload.get("target")
            if isinstance(target, Mapping):
                values.extend((target.get("project_hint"), target.get("project_ref")))
            for value in values:
                if isinstance(value, str) and value.strip() and len(value.strip()) <= 200:
                    project_hints.add(value.strip())
        candidate_counts = {
            candidate_type.value: sum(
                1 for candidate in candidates if candidate.candidate_type is candidate_type
            )
            for candidate_type in CandidateType
        }
        object_count = len(cleanup_refs.object_refs)
        result: Mapping[str, object] = {
            "schema_version": "mailhub.connection_impact_preview.v1",
            "connection": {
                "connection_id": str(connection.connection_id),
                "provider": connection.provider.value,
                "email_address": connection.email_address,
                "status": connection.status.value,
                "revision": connection.revision,
            },
            "mode": "projection_only",
            "provider_query_performed": False,
            "scope": {
                "current_folder_refs": list(current_folders),
                "requested_folder_refs": list(requested_folders),
                "added_folder_refs": sorted(set(requested_folders) - set(current_folders)),
                "removed_folder_refs": sorted(set(current_folders) - set(requested_folders)),
                "remote_message_delta_estimate": None,
            },
            "counts": {
                "messages": len(cleanup_refs.message_ids),
                "threads": len(threads),
                "candidates": len(candidates),
                "candidate_types": candidate_counts,
                "drafts": len(drafts),
                "outbox_operations": len(operations),
                "sync_jobs": len(jobs),
                "subscription_states": len(states),
                "governed_object_refs_estimate": object_count,
            },
            "project_hints": sorted(project_hints),
            "deletion_effects": {
                "provider_access": "revoke_before_cleanup",
                "projection_cleanup": "connection_scoped",
                "knowledge_lifecycle_proof_required": bool(cleanup_refs.knowledge_message_ids),
                "object_store_cleanup_proof_required": object_count > 0,
                "remote_provider_data": "unknown_until_provider_confirmation",
            },
            "warnings": [
                "remote_message_delta_not_estimated_without_provider_query",
                "folder_refs_are_projection_watermarks_not_provider_folder_catalog",
                "candidate_thread_draft_operation_job_counts_are_bounded_to_200",
            ],
        }
        await self.repository.append_audit(
            self._audit(
                "mail.connection.impact_previewed",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                current_folder_count=len(current_folders),
                requested_folder_count=len(requested_folders),
                message_count=len(cleanup_refs.message_ids),
                candidate_count=len(candidates),
            )
        )
        return result

    async def provider_health(
        self, *, tenant_id: str, subject_id: str
    ) -> tuple[Mapping[str, object], ...]:
        """Return bounded connection/provider health without body or credential data."""

        results: list[Mapping[str, object]] = []
        for connection in await self.list_connections(tenant_id=tenant_id, subject_id=subject_id):
            if connection.status is not ConnectionStatus.ACTIVE:
                # A health projection must not become a covert Provider access
                # path after revocation, deletion or reauthorization failure.
                # Keep the bounded state visible so the host can prove the
                # negative path without invoking a connector.
                results.append(
                    {
                        "connection_id": str(connection.connection_id),
                        "provider": connection.provider.value,
                        "status": connection.status.value,
                        "reason": "connection_not_active",
                    }
                )
                continue
            connector = self.connectors.get(connection.provider)
            if connector is None:
                results.append(
                    {
                        "connection_id": str(connection.connection_id),
                        "provider": connection.provider.value,
                        "status": "blocked",
                        "reason": "connector_unavailable",
                    }
                )
                continue
            try:
                report = await connector.health_check(connection)
                results.append(
                    {
                        "connection_id": str(connection.connection_id),
                        "provider": connection.provider.value,
                        "status": str(report.get("status", "unknown")),
                        "report": dict(report),
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "connection_id": str(connection.connection_id),
                        "provider": connection.provider.value,
                        "status": "degraded",
                        "reason": type(exc).__name__,
                    }
                )
        return tuple(results)

    async def ensure_provider_subscription(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        callback_endpoint: str,
        desired_expiry: datetime,
        folder_ref: str = "INBOX",
        client_state_ref: str | None = None,
        idempotency_key: str,
    ) -> MailboxSyncState:
        """Create/renew a host-owned Gmail or Graph subscription.

        MailHub persists only the non-secret lease metadata.  The provider
        token, Pub/Sub OIDC verification, Graph clientState and renewal
        scheduler remain in the host adapter.
        """

        subscription_port = self.provider_subscription_port
        if subscription_port is None:
            raise ProviderFailureError("provider_subscription_unconfigured")
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        coordinator = MailSubscriptionCoordinator(
            self.repository,
            subscription_port=subscription_port,
            event_publisher=self.event_publisher,
        )
        return await coordinator.ensure(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            callback_endpoint=callback_endpoint,
            desired_expiry=desired_expiry,
            folder_ref=folder_ref,
            client_state_ref=client_state_ref,
            idempotency_key=idempotency_key,
        )

    async def cancel_provider_subscription(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        request_id: str,
        folder_ref: str = "INBOX",
    ) -> MailboxSyncState | None:
        """Cancel one persisted subscription before revoke/delete side effects."""

        subscription_port = self.provider_subscription_port
        if subscription_port is None:
            raise ProviderFailureError("provider_subscription_unconfigured")
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        coordinator = MailSubscriptionCoordinator(
            self.repository,
            subscription_port=subscription_port,
            event_publisher=self.event_publisher,
        )
        return await coordinator.cancel(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            folder_ref=folder_ref,
            request_id=request_id,
        )

    async def renew_provider_subscription_if_due(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        folder_ref: str = "INBOX",
        renewal_window: timedelta = timedelta(hours=24),
        desired_expiry: datetime | None = None,
    ) -> MailboxSyncState | None:
        """Bounded scheduler unit for subscription renewal/recovery."""

        subscription_port = self.provider_subscription_port
        if subscription_port is None:
            raise ProviderFailureError("provider_subscription_unconfigured")
        connection = await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        coordinator = MailSubscriptionCoordinator(
            self.repository,
            subscription_port=subscription_port,
            event_publisher=self.event_publisher,
        )
        return await coordinator.renew_if_due(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            folder_ref=folder_ref,
            renewal_window=renewal_window,
            desired_expiry=desired_expiry,
        )

    async def mark_provider_subscription_expired(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        folder_ref: str = "INBOX",
        now: datetime | None = None,
    ) -> MailboxSyncState | None:
        subscription_port = self.provider_subscription_port
        if subscription_port is None:
            raise ProviderFailureError("provider_subscription_unconfigured")
        connection = await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        return await MailSubscriptionCoordinator(
            self.repository,
            subscription_port=subscription_port,
            event_publisher=self.event_publisher,
        ).mark_expired(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            folder_ref=folder_ref,
            now=now,
        )

    async def mark_provider_subscription_failed(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        folder_ref: str = "INBOX",
        error_code: str = "subscription_renewal_failed",
        now: datetime | None = None,
    ) -> MailboxSyncState | None:
        subscription_port = self.provider_subscription_port
        if subscription_port is None:
            raise ProviderFailureError("provider_subscription_unconfigured")
        connection = await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        return await MailSubscriptionCoordinator(
            self.repository,
            subscription_port=subscription_port,
            event_publisher=self.event_publisher,
        ).mark_failed(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            folder_ref=folder_ref,
            error_code=error_code,
            now=now,
        )

    async def record_provider_notification(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        subscription_ref: str,
        received_at: datetime,
        folder_ref: str = "INBOX",
        notification_id: str | None = None,
        change_kind: str | None = None,
        lifecycle_event: str | None = None,
        cursor_hint: str | None = None,
        trace_id: str | None = None,
    ) -> MailboxSyncState:
        """Advance a verified notification watermark without accepting a body."""

        connection = await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        subscription_port = self.provider_subscription_port
        if subscription_port is None:
            raise ProviderFailureError("provider_subscription_unconfigured")
        coordinator = MailSubscriptionCoordinator(
            self.repository,
            subscription_port=subscription_port,
            event_publisher=self.event_publisher,
        )
        state = await coordinator.record_notification(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            subscription_ref=subscription_ref,
            received_at=received_at,
            folder_ref=folder_ref,
            notification_id=notification_id,
            change_kind=change_kind,
            lifecycle_event=lifecycle_event,
            cursor_hint=cursor_hint,
            trace_id=trace_id,
        )
        lifecycle = classify_provider_lifecycle(lifecycle_event)
        if lifecycle in {
            ProviderLifecycleEvent.REAUTHORIZATION_REQUIRED,
            ProviderLifecycleEvent.SUBSCRIPTION_REMOVED,
        }:
            renewed_state = await coordinator.mark_renewal_required(
                tenant_id=tenant_id,
                subject_id=subject_id,
                connection=connection,
                folder_ref=folder_ref,
                error_code=f"provider_{lifecycle.value}",
                now=received_at,
            )
            if renewed_state is not None:
                state = renewed_state
        await self._apply_provider_lifecycle_event(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            lifecycle=lifecycle,
            notification_id=notification_id,
            trace_id=trace_id,
        )
        return state

    async def _apply_provider_lifecycle_event(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        lifecycle: ProviderLifecycleEvent | None,
        notification_id: str | None,
        trace_id: str | None,
    ) -> MailboxConnection | None:
        """Turn trusted provider lifecycle metadata into a fenced connection state."""

        if lifecycle is None:
            return None
        current = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if current is None or current.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        if current.status in {
            ConnectionStatus.REVOKED,
            ConnectionStatus.DELETING,
            ConnectionStatus.DELETED,
        }:
            # Terminal/revoked connections must not be reopened by a late
            # provider callback.  The receipt remains auditable, but no state
            # transition or provider work is allowed.
            await self.repository.append_audit(
                self._audit(
                    "mail.provider.lifecycle_ignored",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(connection_id),
                    lifecycle_event=lifecycle.value,
                    connection_status=current.status.value,
                )
            )
            return current
        target_status = (
            ConnectionStatus.REAUTHORIZATION_REQUIRED
            if lifecycle is ProviderLifecycleEvent.REAUTHORIZATION_REQUIRED
            else ConnectionStatus.DEGRADED
        )
        # Never downgrade a stronger authorization stop to a degraded state.
        if current.status is ConnectionStatus.REAUTHORIZATION_REQUIRED:
            target_status = current.status
        if current.status is target_status:
            return current
        updated = replace(
            current,
            status=target_status,
            revision=current.revision + 1,
            updated_at=utc_now(),
        )
        saved = await self.repository.save_connection(updated)
        notification_hash = (
            sha256(notification_id.encode("utf-8")).hexdigest() if notification_id else None
        )
        await self.repository.append_audit(
            self._audit(
                "mail.provider.lifecycle_applied",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                lifecycle_event=lifecycle.value,
                connection_status=target_status.value,
                notification_id_sha256=notification_hash,
                trace_id=trace_id,
            )
        )
        if target_status is ConnectionStatus.REAUTHORIZATION_REQUIRED:
            await self._publish_event(
                MailHubEventType.CONNECTION_REAUTHORIZATION_REQUIRED,
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                idempotency_key=(
                    f"connection:{connection_id}:provider-lifecycle:"
                    f"{notification_id or lifecycle.value}"
                ),
                data={
                    "provider": saved.provider.value,
                    "connection_status": saved.status.value,
                    "lifecycle_event": lifecycle.value,
                },
                occurred_at=saved.updated_at,
            )
        else:
            await self._publish_event(
                MailHubEventType.SYNC_RECONCILE_REQUIRED,
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                idempotency_key=(
                    f"sync:{connection_id}:provider-lifecycle:{notification_id or lifecycle.value}"
                ),
                data={
                    "provider": saved.provider.value,
                    "connection_status": saved.status.value,
                    "lifecycle_event": lifecycle.value,
                },
                occurred_at=saved.updated_at,
            )
        return saved

    async def _cancel_connection_subscriptions(
        self, *, tenant_id: str, subject_id: str, connection: MailboxConnection, reason: str
    ) -> None:
        states = await self.repository.list_sync_states(
            tenant_id=tenant_id, connection_id=connection.connection_id
        )
        active_states = tuple(
            state
            for state in states
            if state.subscription_ref
            and state.subscription_status
            not in {SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED}
        )
        if not active_states:
            return
        if self.provider_subscription_port is None:
            raise ProviderFailureError("provider_subscription_unconfigured")
        coordinator = MailSubscriptionCoordinator(
            self.repository,
            subscription_port=self.provider_subscription_port,
            event_publisher=self.event_publisher,
        )
        for state in active_states:
            await coordinator.cancel(
                tenant_id=tenant_id,
                subject_id=subject_id,
                connection=connection,
                folder_ref=state.folder_ref,
                request_id=f"mailhub:subscription:cancel:{connection.connection_id}:{state.folder_ref}:{reason}",
            )

    async def _revoke_connection_credential(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection: MailboxConnection,
        reason: str,
    ) -> dict[str, str] | None:
        """Revoke a real-provider grant before exposing a revoked connection.

        ``:revoke`` is the negative-access boundary: a non-sandbox connection
        must not be marked revoked while its host credential remains usable.
        The host returns only a bounded status proof; credential references and
        provider tokens are never copied into MailHub audit/event data.
        """

        if connection.provider is ProviderName.SANDBOX:
            return None
        if self.credential_revocation is None:
            await self.repository.append_audit(
                self._audit(
                    "mail.connection.revocation_blocked",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(connection.connection_id),
                    reason="credential_revocation_unconfigured",
                )
            )
            raise ProviderFailureError("credential_revocation_unconfigured")
        try:
            raw_result = await self.credential_revocation.revoke(
                credential_ref=connection.credential_ref,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            proof = _validate_credential_revocation_metadata(raw_result)
        except Exception as exc:
            await self.repository.append_audit(
                self._audit(
                    "mail.connection.revocation_blocked",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(connection.connection_id),
                    reason="credential_revoke_failed",
                )
            )
            if isinstance(exc, ProviderFailureError):
                raise
            raise ProviderFailureError("credential_revoke_failed") from exc
        await self.repository.append_audit(
            self._audit(
                "mail.connection.credential_revoked",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection.connection_id),
                reason=reason,
                revocation_status=proof["status"],
            )
        )
        return proof

    async def revoke_connection(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID, expected_revision: int
    ) -> MailboxConnection:
        # Resolve the row directly instead of using ``_connection``: the
        # active-only helper intentionally rejects REVOKED rows, while this
        # command must be idempotent after a successful revoke.  Keep the
        # subject fence and terminal-state checks explicit here so a retry
        # cannot reopen a revoked/deleting connection.
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        if connection.revision != expected_revision:
            raise ConflictError("connection_revision_conflict")
        if connection.status is ConnectionStatus.REVOKED:
            return connection
        if connection.status in {ConnectionStatus.DELETING, ConnectionStatus.DELETED}:
            raise ConflictError("connection_not_revokeable")
        if connection.status in {
            ConnectionStatus.PENDING_AUTHORIZATION,
            ConnectionStatus.REAUTHORIZATION_REQUIRED,
        }:
            raise AuthorizationError("connection_not_active")
        cleanup_refs = await self.repository.list_connection_cleanup_refs(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        knowledge_message_ids = cleanup_refs.knowledge_message_ids
        knowledge_lifecycle_proof: Mapping[str, object] | None = None
        if knowledge_message_ids:
            if self.knowledge_lifecycle is None:
                await self.repository.append_audit(
                    self._audit(
                        "mail.connection.revocation_blocked",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        target_ref=str(connection_id),
                        reason="knowledge_lifecycle_unconfigured",
                    )
                )
                raise ProviderFailureError("knowledge_lifecycle_unconfigured")
            request_id = f"mailhub:knowledge-source-revoke:{connection_id}:{connection.revision}"
            try:
                lifecycle_result = await self.knowledge_lifecycle.revoke_source(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    connection_id=connection_id,
                    message_ids=knowledge_message_ids,
                    reason="mail_connection_revoked",
                    request_id=request_id,
                )
                knowledge_lifecycle_proof = _knowledge_lifecycle_proof(
                    lifecycle_result, request_id=request_id
                )
            except Exception as exc:
                await self.repository.append_audit(
                    self._audit(
                        "mail.connection.revocation_blocked",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        target_ref=str(connection_id),
                        reason="knowledge_lifecycle_failed",
                    )
                )
                raise ProviderFailureError("knowledge_lifecycle_failed") from exc
        try:
            await self._cancel_connection_subscriptions(
                tenant_id=tenant_id,
                subject_id=subject_id,
                connection=connection,
                reason="connection_revoked",
            )
        except Exception as exc:
            await self.repository.append_audit(
                self._audit(
                    "mail.connection.revocation_blocked",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(connection_id),
                    reason="subscription_cancel_failed",
                )
            )
            if isinstance(exc, ProviderFailureError):
                raise
            raise ProviderFailureError("subscription_cancel_failed") from exc
        credential_revocation_proof = await self._revoke_connection_credential(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection=connection,
            reason="connection_revoked",
        )
        updated = replace(
            connection,
            status=ConnectionStatus.REVOKED,
            revision=connection.revision + 1,
            updated_at=utc_now(),
        )
        result = await self.repository.save_connection(updated)
        await self.repository.append_audit(
            self._audit(
                "mail.connection.revoked",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                revision=expected_revision,
                knowledge_lifecycle=knowledge_lifecycle_proof,
                provider_grant_status=(
                    credential_revocation_proof["status"]
                    if credential_revocation_proof is not None
                    else None
                ),
            )
        )
        await self._publish_event(
            MailHubEventType.CONNECTION_REVOKED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(connection_id),
            idempotency_key=f"connection:{connection_id}:revoked:{result.revision}",
            data={
                "provider": result.provider.value,
                "status": result.status.value,
                "revision": result.revision,
                "knowledge_lifecycle": knowledge_lifecycle_proof is not None,
                "provider_grant_revoked": credential_revocation_proof is not None,
            },
            occurred_at=result.updated_at,
        )
        return result

    async def delete_connection(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        expected_revision: int,
    ) -> MailboxConnection:
        """Revoke credentials, remove connection-owned data, and retain a tombstone.

        The operation is deliberately fail-closed.  A non-sandbox connection
        cannot become ``deleted`` unless the host credential authority confirms
        revocation and every governed object reference has been handed to the
        object store for deletion.  A failed attempt remains ``deleting`` so a
        worker can retry without reopening provider access.
        """

        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        if connection.status is ConnectionStatus.DELETED:
            # A completed deletion is idempotent.  The caller may be retrying
            # the original command with the pre-delete revision after a
            # network timeout; requiring the tombstone revision here would
            # turn a safe retry into a misleading conflict.
            return connection
        if connection.status is ConnectionStatus.DELETING:
            # A failed object/credential cleanup leaves a DELETING row.  A
            # retry must resume that same fenced revision instead of creating
            # a second deletion operation or reopening provider access.
            if connection.revision != expected_revision:
                raise ConflictError("connection_revision_conflict")
            deleting = connection
        else:
            if connection.revision != expected_revision:
                raise ConflictError("connection_revision_conflict")
            deleting = replace(
                connection,
                status=ConnectionStatus.DELETING,
                revision=connection.revision + 1,
                updated_at=utc_now(),
            )
            await self.repository.save_connection(deleting)
            await self.repository.append_audit(
                self._audit(
                    "mail.connection.deletion_started",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(connection_id),
                    revision=expected_revision,
                )
            )

        try:
            await self._cancel_connection_subscriptions(
                tenant_id=tenant_id,
                subject_id=subject_id,
                connection=connection,
                reason="connection_deleted",
            )
        except Exception as exc:
            await self.repository.append_audit(
                self._audit(
                    "mail.connection.deletion_blocked",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(connection_id),
                    reason="subscription_cancel_failed",
                )
            )
            if isinstance(exc, ProviderFailureError):
                raise
            raise ProviderFailureError("subscription_cancel_failed") from exc

        cleanup_refs = await self.repository.list_connection_cleanup_refs(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        object_refs = set(cleanup_refs.object_refs)
        if object_refs:
            if self.object_store is None:
                raise ProviderFailureError("object_store_unconfigured")
            for object_ref in sorted(object_refs):
                try:
                    await self.object_store.delete(
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        object_ref=object_ref,
                    )
                except Exception as exc:
                    await self.repository.append_audit(
                        self._audit(
                            "mail.connection.deletion_blocked",
                            tenant_id=tenant_id,
                            subject_id=subject_id,
                            target_ref=str(connection_id),
                            reason="object_delete_failed",
                        )
                    )
                    raise ProviderFailureError("object_delete_failed") from exc

        credential_revocation_proof: dict[str, str] | None = None
        if connection.status is not ConnectionStatus.REVOKED:
            try:
                credential_revocation_proof = await self._revoke_connection_credential(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    connection=connection,
                    reason="connection_deleted",
                )
            except Exception as exc:
                await self.repository.append_audit(
                    self._audit(
                        "mail.connection.deletion_blocked",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        target_ref=str(connection_id),
                        reason="credential_revoke_failed",
                    )
                )
                if isinstance(exc, ProviderFailureError):
                    raise
                raise ProviderFailureError("credential_revoke_failed") from exc

        knowledge_message_ids = cleanup_refs.knowledge_message_ids
        knowledge_lifecycle_proof: Mapping[str, object] | None = None
        if knowledge_message_ids:
            if self.knowledge_lifecycle is None:
                await self.repository.append_audit(
                    self._audit(
                        "mail.connection.deletion_blocked",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        target_ref=str(connection_id),
                        reason="knowledge_lifecycle_unconfigured",
                    )
                )
                raise ProviderFailureError("knowledge_lifecycle_unconfigured")
            request_id = f"mailhub:knowledge-source-revoke:{connection_id}:{deleting.revision}"
            try:
                lifecycle_result = await self.knowledge_lifecycle.revoke_source(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    connection_id=connection_id,
                    message_ids=knowledge_message_ids,
                    reason="mail_connection_deleted",
                    request_id=request_id,
                )
                knowledge_lifecycle_proof = _knowledge_lifecycle_proof(
                    lifecycle_result, request_id=request_id
                )
            except Exception as exc:
                await self.repository.append_audit(
                    self._audit(
                        "mail.connection.deletion_blocked",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        target_ref=str(connection_id),
                        reason="knowledge_lifecycle_failed",
                    )
                )
                raise ProviderFailureError("knowledge_lifecycle_failed") from exc

        counts = await self.repository.delete_connection_data(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
        )
        deleted = replace(
            deleting,
            status=ConnectionStatus.DELETED,
            credential_ref=f"deleted:{connection_id}",
            granted_scopes=(),
            revision=deleting.revision + 1,
            updated_at=utc_now(),
        )
        result = await self.repository.save_connection(deleted)
        await self.repository.append_audit(
            self._audit(
                "mail.connection.deleted",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                deletion_proof={
                    "object_refs_deleted": len(object_refs),
                    **dict(counts),
                    **(
                        {"provider_grant_status": credential_revocation_proof["status"]}
                        if credential_revocation_proof is not None
                        else {}
                    ),
                    **(
                        {"knowledge_lifecycle": dict(knowledge_lifecycle_proof)}
                        if knowledge_lifecycle_proof is not None
                        else {}
                    ),
                },
            )
        )
        await self._publish_event(
            MailHubEventType.CONNECTION_DELETED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(connection_id),
            idempotency_key=f"connection:{connection_id}:deleted:{result.revision}",
            data={
                "provider": result.provider.value,
                "status": result.status.value,
                "revision": result.revision,
                "object_refs_deleted": len(object_refs),
                "knowledge_lifecycle": knowledge_lifecycle_proof is not None,
            },
            occurred_at=result.updated_at,
        )
        return result

    async def export_subject_data(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        include_content: bool = False,
        limit: int = 200,
    ) -> Mapping[str, object]:
        """Return a bounded, user-scoped export with no credentials or object refs."""

        if not 1 <= limit <= 200:
            raise ValueError("export_limit_invalid")
        connections = await self.repository.list_connections(
            tenant_id=tenant_id, subject_id=subject_id
        )
        messages: list[MailMessageProjection] = []
        for connection in connections:
            messages.extend(
                await self.repository.list_messages_for_connection(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    connection_id=connection.connection_id,
                    limit=limit,
                )
            )
        messages = sorted(messages, key=lambda item: item.received_at, reverse=True)[:limit]
        drafts = await self.repository.list_drafts(
            tenant_id=tenant_id, subject_id=subject_id, limit=limit
        )
        operations = await self.repository.list_operations(
            tenant_id=tenant_id, subject_id=subject_id, limit=limit
        )
        candidates = await self.repository.list_candidates(
            tenant_id=tenant_id, subject_id=subject_id, limit=min(limit, 200)
        )
        threads = await self.repository.list_threads(
            tenant_id=tenant_id, subject_id=subject_id, limit=min(limit, 200)
        )
        rules = await self.repository.list_rules(
            tenant_id=tenant_id, subject_id=subject_id, limit=min(limit, 200)
        )
        rule_executions = await self.repository.list_rule_executions(
            tenant_id=tenant_id,
            subject_id=subject_id,
            rule_id=None,
            limit=min(limit, 200),
        )
        audits = await self.repository.list_audit_events(
            tenant_id=tenant_id, subject_id=subject_id, limit=min(limit, 200)
        )

        exported_messages: list[dict[str, object]] = []
        for message in messages:
            item: dict[str, object] = {
                "message_id": str(message.message_id),
                "connection_id": str(message.connection_id),
                "thread_id": str(message.thread_id),
                "provider_message_ref": message.provider_message_ref,
                "internet_message_id": message.internet_message_id,
                "sender_address": message.sender_address,
                "recipient_addresses": list(message.recipient_addresses),
                "subject": message.subject,
                "received_at": message.received_at.isoformat(),
                "labels": list(message.labels),
                "is_read": message.is_read,
                "revision": message.revision,
                "content_sha256": message.content_sha256,
            }
            if include_content:
                hydrated = await self._hydrate_message(
                    message, tenant_id=tenant_id, subject_id=subject_id
                )
                item["body_text"] = hydrated.body_text
            exported_messages.append(item)

        exported_drafts: list[dict[str, object]] = []
        for draft in drafts:
            item = {
                "draft_id": str(draft.draft_id),
                "connection_id": str(draft.connection_id),
                "thread_id": str(draft.thread_id) if draft.thread_id else None,
                "recipient_addresses": list(draft.recipient_addresses),
                "cc_addresses": list(draft.cc_addresses),
                "bcc_addresses": list(draft.bcc_addresses),
                # Governed attachment refs are intentionally not part of an
                # export payload; callers can see bounded metadata without
                # receiving object-store namespace handles.
                "attachment_count": len(draft.attachment_refs),
                "subject": draft.subject,
                "content_sha256": draft.content_sha256,
                "revision": draft.revision,
                "status": draft.status.value,
                "expires_at": draft.expires_at.isoformat(),
                "updated_at": draft.updated_at.isoformat(),
            }
            if include_content:
                item["body_text"] = await self._draft_body(
                    draft, tenant_id=tenant_id, subject_id=subject_id
                )
            exported_drafts.append(item)

        export_ref = str(uuid4())
        payload: dict[str, object] = {
            "schema_version": "mailhub.data_export.v1",
            "export_ref": export_ref,
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            "exported_at": utc_now().isoformat(),
            "include_content": include_content,
            "connections": [
                {
                    "connection_id": str(connection.connection_id),
                    "provider": connection.provider.value,
                    "email_address": connection.email_address,
                    "granted_scopes": list(connection.granted_scopes),
                    "content_mode": connection.content_mode.value,
                    "status": connection.status.value,
                    "revision": connection.revision,
                    "created_at": connection.created_at.isoformat(),
                    "updated_at": connection.updated_at.isoformat(),
                }
                for connection in connections
            ],
            "messages": exported_messages,
            "threads": _export_safe(threads),
            "candidates": _export_safe(candidates),
            "drafts": exported_drafts,
            "operations": _export_safe(operations),
            "rules": _export_safe(rules),
            "rule_executions": _export_safe(rule_executions),
            "audit": _export_safe(audits),
        }
        await self.repository.append_audit(
            self._audit(
                "mail.data.exported",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=export_ref,
                include_content=include_content,
                message_count=len(exported_messages),
                draft_count=len(exported_drafts),
            )
        )
        return payload

    async def activate_connection(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID, expected_revision: int
    ) -> MailboxConnection:
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        if connection.revision != expected_revision:
            raise ConflictError("connection_revision_conflict")
        if connection.status in {ConnectionStatus.REVOKED, ConnectionStatus.DELETED}:
            raise ConflictError("connection_not_activatable")
        _validate_provider_scope_input(
            connection.provider,
            connection.granted_scopes,
            require_read_only=True,
        )
        updated = replace(
            connection,
            status=ConnectionStatus.ACTIVE,
            revision=connection.revision + 1,
            updated_at=utc_now(),
        )
        result = await self.repository.save_connection(updated)
        await self._publish_event(
            MailHubEventType.CONNECTION_AUTHORIZED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(connection_id),
            idempotency_key=f"connection:{connection_id}:authorized:{result.revision}",
            data={
                "provider": result.provider.value,
                "status": result.status.value,
                "scope_count": len(result.granted_scopes),
                "revision": result.revision,
                **self._connection_identity_fields(result),
            },
            occurred_at=result.updated_at,
        )
        return result

    async def reauthorize_connection(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        expected_revision: int,
        email_address: str,
        credential_ref: str,
        granted_scopes: tuple[str, ...],
        provider_account_id: str | None = None,
        provider_tenant_id: str | None = None,
        credential_version: int | None = None,
    ) -> MailboxConnection:
        """Rotate a host credential while preserving one connection identity.

        Provider account identity is the stable binding.  If a host reports a
        different account or tenant during reauthorization, the old
        connection is left untouched and the callback fails closed.
        """

        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        if connection.revision != expected_revision:
            raise ConflictError("connection_revision_conflict")
        if connection.status in {ConnectionStatus.DELETING, ConnectionStatus.DELETED}:
            raise ConflictError("connection_not_reauthorizable")
        _validate_provider_scope_input(
            connection.provider,
            granted_scopes,
            require_read_only=True,
        )
        if (
            provider_account_id is not None
            and connection.provider_account_id is not None
            and provider_account_id != connection.provider_account_id
        ):
            raise ProviderFailureError("provider_account_identity_mismatch")
        if (
            provider_tenant_id is not None
            and connection.provider_tenant_id is not None
            and provider_tenant_id != connection.provider_tenant_id
        ):
            raise ProviderFailureError("provider_tenant_identity_mismatch")
        if credential_version is not None and credential_version < connection.credential_version:
            raise ConflictError("credential_version_rollback")
        next_credential_version = max(
            credential_version or connection.credential_version,
            connection.credential_version,
        )
        updated = replace(
            connection,
            email_address=email_address,
            credential_ref=credential_ref,
            granted_scopes=granted_scopes,
            provider_account_id=provider_account_id or connection.provider_account_id,
            provider_tenant_id=provider_tenant_id or connection.provider_tenant_id,
            credential_version=next_credential_version,
            status=ConnectionStatus.ACTIVE,
            revision=connection.revision + 1,
            updated_at=utc_now(),
        )
        result = await self.repository.save_connection(updated)
        identity_fields = self._connection_identity_fields(result)
        await self.repository.append_audit(
            self._audit(
                "mail.connection.reauthorized",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                revision=expected_revision,
                **identity_fields,
            )
        )
        await self._publish_event(
            MailHubEventType.CONNECTION_AUTHORIZED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(connection_id),
            idempotency_key=f"connection:{connection_id}:reauthorized:{result.revision}",
            data={
                "provider": result.provider.value,
                "status": result.status.value,
                "revision": result.revision,
                "scope_count": len(result.granted_scopes),
                "reauthorized": True,
                **identity_fields,
            },
            occurred_at=result.updated_at,
        )
        return result

    async def refresh_connection(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        expected_revision: int,
        reason: str = "scheduled_refresh",
    ) -> MailboxConnection:
        """Ask the Host to refresh a provider grant without exposing tokens.

        The Host may rotate its secret version or credential reference.  Only
        non-secret mailbox identity metadata crosses this boundary; the
        existing connection stays fenced to the same provider account/tenant.
        """

        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        if connection.revision != expected_revision:
            raise ConflictError("connection_revision_conflict")
        if connection.status in {
            ConnectionStatus.DELETING,
            ConnectionStatus.DELETED,
            ConnectionStatus.REVOKED,
        }:
            raise ConflictError("connection_not_refreshable")
        reason_value = reason.strip()
        if (
            not reason_value
            or len(reason_value) > 500
            or any(ord(char) < 32 or ord(char) == 127 for char in reason_value)
        ):
            raise ValueError("credential_refresh_reason_invalid")
        if self.credential_refresh is None:
            raise ProviderFailureError("credential_refresh_unconfigured")
        try:
            raw_metadata = await self.credential_refresh.refresh(
                credential_ref=connection.credential_ref,
                tenant_id=tenant_id,
                subject_id=subject_id,
                reason=reason_value,
            )
        except MailHubError:
            raise
        except Exception as exc:
            raise ProviderFailureError("credential_refresh_failed") from exc
        metadata = _validate_credential_refresh_metadata(raw_metadata)
        provider_account_id = metadata.get("provider_account_id")
        if (
            provider_account_id is not None
            and connection.provider_account_id is not None
            and provider_account_id != connection.provider_account_id
        ):
            raise ProviderFailureError("provider_account_identity_mismatch")
        provider_tenant_id = metadata.get("provider_tenant_id")
        if (
            provider_tenant_id is not None
            and connection.provider_tenant_id is not None
            and provider_tenant_id != connection.provider_tenant_id
        ):
            raise ProviderFailureError("provider_tenant_identity_mismatch")
        credential_version = _credential_version_from_metadata(metadata.get("credential_version"))
        if credential_version is not None and credential_version < connection.credential_version:
            raise ConflictError("credential_version_rollback")
        next_credential_version = max(
            connection.credential_version, credential_version or connection.credential_version
        )
        updated = replace(
            connection,
            email_address=metadata.get("email_address", connection.email_address),
            credential_ref=metadata.get("credential_ref", connection.credential_ref),
            provider_account_id=provider_account_id or connection.provider_account_id,
            provider_tenant_id=provider_tenant_id or connection.provider_tenant_id,
            credential_version=next_credential_version,
            status=ConnectionStatus.ACTIVE,
            revision=connection.revision + 1,
            updated_at=utc_now(),
        )
        result = await self.repository.save_connection(updated)
        identity_fields = self._connection_identity_fields(result)
        await self.repository.append_audit(
            self._audit(
                "mail.connection.credentials.refreshed",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                revision=expected_revision,
                reason_sha256=digest_text(reason_value),
                **identity_fields,
            )
        )
        await self._publish_event(
            MailHubEventType.CONNECTION_AUTHORIZED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(connection_id),
            idempotency_key=f"connection:{connection_id}:refreshed:{result.revision}",
            data={
                "provider": result.provider.value,
                "status": result.status.value,
                "revision": result.revision,
                "credential_ref_rotated": result.credential_ref != connection.credential_ref,
                "refreshed": True,
                **identity_fields,
            },
            occurred_at=result.updated_at,
        )
        return result

    async def update_connection_scopes(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        expected_revision: int,
        granted_scopes: tuple[str, ...],
    ) -> MailboxConnection:
        """Narrow the locally usable scope set without expanding OAuth consent."""

        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        if connection.revision != expected_revision:
            raise ConflictError("connection_revision_conflict")
        if connection.status in {ConnectionStatus.DELETING, ConnectionStatus.DELETED}:
            raise ConflictError("connection_not_updatable")
        if (
            not granted_scopes
            or len(granted_scopes) > 40
            or any(
                not isinstance(scope, str)
                or not scope.strip()
                or len(scope.strip()) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in scope)
                for scope in granted_scopes
            )
        ):
            raise ValueError("connection_scopes_invalid")
        normalized = tuple(dict.fromkeys(scope.strip() for scope in granted_scopes))
        _validate_provider_scope_input(
            connection.provider,
            normalized,
            require_read_only=False,
        )
        if not set(normalized).issubset(set(connection.granted_scopes)):
            raise ConflictError("scope_expansion_requires_reauthorization")
        if normalized == connection.granted_scopes:
            return connection
        updated = replace(
            connection,
            granted_scopes=normalized,
            revision=connection.revision + 1,
            updated_at=utc_now(),
        )
        result = await self.repository.save_connection(updated)
        scope_digest = digest_text("\n".join(normalized))
        await self.repository.append_audit(
            self._audit(
                "mail.connection.scopes.updated",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                revision=expected_revision,
                scope_count=len(normalized),
                scope_digest=scope_digest,
            )
        )
        await self._publish_event(
            MailHubEventType.CONNECTION_AUTHORIZED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(connection_id),
            idempotency_key=f"connection:{connection_id}:scopes:{result.revision}",
            data={
                "provider": result.provider.value,
                "status": result.status.value,
                "revision": result.revision,
                "scope_count": len(normalized),
                "scope_digest": scope_digest,
                "scope_update": True,
            },
            occurred_at=result.updated_at,
        )
        return result

    async def sync_connection(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        limit: int = 50,
        mode: str = "incremental",
        sync_filter: ProviderSyncFilter | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> dict[str, object]:
        """Synchronize one bounded provider page and emit safe telemetry."""

        normalized_mode = mode.casefold().strip()
        if normalized_mode not in {"incremental", "backfill", "reconcile"}:
            raise ValueError("sync_mode_invalid")
        if sync_filter is not None and normalized_mode != "backfill":
            raise ValueError("sync_filter_requires_backfill")

        connection = await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        quota_lease = await self._acquire_quota(
            tenant_id=tenant_id,
            subject_id=subject_id,
            account_id=connection.connection_id,
            operation="mail.sync",
        )
        try:
            result = await self._sync_connection(
                tenant_id=tenant_id,
                subject_id=subject_id,
                connection_id=connection_id,
                limit=limit,
                mode=normalized_mode,
                sync_filter=sync_filter,
                cancel_check=cancel_check,
            )
        except SyncCancelledError:
            await self._record_telemetry(
                "mail.sync.cancelled",
                {
                    "tenant_id": tenant_id,
                    "subject_id": subject_id,
                    "connection_id": str(connection_id),
                    "error_code": SyncCancelledError.code,
                },
            )
            raise
        except MailHubError as exc:
            error_metadata = _provider_error_metadata(exc)
            failure_data: dict[str, object] = {"error_code": exc.message}
            failure_data.update(error_metadata)
            await self._publish_event(
                MailHubEventType.SYNC_FAILED,
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                idempotency_key=f"sync:{connection_id}:failed:{exc.message}",
                data=failure_data,
            )
            telemetry_data: dict[str, object] = {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "connection_id": str(connection_id),
                "error_code": exc.message,
            }
            telemetry_data.update(error_metadata)
            await self._record_telemetry(
                "mail.sync.failed",
                telemetry_data,
            )
            raise
        finally:
            await self._release_quota(quota_lease)
        await self._record_telemetry(
            "mail.sync.completed",
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "connection_id": str(connection_id),
                "fetched_count": result.get("fetched_count", 0),
                "saved_count": result.get("saved_count", 0),
                "duplicate_count": result.get("duplicate_count", 0),
                "deleted_count": result.get("deleted_count", 0),
                "reset_required": result.get("reset_required", False),
            },
        )
        return result

    async def _sync_connection(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        limit: int = 50,
        mode: str = "incremental",
        sync_filter: ProviderSyncFilter | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> dict[str, object]:
        if not 1 <= limit <= 500:
            raise ValueError("sync_limit_invalid")
        connection = await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        connector = self._connector(connection.provider)
        if cancel_check is not None and await cancel_check():
            raise SyncCancelledError()
        folder_ref = sync_filter.folder_ref if sync_filter is not None else "INBOX"
        cursor = None
        if mode != "backfill":
            cursor = await self.repository.get_cursor(
                tenant_id=tenant_id, connection_id=connection_id, folder_ref=folder_ref
            )
        credential: Mapping[str, str]
        if connection.provider is ProviderName.SANDBOX:
            # The fixture connector has no secret material.  Keeping sandbox
            # sync independent of a broker makes the local demo usable while
            # production connectors still fail closed on missing credentials.
            credential = {}
        elif self.credential_broker is None:
            credential = {}
        else:
            credential = await self.credential_broker.resolve(
                credential_ref=connection.credential_ref,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
        _validate_credential_binding(connection, credential)
        await self._publish_event(
            MailHubEventType.SYNC_STARTED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(connection_id),
            idempotency_key=f"sync:{connection_id}:{cursor or 'initial'}:{limit}",
            data={
                "provider": connection.provider.value,
                "has_cursor": cursor is not None,
                "limit": limit,
            },
        )
        try:
            sync_callable = cast(Any, connector).sync
            if sync_filter is None:
                page = await sync_callable(
                    connection,
                    cursor=cursor,
                    limit=limit,
                    credential=credential,
                )
            else:
                try:
                    page = await sync_callable(
                        connection,
                        cursor=cursor,
                        limit=limit,
                        credential=credential,
                        sync_filter=sync_filter,
                    )
                except TypeError as exc:
                    raise ProviderFailureError("provider_backfill_filter_unsupported") from exc
        except ProviderFailureError as exc:
            failure_reason = exc.message
            failure_status = (
                ConnectionStatus.REAUTHORIZATION_REQUIRED
                if any(
                    marker in failure_reason
                    for marker in ("http_401", "access_token", "credential", "permission")
                )
                else ConnectionStatus.DEGRADED
            )
            if connection.status is ConnectionStatus.ACTIVE:
                await self.repository.save_connection(
                    replace(
                        connection,
                        status=failure_status,
                        revision=connection.revision + 1,
                        updated_at=utc_now(),
                    )
                )
            await self.repository.append_audit(
                self._audit(
                    "mail.sync.failed",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(connection_id),
                    error_code=failure_reason,
                    connection_status=failure_status.value,
                    **_provider_error_metadata(exc),
                )
            )
            failure_data: dict[str, object] = {
                "provider": connection.provider.value,
                "error_code": failure_reason,
                "connection_status": failure_status.value,
            }
            failure_data.update(_provider_error_metadata(exc))
            await self._publish_event(
                MailHubEventType.SYNC_FAILED,
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                idempotency_key=f"sync:{connection_id}:{cursor or 'initial'}:{limit}:failed",
                data=failure_data,
            )
            if failure_status is ConnectionStatus.REAUTHORIZATION_REQUIRED:
                await self._publish_event(
                    MailHubEventType.CONNECTION_REAUTHORIZATION_REQUIRED,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(connection_id),
                    idempotency_key=(
                        f"connection:{connection_id}:reauthorization:{connection.revision + 1}"
                    ),
                    data={
                        "provider": connection.provider.value,
                        "connection_status": failure_status.value,
                        "error_code": failure_reason,
                        **_provider_error_metadata(exc),
                    },
                )
            raise
        if cancel_check is not None and await cancel_check():
            raise SyncCancelledError()
        if (
            self.object_store is None
            and connection.provider is not ProviderName.SANDBOX
            and any(item.body_text is not None for item in page.messages)
        ):
            raise ProviderFailureError("object_store_unconfigured")
        saved = 0
        duplicates = 0
        for item in page.messages:
            if cancel_check is not None and await cancel_check():
                raise SyncCancelledError()
            thread_id = uuid5(
                NAMESPACE_URL,
                f"mailhub:thread:{connection.connection_id}:{item.provider_thread_ref}",
            )
            participants = tuple(
                dict.fromkeys(
                    (
                        item.sender_address,
                        *item.recipient_addresses,
                        *item.cc_addresses,
                        *item.bcc_addresses,
                        *item.reply_to_addresses,
                    )
                )
            )
            current_thread = await self.repository.get_thread(
                tenant_id=tenant_id, thread_id=thread_id
            )
            thread = MailThread(
                thread_id=thread_id,
                tenant_id=tenant_id,
                connection_id=connection_id,
                provider_thread_ref=item.provider_thread_ref,
                normalized_subject=item.subject.strip().lower(),
                participant_addresses=participants,
                latest_at=item.received_at,
                message_count=1,
            )
            # PostgreSQL enforces the message -> thread foreign key.  New
            # threads are created before provider content is persisted; an
            # existing thread is updated only after a new message is inserted.
            if current_thread is None:
                await self.repository.save_thread(thread)
            body_text = item.body_text
            body_object_ref = item.body_object_ref
            if body_text is not None and self.object_store is not None:
                body_object_ref = await self.object_store.put_text(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    purpose="mail_message",
                    content=body_text,
                    content_sha256=item.content_sha256,
                    expires_at=utc_now() + self.message_content_ttl,
                )
                body_text = None
            try:
                message = MailMessageProjection(
                    message_id=uuid5(
                        NAMESPACE_URL,
                        f"mailhub:message:{connection.connection_id}:{item.provider_message_ref}",
                    ),
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    thread_id=thread_id,
                    provider_message_ref=item.provider_message_ref,
                    internet_message_id=item.internet_message_id,
                    sender_address=item.sender_address,
                    recipient_addresses=ensure_addresses(item.recipient_addresses),
                    subject=item.subject,
                    received_at=item.received_at,
                    body_text=body_text,
                    body_object_ref=body_object_ref,
                    content_sha256=item.content_sha256,
                    labels=item.labels,
                    attachment_count=item.attachment_count,
                    is_read=item.is_read,
                    provider_metadata=item.provider_metadata,
                    cc_addresses=ensure_addresses(item.cc_addresses) if item.cc_addresses else (),
                    bcc_addresses=ensure_addresses(item.bcc_addresses)
                    if item.bcc_addresses
                    else (),
                    reply_to_addresses=ensure_addresses(item.reply_to_addresses)
                    if item.reply_to_addresses
                    else (),
                )
                # Existing threads can be updated after the insert, and only a
                # newly-created provider message advances the aggregate count.
                _, created = await self.repository.save_message(message)
            except Exception:
                if (
                    self.object_store is not None
                    and body_object_ref is not None
                    and body_object_ref != item.body_object_ref
                ):
                    await self.object_store.delete(
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        object_ref=body_object_ref,
                    )
                raise
            if current_thread is not None and created:
                merged_participants = tuple(
                    dict.fromkeys((*current_thread.participant_addresses, *participants))
                )
                await self.repository.save_thread(
                    replace(
                        current_thread,
                        normalized_subject=thread.normalized_subject,
                        participant_addresses=merged_participants,
                        latest_at=max(current_thread.latest_at, item.received_at),
                        message_count=current_thread.message_count + 1,
                        revision=current_thread.revision + 1,
                    )
                )
            saved += int(created)
            duplicates += int(not created)
            if created:
                await self._publish_event(
                    MailHubEventType.MESSAGE_OBSERVED,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(message.message_id),
                    idempotency_key=f"message:{message.message_id}:observed",
                    data={
                        "connection_id": str(connection_id),
                        "thread_id": str(thread_id),
                        "provider_message_ref": message.provider_message_ref,
                        "content_sha256": message.content_sha256,
                        "received_at": message.received_at.isoformat(),
                        "attachment_count": message.attachment_count,
                    },
                    occurred_at=message.received_at,
                )
            if (
                not created
                and self.object_store is not None
                and body_object_ref is not None
                and body_object_ref != item.body_object_ref
            ):
                await self.object_store.delete(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    object_ref=body_object_ref,
                )
        deleted = 0
        for provider_message_ref in page.deleted_message_refs:
            if cancel_check is not None and await cancel_check():
                raise SyncCancelledError()
            removed = await self.repository.delete_message_by_provider_ref(
                tenant_id=tenant_id,
                connection_id=connection_id,
                provider_message_ref=provider_message_ref,
            )
            if removed is None:
                continue
            if self.object_store is not None and removed.body_object_ref is not None:
                await self.object_store.delete(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    object_ref=removed.body_object_ref,
                )
            deleted += 1
            await self.repository.append_audit(
                self._audit(
                    "mail.message.deleted",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(removed.message_id),
                    connection_id=str(connection_id),
                    provider_message_ref=removed.provider_message_ref,
                    thread_id=str(removed.thread_id),
                    content_sha256=removed.content_sha256,
                )
            )
            await self._publish_event(
                MailHubEventType.MESSAGE_DELETED,
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(removed.message_id),
                idempotency_key=f"message:{removed.message_id}:deleted",
                data={
                    "connection_id": str(connection_id),
                    "provider_message_ref": removed.provider_message_ref,
                    "thread_id": str(removed.thread_id),
                    "content_sha256": removed.content_sha256,
                },
            )
        if mode != "backfill" and sync_filter is None:
            if cancel_check is not None and await cancel_check():
                raise SyncCancelledError()
            await self.repository.commit_cursor(
                tenant_id=tenant_id,
                connection_id=connection_id,
                folder_ref=folder_ref,
                expected_cursor=cursor,
                next_cursor=page.next_cursor,
            )
        await self.repository.append_audit(
            self._audit(
                "mail.sync.completed",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(connection_id),
                fetched=len(page.messages) + len(page.deleted_message_refs),
                saved=saved,
                duplicates=duplicates,
                deleted=deleted,
                provider_request_id=page.provider_request_id,
                reset_required=page.reset_required,
                mode=mode,
                filter_applied=sync_filter is not None,
            )
        )
        await self._publish_event(
            MailHubEventType.SYNC_COMPLETED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(connection_id),
            idempotency_key=f"sync:{connection_id}:{page.provider_request_id}:completed",
            data={
                "provider": connection.provider.value,
                "fetched": len(page.messages) + len(page.deleted_message_refs),
                "saved": saved,
                "duplicates": duplicates,
                "deleted": deleted,
                "provider_request_id": page.provider_request_id,
                "reset_required": page.reset_required,
                "mode": mode,
                "filter_applied": sync_filter is not None,
            },
        )
        return {
            "connection_id": str(connection_id),
            "fetched_count": len(page.messages) + len(page.deleted_message_refs),
            "saved_count": saved,
            "duplicate_count": duplicates,
            "deleted_count": deleted,
            "next_cursor": page.next_cursor,
            "provider_request_id": page.provider_request_id,
            "reset_required": page.reset_required,
            "mode": mode,
            "filter_applied": sync_filter is not None,
        }

    async def enqueue_sync_job(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        mode: str = "incremental",
        limit: int = 50,
        folder_ref: str = "INBOX",
        label_refs: tuple[str, ...] = (),
        received_after: datetime | None = None,
        received_before: datetime | None = None,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> MailSyncJob:
        """Persist a sync request before any provider I/O.

        A caller-supplied idempotency key is required for reliable HTTP
        retries.  When omitted we create a unique request, so an explicit
        retry is never accidentally collapsed into an unrelated job.
        """

        try:
            await self._connection(
                tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
            )
        except AuthorizationError as exc:
            blocked = await self.repository.get_connection(
                tenant_id=tenant_id, connection_id=connection_id
            )
            if (
                blocked is not None
                and blocked.subject_id == subject_id
                and blocked.status
                in {
                    ConnectionStatus.REVOKED,
                    ConnectionStatus.DELETING,
                    ConnectionStatus.DELETED,
                }
            ):
                # This is a durable negative-access observation: the request
                # is fenced before a worker/job or any Provider/Credential
                # Broker call can be created.  Host-side counters remain
                # independently owned and must be supplied in the release
                # evidence bundle.
                await self.repository.append_audit(
                    self._audit(
                        "mail.sync.rejected_after_revoke",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        target_ref=str(connection_id),
                        provider=blocked.provider.value,
                        connection_status=blocked.status.value,
                        provider_requests_after_revoke=0,
                        broker_requests_after_revoke=0,
                        reason=exc.message,
                    )
                )
            raise
        normalized_mode = mode.casefold().strip()
        sync_filter = ProviderSyncFilter(
            folder_ref=folder_ref,
            label_refs=label_refs,
            received_after=received_after,
            received_before=received_before,
        )
        if sync_filter.is_bounded and normalized_mode != "backfill":
            raise ValueError("sync_filter_requires_backfill")
        request_key = idempotency_key or str(uuid4())
        if not request_key.strip() or len(request_key) > 300:
            raise ValueError("sync_idempotency_key_invalid")
        job = MailSyncJob(
            job_id=uuid5(
                NAMESPACE_URL,
                f"mailhub:sync:{tenant_id}:{connection_id}:{request_key}",
            ),
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            mode=normalized_mode,
            requested_limit=limit,
            folder_ref=sync_filter.folder_ref,
            label_refs=sync_filter.label_refs,
            received_after=sync_filter.received_after,
            received_before=sync_filter.received_before,
            idempotency_key=request_key,
            trace_id=trace_id,
        )
        stored, _created = await self.repository.create_or_get_sync_job(job)
        await self.repository.append_audit(
            self._audit(
                "mail.sync.queued",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(stored.job_id),
                connection_id=str(connection_id),
                mode=stored.mode,
                folder_ref=stored.folder_ref,
                label_count=len(stored.label_refs),
                received_after=stored.received_after.isoformat()
                if stored.received_after is not None
                else None,
                received_before=stored.received_before.isoformat()
                if stored.received_before is not None
                else None,
                idempotency_key=stored.idempotency_key,
                created=_created,
            )
        )
        return stored

    async def get_sync_job(self, *, tenant_id: str, subject_id: str, job_id: UUID) -> MailSyncJob:
        job = await self.repository.get_sync_job(tenant_id=tenant_id, job_id=job_id)
        if job is None or job.subject_id != subject_id:
            raise NotFoundError("sync_job_not_found")
        return job

    async def cancel_sync_job(
        self, *, tenant_id: str, subject_id: str, job_id: UUID
    ) -> MailSyncJob:
        job = await self.get_sync_job(tenant_id=tenant_id, subject_id=subject_id, job_id=job_id)
        cancelled = await self.repository.cancel_sync_job(tenant_id=tenant_id, job_id=job.job_id)
        await self.repository.append_audit(
            self._audit(
                "mail.sync.cancel_requested"
                if cancelled.status is SyncJobStatus.CANCELLING
                else "mail.sync.cancelled",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(job_id),
                status=cancelled.status.value,
            )
        )
        return cancelled

    async def list_sync_jobs(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MailSyncJob, ...]:
        return await self.repository.list_sync_jobs(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            limit=limit,
        )

    async def run_sync_job(
        self, *, tenant_id: str, subject_id: str, job_id: UUID, worker_id: str
    ) -> MailSyncJob:
        """Claim and execute one durable job; safe to call from a worker."""

        job = await self.get_sync_job(tenant_id=tenant_id, subject_id=subject_id, job_id=job_id)
        if job.status in {SyncJobStatus.SUCCEEDED, SyncJobStatus.FAILED, SyncJobStatus.CANCELLED}:
            return job
        claimed = await self.repository.claim_sync_job(
            tenant_id=tenant_id,
            job_id=job_id,
            worker_id=worker_id,
            lease_seconds=int(self.worker_lease_ttl.total_seconds()),
        )

        async def cancellation_requested() -> bool:
            current = await self.repository.get_sync_job(tenant_id=tenant_id, job_id=job_id)
            return current is None or current.status is SyncJobStatus.CANCELLING

        try:
            claimed_filter = ProviderSyncFilter(
                folder_ref=claimed.folder_ref,
                label_refs=claimed.label_refs,
                received_after=claimed.received_after,
                received_before=claimed.received_before,
            )
            result = await self.sync_connection(
                tenant_id=tenant_id,
                subject_id=subject_id,
                connection_id=claimed.connection_id,
                limit=claimed.requested_limit,
                mode=claimed.mode,
                sync_filter=claimed_filter if claimed_filter.is_bounded else None,
                cancel_check=cancellation_requested,
            )
            current = await self.repository.get_sync_job(tenant_id=tenant_id, job_id=job_id)
            target_status = (
                SyncJobStatus.CANCELLED
                if current is not None and current.status is SyncJobStatus.CANCELLING
                else SyncJobStatus.SUCCEEDED
            )
            completed = await self.repository.complete_sync_job(
                tenant_id=tenant_id,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=claimed.fencing_token,
                status=target_status,
                fetched_count=int(cast(int, result.get("fetched_count", 0))),
                saved_count=int(cast(int, result.get("saved_count", 0))),
                duplicate_count=int(cast(int, result.get("duplicate_count", 0))),
                deleted_count=int(cast(int, result.get("deleted_count", 0))),
                cursor_before=claimed.cursor_before,
                cursor_after=cast(str | None, result.get("next_cursor")),
                provider_request_id=cast(str | None, result.get("provider_request_id")),
                increment_attempt=True,
            )
            await self.repository.append_audit(
                self._audit(
                    "mail.sync.cancelled"
                    if target_status is SyncJobStatus.CANCELLED
                    else "mail.sync.completed",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(job_id),
                    status=completed.status.value,
                    fetched=completed.fetched_count,
                    saved=completed.saved_count,
                    duplicates=completed.duplicate_count,
                    deleted=completed.deleted_count,
                )
            )
            return completed
        except SyncCancelledError:
            current = await self.repository.get_sync_job(tenant_id=tenant_id, job_id=job_id)
            if current is not None and current.status is SyncJobStatus.CANCELLED:
                return current
            return await self.repository.complete_sync_job(
                tenant_id=tenant_id,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=claimed.fencing_token,
                status=SyncJobStatus.CANCELLED,
                cursor_before=claimed.cursor_before,
                error_code=SyncCancelledError.code,
            )
        except Exception as exc:
            error_code = (
                str(exc.code) if isinstance(exc, MailHubError) else "sync_job_internal_error"
            )
            current = await self.repository.get_sync_job(tenant_id=tenant_id, job_id=job_id)
            if current is not None and current.status is SyncJobStatus.CANCELLED:
                return current
            if current is not None and current.status is SyncJobStatus.CANCELLING:
                return await self.repository.complete_sync_job(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    fencing_token=claimed.fencing_token,
                    status=SyncJobStatus.CANCELLED,
                    cursor_before=claimed.cursor_before,
                    error_code=SyncCancelledError.code,
                )
            # ``attempt_count`` records completed provider calls.  Keep the
            # total number of calls bounded by the retry budget, including
            # the current failed call and the eventual terminal call.
            if (
                _sync_failure_retryable(exc)
                and claimed.attempt_count + 1 < _SYNC_RETRY_MAX_ATTEMPTS
            ):
                attempt_count = claimed.attempt_count + 1
                retry_after = _sync_retry_delay(exc, attempt_count)
                next_attempt_at = utc_now() + timedelta(seconds=retry_after)
                retry_job = await self.repository.complete_sync_job(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    fencing_token=claimed.fencing_token,
                    status=SyncJobStatus.RETRY_WAIT,
                    cursor_before=claimed.cursor_before,
                    error_code=str(error_code),
                    next_attempt_at=next_attempt_at,
                    increment_attempt=True,
                )
                await self.repository.append_audit(
                    self._audit(
                        "mail.sync.retry_scheduled",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        target_ref=str(job_id),
                        status=retry_job.status.value,
                        attempt_count=retry_job.attempt_count,
                        next_attempt_at=next_attempt_at.isoformat(),
                        retry_after_seconds=retry_after,
                        error_code=str(error_code),
                    )
                )
                return retry_job
            return await self.repository.complete_sync_job(
                tenant_id=tenant_id,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=claimed.fencing_token,
                status=SyncJobStatus.FAILED,
                cursor_before=claimed.cursor_before,
                error_code=str(error_code),
                increment_attempt=True,
            )

    async def enqueue_autonomy_run(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        limit: int = 50,
        message_limit: int = 50,
        replay_key: str | None = None,
        trace_id: str | None = None,
    ) -> MailAutonomyRun:
        """Persist one bounded L0/L1 recommendation request before I/O."""

        await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        request_key = replay_key or str(uuid4())
        if not request_key.strip() or len(request_key) > 200:
            raise ValueError("autonomy_replay_key_invalid")
        run = MailAutonomyRun(
            run_id=uuid5(
                NAMESPACE_URL,
                f"mailhub:agent-autonomy:{tenant_id}:{subject_id}:{connection_id}:{request_key}",
            ),
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            replay_key=request_key,
            requested_limit=limit,
            message_limit=message_limit,
        )
        stored, created = await self.repository.create_or_get_autonomy_run(run)
        await self.repository.append_audit(
            self._audit(
                "mail.agent.autonomy.queued",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(stored.run_id),
                connection_id=str(connection_id),
                replay_key=stored.replay_key,
                created=created,
                mode=stored.mode,
                trace_id=trace_id,
            )
        )
        return stored

    async def get_autonomy_run(
        self, *, tenant_id: str, subject_id: str, run_id: UUID
    ) -> MailAutonomyRun:
        run = await self.repository.get_autonomy_run(tenant_id=tenant_id, run_id=run_id)
        if run is None or run.subject_id != subject_id:
            raise NotFoundError("autonomy_run_not_found")
        return run

    async def list_autonomy_runs(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[MailAutonomyRun, ...]:
        return await self.repository.list_autonomy_runs(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            limit=limit,
        )

    async def pause_autonomy_run(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        run_id: UUID,
        reason: str,
    ) -> MailAutonomyRun:
        run = await self.get_autonomy_run(tenant_id=tenant_id, subject_id=subject_id, run_id=run_id)
        paused = await self.repository.pause_autonomy_run(
            tenant_id=tenant_id, run_id=run.run_id, reason=reason
        )
        await self.repository.append_audit(
            self._audit(
                "mail.agent.autonomy.paused",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(run_id),
                reason=reason,
            )
        )
        return paused

    async def resume_autonomy_run(
        self, *, tenant_id: str, subject_id: str, run_id: UUID
    ) -> MailAutonomyRun:
        run = await self.get_autonomy_run(tenant_id=tenant_id, subject_id=subject_id, run_id=run_id)
        resumed = await self.repository.resume_autonomy_run(tenant_id=tenant_id, run_id=run.run_id)
        await self.repository.append_audit(
            self._audit(
                "mail.agent.autonomy.resumed",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(run_id),
            )
        )
        return resumed

    async def cancel_autonomy_run(
        self, *, tenant_id: str, subject_id: str, run_id: UUID
    ) -> MailAutonomyRun:
        run = await self.get_autonomy_run(tenant_id=tenant_id, subject_id=subject_id, run_id=run_id)
        cancelled = await self.repository.cancel_autonomy_run(
            tenant_id=tenant_id, run_id=run.run_id
        )
        await self.repository.append_audit(
            self._audit(
                "mail.agent.autonomy.cancelled",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(run_id),
            )
        )
        return cancelled

    async def run_autonomy_job(
        self, *, tenant_id: str, subject_id: str, run_id: UUID, worker_id: str
    ) -> MailAutonomyRun:
        """Claim and execute one persisted proposal-only run."""

        run = await self.get_autonomy_run(tenant_id=tenant_id, subject_id=subject_id, run_id=run_id)
        if run.status in {
            AutonomyRunStatus.COMPLETED,
            AutonomyRunStatus.FAILED,
            AutonomyRunStatus.CANCELLED,
            AutonomyRunStatus.PAUSED,
        }:
            return run
        claimed = await self.repository.claim_autonomy_run(
            tenant_id=tenant_id,
            run_id=run_id,
            worker_id=worker_id,
            lease_seconds=int(self.worker_lease_ttl.total_seconds()),
        )
        try:
            from mailhub.autonomy import MailAutonomyCoordinator

            result = await MailAutonomyCoordinator(self).run_claimed(claimed)
            completed = await self.repository.complete_autonomy_run(
                tenant_id=tenant_id,
                run_id=run_id,
                worker_id=worker_id,
                fencing_token=claimed.fencing_token,
                status=AutonomyRunStatus.COMPLETED,
                message_ids=cast(tuple[UUID, ...], result["message_ids"]),
                analyzed_message_ids=cast(tuple[UUID, ...], result["analyzed_message_ids"]),
                candidate_ids=cast(tuple[UUID, ...], result["candidate_ids"]),
                sync_result=cast(Mapping[str, object], result["sync_result"]),
                sync_job_id=claimed.sync_job_id,
            )
            await self.repository.append_audit(
                self._audit(
                    "mail.agent.autonomy.completed",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(run_id),
                    mode=completed.mode,
                    message_count=len(completed.message_ids),
                    analyzed_count=len(completed.analyzed_message_ids),
                    candidate_count=len(completed.candidate_ids),
                    side_effects=("metadata_read", "candidate_proposal"),
                )
            )
            return completed
        except Exception as exc:
            # Bounded durable error codes: MailHub taxonomy codes only.
            # Opaque engine codes (e.g. SQLAlchemy's translated DBAPI code)
            # must not leak into durable run state.
            error_code = (
                str(exc.code) if isinstance(exc, MailHubError) else "autonomy_internal_error"
            )
            automatic_pause_reason: str | None = None
            automatic_pause_metadata: dict[str, object] = {}
            if isinstance(exc, AuthorizationError) and exc.message == "connection_not_active":
                connection = await self.repository.get_connection(
                    tenant_id=tenant_id, connection_id=claimed.connection_id
                )
                blocked_statuses = {
                    ConnectionStatus.PENDING_AUTHORIZATION,
                    ConnectionStatus.REAUTHORIZATION_REQUIRED,
                    ConnectionStatus.REVOKED,
                    ConnectionStatus.DELETING,
                    ConnectionStatus.DELETED,
                }
                if connection is None or connection.status in blocked_statuses:
                    connection_state = (
                        connection.status.value if connection is not None else "missing"
                    )
                    automatic_pause_reason = f"connection_{connection_state}_requires_attention"
                    automatic_pause_metadata["connection_status"] = connection_state
            elif isinstance(exc, PolicyDeniedError) and exc.message == "prompt_injection_detected":
                automatic_pause_reason = "prompt_injection_detected_requires_review"
                automatic_pause_metadata["detector"] = "mail_content_safety"
            if automatic_pause_reason is not None:
                try:
                    paused = await self.repository.pause_autonomy_run(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        reason=automatic_pause_reason,
                        worker_id=worker_id,
                        fencing_token=claimed.fencing_token,
                    )
                except Exception:
                    # An owner cancellation or a newer worker may have
                    # won the race.  Never turn that durable decision
                    # back into a failed run from this stale worker.
                    latest = await self.repository.get_autonomy_run(
                        tenant_id=tenant_id, run_id=run_id
                    )
                    if latest is not None and (
                        latest.status
                        in {
                            AutonomyRunStatus.PAUSED,
                            AutonomyRunStatus.CANCELLED,
                            AutonomyRunStatus.COMPLETED,
                            AutonomyRunStatus.FAILED,
                        }
                        or latest.lease_owner != worker_id
                        or latest.fencing_token != claimed.fencing_token
                    ):
                        return latest
                    raise
                await self.repository.append_audit(
                    self._audit(
                        "mail.agent.autonomy.paused",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        target_ref=str(run_id),
                        reason=automatic_pause_reason,
                        automatic=True,
                        error_code=error_code,
                        **automatic_pause_metadata,
                    )
                )
                return paused
            try:
                failed = await self.repository.complete_autonomy_run(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    worker_id=worker_id,
                    fencing_token=claimed.fencing_token,
                    status=AutonomyRunStatus.FAILED,
                    error_code=error_code,
                )
            except Exception:
                # A pause/cancel may win the race while provider I/O is in
                # flight.  Never overwrite that owner decision with FAILED;
                # return the durable terminal/paused state instead.
                latest = await self.repository.get_autonomy_run(tenant_id=tenant_id, run_id=run_id)
                if latest is not None and latest.status in {
                    AutonomyRunStatus.PAUSED,
                    AutonomyRunStatus.CANCELLED,
                    AutonomyRunStatus.COMPLETED,
                    AutonomyRunStatus.FAILED,
                }:
                    return latest
                raise
            await self.repository.append_audit(
                self._audit(
                    "mail.agent.autonomy.failed",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(run_id),
                    error_code=error_code,
                )
            )
            return failed

    async def analyze_message(
        self, *, tenant_id: str, subject_id: str, message_id: UUID
    ) -> IntelligenceResult:
        """Analyze one authorized message and persist reviewable candidates."""

        message = await self.repository.get_message(tenant_id=tenant_id, message_id=message_id)
        if message is None:
            raise NotFoundError("message_not_found")
        if not await self._owns_message(tenant_id, subject_id, message):
            raise AuthorizationError("message_scope_denied")
        hydrated = await self._hydrate_message(message, tenant_id=tenant_id, subject_id=subject_id)
        result = analyze_message(hydrated, policy=self.intelligence_policy)
        if self.ai_execution is not None:
            ai_output = await self.ai_execution.structure(
                tenant_id=tenant_id,
                subject_id=subject_id,
                operation="mail.message.analyze",
                source={
                    "message_id": str(hydrated.message_id),
                    "content_sha256": hydrated.content_sha256,
                    "subject": hydrated.subject,
                    "body_text": (hydrated.body_text or "")[
                        : self.intelligence_policy.max_input_chars
                    ],
                    "baseline": {
                        "project_refs": list(result.project_refs),
                        "task_refs": list(result.task_refs),
                        "date_refs": list(result.date_refs),
                        "decisions": list(result.decisions),
                        "risks": list(result.risks),
                        "commitments": list(result.commitments),
                        "analysis_metadata": dict(result.analysis_metadata),
                    },
                },
                schema=AI_RESULT_SCHEMA,
            )
            result = merge_ai_result(result, ai_output)
        candidates = _candidates_from_result(
            tenant_id=tenant_id,
            subject_id=subject_id,
            result=result,
        )
        existing_candidates = await self.repository.list_candidates(
            tenant_id=tenant_id, subject_id=subject_id, status=None, limit=200
        )
        new_candidates = _assign_candidate_revisions(
            _deduplicate_candidates(candidates, existing_candidates), existing_candidates
        )
        for candidate in new_candidates:
            await self.repository.save_candidate(candidate)
            candidate_event_type = {
                CandidateType.PROJECT: MailHubEventType.CANDIDATE_PROJECT_PROPOSED,
                CandidateType.TASK: MailHubEventType.CANDIDATE_TASK_PROPOSED,
                CandidateType.KNOWLEDGE: MailHubEventType.CANDIDATE_KNOWLEDGE_PROPOSED,
            }[candidate.candidate_type]
            await self._publish_event(
                candidate_event_type,
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(candidate.candidate_id),
                idempotency_key=f"candidate:{candidate.candidate_id}:proposed:{candidate.revision}",
                data={
                    "message_id": str(candidate.message_id),
                    "candidate_type": candidate.candidate_type.value,
                    "candidate_revision": candidate.revision,
                    "confidence": candidate.confidence,
                    "requires_review": candidate.requires_review,
                },
                occurred_at=candidate.created_at,
            )
        await self.repository.append_audit(
            self._audit(
                "mail.message.analyzed",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(message_id),
                injection_detected=result.injection_detected,
                candidate_count=len(new_candidates),
                policy_version=result.policy_version,
                abstain_reason=result.abstain_reason,
            )
        )
        await self._record_telemetry(
            "mail.message.analyzed",
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "message_id": str(message_id),
                "candidate_count": len(new_candidates),
                "confidence": result.confidence,
                "injection_detected": result.injection_detected,
            },
        )
        return result

    async def list_candidates(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        status: CandidateState | None = None,
        candidate_type: CandidateType | None = None,
        limit: int = 50,
    ) -> tuple[MailActionCandidate, ...]:
        return await self.repository.list_candidates(
            tenant_id=tenant_id,
            subject_id=subject_id,
            status=status,
            candidate_type=candidate_type,
            limit=limit,
        )

    async def get_candidate(
        self, *, tenant_id: str, subject_id: str, candidate_id: UUID
    ) -> MailActionCandidate:
        candidate = await self.repository.get_candidate(
            tenant_id=tenant_id, candidate_id=candidate_id
        )
        if candidate is None or candidate.subject_id != subject_id:
            raise NotFoundError("candidate_not_found")
        return candidate

    async def get_operation(
        self, *, tenant_id: str, subject_id: str, operation_id: UUID
    ) -> MailOutboxOperation:
        operation = await self.repository.get_operation(
            tenant_id=tenant_id, operation_id=operation_id
        )
        if operation is None or operation.subject_id != subject_id:
            raise NotFoundError("operation_not_found")
        return operation

    async def reconcile_outcome_unknown(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        operation_id: UUID,
        status: DeliveryStatus,
        provider_message_ref: str | None = None,
        provider_request_id: str | None = None,
        error_code: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> MailOutboxOperation:
        if status not in {
            DeliveryStatus.RECONCILED_SUCCEEDED,
            DeliveryStatus.RETRY_WAIT,
            DeliveryStatus.MANUAL_RESOLUTION,
        }:
            raise ValueError("outcome_reconciliation_status_invalid")
        operation = await self.repository.get_operation(
            tenant_id=tenant_id, operation_id=operation_id
        )
        if operation is None or operation.subject_id != subject_id:
            raise NotFoundError("operation_not_found")
        result = await self.repository.reconcile_outcome_unknown(
            tenant_id=tenant_id,
            operation_id=operation_id,
            status=status,
            provider_message_ref=provider_message_ref,
            provider_request_id=provider_request_id,
            error_code=error_code,
            next_attempt_at=next_attempt_at,
        )
        await self.repository.append_audit(
            self._audit(
                "mail.outbox.reconciled",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(operation_id),
                status=status.value,
                provider_message_ref=provider_message_ref,
                provider_request_id=provider_request_id,
                error_code=error_code,
            )
        )
        await self._publish_event(
            MailHubEventType.OUTBOX_RECONCILED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(operation_id),
            idempotency_key=f"outbox:{operation_id}:reconciled:{result.attempt_count}",
            data={
                "status": result.status.value,
                "provider_message_ref": result.provider_message_ref,
                "provider_request_id": result.provider_request_id,
                "error_code": result.error_code,
                "idempotency_key": result.idempotency_key,
            },
            occurred_at=result.updated_at,
        )
        return result

    async def review_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
        approved: bool,
        expected_revision: int,
        review_reason: str | None = None,
    ) -> MailActionCandidate:
        candidate = await self.repository.get_candidate(
            tenant_id=tenant_id, candidate_id=candidate_id
        )
        if candidate is None or candidate.subject_id != subject_id:
            raise NotFoundError("candidate_not_found")
        if candidate.revision != expected_revision:
            raise ConflictError("candidate_revision_conflict")
        if candidate.status not in {CandidateState.PROPOSED, CandidateState.REJECTED}:
            raise ConflictError("candidate_not_reviewable")
        normalized_reason = review_reason.strip() if review_reason is not None else None
        if not approved and (normalized_reason is None or not normalized_reason):
            raise ValueError("candidate_rejection_reason_required")
        payload = dict(candidate.payload)
        if normalized_reason is not None:
            payload["review_reason"] = normalized_reason
        updated = replace(
            candidate,
            payload=payload,
            status=CandidateState.APPROVED if approved else CandidateState.REJECTED,
            revision=candidate.revision + 1,
            updated_at=utc_now(),
        )
        stored = await self.repository.save_candidate(updated)
        await self.repository.append_audit(
            self._audit(
                "mail.candidate.reviewed",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(candidate_id),
                approved=approved,
                revision=expected_revision,
                review_reason=normalized_reason,
            )
        )
        await self._publish_event(
            MailHubEventType.CANDIDATE_REVIEWED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(candidate_id),
            idempotency_key=f"candidate:{candidate_id}:reviewed:{updated.revision}",
            data={
                "candidate_type": candidate.candidate_type.value,
                "message_id": str(candidate.message_id),
                "approved": approved,
                "revision": updated.revision,
                "review_reason_sha256": digest_text(normalized_reason or ""),
                "status": updated.status.value,
            },
            occurred_at=updated.updated_at,
        )
        # Review is deliberately side-effect free.  The separate apply use case
        # is the only path that calls a host action or knowledge authority.  This
        # keeps a retried review from duplicating an external write and makes the
        # proposal -> review -> apply lifecycle explicit in the contract.
        return stored

    async def revoke_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
        expected_revision: int,
        reason: str,
    ) -> MailActionCandidate:
        """Revoke a reviewable proposal without mutating host facts.

        An applied candidate is deliberately not silently rewound here; its
        downstream HostAction/KnowledgeLifecycle authority must run its own
        governed revoke/review flow.
        """

        candidate = await self.get_candidate(
            tenant_id=tenant_id, subject_id=subject_id, candidate_id=candidate_id
        )
        if candidate.revision != expected_revision:
            raise ConflictError("candidate_revision_conflict")
        if candidate.status is CandidateState.APPLIED:
            raise ConflictError("candidate_applied_revoke_requires_host_lifecycle")
        if candidate.status is CandidateState.REVOKED:
            return candidate
        if candidate.status not in {
            CandidateState.PROPOSED,
            CandidateState.APPROVED,
            CandidateState.REJECTED,
            CandidateState.FAILED,
        }:
            raise ConflictError("candidate_not_revocable")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("candidate_revoke_reason_required")
        payload = dict(candidate.payload)
        payload["revoke_reason"] = normalized_reason
        updated = replace(
            candidate,
            payload=payload,
            status=CandidateState.REVOKED,
            revision=candidate.revision + 1,
            updated_at=utc_now(),
        )
        stored = await self.repository.save_candidate(updated)
        await self.repository.append_audit(
            self._audit(
                "mail.candidate.revoked",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(candidate_id),
                candidate_type=candidate.candidate_type.value,
                revision=expected_revision,
                reason_sha256=digest_text(normalized_reason),
            )
        )
        await self._publish_event(
            MailHubEventType.CANDIDATE_REVOKED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(candidate_id),
            idempotency_key=f"candidate:{candidate_id}:revoked:{updated.revision}",
            data={
                "candidate_type": candidate.candidate_type.value,
                "message_id": str(candidate.message_id),
                "revision": updated.revision,
                "reason_sha256": digest_text(normalized_reason),
                "status": updated.status.value,
            },
            occurred_at=updated.updated_at,
        )
        return stored

    async def list_threads(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> tuple[MailThread, ...]:
        return await self.repository.list_threads(
            tenant_id=tenant_id, subject_id=subject_id, limit=limit
        )

    async def list_thread_page(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        limit: int = 50,
        cursor: str | None = None,
        connection_id: UUID | None = None,
        unread: bool = False,
        important: bool = False,
        has_attachment: bool = False,
        project: bool = False,
        candidate: bool = False,
    ) -> MailThreadPage:
        """Return a stable, bounded, metadata-only unified inbox page.

        The initial implementation deliberately composes existing scoped
        repository reads so the in-memory and PostgreSQL adapters share the
        exact filter semantics.  The result is still bounded to the current
        thread listing contract; a production query plan may replace this
        composition without changing the HTTP cursor contract.
        """

        if not 1 <= limit <= 200:
            raise ValueError("limit_invalid")
        scope_digest = thread_page_scope_digest(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            unread=unread,
            important=important,
            has_attachment=has_attachment,
            project=project,
            candidate=candidate,
        )
        after: tuple[datetime, UUID] | None = None
        if cursor is not None:
            after = decode_thread_cursor(cursor, expected_scope=scope_digest)

        connections = {
            item.connection_id: item
            for item in await self.repository.list_connections(
                tenant_id=tenant_id, subject_id=subject_id
            )
            if connection_id is None or item.connection_id == connection_id
        }
        if not connections:
            return MailThreadPage(items=(), next_cursor=None, has_more=False)
        raw_threads = await self.repository.list_threads(
            tenant_id=tenant_id, subject_id=subject_id, limit=200
        )
        raw_threads = tuple(item for item in raw_threads if item.connection_id in connections)
        messages: list[MailMessageProjection] = []
        for mailbox_id in connections:
            messages.extend(
                await self.repository.list_messages_for_connection(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    connection_id=mailbox_id,
                    limit=10_000,
                )
            )
        by_thread: dict[UUID, list[MailMessageProjection]] = {}
        for message in messages:
            by_thread.setdefault(message.thread_id, []).append(message)
        candidates = await self.repository.list_candidates(
            tenant_id=tenant_id, subject_id=subject_id, status=None, limit=200
        )
        by_message: dict[UUID, list[MailActionCandidate]] = {}
        for item in candidates:
            by_message.setdefault(item.message_id, []).append(item)

        summaries: list[MailThreadSummary] = []
        for thread in raw_threads:
            mailbox = connections.get(thread.connection_id)
            if mailbox is None:
                continue
            thread_messages = by_thread.get(thread.thread_id, [])
            thread_candidates = [
                item
                for message in thread_messages
                for item in by_message.get(message.message_id, [])
            ]
            labels = tuple(
                sorted({label for message in thread_messages for label in message.labels})
            )
            important_value = any(
                label.casefold() in {"important", "starred", "priority", "flagged"}
                for label in labels
            )
            attachment_value = any(
                message.attachment_count > 0
                or any(
                    label.casefold() in {"attachment", "has_attachment"} for label in message.labels
                )
                for message in thread_messages
            )
            project_hint = _thread_project_hint(thread_candidates)
            if project_hint is None and any(
                label.casefold() in {"project", "rfq", "audit"} for label in labels
            ):
                project_hint = "project_hint"
            summary = MailThreadSummary(
                thread_id=thread.thread_id,
                tenant_id=thread.tenant_id,
                connection_id=thread.connection_id,
                provider=mailbox.provider,
                account_email=mailbox.email_address,
                normalized_subject=thread.normalized_subject,
                participant_addresses=thread.participant_addresses,
                latest_at=thread.latest_at,
                message_count=thread.message_count,
                revision=thread.revision,
                labels=labels,
                unread=any(not message.is_read for message in thread_messages),
                important=important_value,
                has_attachment=attachment_value,
                project_hint=project_hint,
                candidate_count=len(thread_candidates),
            )
            if unread and not summary.unread:
                continue
            if important and not summary.important:
                continue
            if has_attachment and not summary.has_attachment:
                continue
            if project and summary.project_hint is None:
                continue
            if candidate and summary.candidate_count == 0:
                continue
            if after is not None and not (
                summary.latest_at < after[0]
                or (summary.latest_at == after[0] and str(summary.thread_id) < str(after[1]))
            ):
                continue
            summaries.append(summary)

        summaries.sort(key=lambda item: (item.latest_at, str(item.thread_id)), reverse=True)
        has_more = len(summaries) > limit
        items = tuple(summaries[:limit])
        next_cursor = (
            encode_thread_cursor(
                latest_at=items[-1].latest_at,
                thread_id=items[-1].thread_id,
                scope_digest=scope_digest,
            )
            if has_more and items
            else None
        )
        return MailThreadPage(items=items, next_cursor=next_cursor, has_more=has_more)

    async def list_messages(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> tuple[MailMessageProjection, ...]:
        return await self.repository.list_messages(
            tenant_id=tenant_id, subject_id=subject_id, limit=limit
        )

    async def get_thread_detail(
        self, *, tenant_id: str, subject_id: str, thread_id: UUID, limit: int = 200
    ) -> tuple[MailThread, tuple[MailMessageProjection, ...]]:
        thread = await self.repository.get_thread(tenant_id=tenant_id, thread_id=thread_id)
        if thread is None:
            raise NotFoundError("thread_not_found")
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=thread.connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise AuthorizationError("thread_scope_denied")
        messages = await self.repository.list_thread_messages(
            tenant_id=tenant_id,
            subject_id=subject_id,
            thread_id=thread_id,
            limit=limit,
        )
        return thread, messages

    async def get_message_content(
        self, *, tenant_id: str, subject_id: str, message_id: UUID
    ) -> str:
        message = await self.repository.get_message(tenant_id=tenant_id, message_id=message_id)
        if message is None or not await self._owns_message(tenant_id, subject_id, message):
            raise NotFoundError("message_not_found")
        hydrated = await self._hydrate_message(message, tenant_id=tenant_id, subject_id=subject_id)
        # The provider body is treated as data.  The response is plain text and
        # has no HTML execution path; callers that need rich rendering must
        # sanitize it in their own isolated view layer.
        from mailhub.intelligence import sanitize_text

        return sanitize_text(hydrated.body_text or "")

    async def search_messages(
        self, *, tenant_id: str, subject_id: str, query: str, limit: int = 50
    ) -> tuple[MailMessageProjection, ...]:
        return await self.repository.search_messages(
            tenant_id=tenant_id, subject_id=subject_id, query=query, limit=limit
        )

    async def create_policy(self, policy: MailAgentPolicy) -> MailAgentPolicy:
        return await self.repository.save_policy(policy)

    async def list_policies(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> tuple[MailAgentPolicy, ...]:
        return await self.repository.list_policies(
            tenant_id=tenant_id, owner_subject_id=subject_id, limit=limit
        )

    async def create_grant(self, grant: DelegationGrant) -> DelegationGrant:
        policy = await self.repository.get_policy(
            tenant_id=grant.tenant_id, policy_id=grant.policy_id
        )
        if policy is None or policy.owner_subject_id != grant.granted_by_subject_id:
            raise AuthorizationError("grant_issuer_not_policy_owner")
        result = await self.repository.save_grant(grant)
        await self._publish_event(
            MailHubEventType.DELEGATION_GRANTED,
            tenant_id=grant.tenant_id,
            subject_id=grant.granted_by_subject_id,
            target_ref=str(grant.grant_id),
            idempotency_key=f"delegation:{grant.grant_id}:granted:{grant.revision}",
            data={
                "policy_id": str(grant.policy_id),
                "agent_subject_id": grant.agent_subject_id,
                "granted_by_subject_id": grant.granted_by_subject_id,
                "capability_count": len(grant.capability_ids),
                "revision": grant.revision,
                "expires_at": grant.expires_at.isoformat(),
            },
            occurred_at=grant.granted_at,
        )
        return result

    async def list_grants(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> tuple[DelegationGrant, ...]:
        return await self.repository.list_grants(
            tenant_id=tenant_id, granted_by_subject_id=subject_id, limit=limit
        )

    async def revoke_grant(
        self, *, tenant_id: str, subject_id: str, grant_id: UUID, expected_revision: int
    ) -> DelegationGrant:
        grant = await self.repository.get_grant(tenant_id=tenant_id, grant_id=grant_id)
        if grant is None or grant.granted_by_subject_id != subject_id:
            raise NotFoundError("delegation_not_found")
        if grant.revision != expected_revision:
            raise ConflictError("delegation_revision_conflict")
        if grant.revoked_at is not None:
            return grant
        revoked = replace(
            grant,
            revoked_at=utc_now(),
            revision=grant.revision + 1,
        )
        result = await self.repository.save_grant(revoked)
        await self.repository.append_audit(
            self._audit(
                "mail.delegation.revoked",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(grant_id),
                revision=expected_revision,
            )
        )
        await self._publish_event(
            MailHubEventType.DELEGATION_REVOKED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(grant_id),
            idempotency_key=f"delegation:{grant_id}:revoked:{result.revision}",
            data={
                "policy_id": str(result.policy_id),
                "agent_subject_id": result.agent_subject_id,
                "revision": result.revision,
                "revoked_at": result.revoked_at.isoformat() if result.revoked_at else None,
            },
            occurred_at=result.revoked_at,
        )
        return result

    async def disable_policy(
        self, *, tenant_id: str, subject_id: str, policy_id: UUID, expected_revision: int
    ) -> MailAgentPolicy:
        policy = await self.repository.get_policy(tenant_id=tenant_id, policy_id=policy_id)
        if policy is None or policy.owner_subject_id != subject_id:
            raise NotFoundError("policy_not_found")
        if policy.revision != expected_revision:
            raise ConflictError("policy_revision_conflict")
        disabled = replace(policy, enabled=False, revision=policy.revision + 1)
        return await self.repository.save_policy(disabled)

    async def create_rule(self, rule: MailRule) -> MailRule:
        if rule.tenant_id.strip() == "" or rule.owner_subject_id.strip() == "":
            raise AuthorizationError("rule_scope_invalid")
        result = await self.repository.save_rule(rule)
        await self._publish_event(
            MailHubEventType.RULE_PUBLISHED if result.enabled else MailHubEventType.RULE_PAUSED,
            tenant_id=result.tenant_id,
            subject_id=result.owner_subject_id,
            target_ref=str(result.rule_id),
            idempotency_key=(
                f"rule:{result.rule_id}:v{result.version}:"
                f"{'enabled' if result.enabled else 'paused'}"
            ),
            data={
                "rule_version": result.version,
                "enabled": result.enabled,
                "action_type": result.action_type.value,
                "automation_level": result.automation_level.value,
            },
            occurred_at=result.updated_at,
        )
        return result

    async def list_rules(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> tuple[MailRule, ...]:
        return await self.repository.list_rules(
            tenant_id=tenant_id, subject_id=subject_id, limit=limit
        )

    async def simulate_rules(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        message_ids: tuple[UUID, ...],
    ) -> tuple[RuleEvaluation, ...]:
        rules = await self.repository.list_rules(
            tenant_id=tenant_id, subject_id=subject_id, limit=200
        )
        messages: list[MailMessageProjection] = []
        for message_id in message_ids:
            message = await self.repository.get_message(tenant_id=tenant_id, message_id=message_id)
            if message is None or not await self._owns_message(tenant_id, subject_id, message):
                raise NotFoundError("rule_simulation_message_not_found")
            messages.append(message)
        return simulate_rules(rules, messages)

    async def execute_rule(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID,
        message_ids: tuple[UUID, ...],
        policy_id: UUID | None = None,
        grant_id: UUID | None = None,
        dry_run: bool = True,
    ) -> tuple[RuleExecution, ...]:
        """Evaluate or execute a bounded L3A rule with durable evidence.

        Dry-run is always available.  Real execution is disabled by the
        independent rule kill switch unless the rule is explicitly L3A and the
        policy/delegation intersection allows the exact low-risk action.  A
        stable execution/action id is persisted before host I/O so retries can
        never silently perform a second organize action.
        """

        if not message_ids or len(message_ids) > 100:
            raise ValueError("rule_execution_message_ids_invalid")
        rule = await self.repository.get_rule(tenant_id=tenant_id, rule_id=rule_id)
        if rule is None or rule.owner_subject_id != subject_id:
            raise NotFoundError("rule_not_found")
        if not dry_run:
            if not self.rule_automation_enabled:
                raise KillSwitchError("rule_automation_kill_switch_active")
            if rule.automation_level is not AutomationLevel.L3A_BOUNDED_ORGANIZE:
                raise PolicyDeniedError("rule_automation_level_not_allowed")
            if self.host_action_port is None:
                raise ProviderFailureError("host_action_port_unconfigured")
        policy = (
            await self.repository.get_policy(tenant_id=tenant_id, policy_id=policy_id)
            if policy_id is not None
            else None
        )
        grant = (
            await self.repository.get_grant(tenant_id=tenant_id, grant_id=grant_id)
            if grant_id is not None
            else None
        )
        results: list[RuleExecution] = []
        for message_id in message_ids:
            message = await self.repository.get_message(tenant_id=tenant_id, message_id=message_id)
            if message is None or not await self._owns_message(tenant_id, subject_id, message):
                raise NotFoundError("rule_execution_message_not_found")
            mode = "dry" if dry_run else "execute"
            action_id = uuid5(
                NAMESPACE_URL,
                f"mailhub:rule-action:{rule.rule_id}:v{rule.version}:{message.message_id}",
            )
            execution_id = uuid5(
                NAMESPACE_URL,
                f"mailhub:rule-execution:{rule.rule_id}:v{rule.version}:{message.message_id}:{mode}",
            )
            input_digest = digest_text(
                json.dumps(
                    {
                        "rule_id": str(rule.rule_id),
                        "rule_version": rule.version,
                        "message_id": str(message.message_id),
                        "action_type": rule.action_type.value,
                        "action_params": dict(rule.action_params),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            initial = RuleExecution(
                execution_id=execution_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                rule_id=rule.rule_id,
                rule_version=rule.version,
                message_id=message.message_id,
                action_id=action_id,
                input_digest=input_digest,
                status=RuleExecutionStatus.PROPOSED,
                reason="evaluation_pending",
            )
            execution, created = await self.repository.create_or_get_rule_execution(initial)
            if not created:
                results.append(execution)
                continue
            evaluation = evaluate_rule(rule, message, dry_run=dry_run)
            if not evaluation.matched:
                execution = replace(
                    execution,
                    status=RuleExecutionStatus.DRY_RUN if dry_run else RuleExecutionStatus.SKIPPED,
                    reason=evaluation.reason,
                    result={"matched": False},
                    revision=execution.revision + 1,
                    updated_at=utc_now(),
                )
                results.append(await self.repository.save_rule_execution(execution))
                continue

            action = AgentActionRequest(
                action_id=action_id,
                action_type=rule.action_type,
                context=AgentActionContext(
                    tenant_id=tenant_id,
                    agent_subject_id=subject_id,
                    connection_id=message.connection_id,
                    folder_ref="INBOX",
                    thread_id=message.thread_id,
                    data_classes=frozenset({"internal"}),
                ),
                input_digest=input_digest,
                source_message_ids=(message.message_id,),
                parameters=dict(rule.action_params),
            )
            decision = evaluate_policy(policy, grant, action)
            decision_result = {
                "matched": True,
                "action_type": rule.action_type.value,
                "policy_decision": decision.reason_code,
                "policy_id": str(decision.policy_id) if decision.policy_id else None,
                "grant_id": str(decision.grant_id) if decision.grant_id else None,
            }
            if (
                decision.allowed
                and policy is not None
                and policy.allowed_automation_level is not AutomationLevel.L3A_BOUNDED_ORGANIZE
            ):
                decision = replace(decision, allowed=False, reason_code="policy_l3a_required")
                decision_result["policy_decision"] = decision.reason_code
            if not decision.allowed:
                execution = replace(
                    execution,
                    status=RuleExecutionStatus.BLOCKED,
                    reason=decision.reason_code,
                    result=decision_result,
                    revision=execution.revision + 1,
                    updated_at=utc_now(),
                )
                results.append(await self.repository.save_rule_execution(execution))
                continue
            if (
                rule.max_per_hour
                and await self.repository.count_rule_executions_since(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    rule_id=rule.rule_id,
                    since=utc_now() - timedelta(hours=1),
                )
                >= rule.max_per_hour
            ):
                execution = replace(
                    execution,
                    status=RuleExecutionStatus.BLOCKED,
                    reason="rule_hourly_limit",
                    result=decision_result,
                    revision=execution.revision + 1,
                    updated_at=utc_now(),
                )
                results.append(await self.repository.save_rule_execution(execution))
                continue
            if (
                rule.max_per_day
                and await self.repository.count_rule_executions_since(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    rule_id=rule.rule_id,
                    since=utc_now() - timedelta(days=1),
                )
                >= rule.max_per_day
            ):
                execution = replace(
                    execution,
                    status=RuleExecutionStatus.BLOCKED,
                    reason="rule_daily_limit",
                    result=decision_result,
                    revision=execution.revision + 1,
                    updated_at=utc_now(),
                )
                results.append(await self.repository.save_rule_execution(execution))
                continue
            if dry_run:
                execution = replace(
                    execution,
                    status=RuleExecutionStatus.DRY_RUN,
                    reason="matched_dry_run",
                    result=decision_result,
                    revision=execution.revision + 1,
                    updated_at=utc_now(),
                )
                results.append(await self.repository.save_rule_execution(execution))
                continue

            connection = await self.repository.get_connection(
                tenant_id=tenant_id, connection_id=message.connection_id
            )
            try:
                kill_switch_allowed, kill_switch_reason = await self._check_kill_switch(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    provider=connection.provider if connection is not None else None,
                    operation="mail.rule.execute",
                    fail_fast=False,
                )
            except MailHubError as exc:
                kill_switch_allowed = False
                kill_switch_reason = exc.message
            if not kill_switch_allowed:
                execution = replace(
                    execution,
                    status=RuleExecutionStatus.BLOCKED,
                    reason=kill_switch_reason or "kill_switch_active",
                    result={**decision_result, "kill_switch": "blocked"},
                    revision=execution.revision + 1,
                    updated_at=utc_now(),
                )
                saved = await self.repository.save_rule_execution(execution)
                await self.repository.append_audit(
                    self._audit(
                        "mail.rule.blocked",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        target_ref=str(execution.execution_id),
                        rule_id=str(rule.rule_id),
                        message_id=str(message.message_id),
                        reason=kill_switch_reason or "kill_switch_active",
                    )
                )
                await self._publish_event(
                    MailHubEventType.RULE_BLOCKED,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(execution.execution_id),
                    idempotency_key=f"rule-execution:{execution.execution_id}:blocked:{execution.revision}",
                    data={
                        "rule_id": str(rule.rule_id),
                        "rule_version": rule.version,
                        "message_id": str(message.message_id),
                        "reason": kill_switch_reason or "kill_switch_active",
                    },
                    occurred_at=saved.updated_at,
                )
                results.append(saved)
                continue

            execution = replace(
                execution,
                status=RuleExecutionStatus.PROPOSED,
                reason="policy_allowed",
                result=decision_result,
                revision=execution.revision + 1,
                updated_at=utc_now(),
            )
            await self.repository.save_rule_execution(execution)
            approval_ref = (
                f"policy-decision:{decision.policy_id}:v{policy.revision if policy else 0}:"
                f"grant:{decision.grant_id}:v{grant.revision if grant else 0}:"
                f"action:{action.action_id}"
            )
            try:
                assert self.host_action_port is not None
                result = await self.host_action_port.execute(
                    action=action, approval_ref=approval_ref
                )
            except (TimeoutError, ConnectionError, OSError):
                execution = replace(
                    execution,
                    status=RuleExecutionStatus.OUTCOME_UNKNOWN,
                    reason="host_action_outcome_unknown",
                    result=decision_result,
                    revision=execution.revision + 1,
                    updated_at=utc_now(),
                )
            except Exception:
                execution = replace(
                    execution,
                    status=RuleExecutionStatus.FAILED,
                    reason="host_action_failed",
                    result=decision_result,
                    revision=execution.revision + 1,
                    updated_at=utc_now(),
                )
            else:
                safe_result = (
                    redact_event(result) if isinstance(result, Mapping) else {"status": "applied"}
                )
                execution = replace(
                    execution,
                    status=RuleExecutionStatus.EXECUTED,
                    reason="host_action_applied",
                    result={**decision_result, "host_result": dict(safe_result)},
                    revision=execution.revision + 1,
                    updated_at=utc_now(),
                )
            saved = await self.repository.save_rule_execution(execution)
            await self.repository.append_audit(
                self._audit(
                    "mail.rule.executed",
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    target_ref=str(execution.execution_id),
                    rule_id=str(rule.rule_id),
                    message_id=str(message.message_id),
                    status=execution.status.value,
                    reason=execution.reason,
                )
            )
            await self._publish_event(
                MailHubEventType.RULE_EXECUTED,
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(execution.execution_id),
                idempotency_key=f"rule-execution:{execution.execution_id}:result:{saved.revision}",
                data={
                    "rule_id": str(rule.rule_id),
                    "rule_version": rule.version,
                    "message_id": str(message.message_id),
                    "status": saved.status.value,
                    "reason": saved.reason,
                },
                occurred_at=saved.updated_at,
            )
            results.append(saved)
        return tuple(results)

    async def list_rule_executions(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[RuleExecution, ...]:
        return await self.repository.list_rule_executions(
            tenant_id=tenant_id,
            subject_id=subject_id,
            rule_id=rule_id,
            limit=limit,
        )

    async def create_draft(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        thread_id: UUID | None,
        recipient_addresses: tuple[str, ...],
        subject: str,
        body_text: str,
        cc_addresses: tuple[str, ...] = (),
        bcc_addresses: tuple[str, ...] = (),
        attachment_refs: tuple[str, ...] = (),
        provider_draft_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> MailDraft:
        if idempotency_key is not None and (
            not idempotency_key.strip() or len(idempotency_key) > 300
        ):
            raise ValueError("draft_idempotency_key_invalid")
        normalized_recipients = ensure_addresses(recipient_addresses)
        normalized_cc = _optional_addresses_for_draft(cc_addresses, "cc_addresses")
        normalized_bcc = _optional_addresses_for_draft(bcc_addresses, "bcc_addresses")
        _ensure_disjoint_recipients(normalized_recipients, normalized_cc, normalized_bcc)
        normalized_attachments = _normalize_attachment_refs(attachment_refs)
        if not body_text or len(body_text) > 200_000:
            raise ValueError("draft_body_text_invalid")
        if not subject.strip() or len(subject) > 1_000:
            raise ValueError("draft_subject_invalid")
        content_sha256 = digest_text(body_text)
        connection = await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=connection_id
        )
        draft_id = (
            uuid5(NAMESPACE_URL, f"mailhub:draft:{tenant_id}:{subject_id}:{idempotency_key}")
            if idempotency_key
            else uuid4()
        )
        if idempotency_key:
            existing = await self.repository.get_draft(tenant_id=tenant_id, draft_id=draft_id)
            if existing is not None:
                if (
                    existing.subject_id == subject_id
                    and existing.connection_id == connection_id
                    and existing.thread_id == thread_id
                    and existing.recipient_addresses == normalized_recipients
                    and existing.cc_addresses == normalized_cc
                    and existing.bcc_addresses == normalized_bcc
                    and existing.attachment_refs == normalized_attachments
                    and existing.subject == subject
                    and existing.content_sha256 == content_sha256
                ):
                    return existing
                raise ConflictError("draft_idempotency_conflict")
        if self.object_store is None and connection.provider is not ProviderName.SANDBOX:
            raise ProviderFailureError("object_store_unconfigured")
        body_object_ref = None
        stored_body: str | None = body_text
        expires_at = utc_now() + self.draft_content_ttl
        if self.object_store is not None:
            body_object_ref = await self.object_store.put_text(
                tenant_id=tenant_id,
                subject_id=subject_id,
                purpose="mail_draft",
                content=body_text,
                content_sha256=content_sha256,
                expires_at=expires_at,
            )
            stored_body = None
        try:
            draft = MailDraft(
                draft_id=draft_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                connection_id=connection_id,
                thread_id=thread_id,
                recipient_addresses=normalized_recipients,
                subject=subject,
                body_text=stored_body,
                content_sha256=content_sha256,
                cc_addresses=normalized_cc,
                bcc_addresses=normalized_bcc,
                attachment_refs=normalized_attachments,
                body_object_ref=body_object_ref,
                provider_draft_ref=provider_draft_ref,
                expires_at=expires_at,
            )
            await self.repository.save_draft(draft)
        except Exception:
            if body_object_ref is not None and self.object_store is not None:
                await self.object_store.delete(
                    tenant_id=tenant_id, subject_id=subject_id, object_ref=body_object_ref
                )
            if idempotency_key:
                existing = await self.repository.get_draft(tenant_id=tenant_id, draft_id=draft_id)
                if (
                    existing is not None
                    and existing.connection_id == connection_id
                    and existing.thread_id == thread_id
                    and existing.recipient_addresses == normalized_recipients
                    and existing.cc_addresses == normalized_cc
                    and existing.bcc_addresses == normalized_bcc
                    and existing.attachment_refs == normalized_attachments
                    and existing.subject == subject
                    and existing.content_sha256 == content_sha256
                ):
                    return existing
            raise
        assert draft is not None
        await self.repository.append_audit(
            self._audit(
                "mail.draft.created",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(draft.draft_id),
            )
        )
        await self._publish_event(
            MailHubEventType.DRAFT_CREATED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(draft.draft_id),
            idempotency_key=f"draft:{draft.draft_id}:revision:{draft.revision}",
            data={
                "connection_id": str(draft.connection_id),
                "thread_id": str(draft.thread_id) if draft.thread_id else None,
                "revision": draft.revision,
                "content_sha256": draft.content_sha256,
                "recipient_digest": digest_recipient_headers(
                    draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
                ),
                "attachment_count": len(draft.attachment_refs),
                "expires_at": draft.expires_at.isoformat(),
            },
            occurred_at=draft.created_at,
        )
        return draft

    async def get_draft(self, *, tenant_id: str, subject_id: str, draft_id: UUID) -> MailDraft:
        draft = await self.repository.get_draft(tenant_id=tenant_id, draft_id=draft_id)
        if draft is None or draft.subject_id != subject_id:
            raise NotFoundError("draft_not_found")
        return draft

    async def update_draft(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        draft_id: UUID,
        expected_revision: int,
        connection_id: UUID,
        thread_id: UUID | None,
        recipient_addresses: tuple[str, ...],
        subject: str,
        body_text: str,
        cc_addresses: tuple[str, ...] = (),
        bcc_addresses: tuple[str, ...] = (),
        attachment_refs: tuple[str, ...] = (),
    ) -> MailDraft:
        """Create a new immutable draft revision without changing send state."""

        draft = await self.repository.get_draft(tenant_id=tenant_id, draft_id=draft_id)
        if draft is None or draft.subject_id != subject_id:
            raise NotFoundError("draft_not_found")
        if draft.revision != expected_revision:
            raise ConflictError("draft_revision_conflict")
        if draft.connection_id != connection_id or draft.thread_id != thread_id:
            raise ConflictError("draft_scope_immutable")
        if draft.status not in {DeliveryStatus.DRAFT, DeliveryStatus.AWAITING_CONFIRMATION}:
            raise ConflictError("draft_not_editable")
        connection = await self._connection(
            tenant_id=tenant_id, subject_id=subject_id, connection_id=draft.connection_id
        )
        if self.object_store is None and connection.provider is not ProviderName.SANDBOX:
            raise ProviderFailureError("object_store_unconfigured")
        expires_at = min(draft.expires_at, utc_now() + self.draft_content_ttl)
        if expires_at <= utc_now():
            raise ConflictError("draft_expired")
        content_sha256 = digest_text(body_text)
        normalized_recipients = ensure_addresses(recipient_addresses)
        normalized_cc = _optional_addresses_for_draft(cc_addresses, "cc_addresses")
        normalized_bcc = _optional_addresses_for_draft(bcc_addresses, "bcc_addresses")
        _ensure_disjoint_recipients(normalized_recipients, normalized_cc, normalized_bcc)
        normalized_attachments = _normalize_attachment_refs(attachment_refs)
        object_ref: str | None = None
        stored_body: str | None = body_text
        if self.object_store is not None:
            object_ref = await self.object_store.put_text(
                tenant_id=tenant_id,
                subject_id=subject_id,
                purpose="mail_draft",
                content=body_text,
                content_sha256=content_sha256,
                expires_at=expires_at,
            )
            stored_body = None
        try:
            updated = replace(
                draft,
                recipient_addresses=normalized_recipients,
                cc_addresses=normalized_cc,
                bcc_addresses=normalized_bcc,
                attachment_refs=normalized_attachments,
                subject=subject,
                body_text=stored_body,
                content_sha256=content_sha256,
                body_object_ref=object_ref,
                revision=draft.revision + 1,
                updated_at=utc_now(),
                expires_at=expires_at,
            )
            result = await self.repository.save_draft(updated)
        except Exception:
            if object_ref is not None and self.object_store is not None:
                await self.object_store.delete(
                    tenant_id=tenant_id, subject_id=subject_id, object_ref=object_ref
                )
            raise
        if draft.body_object_ref is not None and self.object_store is not None:
            await self.object_store.delete(
                tenant_id=tenant_id, subject_id=subject_id, object_ref=draft.body_object_ref
            )
        await self.repository.append_audit(
            self._audit(
                "mail.draft.revised",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(draft_id),
                revision=result.revision,
            )
        )
        await self._publish_event(
            MailHubEventType.DRAFT_REVISED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(draft_id),
            idempotency_key=f"draft:{draft_id}:revision:{result.revision}",
            data={
                "connection_id": str(result.connection_id),
                "thread_id": str(result.thread_id) if result.thread_id else None,
                "revision": result.revision,
                "content_sha256": result.content_sha256,
                "recipient_digest": digest_recipient_headers(
                    result.recipient_addresses, result.cc_addresses, result.bcc_addresses
                ),
                "attachment_count": len(result.attachment_refs),
                "expires_at": result.expires_at.isoformat(),
            },
            occurred_at=result.updated_at,
        )
        return result

    async def queue_draft_send(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        draft_id: UUID,
        expected_revision: int,
        expected_content_sha256: str,
        expected_recipient_digest: str,
        confirmation_ref: str | None = None,
        policy_id: UUID | None = None,
        grant_id: UUID | None = None,
        agent_subject_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        if idempotency_key is not None and (
            not idempotency_key.strip() or len(idempotency_key) > 300
        ):
            raise ValueError("send_idempotency_key_invalid")
        draft = await self.repository.get_draft(tenant_id=tenant_id, draft_id=draft_id)
        if draft is None or draft.subject_id != subject_id:
            raise NotFoundError("draft_not_found")
        now = utc_now()
        if draft.expires_at <= now:
            raise ConflictError("draft_expired")
        send_revision = (
            draft.revision - 1 if draft.status is DeliveryStatus.APPROVED else draft.revision
        )
        idempotency_key = _draft_send_idempotency_key(draft, revision=max(1, send_revision))
        legacy_idempotency_key = (
            f"mail-send:{draft.connection_id}:{draft.draft_id}:{draft.content_sha256}"
        )
        # A replay of an already-approved immutable draft returns its existing
        # operation before checking the caller's original revision precondition.
        # This is the one safe exception to the precondition rule: no new
        # provider I/O can be introduced by the replay.
        if draft.status is DeliveryStatus.APPROVED:
            existing = await self.repository.get_operation(
                tenant_id=tenant_id,
                operation_id=uuid5(NAMESPACE_URL, idempotency_key),
            )
            if existing is None:
                # Preserve replayability for operations created before the
                # recipient/attachment-bound idempotency key was introduced.
                existing = await self.repository.get_operation(
                    tenant_id=tenant_id,
                    operation_id=uuid5(NAMESPACE_URL, legacy_idempotency_key),
                )
            if existing is not None and existing.subject_id == subject_id:
                return {
                    "operation_id": str(existing.operation_id),
                    "status": existing.status.value,
                    "created": False,
                    "pending": existing.status
                    in {DeliveryStatus.QUEUED, DeliveryStatus.LEASED, DeliveryStatus.SENDING},
                    "decision": "idempotent_replay",
                }
        if (
            draft.revision != expected_revision
            or draft.content_sha256 != expected_content_sha256
            or digest_recipient_headers(
                draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
            )
            != expected_recipient_digest
        ):
            raise ConflictError("draft_precondition_failed")
        if draft.status not in {DeliveryStatus.DRAFT, DeliveryStatus.AWAITING_CONFIRMATION}:
            raise ConflictError("draft_not_sendable")
        thread = (
            await self.repository.get_thread(tenant_id=tenant_id, thread_id=draft.thread_id)
            if draft.thread_id is not None
            else None
        )
        if draft.thread_id is not None and thread is None:
            raise ConflictError("draft_thread_not_found")
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=draft.connection_id
        )
        if connection is None or connection.subject_id != draft.subject_id:
            raise ConflictError("draft_connection_not_found")
        known_participants = set(thread.participant_addresses) if thread else set()
        all_recipients = (
            *draft.recipient_addresses,
            *draft.cc_addresses,
            *draft.bcc_addresses,
        )
        has_new_recipient = bool(
            thread is not None
            and any(address not in known_participants for address in all_recipients)
        )
        has_external_recipient, has_large_recipient_set = _recipient_risk_flags(
            connection.email_address, all_recipients
        )
        action_context = AgentActionContext(
            tenant_id=tenant_id,
            agent_subject_id=agent_subject_id or subject_id,
            connection_id=draft.connection_id,
            folder_ref="INBOX",
            thread_id=draft.thread_id,
            recipient_addresses=all_recipients,
            has_attachment=bool(draft.attachment_refs),
            has_bcc=bool(draft.bcc_addresses),
            has_external_recipient=has_external_recipient,
            has_large_recipient_set=has_large_recipient_set,
            has_new_recipient=has_new_recipient,
            contains_high_risk_terms=_contains_high_risk_terms(draft.subject, draft.body_text),
        )
        recipient_digest = digest_recipient_headers(
            draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
        )
        input_digest = sha256(
            f"{draft.draft_id}:{draft.revision}:{recipient_digest}:"
            f"{','.join(draft.attachment_refs)}:{draft.content_sha256}".encode()
        ).hexdigest()
        action = AgentActionRequest(
            action_id=uuid4(),
            action_type=ActionType.SEND_REPLY,
            context=action_context,
            input_digest=input_digest,
            policy_revision=None,
            grant_revision=None,
        )
        policy = (
            await self.repository.get_policy(tenant_id=tenant_id, policy_id=policy_id)
            if policy_id
            else None
        )
        grant = (
            await self.repository.get_grant(tenant_id=tenant_id, grant_id=grant_id)
            if grant_id
            else None
        )
        action = replace(
            action,
            policy_revision=policy.revision if policy is not None else None,
            grant_revision=grant.revision if grant is not None else None,
        )
        if policy is not None:
            hour_count = await self.repository.count_operations_since(
                tenant_id=tenant_id,
                subject_id=subject_id,
                since=now - timedelta(hours=1),
            )
            if policy.max_per_hour and hour_count >= policy.max_per_hour:
                raise RateLimitedError("policy_hourly_limit")
            day_count = await self.repository.count_operations_since(
                tenant_id=tenant_id,
                subject_id=subject_id,
                since=now - timedelta(days=1),
            )
            if policy.max_per_day and day_count >= policy.max_per_day:
                raise RateLimitedError("policy_daily_limit")
        decision = evaluate_policy(policy, grant, action)
        confirmed = False
        if not decision.allowed:
            # Confirmation can satisfy an explicitly approval-required risk
            # decision, but it must never bypass a missing, expired, mismatched
            # or otherwise denied policy/delegation.
            if decision.reason_code != "approval_required":
                raise PolicyDeniedError(
                    decision.reason_code, details={"action_id": str(action.action_id)}
                )
            if confirmation_ref is None or self.approval_port is None:
                raise ApprovalRequiredError(
                    "explicit_confirmation_required",
                    details={
                        "action_id": str(action.action_id),
                        "decision": decision.reason_code,
                    },
                )
            confirmed = await self.approval_port.verify_confirmation(
                confirmation_ref=confirmation_ref,
                action=action,
                # The principal presenting the confirmation right now.
                approver_subject_id=subject_id,
            )
            if not confirmed:
                raise AuthorizationError("confirmation_invalid")
        elif confirmation_ref is not None and self.approval_port is not None:
            confirmed = await self.approval_port.verify_confirmation(
                confirmation_ref=confirmation_ref,
                action=action,
                approver_subject_id=subject_id,
            )
            if not confirmed:
                raise AuthorizationError("confirmation_invalid")

        if not self.outbound_enabled:
            raise KillSwitchError("outbound_kill_switch_active")
        await self._check_kill_switch(
            tenant_id=tenant_id,
            subject_id=subject_id,
            provider=connection.provider,
            operation="mail.outbound.queue",
        )
        updated_draft = replace(
            draft,
            status=DeliveryStatus.APPROVED,
            revision=draft.revision + 1,
            updated_at=now,
        )
        try:
            await self.repository.save_draft(updated_draft)
        except Exception as exc:
            # A concurrent approval may have advanced the draft revision.  Do
            # not expose a storage exception or issue a second operation.
            raise ConflictError("draft_revision_conflict") from exc
        operation = MailOutboxOperation(
            operation_id=uuid5(NAMESPACE_URL, idempotency_key),
            tenant_id=tenant_id,
            subject_id=subject_id,
            draft_id=draft.draft_id,
            connection_id=draft.connection_id,
            idempotency_key=idempotency_key,
            status=DeliveryStatus.QUEUED,
            action_id=action.action_id,
            input_digest=action.input_digest,
            agent_subject_id=action.context.agent_subject_id,
            policy_id=policy.policy_id if policy is not None else None,
            grant_id=grant.grant_id if grant is not None else None,
            policy_revision=policy.revision if policy is not None else None,
            grant_revision=grant.revision if grant is not None else None,
            approval_ref=confirmation_ref if confirmed else None,
            approver_subject_id=subject_id if confirmed else None,
        )
        stored, created = await self.repository.create_or_get_operation(operation)
        await self.repository.append_audit(
            self._audit(
                "mail.outbox.queued",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(stored.operation_id),
                action_id=str(action.action_id),
                policy_id=str(policy_id) if policy_id else None,
                grant_id=str(grant_id) if grant_id else None,
                confirmed=confirmed,
                idempotency_key=idempotency_key,
            )
        )
        await self._publish_event(
            MailHubEventType.OUTBOX_QUEUED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(stored.operation_id),
            idempotency_key=f"outbox:{stored.operation_id}:queued",
            data={
                "draft_id": str(stored.draft_id),
                "connection_id": str(stored.connection_id),
                "idempotency_key": stored.idempotency_key,
                "action_id": str(stored.action_id) if stored.action_id else None,
                "confirmed": confirmed,
            },
            occurred_at=stored.updated_at,
        )
        return {
            "operation_id": str(stored.operation_id),
            "status": stored.status.value,
            "created": created,
            "pending": stored.status
            in {DeliveryStatus.QUEUED, DeliveryStatus.LEASED, DeliveryStatus.SENDING},
            "decision": decision.reason_code,
        }

    async def send_queued(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
    ) -> MailOutboxOperation:
        """Run one durable send and publish bounded outcome telemetry."""

        if not self.outbound_enabled:
            raise KillSwitchError("outbound_kill_switch_active")
        operation = await self.repository.get_operation(
            tenant_id=tenant_id, operation_id=operation_id
        )
        if operation is not None:
            connection = await self.repository.get_connection(
                tenant_id=tenant_id, connection_id=operation.connection_id
            )
            await self._check_kill_switch(
                tenant_id=tenant_id,
                subject_id=operation.subject_id,
                provider=connection.provider if connection is not None else None,
                operation="mail.outbound.send",
            )
        quota_lease = await self._acquire_quota(
            tenant_id=tenant_id,
            subject_id=operation.subject_id if operation is not None else "worker",
            account_id=operation.connection_id if operation is not None else None,
            operation="mail.send",
        )
        try:
            result = await self._send_queued(
                tenant_id=tenant_id,
                operation_id=operation_id,
                worker_id=worker_id,
            )
        except MailHubError as exc:
            await self._publish_event(
                MailHubEventType.OUTBOX_FAILED,
                tenant_id=tenant_id,
                subject_id=operation.subject_id if operation is not None else None,
                target_ref=str(operation_id),
                idempotency_key=f"outbox:{operation_id}:worker-failed:{exc.message}",
                data={"error_code": exc.message},
            )
            await self._record_telemetry(
                "mail.outbox.failed",
                {
                    "tenant_id": tenant_id,
                    "operation_id": str(operation_id),
                    "error_code": exc.message,
                },
            )
            raise
        finally:
            await self._release_quota(quota_lease)
        outcome_event_type = {
            DeliveryStatus.SUCCEEDED: MailHubEventType.OUTBOX_SENT,
            DeliveryStatus.OUTCOME_UNKNOWN: MailHubEventType.OUTBOX_OUTCOME_UNKNOWN,
            DeliveryStatus.DEAD_LETTER: MailHubEventType.OUTBOX_DEAD_LETTERED,
            DeliveryStatus.RETRY_WAIT: MailHubEventType.OUTBOX_FAILED,
        }.get(result.status)
        if outcome_event_type is not None:
            await self._publish_event(
                outcome_event_type,
                tenant_id=tenant_id,
                subject_id=result.subject_id,
                target_ref=str(result.operation_id),
                idempotency_key=(
                    f"outbox:{result.operation_id}:{result.status.value}:{result.attempt_count}"
                ),
                data={
                    "status": result.status.value,
                    "attempt_count": result.attempt_count,
                    "error_code": result.error_code,
                    "provider_message_ref": result.provider_message_ref,
                    "provider_request_id": result.provider_request_id,
                    "idempotency_key": result.idempotency_key,
                },
                occurred_at=result.updated_at,
            )
        await self._record_telemetry(
            "mail.outbox.completed",
            {
                "tenant_id": tenant_id,
                "operation_id": str(result.operation_id),
                "status": result.status.value,
                "attempt_count": result.attempt_count,
                "error_code": result.error_code,
            },
        )
        return result

    async def _send_queued(
        self,
        *,
        tenant_id: str,
        operation_id: UUID,
        worker_id: str,
    ) -> MailOutboxOperation:
        if not self.outbound_enabled:
            raise KillSwitchError("outbound_kill_switch_active")
        operation = await self.repository.lease_operation(
            tenant_id=tenant_id,
            operation_id=operation_id,
            worker_id=worker_id,
            lease_seconds=int(self.outbox_lease_ttl.total_seconds()),
        )
        sending = await self.repository.mark_sending(
            tenant_id=tenant_id,
            operation_id=operation.operation_id,
            worker_id=worker_id,
            fencing_token=operation.fencing_token,
        )
        draft = await self.repository.get_draft(tenant_id=tenant_id, draft_id=sending.draft_id)
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=sending.connection_id
        )
        if draft is None or connection is None:
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.DEAD_LETTER,
                error_code="send_source_missing",
            )
        if connection.status is not ConnectionStatus.ACTIVE:
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.DEAD_LETTER,
                error_code="connection_not_active",
            )
        authorization_error = await self._queued_authorization_error(
            operation=sending,
            draft=draft,
            connection=connection,
            tenant_id=tenant_id,
        )
        if authorization_error is not None:
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.DEAD_LETTER,
                error_code=authorization_error,
            )
        try:
            kill_switch_allowed, kill_switch_reason = await self._check_kill_switch(
                tenant_id=tenant_id,
                subject_id=connection.subject_id,
                provider=connection.provider,
                operation="mail.outbound.send",
                fail_fast=False,
            )
        except MailHubError as exc:
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.RETRY_WAIT,
                error_code=exc.message,
                next_attempt_at=utc_now() + timedelta(seconds=60),
            )
        if not kill_switch_allowed:
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.RETRY_WAIT,
                error_code=kill_switch_reason,
                next_attempt_at=utc_now() + timedelta(seconds=60),
            )
        connector = self._connector(connection.provider)
        if not connector.capabilities.supports_send:
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.DEAD_LETTER,
                error_code="provider_send_capability_disabled",
            )
        if draft.attachment_refs and not connector.capabilities.supports_attachments:
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.DEAD_LETTER,
                error_code="provider_attachment_capability_disabled",
            )
        try:
            if self.credential_broker is None:
                raise AuthorizationError("credential_broker_unconfigured")
            credential = await self.credential_broker.resolve(
                credential_ref=connection.credential_ref,
                tenant_id=tenant_id,
                subject_id=connection.subject_id,
            )
            _validate_credential_binding(connection, credential)
            if connection.provider is ProviderName.IMAP_SMTP:
                username = str(credential.get("username", "")).strip().casefold()
                if username != connection.email_address.casefold():
                    raise AuthorizationError("smtp_sender_identity_mismatch")
            body_text = await self._draft_body(
                draft, tenant_id=tenant_id, subject_id=connection.subject_id
            )
            request = ProviderSendRequest(
                operation_id=sending.operation_id,
                connection_id=connection.connection_id,
                thread_ref=str(draft.thread_id) if draft.thread_id else None,
                recipient_addresses=draft.recipient_addresses,
                cc_addresses=draft.cc_addresses,
                bcc_addresses=draft.bcc_addresses,
                attachment_refs=draft.attachment_refs,
                subject=draft.subject,
                body_text=body_text,
                content_sha256=draft.content_sha256,
                idempotency_key=sending.idempotency_key,
            )
            receipt = await connector.send(request, credential=credential)
            if (
                receipt.idempotency_key != sending.idempotency_key
                or receipt.content_sha256 != draft.content_sha256
            ):
                raise OutcomeUnknownError("provider_receipt_identity_mismatch")
            result = await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.SUCCEEDED,
                provider_message_ref=receipt.provider_message_ref,
                provider_request_id=receipt.provider_request_id,
            )
            await self.repository.append_audit(
                self._audit(
                    "mail.outbox.sent",
                    tenant_id=tenant_id,
                    subject_id=connection.subject_id,
                    target_ref=str(result.operation_id),
                    provider_request_id=receipt.provider_request_id,
                )
            )
            return result
        except OutcomeUnknownError as exc:
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.OUTCOME_UNKNOWN,
                error_code=exc.message,
            )
        except (RateLimitedError, ProviderFailureError) as exc:
            retryable = sending.attempt_count < 4
            status = DeliveryStatus.RETRY_WAIT if retryable else DeliveryStatus.DEAD_LETTER
            next_attempt = (
                utc_now()
                + timedelta(
                    seconds=_retry_delay(
                        operation_id=sending.operation_id,
                        attempt_count=sending.attempt_count,
                    )
                )
                if retryable
                else None
            )
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=status,
                error_code=exc.message,
                next_attempt_at=next_attempt,
            )
        except MailHubError as exc:
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.DEAD_LETTER,
                error_code=exc.message,
            )
        except (TimeoutError, ConnectionError, OSError) as exc:
            del exc
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.OUTCOME_UNKNOWN,
                error_code="provider_outcome_unknown",
            )
        except Exception:
            # An unclassified provider failure is never reported as success.  The
            # operation remains explicitly uncertain so reconciliation can decide
            # whether retry is safe.
            return await self.repository.complete_operation(
                tenant_id=tenant_id,
                operation_id=sending.operation_id,
                worker_id=worker_id,
                fencing_token=sending.fencing_token,
                status=DeliveryStatus.OUTCOME_UNKNOWN,
                error_code="provider_unclassified_failure",
            )

    async def _queued_authorization_error(
        self,
        *,
        operation: MailOutboxOperation,
        draft: MailDraft,
        connection: MailboxConnection,
        tenant_id: str,
    ) -> str | None:
        """Re-check policy/grant/approval immediately before provider I/O."""

        policy = (
            await self.repository.get_policy(tenant_id=tenant_id, policy_id=operation.policy_id)
            if operation.policy_id is not None
            else None
        )
        grant = (
            await self.repository.get_grant(tenant_id=tenant_id, grant_id=operation.grant_id)
            if operation.grant_id is not None
            else None
        )
        if operation.policy_id is not None and (
            policy is None or operation.policy_revision != policy.revision
        ):
            return "policy_revision_changed"
        if operation.grant_id is not None and (
            grant is None or operation.grant_revision != grant.revision
        ):
            return "grant_revision_changed"
        now = utc_now()
        if policy is not None:
            if not policy.enabled:
                return "policy_disabled"
            if not policy.valid_from <= now < policy.valid_until:
                return "policy_expired"
        if grant is not None and (
            grant.revoked_at is not None or not grant.granted_at <= now < grant.expires_at
        ):
            return "delegation_expired"
        all_recipients = (
            *draft.recipient_addresses,
            *draft.cc_addresses,
            *draft.bcc_addresses,
        )
        has_external_recipient, has_large_recipient_set = _recipient_risk_flags(
            connection.email_address, all_recipients
        )
        action_context = AgentActionContext(
            tenant_id=tenant_id,
            agent_subject_id=operation.agent_subject_id or operation.subject_id,
            connection_id=connection.connection_id,
            folder_ref="INBOX",
            thread_id=draft.thread_id,
            recipient_addresses=all_recipients,
            has_attachment=bool(draft.attachment_refs),
            has_bcc=bool(draft.bcc_addresses),
            has_external_recipient=has_external_recipient,
            has_large_recipient_set=has_large_recipient_set,
            has_new_recipient=await self._draft_has_new_recipient(tenant_id, draft),
            contains_high_risk_terms=_contains_high_risk_terms(draft.subject, draft.body_text),
        )
        recipient_digest = digest_recipient_headers(
            draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
        )
        action = AgentActionRequest(
            action_id=operation.action_id or uuid5(NAMESPACE_URL, operation.idempotency_key),
            action_type=ActionType.SEND_REPLY,
            context=action_context,
            input_digest=sha256(
                (
                    f"{draft.draft_id}:{max(1, draft.revision - 1)}:"
                    f"{recipient_digest}:"
                    f"{','.join(draft.attachment_refs)}:{draft.content_sha256}"
                ).encode()
            ).hexdigest(),
            policy_revision=operation.policy_revision,
            grant_revision=operation.grant_revision,
        )
        if operation.input_digest is not None and action.input_digest != operation.input_digest:
            return "draft_input_changed"
        decision = evaluate_policy(policy, grant, action)
        if decision.allowed:
            return None
        if (
            decision.reason_code == "approval_required"
            and operation.approval_ref
            and self.approval_port is not None
            and await self.approval_port.verify_confirmation(
                confirmation_ref=operation.approval_ref,
                action=action,
                # Re-validation immediately before provider I/O, not a fresh
                # approval act.  The approver persisted when the draft was
                # queued is presented again so the host can re-assert
                # separation of duties on the same identity.
                approver_subject_id=operation.approver_subject_id,
                revalidation=True,
            )
        ):
            return None
        if operation.approval_ref and (
            self.approval_port is None
            or not await self.approval_port.verify_confirmation(
                confirmation_ref=operation.approval_ref,
                action=action,
                approver_subject_id=operation.approver_subject_id,
                revalidation=True,
            )
        ):
            return "approval_revoked"
        return f"authorization_{decision.reason_code}"

    async def _draft_has_new_recipient(self, tenant_id: str, draft: MailDraft) -> bool:
        if draft.thread_id is None:
            return False
        thread = await self.repository.get_thread(tenant_id=tenant_id, thread_id=draft.thread_id)
        if thread is None:
            return True
        participants = set(thread.participant_addresses)
        return any(
            address not in participants
            for address in (
                *draft.recipient_addresses,
                *draft.cc_addresses,
                *draft.bcc_addresses,
            )
        )

    async def propose_host_action(
        self,
        *,
        action: AgentActionRequest,
    ) -> Mapping[str, object]:
        if self.host_action_port is None:
            raise ProviderFailureError("host_action_port_unconfigured")
        return await self.host_action_port.propose(action=action)

    async def apply_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
        approval_ref: str,
    ) -> Mapping[str, object]:
        candidate = await self.repository.get_candidate(
            tenant_id=tenant_id, candidate_id=candidate_id
        )
        if candidate is None or candidate.subject_id != subject_id:
            raise NotFoundError("candidate_not_found")
        if candidate.status is CandidateState.APPLIED:
            stored_result = candidate.payload.get("application_result")
            if isinstance(stored_result, Mapping):
                return {"status": "applied", "replayed": True, **dict(stored_result)}
            raise ConflictError("candidate_already_applied")
        if candidate.status is not CandidateState.APPROVED:
            raise ApprovalRequiredError("candidate_approval_required")
        message = await self.repository.get_message(
            tenant_id=tenant_id, message_id=candidate.message_id
        )
        if message is None:
            raise NotFoundError("candidate_source_message_missing")
        action_type = {
            CandidateType.PROJECT: ActionType.UPDATE_TASK,
            CandidateType.TASK: ActionType.CREATE_TASK,
            CandidateType.KNOWLEDGE: ActionType.KNOWLEDGE_CANDIDATE,
        }[candidate.candidate_type]
        action = AgentActionRequest(
            # Candidate application is an external side effect.  A stable
            # action id lets a host adapter deduplicate a retry after the host
            # accepted the command but before MailHub persisted APPLIED.
            action_id=uuid5(
                NAMESPACE_URL,
                f"mailhub:candidate-action:{candidate.candidate_id}:{candidate.revision}",
            ),
            action_type=action_type,
            context=AgentActionContext(
                tenant_id=tenant_id,
                agent_subject_id=subject_id,
                connection_id=message.connection_id,
                folder_ref="INBOX",
                thread_id=message.thread_id,
                data_classes=frozenset({"internal"}),
            ),
            input_digest=digest_text(
                json.dumps(
                    {
                        "candidate_id": str(candidate.candidate_id),
                        "candidate_revision": candidate.revision,
                        "candidate_type": candidate.candidate_type.value,
                        "payload": candidate.payload,
                        "evidence": candidate.evidence,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            ),
            source_message_ids=(message.message_id,),
            parameters={
                "candidate_id": str(candidate.candidate_id),
                "candidate_revision": candidate.revision,
                "candidate_type": candidate.candidate_type.value,
                "candidate_payload": _bounded_candidate_value(candidate.payload),
                "candidate_evidence": _bounded_candidate_value(candidate.evidence),
                "source_message_id": str(message.message_id),
            },
        )
        if self.approval_port is None:
            raise ApprovalRequiredError("approval_port_unconfigured")
        if not await self.approval_port.verify_confirmation(
            confirmation_ref=approval_ref,
            action=action,
            approver_subject_id=subject_id,
        ):
            raise AuthorizationError("candidate_approval_invalid")
        candidate_to_persist = candidate
        memory_result: Mapping[str, object] | None = None
        if candidate.candidate_type is CandidateType.KNOWLEDGE:
            if self.knowledge_sink is None:
                raise ProviderFailureError("knowledge_sink_unconfigured")
            if self.knowledge_safety is None:
                raise ProviderFailureError("knowledge_safety_unconfigured")
            hydrated_message = await self._hydrate_message(
                message, tenant_id=tenant_id, subject_id=subject_id
            )
            gate = await self.knowledge_safety.evaluate_candidate(
                tenant_id=tenant_id,
                subject_id=subject_id,
                message=hydrated_message,
                candidate=candidate,
            )
            gated_candidate = _apply_knowledge_gate(candidate, gate)
            candidate_to_persist = gated_candidate
            result = await self.knowledge_sink.submit_candidate(
                tenant_id=tenant_id,
                subject_id=subject_id,
                message=hydrated_message,
                candidate=gated_candidate,
            )
            if self.agent_memory is not None and _knowledge_result_is_approved(result):
                knowledge_ref = _knowledge_ref_from_result(result)
                if knowledge_ref is None:
                    raise ProviderFailureError("knowledge_reference_missing")
                content_sha256 = gated_candidate.payload.get(
                    "content_sha256", hydrated_message.content_sha256
                )
                if not isinstance(content_sha256, str):
                    raise ProviderFailureError("knowledge_content_digest_missing")
                reference = ApprovedKnowledgeReference(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    candidate_id=candidate.candidate_id,
                    source_message_id=message.message_id,
                    knowledge_ref=knowledge_ref,
                    approval_ref=approval_ref,
                    content_sha256=content_sha256,
                    scope=str(gated_candidate.payload.get("suggested_scope", "personal")),
                    rights_state=str(gated_candidate.payload.get("rights_state", "")),
                    security_state=str(gated_candidate.payload.get("security_state", "")),
                    approved_at=_approved_at_from_result(result),
                )
                memory_result = await self.agent_memory.store_approved_reference(
                    reference=reference
                )
        else:
            if self.host_action_port is None:
                raise ProviderFailureError("host_action_port_unconfigured")
            result = await self.host_action_port.execute(action=action, approval_ref=approval_ref)
        application_evidence = _candidate_application_evidence(result)
        if memory_result is not None:
            application_evidence["memory"] = _candidate_application_evidence(memory_result)
        persisted_payload = dict(candidate_to_persist.payload)
        persisted_payload["application_result"] = application_evidence
        await self.repository.save_candidate(
            replace(
                candidate_to_persist,
                payload=persisted_payload,
                status=CandidateState.APPLIED,
                revision=candidate.revision + 1,
            )
        )
        await self.repository.append_audit(
            self._audit(
                "mail.candidate.applied",
                tenant_id=tenant_id,
                subject_id=subject_id,
                target_ref=str(candidate_id),
                action_id=str(action.action_id),
            )
        )
        await self._publish_event(
            MailHubEventType.CANDIDATE_APPLIED,
            tenant_id=tenant_id,
            subject_id=subject_id,
            target_ref=str(candidate_id),
            idempotency_key=f"candidate:{candidate_id}:applied:{candidate.revision + 1}",
            data={
                "candidate_type": candidate.candidate_type.value,
                "message_id": str(candidate.message_id),
                "candidate_revision": candidate.revision + 1,
                "action_id": str(action.action_id),
                "application_status": (
                    str(result.get("status")) if isinstance(result, Mapping) else "applied"
                ),
            },
        )
        return result

    async def _draft_body(self, draft: MailDraft, *, tenant_id: str, subject_id: str) -> str:
        if draft.body_text is not None:
            return draft.body_text
        if self.object_store is None or draft.body_object_ref is None:
            raise AuthorizationError("draft_body_unavailable")
        try:
            body = await self.object_store.get_text(
                tenant_id=tenant_id, subject_id=subject_id, object_ref=draft.body_object_ref
            )
        except KeyError as exc:
            raise AuthorizationError("draft_body_unavailable") from exc
        if digest_text(body) != draft.content_sha256:
            raise ConflictError("draft_body_digest_mismatch")
        return body

    async def _hydrate_message(
        self, message: MailMessageProjection, *, tenant_id: str, subject_id: str
    ) -> MailMessageProjection:
        if message.body_text is not None:
            return message
        if self.object_store is None or message.body_object_ref is None:
            raise ProviderFailureError("message_body_unavailable")
        try:
            body = await self.object_store.get_text(
                tenant_id=tenant_id, subject_id=subject_id, object_ref=message.body_object_ref
            )
        except KeyError as exc:
            raise ProviderFailureError("message_body_unavailable") from exc
        if digest_text(body) != message.content_sha256:
            raise ConflictError("message_body_digest_mismatch")
        return replace(message, body_text=body)

    async def _owns_message(
        self, tenant_id: str, subject_id: str, message: MailMessageProjection
    ) -> bool:
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=message.connection_id
        )
        return connection is not None and connection.subject_id == subject_id

    async def _connection(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID
    ) -> MailboxConnection:
        connection = await self.repository.get_connection(
            tenant_id=tenant_id, connection_id=connection_id
        )
        if connection is None or connection.subject_id != subject_id:
            raise NotFoundError("connection_not_found")
        if connection.status in {
            ConnectionStatus.PENDING_AUTHORIZATION,
            ConnectionStatus.REAUTHORIZATION_REQUIRED,
            ConnectionStatus.REVOKED,
            ConnectionStatus.DELETING,
            ConnectionStatus.DELETED,
        }:
            raise AuthorizationError("connection_not_active")
        return connection

    def _connector(self, provider: ProviderName) -> ProviderConnector:
        connector = self.connectors.get(provider)
        if connector is None:
            raise ProviderFailureError(
                "provider_connector_unavailable", details={"provider": provider.value}
            )
        return connector

    @staticmethod
    def _audit(
        event_type: str, *, tenant_id: str, subject_id: str, target_ref: str, **fields: object
    ) -> dict[str, object]:
        return {
            "event_type": event_type,
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            "target_ref": target_ref,
            "occurred_at": datetime.now(UTC).isoformat(),
            **fields,
        }

    @staticmethod
    def _connection_identity_fields(connection: MailboxConnection) -> dict[str, object]:
        """Return non-secret, hash-bound connection identity evidence."""

        fields: dict[str, object] = {"credential_version": connection.credential_version}
        for field_name in ("provider_account_id", "provider_tenant_id"):
            value = getattr(connection, field_name)
            fields[f"{field_name}_sha256"] = (
                sha256(value.encode("utf-8")).hexdigest() if value is not None else None
            )
        return fields

    async def _record_telemetry(self, name: str, fields: Mapping[str, object]) -> None:
        """Best-effort safe telemetry; exporter outages never alter mail state."""

        if self.telemetry is None:
            return
        try:
            await self.telemetry.record(name=name, fields=redact_event(fields))
        except Exception:
            # Telemetry is diagnostic evidence, not an authorization or
            # delivery dependency.  The audit ledger remains authoritative.
            return

    async def _publish_event(
        self,
        event_type: MailHubEventType,
        *,
        tenant_id: str,
        subject_id: str | None,
        target_ref: str,
        idempotency_key: str,
        data: Mapping[str, object] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Publish a stable, metadata-only lifecycle event when configured.

        The event bus is a host-owned integration boundary.  Core state and its
        audit ledger remain authoritative if the optional publisher is absent
        or temporarily unavailable; retries use a deterministic event id so a
        durable host can collapse at-least-once delivery.
        """

        if self.event_publisher is None:
            return
        normalized_target = target_ref.strip()
        raw_key = idempotency_key.strip()
        if not normalized_target or not raw_key:
            raise ValueError("mail_event_identity_invalid")
        event_key = raw_key if len(raw_key) <= 480 else f"sha256:{digest_text(raw_key)}"
        payload_data = {"target_ref": normalized_target, **dict(data or {})}
        event_id = uuid5(
            NAMESPACE_URL,
            f"mailhub:event:{event_type.value}:{tenant_id}:{subject_id or ''}:{raw_key}",
        )
        payload = build_event(
            event_type,
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=f"mailhub:{digest_text(raw_key)}",
            idempotency_key=event_key,
            event_id=event_id,
            data=payload_data,
            occurred_at=occurred_at,
        )
        try:
            await self.event_publisher.publish(payload)
        except Exception:
            with suppress(Exception):
                await self.repository.append_audit(
                    self._audit(
                        "mail.event.publish_failed",
                        tenant_id=tenant_id,
                        subject_id=subject_id or "system",
                        target_ref=normalized_target,
                        published_event_type=event_type.value,
                        idempotency_key=event_key,
                    )
                )
            await self._record_telemetry(
                "mail.event.publish_failed",
                {
                    "tenant_id": tenant_id,
                    "subject_id": subject_id,
                    "event_type": event_type.value,
                    "target_ref": normalized_target,
                    "idempotency_key": event_key,
                },
            )

    async def _check_kill_switch(
        self,
        *,
        tenant_id: str,
        subject_id: str | None,
        provider: ProviderName | None,
        operation: str,
        fail_fast: bool = True,
    ) -> tuple[bool, str | None]:
        """Evaluate the host-owned runtime switch immediately before side effects.

        A missing adapter preserves the local/static feature flags for sandbox
        and contract tests. Once configured, malformed, unavailable or denied
        decisions fail closed; a denied decision can be returned to a durable
        caller so it can persist a retryable blocked state instead of leaving a
        leased operation in ``sending``.
        """

        if self.kill_switch is None:
            return True, None
        audit_subject = subject_id or "system"
        try:
            decision = await self.kill_switch.check(
                tenant_id=tenant_id,
                subject_id=subject_id,
                provider=provider,
                operation=operation,
            )
        except MailHubError as exc:
            await self.repository.append_audit(
                self._audit(
                    "mail.kill_switch.check_failed",
                    tenant_id=tenant_id,
                    subject_id=audit_subject,
                    target_ref=operation,
                    provider=provider.value if provider is not None else None,
                    error_code=exc.message,
                )
            )
            raise
        except Exception as exc:
            await self.repository.append_audit(
                self._audit(
                    "mail.kill_switch.check_failed",
                    tenant_id=tenant_id,
                    subject_id=audit_subject,
                    target_ref=operation,
                    provider=provider.value if provider is not None else None,
                    error_code="kill_switch_unavailable",
                )
            )
            raise ProviderFailureError("kill_switch_unavailable") from exc

        if not isinstance(decision, Mapping) or not isinstance(decision.get("allowed"), bool):
            await self.repository.append_audit(
                self._audit(
                    "mail.kill_switch.check_failed",
                    tenant_id=tenant_id,
                    subject_id=audit_subject,
                    target_ref=operation,
                    provider=provider.value if provider is not None else None,
                    error_code="kill_switch_response_invalid",
                )
            )
            raise KillSwitchError("kill_switch_response_invalid")

        if decision["allowed"] is True:
            return True, None

        raw_reason = decision.get("reason")
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        if (
            not reason
            or len(reason) > 120
            or not all(character.isalnum() or character in "_.:-" for character in reason)
        ):
            reason = "kill_switch_active"
        raw_scope = decision.get("scope")
        scope = raw_scope.strip() if isinstance(raw_scope, str) else "host"
        if not scope or len(scope) > 40:
            scope = "host"
        await self.repository.append_audit(
            self._audit(
                "mail.kill_switch.blocked",
                tenant_id=tenant_id,
                subject_id=audit_subject,
                target_ref=operation,
                provider=provider.value if provider is not None else None,
                scope=scope,
                reason=reason,
            )
        )
        if fail_fast:
            raise KillSwitchError(reason)
        return False, reason

    async def _acquire_quota(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        account_id: UUID | None,
        operation: str,
    ) -> QuotaLease | None:
        if self.quota_port is None:
            return None
        return await self.quota_port.acquire(
            tenant_id=tenant_id,
            subject_id=subject_id,
            account_id=account_id,
            operation=operation,
            limits=self.quota_limits,
        )

    async def _release_quota(self, lease: QuotaLease | None) -> None:
        if lease is None or self.quota_port is None:
            return
        try:
            await self.quota_port.release(lease)
        except Exception:
            # Lease cleanup is best effort.  A durable implementation must
            # expire abandoned leases so a crashed worker cannot starve an
            # account indefinitely.
            return


def _thread_project_hint(candidates: list[MailActionCandidate]) -> str | None:
    """Extract only a bounded project hint from candidate metadata."""

    for candidate in candidates:
        for source in (candidate.payload, candidate.payload.get("target")):
            if not isinstance(source, Mapping):
                continue
            for key in ("project_hint", "project_ref", "project_id"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:200]
            refs = source.get("project_refs")
            if isinstance(refs, (list, tuple)) and refs:
                first = refs[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()[:200]
    return None


def _assign_candidate_revisions(
    candidates: tuple[MailActionCandidate, ...], existing: tuple[MailActionCandidate, ...]
) -> tuple[MailActionCandidate, ...]:
    """Give re-analyzed candidates fresh revisions per (message, type).

    The candidate table enforces a unique (tenant, message, type, revision)
    key, so a second analysis of the same message (e.g. a changed model
    output) must not re-use revision 1.  Revisions continue after the
    highest existing revision for the same message and type.
    """

    next_revision: dict[tuple[UUID, CandidateType], int] = {}
    for item in existing:
        key = (item.message_id, item.candidate_type)
        next_revision[key] = max(next_revision.get(key, 0), item.revision)
    result: list[MailActionCandidate] = []
    for candidate in candidates:
        key = (candidate.message_id, candidate.candidate_type)
        revision = next_revision.get(key, 0) + 1
        next_revision[key] = revision
        result.append(
            replace(candidate, revision=revision) if candidate.revision != revision else candidate
        )
    return tuple(result)


def _candidates_from_result(
    *, tenant_id: str, subject_id: str, result: IntelligenceResult
) -> tuple[MailActionCandidate, ...]:
    if result.abstain_reason is not None:
        return ()
    evidence = tuple(
        {
            "message_id": str(item.message_id),
            "field": item.field,
            "start": item.start,
            "end": item.end,
            "text_sha256": item.text_sha256,
        }
        for item in result.evidence
    )
    candidates: list[MailActionCandidate] = []
    for action in result.action_candidates:
        project_refs = _string_values(action.get("project_refs"))
        task_refs = _string_values(action.get("task_refs"))
        for candidate_type, refs in (
            (CandidateType.PROJECT, project_refs),
            (CandidateType.TASK, task_refs),
        ):
            if not refs:
                continue
            payload = dict(action)
            payload["refs"] = refs
            payload["candidate_type"] = candidate_type.value
            candidate_confidence = _confidence(action.get("confidence"), result.confidence)
            if candidate_confidence < result.action_min_confidence:
                continue
            payload["analysis_policy_version"] = result.policy_version
            payload["calibration_version"] = result.calibration_version
            payload["analysis_budget"] = dict(result.analysis_metadata)
            digest = digest_text(
                f"{candidate_type.value}:"
                + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
            )
            candidates.append(
                MailActionCandidate(
                    candidate_id=uuid5(
                        NAMESPACE_URL,
                        f"mailhub:candidate:{result.message_id}:{candidate_type.value}:{digest}",
                    ),
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    message_id=result.message_id,
                    candidate_type=candidate_type,
                    payload=payload,
                    evidence=evidence,
                    confidence=candidate_confidence,
                    requires_review=True,
                )
            )
    if result.knowledge_candidate is not None:
        if result.confidence < result.knowledge_min_confidence:
            return tuple(candidates)
        payload = dict(result.knowledge_candidate)
        if result.content_sha256:
            payload["content_sha256"] = result.content_sha256
        payload["analysis_policy_version"] = result.policy_version
        payload["calibration_version"] = result.calibration_version
        payload["analysis_budget"] = dict(result.analysis_metadata)
        digest = digest_text(f"knowledge:{payload}")
        candidates.append(
            MailActionCandidate(
                candidate_id=uuid5(
                    NAMESPACE_URL,
                    f"mailhub:candidate:{result.message_id}:knowledge:{digest}",
                ),
                tenant_id=tenant_id,
                subject_id=subject_id,
                message_id=result.message_id,
                candidate_type=CandidateType.KNOWLEDGE,
                payload=payload,
                evidence=evidence,
                confidence=result.confidence,
                requires_review=True,
            )
        )
    return tuple(candidates)


def _export_safe(value: object) -> object:
    """Convert export projections while dropping governed reference handles."""

    if is_dataclass(value):
        return _export_safe(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        safe: dict[str, object] = {}
        for key, child in value.items():
            normalized = str(key).casefold()
            if (
                "credential_ref" in normalized
                or "object_ref" in normalized
                or "attachment_ref" in normalized
                or "governed_ref" in normalized
            ):
                continue
            safe[str(key)] = _export_safe(child)
        return safe
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_export_safe(item) for item in value]
    return value


def _knowledge_lifecycle_proof(
    value: Mapping[str, object], *, request_id: str
) -> Mapping[str, object]:
    """Keep downstream lifecycle evidence bounded and body-free."""

    proof: dict[str, object] = {"request_id": request_id}
    for key in ("status", "knowledge_ref", "revoke_ref", "reindexed_count", "revoked_count"):
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)):
            proof[key] = item
    return proof


def _apply_knowledge_gate(
    candidate: MailActionCandidate, gate: Mapping[str, object]
) -> MailActionCandidate:
    """Require host-owned security and rights decisions before submission."""

    security_state = gate.get("security_state")
    rights_state = gate.get("rights_state")
    if not isinstance(security_state, str) or not isinstance(rights_state, str):
        raise ProviderFailureError("knowledge_safety_response_invalid")
    if security_state != "cleared":
        raise ProviderFailureError(
            "knowledge_security_quarantined"
            if security_state == "quarantined"
            else "knowledge_security_gate_blocked"
        )
    if rights_state != "approved":
        raise ApprovalRequiredError("knowledge_rights_review_required")
    safe_payload = dict(candidate.payload)
    safe_payload["security_state"] = security_state
    safe_payload["rights_state"] = rights_state
    gate_ref = gate.get("gate_ref")
    if gate_ref is not None:
        if not isinstance(gate_ref, str) or not gate_ref.strip() or len(gate_ref) > 512:
            raise ProviderFailureError("knowledge_safety_response_invalid")
        safe_payload["gate_ref"] = gate_ref
    return replace(candidate, payload=safe_payload)


def _bounded_candidate_value(value: object, *, depth: int = 0) -> object:
    """Build a body-free, bounded payload for a host action.

    Host project/task systems need the candidate target and source evidence,
    not the original email.  Candidate payloads can be model- or host-supplied
    mappings, so this boundary limits depth/cardinality/string size and drops
    raw-content fields before they cross HostActionPort.
    """

    if depth >= 3:
        return "[truncated]"
    if isinstance(value, Mapping):
        bounded: dict[str, object] = {}
        entries = sorted(
            ((str(key), child) for key, child in value.items()), key=lambda item: item[0]
        )
        for key, child in entries[:32]:
            normalized = key.casefold()
            if any(
                marker in normalized
                for marker in ("body", "html", "mime", "raw_header", "raw_message", "original_text")
            ) or normalized in {"text", "content", "raw"}:
                continue
            bounded[key[:120]] = _bounded_candidate_value(child, depth=depth + 1)
        return bounded
    if isinstance(value, (tuple, list, set, frozenset)):
        items = list(value)
        if isinstance(value, (set, frozenset)):
            items.sort(key=repr)
        return [_bounded_candidate_value(item, depth=depth + 1) for item in items[:32]]
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:200]


def _candidate_application_evidence(result: object) -> dict[str, object]:
    """Persist only bounded, non-content evidence for an applied candidate."""

    if not isinstance(result, Mapping):
        return {"status": "applied"}
    allowed = {
        "status",
        "execution_id",
        "action_id",
        "proposal_id",
        "knowledge_candidate_ref",
        "result_ref",
        "provider_request_id",
        "error_code",
    }
    evidence: dict[str, object] = {}
    for key in allowed:
        value = result.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > 512:
                value = value[:512]
            evidence[key] = value
    evidence.setdefault("status", "applied")
    return evidence


def _knowledge_result_is_approved(result: Mapping[str, object]) -> bool:
    status = result.get("status")
    return isinstance(status, str) and status.casefold() in {"approved", "published"}


def _knowledge_ref_from_result(result: Mapping[str, object]) -> str | None:
    for key in ("knowledge_ref", "knowledge_candidate_ref", "result_ref"):
        value = result.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 500:
            return value
    return None


def _approved_at_from_result(result: Mapping[str, object]) -> datetime:
    value = result.get("approved_at")
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(UTC)
    return utc_now()


def _deduplicate_candidates(
    candidates: tuple[MailActionCandidate, ...],
    existing: tuple[MailActionCandidate, ...],
) -> tuple[MailActionCandidate, ...]:
    """Suppress exact replay and retain same-content lineage for review.

    A content hash is never used to merge permissions. A new source remains a
    separate candidate, with duplicate lineage explicitly attached for the
    host knowledge authority to review.
    """

    existing_ids = {item.candidate_id for item in existing}
    existing_action_fingerprints = {
        fingerprint
        for item in existing
        if (fingerprint := _candidate_action_fingerprint(item)) is not None
    }
    result: list[MailActionCandidate] = []
    for candidate in candidates:
        if candidate.candidate_id in existing_ids:
            continue
        action_fingerprint = _candidate_action_fingerprint(candidate)
        if action_fingerprint is not None and action_fingerprint in existing_action_fingerprints:
            payload = dict(candidate.payload)
            duplicate_refs = tuple(
                str(item.candidate_id)
                for item in (*existing, *result)
                if _candidate_action_fingerprint(item) == action_fingerprint
            )
            payload["action_dedupe_status"] = "duplicate_requires_review"
            payload["duplicate_action_refs"] = duplicate_refs
            candidate = replace(candidate, payload=payload)
        if candidate.candidate_type is CandidateType.KNOWLEDGE:
            content_sha256 = candidate.payload.get("content_sha256")
            if isinstance(content_sha256, str) and content_sha256:
                duplicate_refs = tuple(
                    str(item.candidate_id)
                    for item in existing
                    if item.candidate_type is CandidateType.KNOWLEDGE
                    and item.payload.get("content_sha256") == content_sha256
                )
                if duplicate_refs:
                    payload = dict(candidate.payload)
                    payload["content_dedupe_status"] = "duplicate_requires_lineage_review"
                    payload["duplicate_content_refs"] = duplicate_refs
                    candidate = replace(candidate, payload=payload)
        result.append(candidate)
        existing_ids.add(candidate.candidate_id)
        if action_fingerprint is not None:
            existing_action_fingerprints.add(action_fingerprint)
    return tuple(result)


def _candidate_action_fingerprint(candidate: MailActionCandidate) -> str | None:
    """Return a bounded action identity without merging separate sources."""

    if candidate.candidate_type not in {CandidateType.PROJECT, CandidateType.TASK}:
        return None
    payload = candidate.payload
    refs = _string_values(payload.get("refs"))
    if not refs:
        return None
    comparable = {
        "candidate_type": candidate.candidate_type.value,
        "refs": tuple(sorted(set(refs))),
        "action_type": str(payload.get("action_type", "follow_up")),
        "due_date_refs": tuple(sorted(set(_string_values(payload.get("due_date_refs"))))),
        "commitments": tuple(sorted(set(_string_values(payload.get("commitments"))))),
    }
    return digest_text(json.dumps(comparable, sort_keys=True, separators=(",", ":")))


def _provider_error_metadata(error: MailHubError) -> dict[str, object]:
    """Project only bounded provider retry metadata into runtime evidence."""

    details = error.details
    if not isinstance(details, Mapping):
        return {}
    projected: dict[str, object] = {}
    status = details.get("provider_status")
    if isinstance(status, int) and 100 <= status <= 599:
        projected["provider_status"] = status
    reason = details.get("provider_reason")
    if (
        isinstance(reason, str)
        and 1 <= len(reason) <= 96
        and re.fullmatch(r"[a-z0-9_-]+", reason) is not None
    ):
        projected["provider_reason"] = reason
    retry_after = details.get("retry_after_seconds")
    if isinstance(retry_after, int) and 0 <= retry_after <= 86_400:
        projected["retry_after_seconds"] = retry_after
    return projected


def _sync_failure_retryable(error: object) -> bool:
    """Allow only transient Provider failures into durable retry state.

    Authentication, scope, revocation and malformed-response failures remain
    terminal so a worker cannot hammer a disconnected mailbox forever.
    """

    if isinstance(error, RateLimitedError):
        return True
    if not isinstance(error, ProviderFailureError):
        return False
    if error.message == "provider_http_unavailable":
        return True
    status = error.details.get("provider_status")
    return isinstance(status, int) and 500 <= status <= 599


def _sync_retry_delay(error: object, attempt_count: int) -> int:
    """Return a bounded durable delay, honoring a safe Retry-After hint."""

    if not 1 <= attempt_count <= _SYNC_RETRY_MAX_ATTEMPTS:
        raise ValueError("sync_retry_attempt_invalid")
    retry_after = (
        error.details.get("retry_after_seconds") if isinstance(error, MailHubError) else None
    )
    if isinstance(retry_after, int) and 0 <= retry_after <= 86_400:
        return int(max(1, min(retry_after, _SYNC_RETRY_MAX_SECONDS)))
    return int(
        min(
            _SYNC_RETRY_MAX_SECONDS,
            _SYNC_RETRY_BASE_SECONDS * (2 ** (attempt_count - 1)),
        )
    )


def _string_values(value: object) -> tuple[str, ...]:
    if isinstance(value, (tuple, list, set, frozenset)):
        return tuple(str(item) for item in value)
    return ()


def _confidence(value: object, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return fallback


def _optional_addresses_for_draft(addresses: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not addresses:
        return ()
    try:
        return ensure_addresses(addresses)
    except ValueError as exc:
        raise ValueError(f"{field_name}_invalid") from exc


def _ensure_disjoint_recipients(
    recipient_addresses: tuple[str, ...],
    cc_addresses: tuple[str, ...],
    bcc_addresses: tuple[str, ...],
) -> None:
    all_addresses = (*recipient_addresses, *cc_addresses, *bcc_addresses)
    if len(set(all_addresses)) != len(all_addresses):
        raise ValueError("draft_recipients_duplicate")
    if len(all_addresses) > 50:
        raise ValueError("draft_recipients_too_many")


def _normalize_attachment_refs(attachment_refs: tuple[str, ...]) -> tuple[str, ...]:
    if len(attachment_refs) > 20:
        raise ValueError("draft_attachments_too_many")
    normalized: list[str] = []
    for value in attachment_refs:
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            raise ValueError("draft_attachment_ref_invalid")
        normalized.append(value.strip())
    if len(set(normalized)) != len(normalized):
        raise ValueError("draft_attachments_duplicate")
    return tuple(normalized)


def _normalize_impact_folder_refs(folder_refs: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize a bounded folder set for a projection impact preview."""

    if not folder_refs or len(folder_refs) > 50:
        raise ValueError("impact_preview_folder_refs_invalid")
    normalized: list[str] = []
    for folder_ref in folder_refs:
        if not isinstance(folder_ref, str):
            raise ValueError("impact_preview_folder_ref_invalid")
        value = folder_ref.strip()
        if (
            not value
            or len(value) > 200
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
        ):
            raise ValueError("impact_preview_folder_ref_invalid")
        if value not in normalized:
            normalized.append(value)
    return tuple(sorted(normalized, key=lambda item: (item.casefold(), item)))


def _recipient_risk_flags(
    sender_address: str, recipient_addresses: tuple[str, ...]
) -> tuple[bool, bool]:
    """Return external-domain and large-recipient-set risk flags.

    Provider directory/group expansion is intentionally not guessed from an
    address string.  A host that has authoritative directory metadata can set
    ``AgentActionContext.has_group_recipient`` before policy evaluation.
    """

    sender_domain = sender_address.rsplit("@", 1)[-1].casefold()
    recipient_domains = {address.rsplit("@", 1)[-1].casefold() for address in recipient_addresses}
    return bool(recipient_domains - {sender_domain}), len(recipient_addresses) > 5


_HIGH_RISK_TERM_RE = re.compile(
    r"\b(?:contract|agreement|quotation|quote|invoice|payment|wire|bank|password|secret|"
    r"security\s+code|mfa|legal|liability|penalty|amount|usd|cny|eur)\b"
    r"|合同|协议|报价|发票|付款|银行|密码|验证码|法律|赔偿|违约|金额",
    re.IGNORECASE,
)


def _contains_high_risk_terms(*values: str | None) -> bool:
    """Detect bounded high-impact terms before an outbound operation.

    This is a conservative approval signal, not a classifier.  It never
    stores or emits the matched text; uncertain matches intentionally require
    explicit confirmation rather than enabling autonomous send.
    """

    return any(value is not None and bool(_HIGH_RISK_TERM_RE.search(value)) for value in values)


def _draft_send_idempotency_key(draft: MailDraft, *, revision: int) -> str:
    recipient_digest = digest_recipient_headers(
        draft.recipient_addresses, draft.cc_addresses, draft.bcc_addresses
    )
    envelope_digest = digest_text(f"{recipient_digest}:{','.join(draft.attachment_refs)}")
    return (
        f"mail-send:{draft.connection_id}:{draft.draft_id}:{revision}:"
        f"{envelope_digest}:{draft.content_sha256}"
    )


def _retry_delay(*, operation_id: UUID, attempt_count: int) -> int:
    """Bounded exponential backoff with stable per-operation jitter."""

    base: int = 30 * (2**attempt_count)
    if base > 840:
        base = 840
    jitter: int = int(digest_text(str(operation_id))[:4], 16) % 60
    total: int = base + jitter
    return total if total <= 900 else 900


def _validate_credential_refresh_metadata(value: Mapping[str, str]) -> dict[str, str]:
    """Validate the non-secret metadata returned by a Host refresh call."""

    if not isinstance(value, Mapping) or not value:
        raise AuthorizationError("credential_refresh_metadata_invalid")
    allowed = {
        "credential_ref",
        "email_address",
        "provider_account_id",
        "provider_tenant_id",
        "credential_version",
    }
    secret_keys = {
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "token",
    }
    result: dict[str, str] = {}
    for key, raw_value in value.items():
        if not isinstance(key, str):
            raise AuthorizationError("credential_refresh_metadata_invalid")
        normalized_key = key.casefold().replace("-", "_")
        if normalized_key in secret_keys:
            raise AuthorizationError("credential_refresh_secret_leak")
        if key not in allowed or not isinstance(raw_value, str):
            raise AuthorizationError("credential_refresh_metadata_invalid")
        if any(ord(char) < 32 or ord(char) == 127 for char in raw_value):
            raise AuthorizationError("credential_refresh_metadata_invalid")
        result[key] = raw_value
    if "credential_ref" in result and not 1 <= len(result["credential_ref"]) <= 512:
        raise AuthorizationError("credential_refresh_credential_ref_invalid")
    if "email_address" in result and not 3 <= len(result["email_address"]) <= 320:
        raise AuthorizationError("credential_refresh_email_invalid")
    for key in ("provider_account_id", "provider_tenant_id"):
        if key in result and not 1 <= len(result[key]) <= 512:
            raise AuthorizationError("credential_refresh_metadata_invalid")
    if "credential_version" in result:
        _credential_version_from_metadata(result["credential_version"])
    return result


def _validate_credential_revocation_metadata(value: Mapping[str, object]) -> dict[str, str]:
    """Validate the bounded, non-secret proof returned by a Host revoke call."""

    if not isinstance(value, Mapping) or not value:
        raise ProviderFailureError("credential_revoke_response_invalid")
    allowed = {"status", "provider_request_id", "revocation_id", "revoke_ref"}
    secret_keys = {
        "access_token",
        "authorization",
        "client_secret",
        "credential_ref",
        "id_token",
        "refresh_token",
        "token",
    }
    for key in value:
        if not isinstance(key, str):
            raise ProviderFailureError("credential_revoke_response_invalid")
        normalized = key.casefold().replace("-", "_")
        if normalized in secret_keys:
            raise ProviderFailureError("credential_revoke_secret_leak")
        if key not in allowed:
            raise ProviderFailureError("credential_revoke_response_invalid")
    raw_status = value.get("status")
    if not isinstance(raw_status, str) or not 1 <= len(raw_status) <= 80:
        raise ProviderFailureError("credential_revoke_response_invalid")
    status = raw_status.strip().casefold()
    if status not in {"revoked", "already_revoked", "accepted", "revoke_requested"}:
        raise ProviderFailureError("credential_revoke_response_invalid")
    for key in allowed - {"status"}:
        raw_value = value.get(key)
        if raw_value is None:
            continue
        if not isinstance(raw_value, str) or not 1 <= len(raw_value) <= 512:
            raise ProviderFailureError("credential_revoke_response_invalid")
        if any(ord(char) < 33 or ord(char) == 127 for char in raw_value):
            raise ProviderFailureError("credential_revoke_response_invalid")
    # Only the status is retained.  Provider/host request references remain
    # available in the host's own audit ledger and never cross into MailHub.
    return {"status": status}


def _credential_version_from_metadata(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdigit():
        raise AuthorizationError("credential_refresh_credential_version_invalid")
    version = int(value)
    if not 1 <= version <= 2_147_483_647:
        raise AuthorizationError("credential_refresh_credential_version_invalid")
    return version


def _validate_provider_scope_input(
    provider: ProviderName,
    scopes: tuple[str, ...],
    *,
    require_read_only: bool,
) -> None:
    """Keep direct connection/activation paths aligned with OAuth scope gates.

    OAuth callback validation is the primary path, but host/admin callers can
    also create or reauthorize a connection through the service contract.  A
    direct path must never persist a Gmail/Graph write scope or activate a
    connection without the provider's required read-only grant.
    """

    if provider not in {ProviderName.GMAIL, ProviderName.MICROSOFT_GRAPH}:
        return
    if require_read_only and not scopes:
        raise ValueError("provider_read_only_scopes_invalid")
    try:
        oauth_provider = OAuthProvider(provider.value)
        validate_granted_scopes(
            oauth_provider,
            scopes,
            scopes if require_read_only else (),
        )
    except ValueError as exc:
        raise ValueError("provider_read_only_scopes_invalid") from exc


def _validate_credential_binding(
    connection: MailboxConnection, credential: Mapping[str, str]
) -> None:
    """Reject a broker response that is older or bound to another account.

    The host may omit these optional metadata keys for legacy connections, but
    when present they are checked before any Provider I/O.  Tokens themselves
    remain opaque and are never inspected or logged.
    """

    version_value = credential.get("credential_version")
    if version_value is not None:
        if not version_value.isdigit():
            raise AuthorizationError("credential_version_invalid")
        version = int(version_value)
        if version < connection.credential_version:
            raise AuthorizationError("credential_version_stale")
    for field_name, error_code in (
        ("provider_account_id", "provider_account_identity_mismatch"),
        ("provider_tenant_id", "provider_tenant_identity_mismatch"),
    ):
        expected = getattr(connection, field_name)
        actual = credential.get(field_name)
        if expected is not None and actual is not None and actual != expected:
            raise AuthorizationError(error_code)
