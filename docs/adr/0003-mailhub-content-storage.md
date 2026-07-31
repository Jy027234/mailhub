# ADR-0003: Content and credential storage

状态：accepted (2026-07-28)

OAuth tokens, passwords and refresh material are resolved just-in-time through
`CredentialBrokerPort`; they are not persisted or logged by MailHub. Relational
PostgreSQL tables store metadata, SHA-256 digests and tenant-scoped encrypted
object references. `ObjectStorePort` is required for production bounded body
and draft content. Before analysis or sending, the service hydrates only the
authorized object and verifies its digest again. Raw MIME is bounded and
active HTML/remote content is blocked.
