# MailHub

MailHub is the provider-neutral mail management core described in
`docs/intelligent-mail-management-module-master-todo-2026-07-28.md`.

The package provides the M0/M1 core, a PostgreSQL/SQLAlchemy durable
repository, Gmail/Graph/IMAP-SMTP/Sandbox connectors, deterministic
evidence-first analysis, reviewable project/task/knowledge candidates, an
approval-bound outbox and versioned FastAPI/Agentctl contracts. Production
deployments must supply an asyncpg PostgreSQL URL, encrypted ObjectStorePort,
host identity/approval/action adapters, a durable event publisher and real provider credentials. The
sandbox connector is test-only and must never be advertised as production
mail capability.
SMTP is likewise disabled until `MAILHUB_SMTP_SEND_ENABLED=true` is explicitly
set after provider conformance and outbound safety gates pass.

Drafts accept separate To/CC/BCC lists and governed attachment object refs.
The approval digest and durable outbox bind all recipient classes and refs to
the draft revision. BCC, external-domain, large-recipient and attachment
risk flags fail closed into confirmation/dead-letter paths; no connector
advertises attachment sending until object-store scanning and Provider
attachment DTO conformance are proven.

`mailhub.oauth` contains the provider-neutral state/nonce/PKCE binding.  Hosts
still own OAuth client registration, consent, token exchange and the durable
state store; the in-memory state store is test-only.
`MAILHUB_HOST_SERVICE_TOKEN` authenticates MailHub calls to the host
credential boundary and is never persisted by MailHub. Provider client secrets
remain BFF/Secret-Manager-only and must not be injected into this service.
`POST /v1/mail/connections/{id}:scopes` is revision-fenced and only narrows the
locally usable scope set; scope expansion must use the bound OAuth reauthorization
flow and can never be performed by a rule or agent.
`POST /v1/mail/connections/{id}:refresh` asks the Host credential broker to
refresh/rotate the grant and returns only non-secret metadata; missing refresh
authority or token-shaped responses fail closed.
`GET /v1/mail/connections/{id}/impact-preview` is a read-only, projection-only
scope/deletion preview; it never queries a Provider and explicitly marks remote
message impact as unknown until a real Provider reconciliation is run.

`TelemetryPort` and `QuotaPort` are host/deployment contracts: the included
in-memory implementations are bounded test/sandbox fixtures only. The optional
`mailhub[observability]` extra provides `OpenTelemetryTelemetryPort` and
`StructuredRedactingLogger`; production must still provide a configured
Collector/exporter and distributed/transactional quota leases for tenant, user
and provider-account scopes.
For an independent PostgreSQL deployment, `PostgresQuotaPort` and migration
0012 provide the transactional quota lease implementation; migration 0013
adds durable non-secret provider subscription state for A3; migration 0014
adds bounded provider metadata to normalized message projections; migration 0015
adds body-free webhook route metadata for controlled receipt replay; migration 0016
adds non-secret provider account/tenant identity and credential version to connections;
migration 0017 adds durable folder/label/date bounds for backfill sync jobs and a
scope-aware index; migration 0018 adds the bounded provider deletion count to
durable sync job results; migration 0019 adds the durable `cancelling` state for
running jobs so a worker keeps its lease until a safe cancellation checkpoint;
migration 0020 adds normalized inbound Cc/Bcc/Reply-To address projections while
keeping raw RFC 5322 headers out of the durable model.
Migration 0021 adds bounded durable `retry_wait` state for transient sync failures;
it releases the worker lease and reclaims only after `next_attempt_at` is due.
Backfill filters are accepted only with `mode=backfill`, never
advance the incremental cursor, and are translated per connector with fail-closed
behavior when a Provider cannot safely express a requested bound.
Hosts may use `HttpQuotaAdapter` when quota authority remains in the host platform.

`EventPublisherPort` and the registered `MailHubEventType` catalog provide a
validated metadata-only event boundary; the host owns durable replay and
subscriber authorization. Search responses likewise expose metadata coverage
(including normalized To/Cc/Bcc/Reply-To headers) and completeness, while
unverified provider search is degraded rather than silently falling back to an
incomplete full-text claim.
The default application graph injects the HTTP host adapters when
`MAILHUB_EVENT_PUBLISHER_ENDPOINT`, `MAILHUB_TELEMETRY_ENDPOINT` and
`MAILHUB_QUOTA_ENDPOINT` are set. A3 push additionally injects
`MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT` and
`MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT`; those two endpoints are
required only when a real-provider push flag is enabled, and production
readiness keeps the same fail-closed condition.

The v1 API also exposes connection revocation, deletion and bounded user export.
For non-sandbox providers, `:revoke` calls the host credential revocation port
before writing `REVOKED`; deletion is then resumable (`DELETING` to `DELETED`)
and never completes until governed object references and downstream cleanup are
confirmed. A delete retry for an already revoked connection does not revoke the
host grant a second time. Rule execution is split
into dry-run evidence and an explicitly enabled L3A path; autonomous receive,
sync, classification, candidate generation and drafting are permitted within
owner scope, while sending remains approval-, policy-, idempotency- and
outbox-bound by default.

Migration hosts can use `CAACTRAININGMailAdapter` and `AeroLinkMailAdapter`
without importing either legacy repository. `PostgresSenderInterlockAdapter`
backs the 0007 `mail_sender_leases` table; migration 0008 adds durable
`mail_autonomy_runs` for owner-scoped recommend-only execution and prevents a
legacy and MailHub sender from owning the same tenant/account/purpose at once.
Migration 0009 adds outbox `lease_expires_at`; an abandoned `LEASED`/`SENDING`
operation is converted to `OUTCOME_UNKNOWN` before another worker can retry,
so provider reconciliation—not blind replay—decides whether a send is safe.
Migration 0010 adds expiry to sync/autonomy worker leases; migration 0011 preserves
bounded `attachment_count`/read metadata for unified inbox filtering; migration
0012 adds transactionally admitted PostgreSQL quota leases; migration 0013 adds
subscription lease/status/expiry/watermark state; migration 0014 adds bounded
provider metadata to normalized message projections; migration 0015 adds bounded
body-free webhook route metadata for controlled receipt replay, and migration 0016
adds the non-secret provider account/tenant identity and credential version returned
by the Host OAuth broker; migration 0017 adds durable, bounded backfill filter
metadata (folder, labels and UTC date window) to sync jobs; migration 0019
preserves running-job lease/fencing while an operator cancellation is finalized;
migration 0020 stores bounded inbound Cc/Bcc/Reply-To address classes (not raw
headers or display names).
Migration 0021 stores bounded sync retry attempt metadata and never retries
permission, scope, revocation, or malformed-response failures.
The webhook route metadata is used only for owner-scoped, digest-checked receipt
replay. An abandoned `RUNNING` job is re-queued with a new fencing token and a durable recovery
audit.
The synthetic
intelligence contract set and its dataset card live in `evals/` and
`docs/synthetic-mail-eval-dataset-card.md`; they contain no production mail.

The reusable React surface is in `ui/`: use `MailHubWorkspace` when the host
owns the page frame, or `@fyjtech/mailhub-ui/standalone` and
`MailHubStandalone` when a stable shell is useful. Both require an injected
host client and identity; neither mounts a router or stores credentials. SDK
usage examples are in `examples/sdk/`. The package-level `LICENSE` and
`NOTICE` are required release companions and state the CAPlatform proprietary
license; the source ledgers record all external repositories as references
only.

Run locally from this directory:

```text
python -m pip install -e ".[dev]"
python -m pytest
ruff check src tests scripts
ruff format --check src tests scripts
mypy src tests
./scripts/verify.ps1
# Standalone development entrypoint (sandbox defaults, no real Provider access)
mailhub-api --host 127.0.0.1 --port 8000
# Offline M2/M3 activation preflight (never calls a Provider)
python scripts/provider_preflight.py --provider all --json
# CI/A0 hard gate; disabled providers return exit code 2
python scripts/provider_preflight.py --provider all --json --require-config-ready
# Validate a redacted activation bundle; --require-real rejects gate failures
python scripts/validate_provider_evidence.py provider-activation/gmail/test/<run-id>/sync-reconciliation.json --require-real
# Validate the complete A1-A4 bundle and cross-file identity/revocation evidence
python scripts/validate_provider_bundle.py provider-activation/gmail/test/<run-id> --require-real
```

The same entrypoint selects `mailhub.runtime.create_durable_app` when
`MAILHUB_ENV` is `production`, `prod` or `staging`. That graph requires
PostgreSQL plus all mandatory Host Ports, authenticates every Host request with
`MAILHUB_HOST_SERVICE_TOKEN`, registers no Sandbox connector, and fails startup
instead of substituting local state. Apply the bundled SQL migrations as a
separate operator step before starting the API.

The PostgreSQL adapter never stores OAuth material or raw message/draft bytes
in relational tables. Message and draft rows retain a content digest and an
encrypted object reference; the object store is resolved only for an authorized
analysis or send operation. `InMemoryMailRepository` and `InMemoryObjectStore`
are explicit local/test adapters.

Apply schema changes with `scripts/apply_migrations.py`; it uses a PostgreSQL
advisory lock, checksum ledger, one-transaction-per-migration and explicit
rollback targets. MailHub does not auto-migrate on API startup and does not
accept SQLite as a production substitute.

Host Port implementations and the generic HTTP adapter contract are documented
in `docs/host-adapter-sdk.md`; the HTTP adapters are transport glue only and do
not replace production identity, approval, object-store or governance controls.
Provider capability status and the M2/M3 activation stages are recorded in
`docs/provider-compatibility.yaml` and
`docs/mailhub-provider-activation-runbook.md`; `preflight_*` and
`contract_only` rows are not production-ready. Gmail/Graph are read-only and
do not advertise push by default; real OAuth, isolated-mailbox,
reconciliation and security evidence are still required before promotion.
The A3 contract surface includes `ProviderSubscriptionPort`,
`HttpProviderSubscriptionAdapter`, `ProviderNotificationVerifierPort`,
`HttpProviderNotificationVerifierAdapter` and bounded Gmail/Graph notification
parsers. `POST /v1/mail/provider-notifications/{provider}` accepts only a
host-verified, body-free scoped delivery, writes a deduplicated receipt and
queues an incremental sync job; the host still owns provider verification,
account mapping, renewal and durable receipt/quarantine operations.
Graph lifecycle payloads may omit `resource`; the parser records a stable
subscription-scoped reference and routes the event to reconciliation.
For a real A2 observation, use `scripts/run_provider_activation.py` only against
an authorized MailHub HTTP deployment. It requires two explicit network gates,
performs one bounded sync (optionally a folder/label/date-filtered backfill) plus
idempotency replay, and writes only hashed identifiers/cursors and bounded counts
to the evidence bundle, including account identity hashes (and Graph tenant identity hash).
A backfill observation must record the requested bounds
and confirm that the incremental cursor was not advanced.
After the real OAuth flow, an intentional state replay rejection, credential
refresh and credential revoke, use `scripts/collect_oauth_evidence.py` to derive
`oauth-consent.json` from the owner-scoped audit endpoint. It accepts no JSON
fixture, authorization code, token or credential reference; if one of those
observations is missing it fails closed.
After a real notification wave has run for at least seven days, use
`scripts/collect_notification_evidence.py` to derive `webhook-receipts.json`
from the owner-scoped sync-state, receipt and audit endpoints. It requires a
real renewal, duplicate delivery, positive ACK latency and completed
reconciliation; it accepts no callback body, client state or hand-written
count and fails closed on a short/future observation window.
After the negative-access probe and the separately approved cleanup/delete
operation, use `scripts/collect_revocation_evidence.py` to derive
`revocation-negative.json`. It requires durable credential-revoked,
subscription-cancelled, post-revoke sync rejection, zero provider/broker
request counters and deletion-proof audit records; missing Host evidence fails
closed and the script never performs revoke/delete itself.
Before any M2/M3 checklist item is marked complete, validate the resulting
`sync-reconciliation.json` with `scripts/validate_provider_evidence.py --require-real`;
the validator uses a closed-world schema and rejects unreviewed fields in addition to
secrets, raw message fields and an `activation_gate_failure`. A gate failure records a
blocked environment but never proves Provider activation. For A1-A4 release evidence,
run `scripts/validate_provider_bundle.py --require-real` on the complete bundle; it
cross-checks OAuth, notification, revocation and deletion proof without contacting a
Provider.
