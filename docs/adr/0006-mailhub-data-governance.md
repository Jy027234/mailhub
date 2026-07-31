# ADR 0006: MailHub data governance, residency, retention and model use

- Status: accepted for the implementation baseline
- Date: 2026-07-28
- Owners: MailHub + Security/Privacy + host data owners

## Decision

MailHub uses data minimisation and host-owned authority as the default. A provider
remains authoritative for live mail and a host remains authoritative for identity,
secrets, objects, projects and knowledge. MailHub stores only the normalized
projection, hashes, provider references, bounded evidence, policy decisions and
operation receipts needed to provide its contract.

Every connection has an explicit content mode: `metadata_only`,
`bounded_processing`, `encrypted_cache`, or separately approved `archive`. The
mode is part of the connection/account scope and is recorded in audit metadata.
Changing to a less restrictive mode is an approval-gated change with a preview;
downgrading the mode creates a durable purge job and deletion evidence. Raw MIME,
attachments and draft bodies are object-store references, never ordinary database
columns. Object storage must enforce tenant/subject scope, encryption and TTL.

Mail data is classified as `public`, `internal`, `confidential` or `restricted`.
Restricted content cannot be sent to an AI execution port unless the host policy
explicitly permits that operation and the host reports the approved processing
region/provider. The AI port receives the smallest bounded source and a schema,
never unrestricted mailbox tools or permanent credentials. Production mail is not
used as a training fixture or model-improvement corpus without a separate consent,
legal and data-owner decision.

Retention is purpose-bound: receipts, projections, evidence, drafts, outbox
attempts and object content each have a configured TTL. A delete or account revoke
must fan out to object storage, search/index, candidate evidence, downstream
knowledge references and audit tombstones while retaining only the minimum
non-content proof that deletion was requested/completed. Export is a host-mediated
operation that emits governed references and a manifest; it does not bypass
tenant/subject authorization.

## Consequences and release gates

- Runtime configuration must name the data region, content mode, retention policy,
  KMS/object-store adapter and AI processing policy. Missing production values fail
  closed.
- The host owns legal basis, consent, data-subject requests, cross-border review,
  provider terms and knowledge publication rights. MailHub records references and
  decision evidence rather than becoming a second compliance authority.
- Tests must cover TTL expiry, revoke/delete propagation, cross-tenant object and
  vector access, restricted-content AI denial and redacted logs/traces.
- Any new field containing content, PII, a provider reference or a secret requires
  a classification/retention entry and an ADR or RFC review.

## References

- `docs/adr/0003-knowledge-authority-and-tenancy.md`
- `docs/adr/0003-mailhub-content-storage.md`
- `docs/security/threat-model.md`
- `packages/mailhub/src/mailhub/ports.py`
