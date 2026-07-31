"""Durable worker entry points.

Scheduling, process supervision and retry wake-ups belong to deployment
infrastructure.  These methods operate one bounded unit and return the
persisted result, so a crash can be replayed by an external worker without an
in-memory queue or API-process timer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from mailhub.autonomy import MailAutonomyCoordinator, MailAutonomyRunResult
from mailhub.domain import (
    DeliveryStatus,
    MailAutonomyRun,
    MailboxSyncState,
    MailOutboxOperation,
    MailSyncJob,
)
from mailhub.intelligence import IntelligenceResult
from mailhub.rules import RuleExecution
from mailhub.service import MailService


class MailWorker:
    def __init__(self, service: MailService, *, worker_id: str) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id_required")
        self.service = service
        self.worker_id = worker_id

    async def send_one(self, *, tenant_id: str, operation_id: UUID) -> MailOutboxOperation:
        return await self.service.send_queued(
            tenant_id=tenant_id,
            operation_id=operation_id,
            worker_id=self.worker_id,
        )

    async def analyze_one(
        self, *, tenant_id: str, subject_id: str, message_id: UUID
    ) -> IntelligenceResult:
        return await self.service.analyze_message(
            tenant_id=tenant_id,
            subject_id=subject_id,
            message_id=message_id,
        )

    async def autonomy_one(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        limit: int = 50,
        message_limit: int = 50,
        replay_key: str = "default",
    ) -> MailAutonomyRunResult:
        """Run one proposal-only autonomous mail cycle.

        This is a worker unit.  It may read/summarize and persist reviewable
        candidates, but cannot send, apply candidates, or write host facts.
        """

        return await MailAutonomyCoordinator(self.service).run_one(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            limit=limit,
            message_limit=message_limit,
            replay_key=replay_key,
        )

    async def autonomy_job_one(
        self, *, tenant_id: str, subject_id: str, run_id: UUID
    ) -> MailAutonomyRun:
        """Claim and execute one persisted autonomy run."""

        return await self.service.run_autonomy_job(
            tenant_id=tenant_id,
            subject_id=subject_id,
            run_id=run_id,
            worker_id=self.worker_id,
        )

    async def sync_one(
        self, *, tenant_id: str, subject_id: str, connection_id: UUID, limit: int = 50
    ) -> dict[str, object]:
        return await self.service.sync_connection(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            limit=limit,
        )

    async def sync_job_one(self, *, tenant_id: str, subject_id: str, job_id: UUID) -> MailSyncJob:
        """Claim and execute one durable sync job from an external scheduler."""

        return await self.service.run_sync_job(
            tenant_id=tenant_id,
            subject_id=subject_id,
            job_id=job_id,
            worker_id=self.worker_id,
        )

    async def ensure_subscription_one(
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
        """Create/renew one host-owned provider subscription."""

        return await self.service.ensure_provider_subscription(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            callback_endpoint=callback_endpoint,
            desired_expiry=desired_expiry,
            folder_ref=folder_ref,
            client_state_ref=client_state_ref,
            idempotency_key=idempotency_key,
        )

    async def cancel_subscription_one(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        request_id: str,
        folder_ref: str = "INBOX",
    ) -> MailboxSyncState | None:
        """Cancel one subscription during revoke/delete cleanup."""

        return await self.service.cancel_provider_subscription(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            request_id=request_id,
            folder_ref=folder_ref,
        )

    async def renew_subscription_one(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        folder_ref: str = "INBOX",
        renewal_window: timedelta = timedelta(hours=24),
        desired_expiry: datetime | None = None,
    ) -> MailboxSyncState | None:
        """Run one bounded expiry/renewal scheduler unit."""

        return await self.service.renew_provider_subscription_if_due(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            folder_ref=folder_ref,
            renewal_window=renewal_window,
            desired_expiry=desired_expiry,
        )

    async def mark_subscription_expired_one(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        folder_ref: str = "INBOX",
        now: datetime | None = None,
    ) -> MailboxSyncState | None:
        return await self.service.mark_provider_subscription_expired(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            folder_ref=folder_ref,
            now=now,
        )

    async def mark_subscription_failed_one(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        folder_ref: str = "INBOX",
        error_code: str = "subscription_renewal_failed",
        now: datetime | None = None,
    ) -> MailboxSyncState | None:
        return await self.service.mark_provider_subscription_failed(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            folder_ref=folder_ref,
            error_code=error_code,
            now=now,
        )

    async def execute_rule_one(
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
        """Run one bounded rule batch under an external worker lease.

        The service persists deterministic execution rows before HostAction
        I/O.  Deployment schedulers can therefore call this entry point
        instead of embedding rule execution in an API-process timer.
        """

        return await self.service.execute_rule(
            tenant_id=tenant_id,
            subject_id=subject_id,
            rule_id=rule_id,
            message_ids=message_ids,
            policy_id=policy_id,
            grant_id=grant_id,
            dry_run=dry_run,
        )

    async def run_send_batch(
        self, *, tenant_id: str, operation_ids: Sequence[UUID]
    ) -> tuple[MailOutboxOperation, ...]:
        results: list[MailOutboxOperation] = []
        for operation_id in operation_ids:
            results.append(await self.send_one(tenant_id=tenant_id, operation_id=operation_id))
        return tuple(results)

    async def purge_expired_objects(self, *, limit: int = 500) -> tuple[Mapping[str, object], ...]:
        """Run a bounded TTL cleanup when the configured store supports it."""

        store = self.service.object_store
        purge = getattr(store, "purge_expired", None)
        if store is None or purge is None:
            return ()
        result = cast(tuple[Mapping[str, object], ...], await purge(limit=limit))
        for evidence in result:
            await self.service.repository.append_audit(
                {
                    "event_type": "mail.object.deleted",
                    "tenant_id": str(evidence.get("tenant_id", "unknown")),
                    "subject_id": str(evidence.get("subject_id", "unknown")),
                    "target_ref": str(evidence.get("object_ref", "unknown")),
                    "reason": evidence.get("reason", "ttl_expired"),
                    "content_sha256": evidence.get("content_sha256"),
                }
            )
        return result

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
    ) -> MailOutboxOperation:
        return await self.service.reconcile_outcome_unknown(
            tenant_id=tenant_id,
            subject_id=subject_id,
            operation_id=operation_id,
            status=status,
            provider_message_ref=provider_message_ref,
            provider_request_id=provider_request_id,
            error_code=error_code,
        )
