# MailHub SDKs

The Python and TypeScript clients are thin clients for the versioned HTTP
contract. They send host tenant/subject context and idempotency keys; they do
not accept, persist, refresh, or log Gmail/Graph/IMAP credentials.

The TypeScript package includes a strict `tsconfig.json`; run `npm run
typecheck` or `npm run build` from `sdk/typescript` to generate declaration and
source-map artifacts in `dist/`. A release pipeline must publish an integrity
hash and run the OpenAPI/contract compatibility check before publishing.

Candidate review clients accept an optional bounded `review_reason`; a
rejection without a reason is refused by the MailHub API and Agentctl handler.
Candidate clients expose both the generic `list_candidates`/`listCandidates`
filter and typed project/knowledge/task views. The server applies the type
filter inside the tenant/subject scope; callers must not infer that a
candidate is authoritative until it has passed review and the Host action or
Knowledge lifecycle boundary.
`get_candidate`/`getCandidate` and revision-bound `revoke_candidate`/
`revokeCandidate` expose the reviewable lifecycle; revoking an already-applied
candidate is rejected until the Host Action or Knowledge lifecycle authority
performs its governed downstream revoke.

Autonomy clients expose only the durable `recommend_only` lifecycle:
enqueue with a replay/idempotency key, inspect owner-scoped runs, and
run/pause/resume/cancel a persisted job. They never bypass candidate review,
host project/knowledge ports, explicit draft confirmation, or the outbound
interlock.

Thread clients expose `list_thread_page`/`listThreadPage` with an opaque
scope-bound cursor and account, unread, important, attachment, project and
candidate filters. The client forwards cursors without decoding them, so a
cursor cannot be reused across a different tenant, subject or filter scope.

Search clients expose an explicit `mode`: metadata search returns its bounded
coverage, completeness flag and incomplete reason; `provider` search fails
closed until a separately verified Provider search contract is enabled.

OAuth clients expose `begin_oauth`/`beginOAuth` and
`complete_oauth`/`completeOAuth` for Gmail and Microsoft Graph. A reauthorization
request carries the existing `connection_id` and `expected_revision`; the
one-time state and Host exchange bind those values so the callback rotates the
credential on the same connection. The SDK never receives a verifier, access
token, refresh token or client secret.

`update_connection_scopes`/`updateConnectionScopes` can only narrow the locally
usable granted scope set and is revision-fenced. Any scope expansion is rejected
until the caller completes a bound OAuth reauthorization; this prevents a rule or
agent from silently broadening provider consent.

`refresh_connection`/`refreshConnection` requests a Host-owned OAuth refresh by
connection revision and receives only updated non-secret identity/version
metadata. The Host performs refresh-token rotation and provider revocation; the
SDK never receives a token.

`revoke_connection`/`revokeConnection` is the explicit revision-fenced negative
path: it revokes Provider access and keeps the MailHub projection available for
post-revocation verification. Deletion remains a separate operation and must
only run after credential, object-store and downstream knowledge cleanup is
confirmed.

`export_data`/`exportData` enforces the bounded 1–200 limit and defaults to
metadata-only output; `include_content=true` is an explicit caller choice.
`list_audit`/`listAudit` and `provider_health`/`providerHealth` are bounded
host-authorized admin surfaces and return metadata only.

Subscription clients expose `list_sync_states`, `ensure_subscription`,
`renew_subscription` and `cancel_subscription`. Ensure/cancel require an
explicit idempotency key; the host remains the authority for provider tokens,
Pub/Sub/OIDC or Graph validation and scheduler leases. These calls persist only
bounded subscription metadata and never turn a notification into a completed
sync. Receipt operations additionally expose `replay_webhook_receipt`/
`replayWebhookReceipt`; they require the original body digest, a bounded reason
and an idempotency key, then queue only an owner-scoped `reconcile` job. The
append-only receipt and callback body are never mutated or re-saved.

`enqueue_sync`/`enqueueSync` accepts an optional bounded backfill input with
`folder_ref`, `label_refs`, `received_after` and `received_before`. The clients
validate control characters, label count, timezone-aware UTC normalization and date ordering before sending;
the server accepts the input only for `mode=backfill`, persists it in migration
0017, and never advances the incremental cursor for that job. Durable sync job
responses also expose the bounded provider deletion count added by migration 0018.
An operator cancellation of a running job returns `cancelling` while the worker
retains its lease and reaches a safe checkpoint; migration 0019 makes that
transition durable and prevents an expired cancellation from being requeued.
Migration 0021 adds durable `retry_wait` for transient sync failures; a retry is
pending until `next_attempt_at` and is bounded to five total provider calls.
`connection_impact_preview` / `connectionImpactPreview` exposes a bounded,
projection-only preview before a folder-scope change or revoke/delete. It
returns local counts and explicit unknown remote impact; it never calls Gmail,
Graph or another Provider.
Provider adapters
must translate only safe folder/label/date predicates and fail closed when a
requested predicate is not supported.

## Host examples and release contract

Copy the examples in `../examples/sdk/` into a host-owned service only after
injecting its authenticated tenant/subject context:

- `python_client.py` demonstrates owner-scoped inbox reads and durable
  `recommend_only` enqueueing with a replay key;
- `typescript_client.ts` demonstrates the same flow in a browser/server host.

The examples intentionally do not contain credentials, Provider tokens, or
real mailbox identifiers. Host code must generate idempotency keys per command,
persist the key/result association, and surface `MailHubApiError`/`MailHubError`
without converting `outcome_unknown` into success.

The authoritative HTTP contract is
`packages/mailhub/schemas/mailhub.openapi.v1.json`; the checked-in Python and
TypeScript clients are thin, reviewed clients rather than an unreviewed
generated client. A release may add generated clients only when the generator,
OpenAPI diff check, package integrity hash, SBOM and compatibility registry are
part of the same approved pipeline.
