# MailHub v1 compatibility matrix

Owner: MailHub API/SDK maintainers. Review on every release or public schema
change. “Sandbox” is contract evidence only; “external” requires host/provider
evidence before production claims.

| Surface | Current version | Compatibility promise | Evidence / owner |
| --- | --- | --- | --- |
| HTTP API | OpenAPI `3.1`, MailHub `0.1.0`, contract `v1` | additive fields only within v1; breaking change -> v2 | generated `schemas/mailhub.openapi.v1.json`, API tests / API owner |
| Unified inbox page | `MailThreadSummary` + opaque scope-bound cursor | cursor is valid only for the same tenant/subject/account/filter scope; `attachment` and `has_attachment` are equivalent aliases | service/page tests, BFF/SDK/UI contracts / core owner |
| JSON contract envelope | `mailhub.contract.v1` | stable error/idempotency/outcome-unknown semantics | schema + service/API tests / core owner |
| Events | `mailhub.event_envelope.v1`; message/action/provider v1 | at-least-once, dedupe by event/idempotency identity | event schemas + `test_events.py` / integration owner |
| Business event registry | `mailhub.business_event_types.v1` + `MailHubEventType` | unknown event names fail closed; lifecycle producers use stable event IDs; host event publisher receives validated metadata-only envelopes | registry/runtime tests + HTTP EventPublisher contract / integration owner |
| Search | `metadata` / `provider` mode | metadata response declares coverage/completeness; unverified provider mode returns degraded, never an incomplete full-text success | API/BFF/Agentctl/SDK tests / core owner |
| Message recipient headers | additive `cc_addresses`, `bcc_addresses`, `reply_to_addresses` | normalized address-only projection; raw headers/display names are discarded; Bcc absence is not evidence of no Bcc | connector/conformance tests, migration `0020`; real provider visibility external |
| Python SDK | source client (pre-dist) | sends host scope and idempotency; no provider token | SDK source + root regression / SDK owner |
| TypeScript SDK | strict source client with `tsconfig.json`/`build` contract | same v1 request/response/error boundary; declaration/source-map output is generated per release | SDK source/build contract; dist/N-1 publication remains release evidence / SDK owner |
| Draft recipients | To/CC/BCC + governed attachment refs | additive optional fields; `recipient_digest` and send idempotency bind all classes | OpenAPI, migration `0005`, service/provider tests / outbox owner |
| Privacy lifecycle | connection `:delete` + bounded `data-export` | deletion is resumable and tombstoned; export omits credentials/object refs unless explicitly hydrated by scope | OpenAPI, lifecycle tests / security and host owner |
| Rule execution evidence | L3A low-risk action proposals/executions | additive `mail_rule_executions`; stable execution/action id and replay suppression | OpenAPI, migration `0006`, host policy/worker owner |
| Rule automation level | `L2_REVIEW_QUEUE` default, explicit `L3A_BOUNDED_ORGANIZE` opt-in | creating an L3A rule never bypasses the independent kill switch, policy/grant intersection or host action gate | OpenAPI, rule execution tests / control owner |
| Provider reconciliation | Gmail history reset backfill; Graph `/delta` pagination and continuation cursor | reset or continuation is explicit and bounded; provider cursor is never silently advanced past an incomplete page | connector tests / provider owner; real mailbox evidence still external |
| Database migrations | `scripts/apply_migrations.py` with checksum ledger and advisory lock; current chain 0001–0021 | one migration per transaction; rollback requires an explicit target; checksum drift and ledger gaps fail closed; 0007 adds sender leases, 0008 durable autonomy runs, 0009 outbox lease expiry, 0010 sync/autonomy worker lease expiry, 0011 bounded message read/attachment metadata, 0012 transactional quota leases, 0013 durable subscription lease/status/expiry/watermark state, 0014 bounded provider metadata projection, 0015 body-free webhook route metadata for controlled replay, 0016 non-secret provider account/tenant identity plus credential version, 0017 durable folder/label/date bounds for backfill jobs, 0018 durable provider deletion counts on sync jobs, 0019 durable running-job cancellation fencing, 0020 normalized inbound Cc/Bcc/Reply-To projections, and 0021 bounded durable sync retry state | static runner tests/runbook; live PostgreSQL/N-N-1 evidence external |
| Send risk flags | external domain, BCC, group metadata, large set, attachment, new participant | risk flags can only raise confirmation/deny; missing host/provider capability fails closed | domain/policy/service tests; group metadata and real security matrix open |
| Host Ports | typed Protocols + generic HTTP adapters | host may replace adapters; fail closed on missing/invalid result | `host-adapter-sdk.md`, transport tests / host owner |
| Migration sender interlock | `SenderInterlockPort` + HTTP acquire/release adapter; metadata-only audit events | one tenant/account sender lease; `409`/malformed result never grants authority; process-local implementation is test-only | migration/host adapter tests; durable DB/lease and cutover evidence external |
| Sandbox provider | test capability | never production-ready; deterministic replay | connector/service tests / core owner |
| Gmail / Graph / IMAP-SMTP | connector implementations | capability advertised only after real controlled evidence | provider conformance + external gates / provider owner |

Required release artifacts: regenerated OpenAPI/JSON Schema, SDK contract tests,
compatibility diff, migration/rollback notes and a record of any provider or host
capability status change. A green Sandbox test cannot promote an external row.
