"""Fail-closed durable runtime assembly for deployed MailHub instances."""

from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI

from mailhub.api import _build_default_oauth, _host_service_headers, create_app
from mailhub.config import MailHubSettings
from mailhub.connectors.http_providers import GmailConnector, MicrosoftGraphConnector
from mailhub.connectors.imap_smtp import ImapSmtpConnector
from mailhub.domain import ProviderName
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
    HttpObjectStoreAdapter,
    HttpProviderNotificationVerifierAdapter,
    HttpProviderSubscriptionAdapter,
    HttpQuotaAdapter,
    HttpTelemetryAdapter,
)
from mailhub.persistence.sqlalchemy import SqlAlchemyMailRepository
from mailhub.ports import ProviderConnector
from mailhub.service import MailService

_PRODUCTION_LIKE = {"production", "prod", "staging"}


def create_durable_app(runtime_settings: MailHubSettings | None = None) -> FastAPI:
    """Assemble the PostgreSQL and authenticated Host-Port deployment graph.

    This factory is deliberately separate from the development graph. It never
    registers Sandbox, never falls back to in-memory state, and validates every
    production dependency before constructing a provider connector.
    """

    settings = runtime_settings or MailHubSettings.from_env()
    if settings.environment.casefold() not in _PRODUCTION_LIKE:
        raise RuntimeError("mailhub_durable_runtime_requires_production_like_environment")
    settings.require_ready()
    if settings.database_url is None:
        raise RuntimeError("mailhub_database_url_required")
    if settings.credential_broker_endpoint is None:
        raise RuntimeError("mailhub_credential_broker_endpoint_required")
    if settings.host_identity_endpoint is None:
        raise RuntimeError("mailhub_host_identity_endpoint_required")
    if settings.object_store_endpoint is None:
        raise RuntimeError("mailhub_object_store_endpoint_required")
    if settings.approval_endpoint is None:
        raise RuntimeError("mailhub_approval_endpoint_required")
    if settings.host_action_endpoint is None:
        raise RuntimeError("mailhub_host_action_endpoint_required")
    if settings.knowledge_endpoint is None:
        raise RuntimeError("mailhub_knowledge_endpoint_required")
    if settings.ai_execution_endpoint is None:
        raise RuntimeError("mailhub_ai_execution_endpoint_required")
    if settings.telemetry_endpoint is None:
        raise RuntimeError("mailhub_telemetry_endpoint_required")
    if settings.event_publisher_endpoint is None:
        raise RuntimeError("mailhub_event_publisher_endpoint_required")
    if settings.quota_endpoint is None:
        raise RuntimeError("mailhub_quota_endpoint_required")

    headers = _host_service_headers(settings)
    if not headers:
        raise RuntimeError("mailhub_host_service_token_required")
    repository = SqlAlchemyMailRepository.from_url(
        settings.database_url,
        pool_pre_ping=True,
    )
    connectors: dict[ProviderName, ProviderConnector] = {}
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
    if settings.imap_enabled:
        if not settings.imap_host or not settings.smtp_host:
            raise RuntimeError("mailhub_imap_hosts_required")
        connectors[ProviderName.IMAP_SMTP] = ImapSmtpConnector(
            imap_host=settings.imap_host,
            smtp_host=settings.smtp_host,
            imap_port=settings.imap_port,
            smtp_port=settings.smtp_port,
            folder=settings.imap_folder,
            send_enabled=settings.outbound_enabled and settings.smtp_send_enabled,
            max_send_bytes=settings.smtp_max_send_bytes,
        )

    oauth_service, oauth_callback = _build_default_oauth(settings)
    if (settings.gmail_enabled or settings.microsoft_graph_enabled) and (
        oauth_service is None or oauth_callback is None
    ):
        raise RuntimeError("mailhub_oauth_host_boundary_not_ready")

    credential_broker = HttpCredentialBrokerAdapter(
        base_url=settings.credential_broker_endpoint,
        headers=headers,
    )
    provider_subscription = (
        HttpProviderSubscriptionAdapter(
            base_url=settings.provider_subscription_endpoint,
            headers=headers,
        )
        if settings.provider_subscription_endpoint
        else None
    )
    notification_verifier = (
        HttpProviderNotificationVerifierAdapter(
            base_url=settings.provider_notification_verifier_endpoint,
            headers=headers,
        )
        if settings.provider_notification_verifier_endpoint
        else None
    )
    service = MailService(
        repository,
        connectors=connectors,
        credential_broker=credential_broker,
        credential_refresh=credential_broker,
        credential_revocation=credential_broker,
        approval_port=HttpApprovalAdapter(
            base_url=settings.approval_endpoint,
            headers=headers,
        ),
        host_action_port=HttpHostActionAdapter(
            base_url=settings.host_action_endpoint,
            headers=headers,
        ),
        host_identity=HttpHostIdentityAdapter(
            base_url=settings.host_identity_endpoint,
            headers=headers,
        ),
        knowledge_sink=HttpKnowledgeSink(
            base_url=settings.knowledge_endpoint,
            headers=headers,
        ),
        knowledge_lifecycle=HttpKnowledgeLifecycleAdapter(
            base_url=settings.knowledge_endpoint,
            headers=headers,
        ),
        agent_memory=(
            HttpAgentMemoryAdapter(base_url=settings.agent_memory_endpoint, headers=headers)
            if settings.agent_memory_endpoint
            else None
        ),
        knowledge_safety=HttpKnowledgeSafetyAdapter(
            base_url=settings.knowledge_endpoint,
            headers=headers,
        ),
        kill_switch=(
            HttpKillSwitchAdapter(base_url=settings.kill_switch_endpoint, headers=headers)
            if settings.kill_switch_endpoint
            else None
        ),
        object_store=HttpObjectStoreAdapter(
            base_url=settings.object_store_endpoint,
            headers=headers,
        ),
        ai_execution=HttpAiExecutionAdapter(
            base_url=settings.ai_execution_endpoint,
            headers=headers,
        ),
        telemetry=HttpTelemetryAdapter(
            base_url=settings.telemetry_endpoint,
            headers=headers,
        ),
        event_publisher=HttpEventPublisherAdapter(
            base_url=settings.event_publisher_endpoint,
            headers=headers,
        ),
        quota_port=HttpQuotaAdapter(
            base_url=settings.quota_endpoint,
            headers=headers,
        ),
        provider_subscription_port=provider_subscription,
        worker_lease_ttl=timedelta(seconds=settings.job_lease_seconds),
        intelligence_policy=settings.analysis_policy(),
        outbound_enabled=settings.outbound_enabled,
        rule_automation_enabled=settings.rule_automation_enabled,
    )
    app = create_app(
        service,
        runtime_settings=settings,
        oauth_service=oauth_service,
        oauth_callback_port=oauth_callback,
        provider_notification_verifier=notification_verifier,
    )
    app.state.mail_repository = repository
    app.router.add_event_handler("shutdown", repository.dispose)
    return app


__all__ = ["create_durable_app"]
