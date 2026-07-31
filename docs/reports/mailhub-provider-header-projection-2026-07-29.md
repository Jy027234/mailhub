# MailHub inbound recipient-header projection evidence

日期：2026-07-29
范围：Gmail/ Microsoft Graph/ IMAP/ EML-MBOX 的收件消息 DTO 与 durable projection

## 已完成的本地合同

- `ProviderMessage` 与 `MailMessageProjection` 新增 `cc_addresses`、`bcc_addresses`、
  `reply_to_addresses`；地址统一为小写、无 display name 的 bounded 地址值。
- Gmail 使用 `Cc`、`Bcc`、`Reply-To` headers；Graph 使用 `ccRecipients`、
  `bccRecipients`、`replyTo`；IMAP 与离线导入沿用 RFC 5322 address parser。
- PostgreSQL migration `0020_mailhub_message_recipient_headers.sql` 为消息表添加三个
  `TEXT[]` 列，旧行以空数组兼容；down migration 可显式回滚。
- 线程参与者聚合和删除后的线程重建会包含 To/Cc/Bcc/Reply-To；API/CAPlatform Web/嵌入
  UI 类型以可选字段发布，避免旧宿主被破坏。
- conformance kit 拒绝未解析的 display name、非法地址和空 To；原始 headers、MIME、正文、
  token、client state 不进入 metadata。Bcc 缺失只能表示 Provider 未向该邮箱暴露，不可推断
  “没有 Bcc”。

## 验证

- `python -m pytest tests/test_connectors.py tests/test_persistence_contract.py tests/test_migration_runner.py`
  通过（37 tests）。
- `python -m pytest`、Ruff format/check、strict mypy、静态 migration contract 和 migration
  dry-run 均通过；最近一次 MailHub verify 为 283 passed（1 个既有 FastAPI deprecation warning）。

## 未宣称完成

本报告是离线代码/合同证据，不是 Gmail 或 Graph 真实邮箱证据。真实账号中的新增、修改、
删除、长线程、附件、Bcc 可见性、history/delta 对账、撤权后零 Provider 请求仍须按
`docs/mailhub-provider-activation-runbook.md` 在隔离宿主和测试邮箱中执行并由责任人签字。
