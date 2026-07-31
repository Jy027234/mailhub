# MailHub Agentctl gate evidence — 2026-07-29

## Adoption decision

`agentctl integration assess --root . --json` returned:

- recommendation: `extend_agentctl`
- mode: `assist`
- assurance: `enterprise_controlled`
- triggers: provider governance, tenant/scope/entitlement, approval before side effects,
  durable execution, idempotency/replay, trace/ledger/evidence and capability discovery
- vendored Agentctl snapshot: none

The MailHub capabilities remain product-owned JSON-in/JSON-out handlers in the
single `deployment/agentctl.capabilities.yaml` manifest. No product-specific
Agentctl Core route was added.

## Local gate results

| Gate | Result | Evidence |
| --- | --- | --- |
| `integration init --dry-run` | existing integration detected | Existing manifest/config/handler/test files were reported as conflicts; no overwrite was performed. |
| `integration validate` | pass | Manifest digest `2f0d064118ca6de16f016f3b54a612a4abbd6ef7e352798ed5f2ba1c61b05261`, 36 capability descriptors loadable, including bounded backfill filter fields on `ca.mail.sync.enqueue`. |
| idempotent `integration apply` | pass | Re-applied after the sync capability schema change: `created=0`, `updated=73`, `unchanged=0`, `conflicts=0`; a subsequent doctor projection reports `unchanged=73`. |
| `integration doctor` | pass with one environment warning | 16 pass, 0 fail when run with the script's ephemeral Lite valuation SecretRef; warning is source-tree Agentctl distribution. The ephemeral reference is configuration-only and is not provider authentication evidence. |
| `integration smoke` | blocked | No `CAPLATFORM_MODEL_API_KEY`/`CAPLATFORM_MODEL_BASE_URL` was available, so the coordinator could not materialize a real executable plan. The failure is preserved; no fake provider fallback was added. |

The Windows verification script now works under both Windows PowerShell 5.1
and PowerShell 7: it resolves default paths after parameter binding, uses an
ASCII smoke query to avoid code-page parsing errors, passes explicit
`--confirm-side-effects`, and keeps smoke failure visible when the real model
provider is unavailable.

This gate is independent of the MailHub M2/M3 provider-activation wave. The
repository now contains the preflight and host OAuth boundary, but no Gmail or
Microsoft Graph OAuth application, mailbox, watch/subscription or real Provider
network evidence was created in this run.
