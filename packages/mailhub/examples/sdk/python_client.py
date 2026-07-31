"""Minimal host-side Python SDK example.

The example deliberately receives tenant/subject context from the host and
never accepts a Provider token.  It only queues durable work; a worker and the
host approval flow decide whether any later side effect is allowed.
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

# Run from a host that has the SDK directory on its import path, or vendor the
# reviewed client into the host service.  The MailHub wheel itself does not
# silently package a second credential-aware client namespace.
from mailhub_client import MailHubApiError, MailHubClient


async def main() -> None:
    client = MailHubClient(
        base_url=os.environ["MAILHUB_URL"],
        tenant_id=os.environ["MAILHUB_TENANT_ID"],
        subject_id=os.environ["MAILHUB_SUBJECT_ID"],
    )
    connections = await client.list_connections()
    connection_id = UUID(os.environ["MAILHUB_CONNECTION_ID"])
    page = await client.list_thread_page(limit=50, unread=True)
    print({"connections": len(connections.get("data", [])), "threads": len(page.get("data", []))})

    # Stable keys belong to the host command/request, not to the Provider.
    await client.enqueue_autonomy(
        connection_id,
        replay_key=f"demo-replay:{connection_id}",
        idempotency_key=f"demo-autonomy:{uuid4()}",
        limit=50,
        message_limit=50,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyError, ValueError, MailHubApiError) as exc:
        raise SystemExit(f"MailHub example failed: {exc}") from exc
