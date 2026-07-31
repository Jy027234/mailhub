# ADR-0002: Agent authorization and side-effect safety

状态：accepted (2026-07-28)

All agent actions carry tenant, subject, connection/folder/thread, data-class,
recipient and source evidence context. `MailAgentPolicy` and
`DelegationGrant` are intersected server-side and fail closed on missing,
expired, revoked or mismatched scope. L0/L1 analysis and candidates are
reviewable; L3A/L3B require narrow ranges and grant; L4 actions, new
recipients, forwarding, bulk, attachments, task writes and knowledge writes
require explicit approval. A global outbound kill switch blocks queued work.

An agent never receives provider credentials and message text is never
interpreted as a tool instruction. Every proposal records a content digest,
source message IDs and evidence locators.
