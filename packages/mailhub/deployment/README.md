# Deployment examples

`docker-compose.sandbox.yml` is a local sandbox only. It deliberately keeps
outbound disabled and does not claim PostgreSQL-backed production readiness.

The image uses the packaged `mailhub-api` entrypoint; `--host`/`--port` are
the only process-level bind controls. In `production`/`staging`, the entrypoint
selects the fail-closed durable factory: PostgreSQL, authenticated Host Ports
and explicitly enabled real Provider connectors only. It never registers the
Sandbox connector or falls back to in-memory state. Run the bundled
`python scripts/apply_migrations.py` as an operator-controlled release step
before starting the API; migrations are not applied implicitly at startup.

The Helm chart is a contract example: it refuses an empty image digest and
references an operator-owned external Secret object. A release pipeline must
render it with a signed immutable image, run migrations, inject all Host Ports,
inject the host-owned `KillSwitchPort` endpoint when outbound or L3A automation
is enabled, and pass the live provider/security gates before enabling production
traffic. The kill-switch endpoint owns four-eyes changes and its durable audit
ledger; MailHub only performs bounded pre-side-effect checks.
