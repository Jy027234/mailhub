# ADR-0001: MailHub boundary and ownership

状态：accepted (2026-07-28)

MailHub is a provider-neutral product module. Its domain/application layers
own connection projections, synchronization cursors, candidates, drafts,
outbox operation state and minimum audit metadata. Host systems own user,
tenant, consent, project/task facts, knowledge publication and authoritative
billing. Providers are reachable only through `ProviderConnector`; host
systems only through versioned ports. CAACTRAINING and AeroLink are behavior
references and migration hosts, never imports into core.

The CAPlatform integration is a thin API/Host Port adapter. Agentctl receives
one product capability manifest and product-owned JSON handlers; no MailHub
specific Agentctl Core route is introduced.
