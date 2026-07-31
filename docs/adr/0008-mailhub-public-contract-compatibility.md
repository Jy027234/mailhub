# ADR 0008: MailHub public contracts and compatibility policy

- Status: accepted for v1
- Date: 2026-07-28
- Owners: MailHub API/SDK owner + host integration owners

## Contract sources

The normative v1 sources are:

- OpenAPI 3.1 at `packages/mailhub/schemas/mailhub.openapi.v1.json`;
- JSON Schema contracts in `packages/mailhub/schemas/`;
- event envelope and event payload schemas in `packages/mailhub/schemas/events/`;
- stable error taxonomy in `packages/mailhub/src/mailhub/errors.py` and the
  `mailhub.contract.v1` envelope.

The FastAPI implementation is generated input to the OpenAPI artifact, not a
license to change wire behavior without review. Schema artifacts are regenerated
in CI and a diff is required for every public change.

## Wire invariants

- All public JSON uses `snake_case`, explicit `schema_version`, bounded strings and
  `additionalProperties: false` where the contract owns the object.
- Errors expose a stable `code`, safe `message`, `retryable`, `outcome_unknown` and
  bounded `details`; SQL, provider tokens, raw bodies and secrets never cross the
  boundary.
- Long work returns a stable `job_ref`/operation reference. `queued`, `running`,
  `waiting_confirmation` and `outcome_unknown` are pending/uncertain states, never
  success.
- Mutating requests accept an idempotency key. Replays return the original result;
  a key reused with a different scope or payload is a conflict. Revision and
  content/recipient digests bind send commands.
- List endpoints use bounded limits and deterministic ordering. Additive response
  fields are allowed; removing or changing meaning requires a new major contract.
- Events are at-least-once, have `event_id`, `occurred_at`, `trace_id`, tenant and
  optional subject/idempotency context. Consumers deduplicate by event identity and
  must tolerate unknown additive fields only where the schema permits them.

## Versioning and compatibility

`v1` is compatible within the same major version. A breaking field/type/state,
authorization or idempotency change requires `v2`, migration notes and an N/N-1
compatibility window. Provider-specific behavior belongs in capability descriptors
and does not silently expand the common contract. SDKs pin the major version and
surface the same error codes; generated clients are checked against the published
OpenAPI artifact.

## Required evidence

CI must regenerate OpenAPI, validate every JSON Schema, run contract tests against
the Python and TypeScript SDKs, verify pagination/idempotency/error semantics and
publish a compatibility report. Host adapters may add fields internally but must
not bypass the v1 boundary.
