"""Bounded, proposal-only Agent mail orchestration.

This coordinator is deliberately narrower than a general-purpose Agent loop:
it runs one scoped synchronization, analyzes a bounded set of resulting
messages, and leaves every candidate in the existing review queue.  It never
creates a draft, sends a message, applies a project/knowledge action, or
accepts instructions from message content as authority.  A deployment worker
owns scheduling and can replay the same call safely because message and
candidate identity are deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from mailhub.domain import AutonomyRunStatus, MailAutonomyRun
from mailhub.errors import PolicyDeniedError
from mailhub.service import MailService


@dataclass(frozen=True, slots=True)
class MailAutonomyRunResult:
    run_id: UUID
    tenant_id: str
    subject_id: str
    connection_id: UUID
    mode: Literal["recommend_only"]
    sync_result: Mapping[str, object]
    message_ids: tuple[UUID, ...]
    analyzed_message_ids: tuple[UUID, ...]
    candidate_ids: tuple[UUID, ...]
    status: str = "completed"


class MailAutonomyCoordinator:
    """Run one owner-scoped L0/L1 recommendation cycle.

    The method is intended for an external durable worker, not an API-process
    timer.  Provider I/O remains inside ``MailService.sync_connection`` and
    all host/project/knowledge side effects remain behind their existing
    approval/review ports.
    """

    def __init__(self, service: MailService) -> None:
        self.service = service

    async def run_one(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        limit: int = 50,
        message_limit: int = 50,
        replay_key: str = "default",
    ) -> MailAutonomyRunResult:
        run = await self.service.enqueue_autonomy_run(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            limit=limit,
            message_limit=message_limit,
            replay_key=replay_key,
        )
        if run.status is AutonomyRunStatus.QUEUED:
            run = await self.service.run_autonomy_job(
                tenant_id=tenant_id,
                subject_id=subject_id,
                run_id=run.run_id,
                worker_id=f"inline-autonomy:{subject_id}",
            )
        return _result_from_run(run)

    async def run_claimed(self, run: MailAutonomyRun) -> Mapping[str, object]:
        """Execute a worker-claimed run and return only governed references."""

        sync_result = await self.service.sync_connection(
            tenant_id=run.tenant_id,
            subject_id=run.subject_id,
            connection_id=run.connection_id,
            limit=run.requested_limit,
        )
        messages = await self.service.repository.list_messages_for_connection(
            tenant_id=run.tenant_id,
            subject_id=run.subject_id,
            connection_id=run.connection_id,
            limit=run.message_limit,
        )
        analyzed: list[UUID] = []
        for message in messages:
            result = await self.service.analyze_message(
                tenant_id=run.tenant_id,
                subject_id=run.subject_id,
                message_id=message.message_id,
            )
            analyzed.append(message.message_id)
            if result.injection_detected:
                # Message content is data, never authority.  Stop the
                # autonomous cycle at the first detected injection so a
                # future policy change cannot turn the remaining batch into
                # an unreviewed action path.
                raise PolicyDeniedError("prompt_injection_detected")
        candidates = await self.service.list_candidates(
            tenant_id=run.tenant_id,
            subject_id=run.subject_id,
            status=None,
            limit=200,
        )
        message_id_set = {message.message_id for message in messages}
        return {
            "sync_result": dict(sync_result),
            "message_ids": tuple(message.message_id for message in messages),
            "analyzed_message_ids": tuple(analyzed),
            "candidate_ids": tuple(
                candidate.candidate_id
                for candidate in candidates
                if candidate.message_id in message_id_set
            ),
        }


def _result_from_run(run: MailAutonomyRun) -> MailAutonomyRunResult:
    return MailAutonomyRunResult(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        subject_id=run.subject_id,
        connection_id=run.connection_id,
        mode="recommend_only",
        sync_result=dict(run.sync_result),
        message_ids=run.message_ids,
        analyzed_message_ids=run.analyzed_message_ids,
        candidate_ids=run.candidate_ids,
        status=run.status.value,
    )
