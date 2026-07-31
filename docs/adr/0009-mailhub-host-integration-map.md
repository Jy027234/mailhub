# ADR 0009: CAPlatform host integration map

- Status: accepted as the first-host boundary
- Date: 2026-07-28
- Owners: CAPlatform integration owner + MailHub owner

## Request path

```text
CAPlatform React page
  -> CAPlatform BFF mailhub_adapter (host identity, trace, idempotency)
  -> MailHub v1 API / Agentctl JSON handler
  -> MailHub application service
  -> Host Port adapter
  -> authoritative Platform Core / AIProjectOPS / Knowledge / Secret systems
```

The browser never chooses a tenant or subject authority. The BFF supplies a
verified host context and propagates trace and idempotency metadata. MailHub still
rechecks the context and policy before every read or side effect.

| User action | MailHub contract | Host Port | Authority |
| --- | --- | --- | --- |
| connect/revoke mailbox | connection/OAuth endpoints | Identity + Credential Broker | Platform Core / provider |
| inbox/thread/message | projection/detail endpoints | Identity + Object Store | provider + MailHub projection |
| analyze/summary | `ca.mail.message.analyze` | AI Execution + Audit | Agentctl/model gateway |
| project candidate | candidate review/apply | HostAction | AIProjectOPS |
| knowledge candidate | candidate review/apply | KnowledgeSink + ObjectStore | Knowledge/EDM |
| draft/send | draft revision + approved operation | Approval + Credential Broker | provider + Host Approval |
| health/reconcile | jobs/receipts/outbox | Audit/Notification | MailHub operations |

The `apps/bff/src/caplatform_bff/mailhub_adapter.py` file is intentionally a
thin transport adapter. It must not import Gmail/Graph/IMAP SDKs, persist
credentials, decide project facts or publish knowledge. Other hosts can replace
the Port implementations and keep the same MailHub API/SDK.

## Side-effect rule

Propose/review endpoints are side-effect free. Apply/send endpoints require an
approved, revision-bound action and stable idempotency key. MailHub emits a
result/reference; the host authority owns the final task, knowledge or business
state. A missing host port, approval or governance capability fails closed.

## Evidence and rollout

The integration package must provide contract tests for identity mismatch,
approval mismatch, object scope and host action failure. CAPlatform rollout uses
read-only -> proposal -> approved write -> controlled send feature flags. No
legacy sender is disabled until shadow and rollback evidence is signed.
