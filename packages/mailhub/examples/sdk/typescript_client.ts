import { MailHubClient } from "@mailhub/client";

const client = new MailHubClient({
  baseUrl: process.env.MAILHUB_URL ?? "http://127.0.0.1:8000",
  tenantId: process.env.MAILHUB_TENANT_ID ?? "demo-tenant",
  subjectId: process.env.MAILHUB_SUBJECT_ID ?? "demo-user",
});

const page = await client.listThreadPage({ limit: 50, unread: true });
console.log({ threads: page.data.length, complete: page.has_more === false });

// Queue only the durable recommend-only run.  Sending and host actions still
// require the server-side policy, approval and idempotency contracts.
const connectionId = process.env.MAILHUB_CONNECTION_ID;
if (connectionId) {
  await client.enqueueAutonomy(
    { connectionId, replayKey: `demo-replay:${connectionId}`, limit: 50, messageLimit: 50 },
    `demo-autonomy:${crypto.randomUUID()}`,
  );
}
