# ADR 0007: MailHub threat model and privacy impact assessment baseline

- Status: accepted as the pre-production control baseline
- Date: 2026-07-28
- Owners: MailHub Security/Privacy + host security owner

## Assets and trust boundaries

The protected assets are provider credentials and grants, mailbox content and
attachments, tenant/subject identity, AI prompts and outputs, project/knowledge
references, approval decisions, outbox operations and audit evidence. External
mail, HTML, MIME parts, attachment bytes, URLs and webhook bodies are untrusted.
The boundaries are: provider -> ingress, browser/host -> API, MailHub -> Host
Ports, MailHub -> Agentctl/model gateway, and MailHub -> database/object/index.

## Required controls

| Threat | Control | Evidence required before production |
| --- | --- | --- |
| OAuth CSRF, state replay, token theft | signed opaque state, nonce/PKCE, allowlisted redirect, one-time state store, Credential Broker | negative callback tests, rotation/revoke drill, penetration report |
| Tenant or subject confusion | verified host context, composite identity, application filters plus PostgreSQL RLS, object/index scope | cross-tenant negative matrix and live RLS tests |
| Prompt injection/tool escalation | evidence-first parser, untrusted-content labels, structured AI schema, no tool/credential passthrough, approval for side effects | injection corpus, abstention metrics and action authorization tests |
| HTML/URL/attachment attack | safe text view, active-content blocking, MIME/size/decompression limits, AV/DLP/quarantine and no server-side URL fetch | XSS/SSRF/MIME/zip-bomb/macros test report |
| Duplicate or ambiguous send | idempotency, revision/digest/approval binding, durable outbox, lease/fencing, outcome-unknown reconciliation, single sender lease | crash/replay and provider reconciliation evidence |
| Webhook forgery/replay/flood | bounded body, content type, HMAC/provider verification, replay window, durable dedupe/quarantine, rate limits | provider-specific verification and load/failure tests |
| Secret/content leakage | SecretRef-only persistence, redacted telemetry, object encryption/TTL, no raw payload errors, source secret scan | logs/traces/backups/dead-letter scan and KMS/object-store evidence |
| Knowledge pollution or rights expansion | candidate review, rights/security hard gate, governed object refs, revoke/reindex workflow | contamination and rights negative corpus |

## Privacy impact baseline

Processing is purpose-limited to mailbox synchronization, user-requested
organization, bounded intelligence and explicitly approved host actions. The host
must provide notice, consent/legal basis, data region, processor/subprocessor
decisions and data-subject request handling. MailHub must support access/export,
rectification through the provider/host authority, deletion/revoke, retention
expiry and an auditable explanation of where a candidate came from. The default
behavior is to abstain or quarantine when a control or rights decision is
unavailable; availability is never a reason to weaken authorization.

Residual risks (provider compromise, malicious sender content, model error,
misconfigured retention or an unavailable downstream authority) remain explicit
operational risks. They require a named owner, a kill switch and an incident
runbook; this ADR is not a legal approval or a claim that production review is
complete.

## References

- `docs/adr/0002-mailhub-agent-safety.md`
- `docs/adr/0004-mailhub-auth-mail-isolation.md`
- `packages/mailhub/src/mailhub/security.py`
- `packages/mailhub/src/mailhub/oauth.py`
