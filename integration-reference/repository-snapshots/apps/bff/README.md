# CAPlatform BFF

The BFF is the trusted server boundary for Platform Core identity, agentctl Assist invocation,
Application Interaction Event validation and durable conversation publishing.

It deliberately does not own Platform Core authorization, agentctl execution state, Review decisions,
AIProjectOPS objects or Enterprise Document Management lifecycle facts.

For local UI development, `CAPLATFORM_AI_MODE=fixture` exposes explicit non-authoritative event
fixtures. Shared and production environments must use `remote`.

The optional `/api/mcp/**` facade is disabled unless `MCP_INTEGRATION_BASE_URL`
and `MCP_INTEGRATION_SERVICE_TOKEN` are configured. It forwards only the
published-binding invocation contract and host identity headers to the
independent `mcp-integration` module; browser callers cannot select a remote
endpoint, tool name, or credential.

The optional `/api/mail/**` projection is equally fail-closed when
`MAILHUB_BASE_URL` is absent. MailHub OAuth registration is server-owned:
`MAILHUB_GMAIL_*`/`MAILHUB_GRAPH_*` metadata is used by the BFF to build the
authorize request, while browser callers submit only a connection revision or
the one-time provider callback state/code. Credential refs and provider tokens
never cross this boundary.

The BFF also implements the service-authenticated MailHub Host credential
contract under `/v1/mail-host/oauth/*` and `/v1/mail-host/credentials/*`.
`MAILHUB_HOST_SERVICE_TOKEN` authenticates MailHub-to-BFF calls;
`MAILHUB_GMAIL_CLIENT_SECRET`, `MAILHUB_GRAPH_CLIENT_SECRET` and
`MAILHUB_CREDENTIAL_ENCRYPTION_SECRET` are BFF-only. The included
`EncryptedSQLiteMailCredentialBroker` atomically consumes OAuth state, encrypts
tokens at rest, rotates refresh tokens and destroys the local grant on revoke.
It is the local/controlled-Beta adapter, not a production Secret Manager;
production must provide the same contract through an approved KMS/Secret
backend. Gmail additionally calls Google's revocation endpoint. Microsoft does
not expose an equivalent app-safe per-refresh-token endpoint, so the Beta
broker destroys its only token copy and requires tenant/user consent revocation
as a separately evidenced operator action.

The mail projection also exposes a bounded, non-cacheable
`GET /api/mail/data-export` for the verified session and an admin-role-only
`GET /api/mail/admin/provider-health` and permission-gated `GET /api/mail/audit`
projections. All routes remain fail-closed when MailHub is unavailable; health,
audit and export responses are recursively redacted for credential/token-shaped
fields. Connection lifecycle also exposes a revision-fenced
`POST /api/mail/connections/{id}:revoke` negative path; it keeps the projection
for post-revocation verification, while `:delete` remains a separate cleanup
operation.
