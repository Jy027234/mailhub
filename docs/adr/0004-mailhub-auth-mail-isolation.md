# ADR-0004: Transactional authentication mail isolation

状态：accepted (2026-07-28)

Registration, password reset, MFA, activation and security-alert mail remain
host-owned transactional flows. They do not enter MailHub intelligence,
knowledge extraction, project actions, autonomous reply policy or agent
outbox. A host may share a transport implementation only through a separate
restricted adapter with independent audit, templates, scopes and approval
rules.
