# ADR 0010: MailHub development and release standard

- Status: accepted
- Date: 2026-07-28
- Owners: MailHub maintainers + Security/SRE release owner

## Change protocol

Every material behavior change starts with an RFC when it changes a public
contract, data class, authority boundary, provider capability, automation level or
operational SLO. A decision that affects architecture or risk receives an ADR.
The change records owner, scope, alternatives, threat/privacy impact, migration,
rollback and evidence. The templates live in `docs/templates/`.

## Mandatory gates

The package gate runs unit/contract/security tests, Ruff format/lint, strict mypy,
OpenAPI export and schema validation, migration static checks, import-boundary and
secret scans, source provenance, Agentctl manifest validation and deployment
contract tests. Root CI additionally runs the complete CAPlatform regression set.
Production promotion adds PostgreSQL/RLS, real provider, object/KMS/AV/DLP,
OpenTelemetry, load, backup/restore, license/SBOM/signing and shadow/cutover
evidence; sandbox green is never treated as production evidence.

## Definition of done

A change is done only when code, tests, docs, public schema, provenance and
rollback evidence agree. Tests must include the negative path and the relevant
tenant/subject scope. New side effects require idempotency, approval/policy,
durable execution and audit fields. New secrets or content fields require the data
governance ADR to be updated. Unverified external work remains explicitly open in
the master todo and implementation status.

## Release and rollback

Artifacts are immutable versioned images/SDKs with SBOM and provenance. Database
migrations support upgrade/rollback/re-upgrade and an N/N-1 compatibility window.
Feature flags gate providers, AI, write actions and autonomous behavior. A release
must have a kill-switch owner, rollback command, migration boundary, incident
contact and known-limitations page. Legacy MailHub/CAACTRAINING/AeroLink senders
remain disabled only after a signed single-sender and rollback drill.
