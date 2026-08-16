"""Minimal local outbox worker for the B4 durable graph.

MailHub deliberately has no in-core scheduler: worker units are host-owned.
This loop builds the same durable graph as the API process, shares the
PostgreSQL database, and claims/sends QUEUED outbox operations with the
durable lease/fencing contract.  It is a controlled local worker, not a
production scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "packages" / "mailhub" / "src")
)

from mailhub.domain import DeliveryStatus  # noqa: E402
from mailhub.runtime import create_durable_app  # noqa: E402
from mailhub.worker import MailWorker  # noqa: E402

logger = logging.getLogger("local-worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _scopes() -> list[tuple[str, str]]:
    raw = os.getenv("WORKER_SCOPES", "b4-tenant|b4-user")
    scopes: list[tuple[str, str]] = []
    for item in raw.split(","):
        tenant, separator, subject = item.strip().partition("|")
        if separator and tenant and subject:
            scopes.append((tenant, subject))
    return scopes


async def _run_once(
    worker: MailWorker, *, tenant_id: str, subject_id: str
) -> tuple[int, int]:
    sent = failed = 0
    repository = worker.service.repository
    operations = await repository.list_operations(
        tenant_id=tenant_id, subject_id=subject_id, limit=100
    )
    for operation in operations:
        if operation.status is not DeliveryStatus.QUEUED:
            continue
        try:
            result = await worker.send_one(
                tenant_id=tenant_id, operation_id=operation.operation_id
            )
            if result.status is DeliveryStatus.SUCCEEDED:
                sent += 1
                logger.info("outbox sent operation=%s", result.operation_id)
            else:
                failed += 1
                logger.warning(
                    "outbox terminal operation=%s status=%s error=%s",
                    result.operation_id,
                    result.status.value,
                    result.error_code,
                )
        except Exception:  # noqa: BLE001 - a worker loop must survive single failures
            failed += 1
            logger.exception(
                "outbox attempt failed operation=%s", operation.operation_id
            )
    return sent, failed


async def main() -> None:
    app = create_durable_app()
    service = app.state.mail_service
    worker = MailWorker(service, worker_id="local-b4-worker")
    logger.info("local worker started, scopes=%s", _scopes())
    try:
        while True:
            for tenant_id, subject_id in _scopes():
                sent, failed = await _run_once(
                    worker, tenant_id=tenant_id, subject_id=subject_id
                )
                if sent or failed:
                    logger.info(
                        "scope=%s/%s sent=%d failed=%d",
                        tenant_id,
                        subject_id,
                        sent,
                        failed,
                    )
            await asyncio.sleep(5)
    finally:
        await app.state.mail_repository.dispose()


if __name__ == "__main__":
    asyncio.run(main())
