# MailHub Gmail / Microsoft Graph 真实 Provider 激活手册

状态：`preflight`（2026-07-29）
适用范围：M2 Gmail 真实只读接入、M3 Microsoft Graph 真实只读接入
适用宿主：CAPlatform，以及实现同一 Host Port 合同的其他项目

本手册取代“暂缓”描述，但不把代码合同、Sandbox 或离线预检当成真实
Provider 完成证据。每个阶段必须留下可复核的配置摘要、审计事件、脱敏日志、
测试结果和负责人签字；没有真实隔离账号时只能停在 A0/A1。

## 安全边界

- M2/M3 首次激活固定为只读：`MAILHUB_GMAIL_READ_ONLY=true`、
  `MAILHUB_MICROSOFT_GRAPH_READ_ONLY=true`；发送能力和 Provider push 能力必须分别
  通过发布审批，不能随连接开通而隐式开启。
- A0 还会拒绝 Gmail `gmail.send/modify/compose/insert/settings.*` 与 Graph
  `Mail.Send/Mail.ReadWrite/Mail.Manage/Mail.FullAccessAsUser` 等写权限 scope；仅把
  `read_only` 设为 `true` 不能掩盖过度授权。
- 即使部署者绕过 A0，MailHub 默认 OAuth host registration 仍会再次要求必需的只读 scope，
  并拒绝写 scope 或 `read_only=false`；预检是运维诊断，不是唯一安全边界。
- OAuth client secret、authorization code、access token、refresh token、client state
  只允许出现在宿主 Credential Broker/Secret Manager。MailHub 只保存短期 state 绑定、
  `credential_ref` 和非敏感账号元数据。
- MailHub 不接受邮件正文作为系统指令；同步正文与附件仍受对象存储、AV/DLP、权限、
  TTL 和知识审核合同约束。
- 任何阶段失败都保持连接 `pending_authorization`/`reauthorization_required` 或
  `degraded`，不得用 Sandbox、fixture、录制 HTTP 响应替代真实 Provider 证据。

## 阶段与退出门槛

| 阶段 | 目标 | 必须完成 | 退出证据 |
| --- | --- | --- | --- |
| A0 配置预检 | 校验非秘密配置 | OAuth app 环境隔离、redirect allowlist、PKCE/state 签名 Secret、Host Identity/Credential Broker/KMS 端点、只读开关 | `python packages/mailhub/scripts/provider_preflight.py --provider all --json`，报告中不出现 secret |
| A1 授权试点 | 完成一名隔离用户的一次授权 | provider app、最小 delegated/read-only scope、同意文案、一次性 state、PKCE、redirect 绑定、state-bound scope、账号 identity 校验 | OAuth 审计 event、host exchange 结果只含 `email_address/credential_ref/granted_scopes`、token 不穿越 MailHub；返回 scope 不得超出 authorize 请求 |
| A2 首次同步 | 有界 backfill 与标准化投影 | Gmail `historyId`/profile cursor 或 Graph `/delta`、分页上限、429/5xx/Retry-After、正文/附件 metadata 预算、bounded body/content/folder/category/changeKey metadata、To/Cc/Bcc/Reply-To 标准化、幂等重放；`folder_ref`、`label_refs`、`received_after`、`received_before` 只能随 `mode=backfill` 传入并持久化 | 新增/修改/删除/撤权场景脱敏对账；过滤边界与 provider request id 可追溯；backfill 不推进 incremental cursor；Bcc 可见性、raw header 不落盘和 recipient-header projection 证据 |
| A3 变化接收与对账 | 处理推送/增量失真 | 仓库已提供 `ProviderSubscriptionPort`/HTTP adapter、`ProviderNotificationVerifierPort`/HTTP adapter、`MailboxSyncState` migration 0013、`MailSubscriptionCoordinator`/`MailWorker` 续期、notification watermark、fencing、body-free connection routing、durable receipt 去重和增量 sync job 入队，以及 Gmail/Graph 有界 notification parser；宿主仍需 Gmail Pub/Sub watch 至少每 7 天续期（建议每日）、ACK/丢通知 reconciliation，或 Graph change notification、clientState/lifecycle、`202` ACK、订阅重建/续期和 reauth action fence；定期全量/窗口对账 | receipt 去重、续期告警、失效 cursor/token reset、dead-letter 与人工处置记录 |
| A4 受控运行 | 证明稳定性和撤权 | 隔离邮箱连续运行至少 7 天；限额、同步延迟、认证失败、对象删除、跨租户负向、kill switch | 7 天 SLO/容量/安全报告；撤权后 provider 请求为零且连接自动暂停 |
| A5 安全/合规决策 | 决定是否扩大范围 | Gmail restricted scope 评估/验证与安全评审；Entra publisher/tenant consent/管理员控制；DPA/PIPL/GDPR、保留与删除 | Security/Privacy/Provider owner 签字；无签字不得 GA |
| A6 发布与迁移 | 可复用地接入宿主 | immutable build、SBOM/signature、N/N-1 合同、回滚、单 sender interlock、CAACTRAINING/AeroLink shadow | 发布审批、回滚演练、无双 sender 证明；发送能力另开独立 release |

## A0 预检

从仓库根目录或 `packages/mailhub` 目录运行：

```powershell
python packages/mailhub/scripts/provider_preflight.py --provider all --json
# A0/CI hard gate (disabled providers are not accepted as ready)
python packages/mailhub/scripts/provider_preflight.py --provider all --json --require-config-ready
```

预检是离线命令，不会调用 Gmail、Entra、Graph 或任何宿主端点。启用 Provider
时必须提供：

| 变量 | Gmail | Graph |
| --- | --- | --- |
| 开关 | `MAILHUB_GMAIL_ENABLED` | `MAILHUB_MICROSOFT_GRAPH_ENABLED` |
| 只读 | `MAILHUB_GMAIL_READ_ONLY=true` | `MAILHUB_MICROSOFT_GRAPH_READ_ONLY=true` |
| client id | `MAILHUB_GMAIL_CLIENT_ID` | `MAILHUB_GRAPH_CLIENT_ID` |
| authorization endpoint | `MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT`，主机须为 `accounts.google.com` | `MAILHUB_GRAPH_AUTHORIZATION_ENDPOINT`，主机须为 `login.microsoftonline.com` |
| redirect | `MAILHUB_GMAIL_REDIRECT_URI`（可用 `MAILHUB_GMAIL_REDIRECT_URIS` 声明完整 allowlist） | `MAILHUB_GRAPH_REDIRECT_URI`（可用 `MAILHUB_GRAPH_REDIRECT_URIS` 声明完整 allowlist） |
| scope | `https://www.googleapis.com/auth/gmail.readonly` | `Mail.Read offline_access`（通常还需 `User.Read`） |
| 租户 | — | `MAILHUB_GRAPH_AUTHORITY_TENANT`：CAPlatform Beta 必须绑定隔离 Entra tenant UUID；`common/organizations/consumers` 留到多租户安全评审后 |

启用 `MAILHUB_GMAIL_PUSH_ENABLED` 或 `MAILHUB_MICROSOFT_GRAPH_PUSH_ENABLED` 时，还必须配置全局
`MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT` 与 `MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT`；
两者必须是无凭据/query/fragment 的 HTTPS base URL（本地开发仅允许 loopback HTTP）。
前者由宿主负责 provider watch/subscription 的创建、续期和撤销，后者负责 Gmail Pub/Sub OIDC 或
Graph clientState/证书验证及 connection 路由；MailHub connector 不直接接收未经验证的 callback。

配置了 plural redirect allowlist 时，单值 `*_REDIRECT_URI` 必须属于该 allowlist，且每个条目都必须
是无 query/fragment 的 HTTPS（开发环境仅允许 localhost/127.0.0.1/::1 HTTP，并可带显式开发端口）；
公网 HTTPS 回调不得带显式端口。A0 会逐项 fail closed。

另外必须有 `MAILHUB_OAUTH_STATE_SIGNING_SECRET`（至少 32 bytes）、
`MAILHUB_CREDENTIAL_BROKER_ENDPOINT`、`MAILHUB_HOST_IDENTITY_ENDPOINT` 和
`MAILHUB_KMS_KEY_REF`。预检输出只显示 `<set>/<missing>` 与计数，不显示值。

`ready` 字段为兼容旧调用保留：关闭 Provider 时可能为 `true`，但 `readiness=disabled` 且
`config_ready=false`。A0/CI 应使用 `--require-config-ready`，这样关闭或缺配置的 Provider
都会返回退出码 `2`。当前恢复核验在临时启用 Gmail/Graph 开关、但未注入任何值时返回退出码 `2`：两 Provider
均 `ready=false`，缺少 OAuth 注册、Host Identity、Credential Broker、KMS 和只读 scope；
`network_access=false`。这是预期的 fail-closed 环境门禁，不是 Provider 失败，也不产生真实
授权证据。默认未启用时报告会显示 `provider_disabled` 且 `readiness=disabled`，同样不能当作
已接入；`readiness=config_ready`/`config_ready=true` 也只表示非秘密配置门通过，报告中的
`real_provider_evidence` 仍为 `false`，必须继续执行 A1–A4。

CAPlatform BFF 使用同一组非秘密注册变量提供 `GET /api/mail/oauth/providers`、
`POST /api/mail/oauth/{provider}:authorize` 和 `POST /api/mail/oauth/{provider}:callback`。
浏览器只能提交现有连接的 `connection_id/expected_revision` 或一次性 `state/code`；
endpoint、client id、redirect、scope 由 BFF 固定注入，回调响应会移除
`credential_ref`。BFF 的 `MAILHUB_*_REDIRECT_URI` 必须与 Web 注册回调 URL 完全一致；
未配置或未通过 readiness 的提供方返回 `mailhub_oauth_not_ready`，不得降级到 fixture。

### A2 真实同步证据采集器

仓库提供 `scripts/run_provider_activation.py`，只通过已部署的 MailHub HTTP API
执行一次有界同步和同一幂等键重放。它不接受 provider access/refresh token，凭据仍由
宿主身份和 Credential Broker 注入；在读取连接前会要求 `/health/ready` 返回注入式
`status=ready`，因此默认 Sandbox、开发内存图或 degraded runtime 不能被当作真实 Provider。
必须同时显式传入 `--confirm-real-provider` 和
`MAILHUB_ACTIVATION_ALLOW_NETWORK=true` 才会发出网络请求。

```powershell
$env:MAILHUB_ACTIVATION_ALLOW_NETWORK = "true"
python packages/mailhub/scripts/run_provider_activation.py `
  --provider gmail `
  --mode backfill `
  --folder-ref INBOX `
  --received-after 2026-07-01T00:00:00Z `
  --received-before 2026-07-08T00:00:00Z `
  --api-url https://mailhub.example.test `
  --tenant-id <tenant> `
  --subject-id <isolated-subject> `
  --connection-id <active-connection-uuid> `
  --evidence-root provider-activation `
  --confirm-real-provider
```

也可通过 `MAILHUB_ACTIVATION_*` 环境变量提供非秘密参数；可选的宿主
`MAILHUB_ACTIVATION_AUTHORIZATION` 只用于 HTTP 请求，绝不会写入 evidence。
脚本先重新执行仓库内离线 A0 门禁；未达到 `config_ready=true` 时不会访问 MailHub。通过后生成
`preflight.json`、`sync-reconciliation.json` 与 `test-output.txt`，其中 readiness、tenant/subject/cursor
只保留 hash，`fetched/saved/duplicate/deleted` 只保留有界计数，正文、邮箱地址、token 和原始
Provider response 均不会落盘。HTTP API
必须返回真实 durable sync job；脚本会轮询到 terminal 状态，并用相同 idempotency key
重放一次，重放得到不同 `job_ref` 时失败。使用 `--mode backfill` 时，脚本将只提交
上述有界 folder/label/date 参数，并把过滤摘要写入证据；部署方还必须核对本次 backfill
没有推进该账号的 incremental cursor。未提供过滤参数时，仍应显式确认运行目的和消息上限，
不得把默认 `INBOX` 误认为 Provider 全量估算。

同步作业取消也必须按 durable 状态观察：queued 作业可立即进入 `cancelled`；已由 Worker
claim 的作业先进入 `cancelling`，保留 lease/fencing，并在 Provider 返回、投影循环或 cursor
提交前的安全检查点结束为 `cancelled`。租约过期的 `cancelling` 作业只允许完成取消收尾，不能
重新排队访问 Provider。取消状态和对应的错误/审计 metadata 应写入 A2 证据包。

### A1 OAuth 脱敏证据采集器

OAuth 证据不能由人工拼接布尔值或上传任意 JSON。完成真实授权、同一 state 的故意重放
（应被拒绝）、Host credential refresh 和撤权后，运行独立采集器从 owner-scoped
`GET /v1/mail/audit` 读取 MailHub 审计账本：

```powershell
$env:MAILHUB_ACTIVATION_ALLOW_NETWORK = "true"
python packages/mailhub/scripts/collect_oauth_evidence.py `
  --provider gmail `
  --environment test `
  --api-url https://mailhub.example.test `
  --tenant-id <tenant> `
  --subject-id <isolated-subject> `
  --connection-id <connection-uuid> `
  --run-id <run-id> `
  --evidence-dir provider-activation/gmail/test/<run-id> `
  --confirm-real-provider
```

采集器只接受已认证 MailHub API 的审计记录，要求 scope/PKCE/redirect、同一 OAuth flow
的 replay rejection、refresh 和 revoke 事件，以及 provider account/tenant hash；不接受
access/refresh token、authorization code、credential ref 或手工 JSON。撤权后连接不再是
active，所以该命令独立于 A2 同步运行器执行；失败时不生成 `oauth-consent.json`，不能把
缺观察当作 A1 通过。采集完成后仍需用 `validate_provider_bundle.py --require-real` 做
跨文件身份、run-id、scope 和真实网络证据校验。

### A3/A4 通知与对账脱敏证据采集器

真实 watch/change notification 已连续运行至少 168 小时，并完成一次续期、同一通知的
重复投递、正向 ACK 以及一次 `reconcile` 作业后，使用独立采集器：

```powershell
$env:MAILHUB_ACTIVATION_ALLOW_NETWORK = "true"
python packages/mailhub/scripts/collect_notification_evidence.py `
  --provider gmail `
  --environment test `
  --api-url https://mailhub-test.example `
  --tenant-id <tenant> `
  --subject-id <subject> `
  --connection-id <uuid> `
  --run-id <run-id> `
  --evidence-dir provider-activation/gmail/test/<run-id> `
  --confirm-real-provider
```

采集器只读取 owner-scoped `sync-state`、receipt 和 audit API，要求 MailHub 账本中存在
续期、duplicate、ACK p95 和已完成 reconciliation；它从真实事件时间戳计算观察窗口，
拒绝未来或不足 168 小时的窗口，不接收 callback body、clientState、token 或人工填写的
计数。缺观察时不生成 `webhook-receipts.json`，不能把空通知窗口冒充 A3/A4 运行证据。
采集后仍必须执行完整 bundle validator；撤权零访问与 Host Credential Broker 计数必须
由独立撤权演练和 Host 报告证明。

### A4 撤权负向证据采集器

完成 `:revoke`、确认后续同步请求被拒绝并保存 Host 的零访问报告，再按批准的清理流程
执行 `:delete`，最后使用：

```powershell
$env:MAILHUB_ACTIVATION_ALLOW_NETWORK = "true"
python packages/mailhub/scripts/collect_revocation_evidence.py `
  --provider gmail `
  --environment test `
  --api-url https://mailhub-test.example `
  --tenant-id <tenant> `
  --subject-id <subject> `
  --connection-id <uuid> `
  --run-id <run-id> `
  --evidence-dir provider-activation/gmail/test/<run-id> `
  --confirm-real-provider
```

采集器只观察 owner-scoped connection/audit API，不执行撤权、删除、重试或 Provider 请求，
不接受任意 JSON、token、credential ref 或手填计数。它要求 `credential_revoked`、
`subscription.cancelled`、`sync.rejected_after_revoke`（Provider 与 Host broker 计数均为零）、
`connection.deleted` 的 bounded deletion proof；缺少任一审计记录即失败。该命令允许
清理后的 `connection_status=deleted`，仍由 bundle validator 交叉绑定身份和环境。

### A3 订阅状态与撤权合同（仓库侧）

仓库现在把 `mail_sync_cursors` 扩展为 `MailboxSyncState`（migration `0013`）：同步
cursor 与 subscription ref/status/expiry、回调端点、opaque client-state ref、provider
request ref、watermark 和 fencing 在同一租户范围内持久化。`MailSubscriptionCoordinator` 提供：

- `ensure`：调用宿主 `ProviderSubscriptionPort`，按旧 subscription ref 做 fencing，成功后
  发布 `mail.subscription.created` 或 `mail.subscription.renewed` 的脱敏事件；
- `renew_if_due`：只在 expiry window 内由外部 scheduler 调用，使用稳定幂等键，不能把 API
  进程内计时器当作 durable scheduler；
- `record_notification`：签名/OIDC/clientState 和 receipt 去重通过后才推进 watermark，
  subscription 不匹配或乱序通知 fail closed；已知 Graph `reauthorizationRequired`、
  `subscriptionRemoved`、`missed` 由本地分类器转成有界 lifecycle disposition，未知值只保留
  脱敏 metadata，不得触发未经审计的状态转换；
- `mark_failed`/`mark_expired`：续期失败和过期分别落为 durable status，并发布
  `mail.subscription.failed`/`mail.subscription.expired` metadata，供告警和人工处置；
- `cancel`：撤权/删除前取消 host subscription，保留 bounded 状态和审计；宿主取消失败时
  连接不会被标记为 revoked/deleted。

仓库还提供 owner-scoped 运维路由：

- `GET /v1/mail/connections/{connection_id}/sync-state` 查看 cursor、subscription
  expiry/status 和 notification watermark；不返回 token、clientState 原文或邮件正文。
- `POST /v1/mail/connections/{connection_id}/subscription:ensure` 创建/续租，要求
  `Idempotency-Key`、HTTPS callback 和未来 expiry。
- `POST .../subscription:renew` 作为人工/调度补偿入口；`renewal_window_hours` 限制在
  1–168 小时。
- `POST .../subscription:cancel` 作为撤权/删除前的显式取消入口，要求独立幂等键。

Provider callback 入口为 `POST /v1/mail/provider-notifications/{provider}`。它不接受
`tenant_id`、`subject_id` 或 `connection_id` 请求字段；注入的 Host
`ProviderNotificationVerifierPort` 必须先完成 Gmail Pub/Sub OIDC 或 Graph
clientState/证书验证，并返回 owner-scoped、body-free delivery。MailHub 随后按
`notification_id` 写 append-only receipt，推进匹配 subscription watermark，并以
`provider-notification:{notification_id}` 幂等键创建 `incremental` sync job。Graph
`reauthorizationRequired` 会把连接 fenced 为 `reauthorization_required`、订阅 lease 置为
`renewal_required` 并返回 blocked disposition，不排队只能失败的 Provider job；
`subscriptionRemoved`/`missed` 会把连接置为 `degraded`（不降低已有重授权状态）并创建
`reconcile` job。终态 revoked/deleting/deleted 连接只记录审计，不会被迟到回调重开。该路由只返回
receipt/queue 元数据；正文、raw callback、OIDC claims、clientState 和 token 不进入 MailHub
持久化。Host HTTP adapter 会过滤 `Authorization` 等敏感入站 header，只发送 bounded
base64 body 与允许的 callback metadata 给宿主 verifier。

已实现的 receipt 运维重放入口为
`POST /v1/mail/webhooks/receipts/{provider}/{event_id}:replay`。它只使用
`0015_mailhub_webhook_route_metadata.sql` 中的 bounded、body-free owner route
metadata，要求 `mail.audit` entitlement、原始 `body_sha256` 和独立
`Idempotency-Key`，校验当前 subject/connection scope 后仅创建 `reconcile`
sync job。原 receipt 仍 append-only，不重新保存 callback body；旧的无 route
metadata receipt 继续可查询但 fail closed 不可重放。丢弃/重新路由、真实宿主持久
化与运行告警仍由部署验收。

OAuth callback 的 Host exchange 还可返回 `provider_account_id`、
`provider_tenant_id`（Graph 企业租户）和正整数 `credential_version`；MailHub
通过 migration `0016_mailhub_connection_identity.sql` 持久化这些非秘密身份事实，
用于刷新凭据的账号/租户绑定。token、refresh token 和 client secret 仍只留在 Host
Credential Broker；缺少真实 identity/refresh/revoke 运行证据时，M2/M3 条目仍不勾选。
OAuth state store 同时保存 authorize 阶段的非秘密 `requested_scopes`；callback 会要求
Host 返回的 `granted_scopes` 是该集合的子集，且 Gmail/Graph 必需的只读 scope 存在，
任何写 scope 或 scope 漂移都会 fail closed。这样即使 Host exchange 响应异常，也不能在
callback 阶段把只读连接升级为写权限。直接调用 Core service 的 connection
create/activate/reauthorize 也复用该只读 scope gate；Gmail/Graph 的旧 state 记录若缺少
`requested_scopes` 会拒绝消费，而不是隐式按 legacy scope 放行；authorize 请求本身也必须
包含 provider 必需的只读 scope，不能只请求 `openid` 后再等待 callback 补齐权限。
重授权必须在 `:authorize` 请求中绑定现有 `connection_id` 与
`expected_revision`；callback 只更新同一连接，账号/租户 identity 变化或 credential
version 回退会 fail closed。
连接级 refresh 使用 `POST /v1/mail/connections/{id}:refresh`，由 Host
`CredentialRefreshPort` 调用自身 OAuth refresh-token/Secret Manager 流程；返回值只允许
`credential_ref`、账号/租户 identity 和 `credential_version` 等 metadata，MailHub 以
revision/identity fencing 写回同一连接。测试验收必须同时覆盖 refresh 失败、token-shaped
响应、版本回退和账号/租户漂移；真实 Provider refresh/revoke 成功与撤权后访问为外部证据。

撤权验收必须先调用 `POST /v1/mail/connections/{id}:revoke`（CAPlatform BFF 与 SDK
提供同名投影）并绑定当前 `expected_revision`；对 Gmail/Graph/IMAP 等真实 Provider，
MailHub 会在把连接标记为 `revoked` 前调用 Host `CredentialRevocationPort`，只接受
`revoked`/`already_revoked`/`accepted`/`revoke_requested` 等有界状态证明，并拒绝
token-shaped 或 credential-ref-shaped 响应。Host 撤权或订阅取消失败时连接保持可见的
非终态，不能伪报撤权成功。成功后保留连接和同步状态投影，供值班人员核对
`revoked`、订阅取消、后续 sync 请求被拒绝以及 Host Provider request/credential broker
计数为零。只有这些负向证据保存后，才允许调用独立的 `:delete` 清理流程；已 revoked
连接的 delete 重试不会再次调用 Host 撤权。不能用“删除成功”反推撤权后的零访问，也不能
把 Sandbox 的跳过调用当作真实 Provider 撤权证据。

这些路由只调用 Host `ProviderSubscriptionPort`，不直接接触 Gmail/Graph token；Host
仍须在固定 callback 上完成 OIDC/clientState 验证、receipt 去重和异步 reconciliation。

该闭环只证明本地状态机、迁移和 Host Port 合同；尚未证明真实 Gmail watch、Graph
subscription、连续 reconciliation、expiry 告警或 7 天运行。真实执行仍须使用隔离邮箱、宿主
Credential Broker/Identity/KMS 和 A3 运行报告，不能用 fixture 替代。

## Provider 验收要点

### Gmail（M2）

1. 先用 `gmail.readonly` 完成一名隔离账号授权；该 scope 属于 restricted scope，
   是否需要 Google OAuth verification/security assessment 必须由 Security/Privacy
   按实际数据处理方式确认。
2. 初始同步使用有界 `messages.list`/`messages.get`；增量同步使用 `history.list`。
   `404 historyId` 必须进入有界重建并标记 `reset_required`，不能当成空同步。
3. 推送阶段才启用 Pub/Sub watch：快速 ACK、保存 historyId、处理过期续期与 dropped/
   delayed notification，并以定期 reconciliation 兜底。Gmail watch 必须至少每 7 天续期一次，
   运行手册按每日续期窗口调度；同一用户每秒通知超过 Provider 限制时可能丢弃，因此不能把
   Pub/Sub receipt 当作完整变更证明。watch 续期和订阅凭据不属于 A1/A2 的只读授权完成条件。

参考官方合同：[Gmail sync](https://developers.google.com/workspace/gmail/api/guides/sync)、
[Gmail push notifications](https://developers.google.com/workspace/gmail/api/guides/push)、
[Gmail error handling](https://developers.google.com/workspace/gmail/api/guides/handle-errors)、
[Gmail OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)、
[OAuth 2.0 web-server](https://developers.google.com/identity/protocols/oauth2/web-server)。

### Microsoft Graph（M3）

1. 首期只使用 delegated permissions；按测试矩阵选择个人账号、`organizations` 或
   明确 tenant id，不把 application permission 或 shared mailbox 默认为可用。
2. 初始和增量同步使用 Inbox message delta；必须保存并验证 `@odata.nextLink` 与
   `@odata.deltaLink`，`404/410` 失效时只允许一次有界 reset，然后进入对账。
   有界 backfill 的 `folder_ref` 使用受限 folder id，`label_refs` 只接受
   `category:<name>` 并生成安全的 categories 谓词；无法安全表达的自由标签必须 fail closed。
3. 推送阶段使用 change notification、clientState/lifecycle notification、订阅过期
   续期和恢复；订阅数量、邮箱类型、租户管理员同意和 throttling 需要单独记录。
   Graph lifecycle endpoint 必须返回 `202` 并先完成真实性校验：`reauthorizationRequired`
   进入显式重授权/续期队列，`subscriptionRemoved` 先重建订阅再用 delta 补齐窗口，`missed`
   触发资源级 delta/full reconciliation。宿主不得在同一 subscription 的 10 分钟窗口内并发
   `/reauthorize` 与更新请求；重授权与续期动作要用同一 durable action fence。
   生命周期通知可能没有 `resource`，只能按 `subscriptionId` 记录状态事件并触发
   reconciliation；不能把它当作某封邮件已变更。解析器对此生成
   `subscription:<subscriptionId>` 的脱敏资源引用。

参考官方合同：[delegated user auth](https://learn.microsoft.com/en-us/graph/auth-v2-user)、
[authorization code/PKCE](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)、
[message delta](https://learn.microsoft.com/en-us/graph/delta-query-messages)、
[change notifications](https://learn.microsoft.com/en-us/graph/outlook-change-notifications-overview)、
[lifecycle events](https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events)。

### 统一限流、权限与服务端错误处置

Connector 只保留脱敏后的 Provider 状态码和有限 machine-readable reason，不保存原始
Provider error/message/body：Gmail 的 `rateLimitExceeded`、`userRateLimitExceeded`、
`dailyLimitExceeded`/`quotaExceeded` 以及 Graph 的 429/`TooManyRequests` 统一为
`rate_limited`，解析数值或 HTTP-date `Retry-After` 并交给有界退避；Gmail/Graph 的已知
权限或凭据 reason 统一为 `permission_denied`，同步连接进入
`reauthorization_required`；其余 4xx 保留具体 HTTP 状态，便于人工分流。5xx 在只读同步中
标记为可重试 Provider failure；任何未来写操作的 5xx 必须标记 `outcome_unknown`，先对账再决定
是否重试。同步失败的审计、事件和 telemetry 只追加这些有限字段，便于告警/退避调度而不暴露
Provider 原文。该分类已有 MailHub connector/service contract tests，但真实账号的配额、权限和退避窗口仍须
在 A2/A3 证据包中验证，不能用本地测试替代。只读 GET transport 最多做 3 次有界尝试，遵循
`Retry-After`（单次等待上限 30 秒）或指数退避；POST/send 不复用该重试，避免把结果未知误当作安全重试。

HTTP 层耗尽尝试后，durable `MailSyncJob` 只把 `rate_limited`、网络不可用和 Provider 5xx
转为 `retry_wait`：最多 5 次总尝试，优先使用脱敏 `Retry-After`（上限 1 小时），否则从 30 秒起
指数退避；worker lease 会释放，scheduler 仅在 `next_attempt_at` 到期后重新 claim。权限、
scope、撤权、4xx 业务错误和无效响应不进入 retry，保持 `failed`/重新授权处置。`retry_wait`
也可在到期前取消，取消会清除后续 Provider 访问；retry/cancel/lease fencing 的记录应纳入
A2 证据包。该状态机与 migration `0021_mailhub_sync_retry.sql` 已有本地 Memory/SQLAlchemy
合同测试，真实 Provider 的退避窗口和连续运行仍必须由隔离邮箱验证。

参考官方合同：[Gmail error handling](https://developers.google.com/workspace/gmail/api/guides/handle-errors)、
[Graph throttling](https://learn.microsoft.com/en-us/graph/throttling)。

## 证据包格式

每个 Provider、环境和隔离邮箱建立一个不可变 evidence bundle：

其中 `oauth-consent.json`、`webhook-receipts.json` 和 `revocation-negative.json` 必须携带
相同的 `provider`、`environment`、`run_id` 以及脱敏后的 account/tenant identity hash；
校验器会拒绝跨环境或跨运行串包。

`oauth-consent.json` 还必须记录 `requested_scopes` 与 `granted_scopes` 的实际字符串列表。
校验器独立确认 Gmail `gmail.readonly` 或 Graph `Mail.Read` + `offline_access` 存在、granted
是 requested 的子集，并拒绝 Gmail/Graph 写权限；布尔字段不能替代 scope 列表。

`webhook-receipts.json` 至少要有一次续期和一次 receipt，`duplicate_count` 不能超过
`receipt_count`，ACK p95 不能为零，并记录 `observation_started_at`/
`observation_finished_at`；校验器计算观察窗口必须至少 168 小时。空窗口或短跑不能充当
A3/A4 连续运行证据。

```text
provider-activation/<provider>/<environment>/<run-id>/
  preflight.json                 # 已脱敏
  oauth-consent.json             # scope、tenant、redirect、时间、结果
  sync-reconciliation.json       # counts/digests/cursors，不含正文
  webhook-receipts.json          # receipt id、dedupe、renewal、latency
  revocation-negative.json       # 撤权后请求为零与连接状态
  security-review.md             # scope、DLP/AV、保留、删除、签字
  test-output.txt                # 机器可读命令结果、network_access、退出码
```

`security-review.md` 仍可包含人工备注，但必须包含以下无正文、无个人姓名的机器字段：
`schema_version=mailhub.provider_security_review.v1`、匹配的 `provider`/`environment`/`run_id`、
`decision=approved`、`scope_reviewed=true`、`dlp_av_reviewed=true`、`privacy_reviewed=true`、
`retention_reviewed=true`、`deletion_reviewed=true` 和脱敏的 `signoff_ref_sha256`。缺字段、
未批准或 hash 不合法时，`--require-real` 必须失败。

生成 `sync-reconciliation.json` 后，先执行脱敏证据验收：

```powershell
python packages/mailhub/scripts/validate_provider_evidence.py provider-activation/gmail/test/<run-id>/sync-reconciliation.json --require-real
```

`--require-real` 只接受成功的 `real_api_observation`，并检查注入式 readiness、active
connection、账号 identity hash（Graph 还必须有 tenant identity hash）、durable sync、幂等 replay
和敏感字段不存在；校验器还使用 closed-world schema 拒绝未知字段，避免手工补包混入未审计
信息。没有真实观察时只能不带该参数验证 `activation_gate_failure`，不能勾选 M2/M3。

完成 A1–A4 的多文件 bundle 后，再运行：

```powershell
python packages/mailhub/scripts/validate_provider_bundle.py provider-activation/microsoft_graph/test/<run-id> --require-real
```

该命令离线复核 `preflight.json`、`oauth-consent.json`、`sync-reconciliation.json`、
`webhook-receipts.json`、`revocation-negative.json`、`security-review.md` 和
`test-output.txt`，交叉核对 environment、run id、账号/租户 identity hash，并要求
`preflight` 明确 `config_ready`、Provider 已启用且 `read_only=true`；定时/拉取式波次要求
`push_enabled=false`，显式 A3 push 波次则要求 `push_enabled=true` 且两类 Host endpoint
marker 均为 `<set>`；
同时要求 scope/state/PKCE、通知去重与对账、撤权后 Provider/Credential Broker 零访问和删除
证明。缺任一文件、跨文件身份或环境不一致、只读门不成立或包含未经审计字段均 fail closed。
它不会把本地合同或 gate failure 转换为真实 Provider 证据。

禁止提交 authorization code、token、邮箱正文、附件、真实收件人列表或未经脱敏的
provider response。证据必须关联 tenant、subject、connection、trace 和 provider
request id；人工截图只能作辅助，不能替代结构化对账。

## 暂停与回滚

- 任一认证失败、撤权、scope 漂移、cursor reset 超限、watch/subscription 续期失败、
  DLP/AV 不可用或跨租户负向失败：立即停用对应 Provider 开关，连接进入
  `reauthorization_required`/`degraded`，保留 receipt 与审计。
- 只读阶段不得调用发送、草稿 Provider API 或自动写回项目/知识；候选仍需宿主 review/apply。
- 若将来开启发送，必须单独建立 outbound capability、四眼审批、Kill Switch、配额、
  outcome-unknown 对账和 sender interlock；不得把 `read_only=false` 当作上线批准。
