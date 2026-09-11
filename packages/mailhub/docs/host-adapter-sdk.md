# MailHub Host Adapter SDK and conformance guide

MailHub core depends on ports, not on a host's framework or product names. A
host implementation may use the included HTTP adapters or implement the Python
Protocols directly. The host owns authentication to its services; MailHub only
receives a scoped request and short-lived result.

The matrix below is executable: record what your ports returned and run
`python scripts/host_conformance.py --bundle <file>`
(`python scripts/host_conformance.py --emit-template` starts a bundle). See
`docs/host-quickstart.md` for the full onboarding path.

## Port matrix

| Port | Required behavior | Never do |
| --- | --- | --- |
| `HostIdentityPort` | verify tenant, subject, capability and optional connection | trust browser tenant headers or default allow |
| `CredentialBrokerPort` | resolve a short-lived credential for the exact tenant/subject/ref | return a token to logs or persist it in MailHub |
| `CredentialRefreshPort` | refresh/rotate the Host grant and return only credential ref, identity and version metadata | return access/refresh tokens or let MailHub call a Provider refresh endpoint |
| `ObjectStorePort` | encrypted, tenant-scoped put/get/delete with digest and TTL | expose raw bytes through a public MailHub DB/API |
| `ApprovalPort` | create and verify revision/action-bound approval; support revoke/expiry | self-approve or accept an unbound confirmation |
| `AiExecutionPort` | schema-constrained processing of bounded, classified input | expose mailbox tools, secrets or unrestricted URLs |
| `HostActionPort` | discover/propose/execute host-owned actions with result refs; dedupe execute by the stable `action_id` | write project facts directly from raw email or execute a retried action twice |
| `KnowledgeSinkPort` | receive reviewed governed refs, preserve rights/lineage, and dedupe by candidate ref | publish a candidate without host review or create duplicate knowledge records on retry |
| `KnowledgeLifecyclePort` | revoke/quarantine/reindex source refs on account deletion, revocation or rights change; accept only tenant/subject/connection/message IDs | acknowledge source deletion while downstream knowledge remains searchable or accept raw mail content |
| `AgentMemoryPort` | store only host-approved `ApprovedKnowledgeReference` values with rights/security/approval provenance | write email body, prompt text, unapproved candidate or raw object ref into Agent memory |
| `KnowledgeSafetyPort` | run AV/DLP/file/PII/rights checks and return explicit `security_state`/`rights_state` | treat an unscanned or rights-pending candidate as publishable |
| `AuditPort` | append redacted IDs, hashes, status, policy and evidence refs | persist body, MIME, token or sensitive prompt |
| `NotificationPort` | deliver bounded status/approval/reauthorization notices | treat notification delivery as a side-effect approval |
| `EventPublisherPort` | publish validated v1 metadata-only envelopes with stable event IDs | publish unvalidated event names, body/MIME, tokens or make MailHub own subscriber delivery |
| `TelemetryPort` | export bounded IDs, hashes, status, counts and error codes to the host OpenTelemetry/metrics pipeline | send body, MIME, token, raw provider response or high-cardinality address labels |
| `QuotaPort` | atomically reserve/release tenant, subject and provider-account worker leases with expiry | use process-local counters as production authority or let an expired lease block an account forever |
| `KillSwitchPort` | return an explicit global/tenant/provider decision immediately before each side effect; host owns four-eyes mutation/audit | infer runtime switch state from mail content, browser flags or a stale process-local value |
| `SenderInterlockPort` | atomically claim/release the single sender lease for a tenant/account during migration | allow a second sender, infer authority from a local flag, or release another owner's lease |
| `OAuthStateStore` | save/consume a short-lived, one-time tenant/subject/provider/redirect/PKCE binding in an encrypted TTL store | put provider tokens in state, let a state be replayed, or use process-local state in production |
| `OAuthCallbackPort` | exchange the one-time code inside the host and return only mailbox identity, granted scopes and `credential_ref` | return access/refresh/id tokens, client secrets or provider response bodies to MailHub |
| `ProviderSubscriptionPort` | create/renew/cancel a Gmail watch or Graph subscription and return bounded expiry/ref metadata | pass provider tokens/clientState secrets to MailHub, treat a notification as a completed sync, or let a stale lease renew itself |
| `ProviderNotificationVerifierPort` | authenticate the fixed Gmail/Graph callback in the host, resolve subscription/account to tenant/subject/connection, and return body-free normalized deliveries | trust callback tenant headers, return raw body/OIDC claims/clientState/tokens, or let a notification directly mutate mailbox projections |

## HTTP adapter contract

`mailhub.hosts.http` contains generic JSON adapters for the ports. Endpoints must
be HTTPS (or explicitly localhost for development), reject redirects, bound
timeouts and paths, return JSON objects, and map 401/403 to
`AuthorizationError`. The adapter never logs request bodies or stores host
headers. Production authentication headers are injected by deployment and must
come from workload identity/Secret Manager.

The request payload always includes tenant and subject scope where the port has
one. A host must return explicit booleans/refs; missing or malformed values fail
closed. `HttpHostActionAdapter.execute` sends an `Idempotency-Key` equal to the
stable action ID, and `HttpKnowledgeSink` sends a candidate-scoped key. Hosts
must persist the key/result association before acknowledging a side effect.

For candidate application, `AgentActionRequest.parameters` contains the
bounded `candidate_id`, `candidate_revision`, `candidate_type`,
`candidate_payload`, `candidate_evidence` and `source_message_id`. Raw body,
HTML, MIME, header and original-text fields are removed before this boundary;
the host remains responsible for validating the target object and applying the
approved project/task action.
`HttpKnowledgeLifecycleAdapter` and `HttpAgentMemoryAdapter` send stable
idempotency keys. An HTTP 204 is accepted only for void
audit/notification/object-delete operations. Knowledge lifecycle and memory
hosts must return a JSON result/ref so MailHub can persist bounded evidence.
`HttpKillSwitchAdapter` calls `POST /v1/mail-host/kill-switch/check` with only
tenant/subject/provider/operation scope. The response must contain an explicit
boolean `allowed`; a denied response must include a bounded reason. Switch
mutation, four-eyes approval and the authoritative change ledger remain in the
host. A missing, malformed or unavailable decision fails closed.

`HttpEventPublisherAdapter` validates the registered event type and envelope
before calling `POST /v1/mail-host/events`, using `mailhub-event:{event_id}` as
the idempotency key. The host owns durable replay and subscriber authorization.
Set `MAILHUB_EVENT_PUBLISHER_ENDPOINT` to inject this adapter into the default
application graph; production readiness rejects a missing endpoint. The core
publishes only bounded lifecycle metadata and treats a temporary publisher
failure as an auditable/telemetry-visible integration failure without changing
already-committed mail state.

The OAuth adapters are `HttpOAuthStateStore` and `HttpOAuthCallbackAdapter`.
Their default host endpoints are `POST /v1/mail-host/oauth/state`,
`POST /v1/mail-host/oauth/state/consume` and
`POST /v1/mail-host/oauth/exchange`. State save/consume is one-time and
idempotent by `mailhub-oauth-state:{state_id}`. The exchange request contains
the one-time code, PKCE binding and the state-bound `requested_scopes`; the
response is reduced to `email_address`, `credential_ref`, `granted_scopes` and
optional provider account/tenant IDs. MailHub rejects scope drift, missing
required Gmail/Graph read-only scopes and any write-capable scope, as well as
token-shaped response fields. The authorization service also refuses a Gmail/Graph
authorize request that omits the required read-only scope or includes a write scope.
Direct service connection creation, scope narrowing,
activation and reauthorization use the same read-only scope gate; a legacy Gmail/Graph state
record without requested scopes is rejected instead of being upgraded. A production host must encrypt and TTL the state
record, atomically consume it once, revoke/delete the credential on connection
deletion, and maintain provider-specific OAuth verification/consent evidence
outside MailHub.

CAPlatform's reference host implementation is
`apps/bff/src/caplatform_bff/mailhub_credentials.py`. It exposes the same state,
exchange, resolve, refresh and revoke paths behind a constant-time checked
`Authorization: Bearer` service token. The MailHub runtime reads that token
from `MAILHUB_HOST_SERVICE_TOKEN` as `SecretStr` and injects it only into Host
adapter request headers. Provider client secrets are never configured in the
MailHub process. The reference encrypted SQLite store is explicitly a
local/controlled-Beta adapter; production replaces its storage backend with an
approved Secret Manager/KMS without changing the HTTP contract.

`HttpProviderSubscriptionAdapter` uses
`POST /v1/mail-host/provider-subscriptions/ensure` and
`POST /v1/mail-host/provider-subscriptions/cancel`. It returns a
`ProviderSubscriptionLease` containing provider, connection, opaque
subscription ref, status, callback endpoint and expiry only. The host must
perform Gmail Pub/Sub OIDC validation and watch renewal, or Graph clientState/
certificate/lifecycle validation and renewal. The provider push flag remains
false by default; enabling it only advertises the host-boundary capability when
both subscription and verifier endpoints are configured. Production push is
still not approved until the host has durable scheduler, receipt deduplication
and reconciliation evidence.

`MailSubscriptionCoordinator` persists that bounded lease in
`MailboxSyncState` (migration `0013_mailhub_subscription_state.sql`) beside the
provider cursor. Its `renew_if_due` and `record_notification` methods are
worker/scheduler units, not API-process timers; verified notifications advance
only a watermark, and connection revoke/delete cancels active subscriptions
before the terminal state transition. A host must still provide the real
watch/subscription, scheduler, receipt and reconciliation evidence.

Normalized message projections may also carry bounded `provider_metadata`
(migration `0014_mailhub_provider_metadata.sql`). It is string-only metadata
such as body content type, category/folder identifiers, Gmail history id or
Graph change key. Raw headers/MIME/body, OAuth material, client state and
cookies are rejected by the domain contract and are never part of this field.

Inbound projections also expose normalized `cc_addresses`, `bcc_addresses` and
`reply_to_addresses` (migration `0020_mailhub_message_recipient_headers.sql`).
Only address values visible to the connected mailbox are retained; display names,
raw header syntax and MIME are discarded. Providers may return empty Bcc when the
mailbox cannot observe it, and absence must not be interpreted as proof that no
Bcc existed.

Migration `0021_mailhub_sync_retry.sql` adds bounded `retry_wait` metadata for
transient sync failures. The host scheduler must reclaim a job only after its
durable `next_attempt_at` timestamp; a retry-wait job is pending, not success.

Migration `0015_mailhub_webhook_route_metadata.sql` stores only bounded,
body-free route metadata for verified provider receipts. The owner-scoped
`POST /v1/mail/webhooks/receipts/{provider}/{event_id}:replay` endpoint requires
the original body digest and an idempotency key, then queues a reconcile job
without mutating or re-ingesting the receipt. Legacy receipts without route
metadata remain queryable but cannot be replayed.

Migration `0016_mailhub_connection_identity.sql` stores the non-secret
`provider_account_id`, optional `provider_tenant_id` and monotonic
`credential_version` returned by the Host OAuth broker. These fields bind a
refreshed credential to the expected provider identity; access/refresh tokens
remain host-only. Older connections may leave the identity fields null until
reauthorization supplies them.

Migration `0017_mailhub_sync_filters.sql` stores the bounded backfill scope on a
durable sync job: `folder_ref`, up to 20 `label_refs`, and an optional UTC
`received_after`/`received_before` window. The API and SDK accept these fields
only with `mode=backfill`; an incremental job with a filter is rejected, and a
backfill never reads or advances the incremental cursor. Connector adapters must
translate only provider-safe filters (or return a bounded unsupported-filter
error); they must not broaden the query or fall back to an unbounded scan.

Migration `0018_mailhub_sync_deleted_count.sql` stores the bounded number of
provider deletion references applied by a durable sync job. The field is
metadata-only and is populated from the provider page result after projection
deletion succeeds; it never contains a provider tombstone or message body.

Migration `0019_mailhub_sync_cancelling.sql` keeps a running job in a durable
`cancelling` state while its worker lease reaches a safe cancellation checkpoint;
an expired cancelling lease is finalized as cancelled rather than requeued.

Hosts can call `GET /v1/mail/connections/{id}/impact-preview` before changing a
folder scope or starting revoke/delete. The response is projection-only, bounded
to the owner connection, contains counts and governed project-hint summaries,
and never performs Provider I/O. `remote_message_delta_estimate: null` is
intentional: a remote delta requires a real Provider reconciliation and must not
be inferred from local projections.

When `HttpCredentialBrokerAdapter` resolves a credential, the host may return
`credential_version`, `provider_account_id` and `provider_tenant_id` as
metadata alongside the short-lived secret. MailHub checks a supplied version
for rollback and supplied identities for mismatch before calling Gmail/Graph;
the metadata is never logged or sent to the browser.
The same adapter exposes `POST /v1/mail-host/credentials/refresh`; its response
is metadata-only and is revision-fenced by `POST /v1/mail/connections/{id}:refresh`.
The Host remains responsible for OAuth refresh-token rotation and provider
revocation; MailHub rejects token-shaped refresh responses.

`HttpProviderNotificationVerifierAdapter` calls
`POST /v1/mail-host/provider-notifications/verify` with the provider, a bounded
base64 callback body and an allowlisted set of non-secret callback headers. It
rejects token-shaped response fields and accepts only verified deliveries that
contain tenant/subject/connection scope plus normalized notification metadata.
The public MailHub route `POST /v1/mail/provider-notifications/{provider}` then
persists an append-only receipt, advances the matching watermark and creates an
incremental sync job with idempotency key
`provider-notification:{notification_id}`. The host endpoint must own OIDC,
Graph clientState/certificate validation, account mapping and quarantine for
unrouteable payloads; a successful parser/adapter contract test is not real
Provider evidence.

Set `MAILHUB_TELEMETRY_ENDPOINT` and `MAILHUB_QUOTA_ENDPOINT` to inject the
host-owned `HttpTelemetryAdapter` and durable `HttpQuotaAdapter`. Production
readiness rejects either endpoint when the deployment claims production-like
readiness; the in-memory implementations remain sandbox/contract-test only.
An independent PostgreSQL deployment can inject `PostgresQuotaPort` instead;
it uses migration `0012_mailhub_quota_leases.sql` (and `0013` for subscription
state), tenant RLS and a
transaction-scoped advisory lock to make concurrent quota admission atomic.

## Conformance checklist

For every adapter, run the shared negative tests and record evidence for:

1. TLS/redirect/timeout and malformed JSON behavior;
2. tenant/subject mismatch, missing capability and 401/403 denial;
3. secret non-persistence and redacted error/telemetry output;
4. object digest, TTL, delete and cross-tenant denial;
5. approval/action revision binding, expiry, revoke and four-eyes policy;
6. AI schema rejection, content classification and prompt-injection boundary;
7. HostAction/Knowledge idempotency (including transport retry after a timeout), result refs and no raw-mail write-through;
8. AV/DLP/rights gate fail-closed behavior and quarantine/review transitions;
9. audit/notification bounded payloads and retry semantics.
10. telemetry exporter outage does not change mail state; trace/metric fields remain bounded and redacted.
11. sender interlock claims are atomic, owner-bound, expiry/recovery aware, and emit only metadata-only migration audit events; an HTTP `409` means the lease was not acquired.
12. Knowledge lifecycle revocation is idempotent and blocks deletion when the downstream port is absent; Agent memory accepts only an approved body-free reference.
13. Kill-switch checks cover global, tenant and provider denials immediately before queue, rule and provider-send side effects; denied operations are audited and durable sends remain retryable rather than being reported as sent.
14. Connection revoke/delete cleanup must use the repository's complete opaque-reference snapshot
    (`list_connection_cleanup_refs`), never a user-facing page limit; adapters must preserve all
    message IDs, knowledge source IDs and message/draft object refs for downstream revoke/delete proof.
15. Provider subscription tests cover callback TLS, expiry bounds, idempotent ensure/cancel, provider/client-state binding, OIDC/certificate verification in the host, renewal failure, receipt deduplication and reconciliation scheduling. A green parser test is not real Pub/Sub/Graph evidence.

The included `packages/mailhub/tests/test_host_adapters.py` is a transport
contract smoke test, not production evidence. A host must add live integration,
failure-injection and load tests before enabling production endpoints.

## Migration sender lease

`MigrationController` accepts a host-owned `SenderInterlockPort`. The default
`NoDualSenderInterlock` is intentionally process-local and is suitable only for
contract tests. Production hosts should use `HttpSenderInterlockAdapter` (or a
direct implementation backed by a transactional database/lease service) at:

- `POST /v1/mail-host/migration/sender-lease/acquire`
- `POST /v1/mail-host/migration/sender-lease/release`

The acquire response must be a JSON object with an explicit boolean `acquired`
or `claimed`; malformed responses fail closed. The host must scope the lease by
`tenant_id` and `account_ref`, bind it to `owner`, fence stale workers, and keep
the lease authority shared by legacy and MailHub senders. Migration audit events
(`mail.migration.shadow_compared`, `mail.migration.authority_changed`, and
`mail.migration.rollback_completed`) include tenant/account/purpose/owner IDs and
status/timestamps only—never
mail body, attachment bytes, credentials, or raw provider responses.
