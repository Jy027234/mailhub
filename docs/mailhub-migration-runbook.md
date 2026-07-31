# MailHub → CAACTRAINING / AeroLink 迁移运行手册

本手册只描述可回滚的 host adapter 流程。MailHub core 不导入两个旧仓，旧宿主仍拥有 campaign、认证邮件、项目状态和业务 outbox 事实。

当前 clean-room 入口位于 `packages/mailhub/src/mailhub/hosts/legacy.py`：
`CAACTRAININGMailAdapter` 只接受已批准 Campaign snapshot，
`AeroLinkMailAdapter` 只接受 `EMAIL` delivery snapshot；两者输出 MailHub
command-shaped metadata，不携带正文，不读取旧仓库。结果投影使用
`project_delivery_result`，`outcome_unknown`/人工处置永远不会变成 `sent`。
这些是可直接复用的 MailHub adapter，不是旧仓代码的授权复制；来源/许可仍以
`docs/provenance/source-ledger.yaml` 和固定 commit 为准。

## 0. 绝对门禁

- 使用固定快照与 `docs/provenance/source-ledger.yaml`；未完成许可/来源审查不得复制代码。
- 为每个 tenant/account 建立 `LegacyReferenceMap`，旧 ID 与 MailHub ref 不得跨租户解析。
- `LegacyReferenceMap.put` 对同一 `(tenant, legacy_system, legacy_id)` 的重放可以推进 state，
  但如果 `mailhub_ref`、owner 或 migration batch 发生变化会 fail closed；迁移报告应保留
  `list_for_tenant()` 的确定性快照，不允许用重跑覆盖映射冲突。
- 迁移批次启动应携带稳定 `idempotency_key`；`MigrationController.start` 会重放同一批次而不
  重复写入 `shadow_compared` 事件。sender claim/release 也必须允许安全重试，不能因为网络重试
  产生第二个 authority-change 事件或错误释放其他 owner 的租约。
- 只允许一个 sender authority。`NoDualSenderInterlock` 的生产实现必须是 PostgreSQL lease/advisory lock 或宿主等价控制面，进程内实现仅用于测试。当前 PostgreSQL 实现为 0007 `mail_sender_leases` + `PostgresSenderInterlockAdapter`，按 `tenant_id/account_ref/purpose` 原子 claim/release，并以 owner/fencing token 防止 stale worker；0008 `mail_autonomy_runs` 为自主推荐运行提供 durable 状态，0009 为 outbox 增加 `lease_expires_at`，过期的 `LEASED/SENDING` 先变为 `OUTCOME_UNKNOWN`，必须对账后才可重试；0010 为 sync/autonomy `RUNNING` worker lease 增加过期恢复，自动回到 `QUEUED` 后由新 fencing token 重试并写入审计；0011 为统一收件箱保留 bounded `attachment_count`/`is_read` 元数据；0012 为独立部署提供带 RLS/tenant advisory lock 的 `PostgresQuotaPort`；0013–0016 分别持久化 Provider subscription state、bounded provider metadata、body-free webhook route metadata 和 OAuth provider identity/credential version；0017 持久化 backfill 的 folder/label/UTC date bounds，0018 持久化 durable sync job 的 provider deletion count，0019 持久化运行中取消的 lease/fencing 收尾，0020 持久化 normalized inbound Cc/Bcc/Reply-To（不存原始 headers），0021 持久化有界 transient sync retry 状态。正文和附件字节仍不进入 MailHub 表。
- 注册激活、密码重置、MFA 和安全告警保持旧宿主事务邮件链；不能切到 MailHub 智能规则。

数据库 schema 只能由版本化 runner 执行；API 启动不会自动迁移。先做离线计划，再在受控 PostgreSQL
环境执行升级或回滚：

```powershell
cd packages/mailhub
python scripts/apply_migrations.py --dry-run --direction upgrade
$env:MAILHUB_DATABASE_URL = "postgresql+asyncpg://<secret-ref-resolved-at-runtime>"
python scripts/apply_migrations.py --direction upgrade
# 只允许回滚到已验证的目标版本
python scripts/apply_migrations.py --direction down --target 5
```

runner 会取得 PostgreSQL advisory lock、校验已应用 migration checksum，并将每个 migration 与 ledger
记录放入同一事务；不支持 SQLite 替代，也不会打印连接字符串或凭据。

生产 host lease endpoint（若采用 HTTP adapter）必须提供：

```text
POST /v1/mail-host/migration/sender-lease/acquire
POST /v1/mail-host/migration/sender-lease/release
```

请求至少包含 `tenant_id`、`account_ref`、`purpose`、`owner`；冲突返回 409 或
`{"acquired": false}`，任何缺字段/非布尔结果都 fail closed。宿主启动时执行
`PostgresSenderInterlockAdapter.check()` 或等价数据库自检，证明租约表可达且作用域正确。

## 1. Sandbox → isolated mailbox → shadow

1. 在 MailHub Sandbox 重放无敏感 fixture，运行 `python -m pytest packages/mailhub`，核对 digest、thread、cursor、outbox、outcome unknown 和撤权。
   先运行 `packages/mailhub/scripts/verify.ps1`；该脚本会验证 format/lint/mypy、OpenAPI 导出、迁移静态合同与 upgrade dry-run、以及 Agentctl manifest。
2. 用隔离测试邮箱建立 Provider connection；验证 CredentialBroker/KMS、撤权、对象存储 TTL、DLP/AV/HTML 安全链。
3. CAACTRAINING：只把允许的 outreach 行为映射到 `MailHub` draft/outbox；campaign 语义和收件人业务规则留在 CAA 宿主。
4. AeroLink：仅把 `EMAIL` delivery 分支映射到 MailHub operation；WEBHOOK/SOCKET/business outbox 与 worker 生命周期留在 AeroLink。
5. 双写 shadow，不发送第二份邮件。使用 `compare_shadow` 记录 identity/cursor/content hash/attachment/business link/failure 状态；任何差异进入人工处置。

## 2. Single-sender cutover

- 先冻结旧 sender，取得旧 worker 退出与 in-flight drain 证据。
- 原子取得 tenant/account interlock；启用 MailHub worker，旧 transport 只能读/查询。
- 发送命令沿用稳定的 action/delivery idempotency key；CAACTRAINING snapshot 会把收件人 envelope
  与内容 digest 纳入 key，宿主必须在接受副作用前持久化 key/result 关联。
- 先切一个 tenant/account 和低风险域，观察 queue age、dead-letter、outcome unknown、Provider quota、recipient diff。
- L3B 保持 feature flag 关闭，直到单独的真实审批、误发、提示注入和 outage 演练通过。

## 3. 回滚

- 停止 MailHub 新出站，保留已发送 operation/provider request ref，禁止盲重试 `outcome_unknown`。
- 运行 Provider reconciliation；无法确认的 operation 进入人工队列，不直接切回旧 sender 重发。
- 释放 MailHub interlock，恢复旧 sender authority，旧宿主继续使用 legacy ID map。
- 记录 `mail.migration.authority_changed` 与 `rollback_completed`，保留 shadow diff、审批和队列证据。

## 4. 完成证据

需要同时具备：固定 commit/path/source ledger、测试邮箱 7 天稳定运行、撤权阻断、shadow 差异归零、no-dual-sender 并发/网络分区/worker crash 演练、备份恢复后 cursor/idempotency/approval refs 一致、业务回归和可执行回滚。缺任一项只能标记 `pilot`，不能宣称已经替换旧邮件模块。
