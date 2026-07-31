# MailHub 智能邮件管理模块实现状态

更新时间：2026-07-30（Asia/Shanghai）

> **工程 Beta 完成记录（2026-07-30）**：MailHub 本地/宿主闭环提交 `41f1204` 已通过普通
> `git push` 更新到 `origin/codex/mailhub-caplatform-beta`；提交范围未包含并行的业务事件中心、
> Work 页面及其文档。当前不创建 PR、不等待 GitHub Actions。M2/M3、真实知识源发布、生产
> Secret Manager、AV/DLP/rights 和真实 Provider E2E 仍按后续激活门单独记账。

> **CAPlatform 本地闭环增量（2026-07-30）**：浏览器不再提交任意 approval ref；BFF 在
> 用户确认 apply 后签发 tenant/subject/candidate/revision 绑定的短期 opaque reference，MailHub
> 首次 verify 时再绑定确定 action id/input digest。任务候选通过 Agentctl
> `ca.project.create_task` 的显式确认 preflight/invoke 执行，CAPlatform 加密 SQLite action ledger
> 对完成结果做幂等重放并对冲突 fail closed。知识候选仅在 Sandbox/受控导入环境通过：安全端点
> 拒绝原始正文、MIME 和 credential-shaped 字段，签发 scope/candidate/content 绑定且有时效的
> encrypted gate，随后进入 CAPlatform 加密、幂等的“已审批知识候选台账”。该台账不是已发布
> KnowledgeSource；production/staging 或显式启用 Gmail/Graph 时仍要求外部 AV/DLP/rights scanner。
> 最新门禁（排除未纳入本提交的业务事件中心测试）：MailHub 333、BFF 137、根项目 230、Web 10 tests；Web typecheck/build、Ruff、strict
> mypy、迁移/OpenAPI/provenance/license 与 Agentctl manifest validate 通过。GitHub Actions 不属于
> 当前门禁，Gmail/Graph 外部激活继续延后。

> **CAPlatform Beta durable-runtime 增量（2026-07-30）**：BFF Host Credential Broker
> 已具备一次性 OAuth state、Provider code exchange、加密 token、refresh/revoke 与
> tenant/subject/provider identity fencing；真实 Provider readiness 同时要求 client secret、独立
> credential encryption secret 和 Host service token。`mailhub.runtime.create_durable_app` 现作为
> production/staging 装配器，只使用 PostgreSQL、带服务鉴权的 Host Ports 和显式 Gmail/Graph
> connector，不允许 Sandbox/内存 fallback；镜像安装 asyncpg 并包含迁移资产，迁移仍为显式
> operator step。最新本地门为 MailHub 333、BFF 133、根项目 226、deployment 93、Web 10 tests，
> 静态检查和 Web build 通过，Agentctl validate/apply 通过。真实 Provider hard preflight 因两者
> disabled、无 app/隔离账号而 exit 2，故不产生 A1/A2 或 Beta 完成声明。
> MailHub 范围已形成可回退提交 `4460597`；业务事件中心/Work 页面等非邮箱工作区改动未混入。

> **范围声明**：M2 Gmail 真实只读接入与 M3 Microsoft Graph 真实只读接入的代码和激活手册保留，
> 真实 OAuth/邮箱执行延后，不阻塞当前工程 Beta。仓库已有只读默认 transport、OAuth state/PKCE 宿主边界、
> Credential Broker handoff、配置预检和 cursor 合同；没有真实 OAuth、真实邮箱、watch/change
> notification、长期 reconciliation 或 7 天运行证据，连接中心仍必须标记为“演示连接 · 未验证”，
> 不得宣称 GA。

> **CAPlatform B1 Host Credential Broker 增量（2026-07-30）**：BFF 新增服务鉴权的
> `/v1/mail-host/oauth/state`、`state/consume`、`oauth/exchange` 和
> `credentials/resolve|refresh|revoke`；本地/受控 Beta adapter 对 state 和 token 分别做
> SHA-256 索引/一次性消费与 Fernet 加密，绑定 tenant/subject/provider/redirect/scope，自动轮换
> 即将过期的 access token，审计只保留 credential-ref hash 和有界元数据。Gmail 使用官方 token、
> profile 和 revoke endpoint；Graph 使用 tenant-specific token endpoint 与 `/me`，撤权销毁宿主
> 唯一 token 副本，tenant/user consent revoke 仍需单独 operator evidence。新增 BFF 4 项 broker/
> 路由/secret-readiness 测试，BFF 全量基线随后更新为 133 passed；MailHub Host adapter 允许安全的 Bearer
> header 并新增生产 `host_service_token` 门。上述均为代码/合同证据，真实 Google/Entra app、
> 隔离邮箱、生产 Secret Manager 和 Provider 运行 bundle 仍未完成。

> **最新本地修复（2026-07-29）**：真实 Provider 的 `:revoke` 已在写入 `REVOKED` 前调用
> Host `CredentialRevocationPort`，Host 撤权失败、缺 Port 或秘密形响应均 fail closed；已
> revoked 连接的 `:delete` 不重复撤权。MailHub 包回归为 307 tests，细节见
> [`mailhub-revoke-host-boundary-2026-07-29.md`](reports/mailhub-revoke-host-boundary-2026-07-29.md)。

> **当前验证口径**：本轮之后 MailHub 包完整回归为 333 tests；文档下方保留的
> BFF/根项目与更早 MailHub 计数均为历史基线，不代表当前最新总数。

> **A3/A4 通知证据采集增量（2026-07-30）**：Provider notification 路由现在追加
> body-free duplicate 与 ACK latency 审计事件；新增 `scripts/collect_notification_evidence.py`
> 只从 owner-scoped sync-state、receipt 和 audit API 生成 `webhook-receipts.json`，要求真实
> renewal、重复通知、正向 ACK、reconcile 完成和至少 168 小时的事件时间窗。采集器不接收
> callback body、clientState 或手工计数；SQL audit projection 也统一为与内存仓储相同的
> 平坦事件形状。通知 API 不再返回 host-owned client-state 原文。当前 MailHub 包为 327 tests，
> 真实 A3/A4 仍未运行。

> **A4 撤权证据采集增量（2026-07-30）**：新增 `scripts/collect_revocation_evidence.py`，
> 只观察 owner-scoped connection/audit API，要求 credential-revoked、订阅取消、撤权后
> sync rejection、Host broker/provider 零访问计数和 bounded deletion proof；它不执行
> revoke/delete，也不接受 token、credential ref 或手工计数。bundle validator 允许清理后的
> `connection_status=deleted`，但仍要求所有负向审计事实。当前真实 Host/Provider 撤权报告
> 尚未提供，故 M2/M3 与 A4 仍未完成。

## 2026-07-30 远端状态（非当前门禁）

- 已推送分支：`codex/mailhub-caplatform-beta`；MailHub 基线提交 `4460597`，证据文档提交
  `e6a8842`。
- GitHub Actions `quality` run `30477369954` 未执行任何测试步骤：5 个 Job 均未分配 runner，
  check annotation 明确报告账户付款失败或 Actions spending limit 不足。
- 该红灯属于仓库外部计费/额度门禁，不是 MailHub、BFF、Web 或 deployment 测试失败；不修改产品代码，
  不把本地绿色冒充远端绿色。恢复 GitHub Billing & plans 后须对同一分支重新运行 `quality`。
- 不等待、不重跑 GitHub Actions；当前发布只执行普通 Git commit/push，本地门禁失败才阻止推送。
- 同步作业继续采用外部调度器持有的 `tenant_id + subject_id + job_ref` 调度合同，MailHub Worker 负责
  单个有界 claim/lease/fencing/retry 单元；不为“自动领取”增加跨租户 BYPASSRLS 扫描器。

执行优先级已调整：GitHub Actions 计费/额度问题只记为远端待复验项，不阻塞本地开发；Gmail/Graph
真实 OAuth 与邮箱激活延后。当前继续以 Sandbox/受控导入完成 CAPlatform `/mail`、任务候选、知识候选、
人工确认、宿主权威写入和本地发布门，代码仅使用普通 Git commit/push。

> **A1 OAuth 证据采集增量（2026-07-30）**：OAuth callback 审计现在绑定 scope/PKCE/
> redirect、OAuth flow digest 与 Provider identity hash；故意重放同一 state 的拒绝也会
> 以同一 flow digest 记录。新增 `scripts/collect_oauth_evidence.py` 只从 owner-scoped
> audit API 生成 `oauth-consent.json`，并要求 refresh/revoke 观察；它不接收任意 JSON、
> authorization code 或 token。当前 MailHub 包最新全量为 327 tests，真实 A1 仍未运行。

> **A0 运行时门禁更新（2026-07-29）**：定时/拉取式只读生产配置不再无条件要求 A3
> subscription/verifier endpoint；显式启用 push 时，预检与 `MailHubSettings.validate_for_runtime()`
> 都要求两类安全 Host URL。此前 A0 回归后 MailHub 包为 312 tests；这仍不产生真实 Provider 证据。

> **证据校验增量（2026-07-29）**：A1–A4 bundle validator 现在绑定 artifact 的
> `environment`/`run_id`/账号与租户 hash，并验证 A0 的 Provider enabled、只读及定时/推送
> capability 门；串包、写权限预检或缺依赖会 fail closed。该增量只强化真实证据验收，不改变当前
> `real_provider_evidence=false` 的环境状态。

> **A2 采集器边界更新（2026-07-29）**：真实同步采集器在发出网络请求前重新执行离线
> A0 `config_ready` 门，并把脱敏 `preflight.json` 写入证据目录；缺少 OAuth/Host/KMS
> 配置时 fail closed，不会生成伪真实同步包。

> **A0 redirect allowlist 更新（2026-07-29）**：`provider_preflight.py` 现在逐项验证
> `MAILHUB_GMAIL_REDIRECT_URIS`/`MAILHUB_GRAPH_REDIRECT_URIS`，并要求单值回调在 allowlist
> 内；多值配置中的恶意 query/fragment 或非 TLS 地址会阻断 config readiness。
> 显式启用 A3 push 时，Host subscription/verifier base URL 同样必须是 HTTPS（本地仅允许 loopback），
> 无凭据、query 或 fragment；定时拉取式只读不触发这两项依赖。
> 同一合同已下沉到 `MailHubSettings.validate_for_runtime()`，因此注入式生产图绕过离线预检时也会
> 对不安全 Host URL fail closed。
> 底层 `OAuthAuthorizationService` 同步拒绝回调 query、凭据、空路径和公网显式端口；loopback
> 开发回调允许显式端口，防止直接 service/API 调用绕过 A0；该 OAuth 回归属于前一轮 307 tests 基线。
> Gmail/Graph connector 的 push 能力现在由显式开关声明，但实际 watch/change-notification 仍强制经
> Host subscription/verifier Port；预检缺任一 endpoint 会 fail closed。

> **撤权安全边界更新（2026-07-29）**：真实 Provider 的 `POST /v1/mail/connections/{id}:revoke`
> 现在先取消订阅并调用 Host `CredentialRevocationPort`，只有收到有界的
> `revoked`/`already_revoked`/`accepted`/`revoke_requested` 状态证明后才写入 `REVOKED`；
> token-shaped 或 credential-ref-shaped 响应、缺少 Host Port 或撤权失败均保持非终态并
> fail closed。已 revoked 连接执行独立 `:delete` 时不会重复调用 Host 撤权。仓库回归覆盖
> 该边界，但真实 Credential Broker/Provider 计数、撤权后零访问和生产 downstream 清理
> 仍需 A1–A4 部署证据。

## 已实现

- 可复用 UI/SDK 增补：`packages/mailhub/ui` 现在提供统一收件箱筛选、metadata 搜索和 opaque cursor 加载更多；Python/TypeScript SDK 提供 scope-bound 线程 page/cursor/filter 方法。
- 项目动作边界增补：候选 `apply` 的 HostAction 请求带有 bounded candidate id/revision/type、目标 payload 和 body-free evidence；原始邮件正文不跨宿主动作边界，新增合同测试覆盖该约束。
- 项目候选去重增补：Project/Task 候选按 bounded refs/action/due-date fingerprint 检测重复，保留独立 source lineage 并标记 `duplicate_requires_review`，不自动合并跨来源任务。
- 事件运行时增补：`MailHubEventType` 注册所有生命周期/同步/候选/草稿/出站/授权/迁移事件，`build_event`/`validate_event_payload` 与 `mailhub.business_event_types.v1.json` 保持一致；未知事件名、超限 envelope、未脱敏正文/凭据形字段和无效时间戳均 fail closed，合法构建路径只发布 bounded/redacted metadata。
- Agentctl/搜索合同增补：线程读取能力转发 opaque cursor、账户和布尔筛选；搜索明确 `metadata` 覆盖范围/完整性/不完整原因，未启用独立 Provider search contract 时 `provider` mode 返回 degraded。
- 可嵌入/standalone UI 增补：连接卡片展示同步状态、最近同步、错误码；连接中心提供服务端固定注册信息的 Gmail/Graph 只读 OAuth 向导、回调状态和错误连接的 revision-bound 重新授权；统一筛选/视图使用 tab/ARIA 语义、键盘 focus-visible 和空状态 status；`@caplatform/mailhub-ui/standalone` 提供不绑定 router/session/fetch 的 `MailHubStandalone` shell，主题通过受限 CSS custom properties 注入。
- 连接权限收窄增补：MailHub core 提供 revision-fenced `POST /v1/mail/connections/{id}:scopes`，Python/TypeScript SDK、CAPlatform BFF adapter/route 与 Web client 均只允许减少本地可用 scope；任何扩权都必须回到绑定 connection/revision 的 OAuth 重授权。
- 连接影响预览增补：新增 owner-scoped `GET /v1/mail/connections/{id}/impact-preview`，通过 Core、Python/TypeScript SDK、CAPlatform BFF 和连接中心“预览影响”入口透传本地 folder watermark、消息/线程/候选/草稿/操作/对象引用计数与 project hint；消息数、知识来源和 object refs 使用无分页清理引用快照，其他列表计数保留 200 条上限并在 warnings 标明；连接中心同时支持绑定 revision 的本地 scope 收窄，扩权仍回到 OAuth 重授权。响应明确不触发 Provider I/O，远端新增/删除数量保持 unknown。
- Provider preflight 语义增补：离线报告保留兼容的 `ready` 字段，同时输出 `readiness=disabled|blocked|config_ready`、明确的 `config_ready` 硬门和恒为 false 的 `real_provider_evidence`；`--require-config-ready` 会把关闭开关或缺配置变成退出码 2，避免误读为真实 Provider 激活。
- OAuth scope drift hardening：authorize state store 记录非秘密 `requested_scopes`，authorize、Host exchange 与 API callback 都拒绝超出该集合的 `granted_scopes`、缺少 Gmail/Graph 必需只读 scope 或任何写 scope；直接 `create/activate/update-scopes/reauthorize` service path 也复用同一只读门禁，旧的无 scope real-provider state 记录 fail closed；scope 合同已补充 adapter/API/PKCE/service 测试，真实 Provider consent/refresh/revoke 仍需外部证据。
- Provider deletion projection hardening：Gmail History 的 `messagesDeleted` 与 Inbox/过滤标签的 `labelsRemoved`、Graph delta 的 `@removed` 由 connector 传递为 `ProviderSyncPage.deleted_message_refs`；同步服务在提交 cursor 前以租户/连接范围幂等删除消息、候选和规则执行记录，重算线程计数并清理正文对象，发布 body-free deletion audit/event。重复删除保持 no-op；真实 Provider 的移动/删除对账仍须外部激活证据。
- Webhook receipt 读取现在要求 `mail.audit` entitlement，不再把 tenant 内的 Provider receipt 视为普通 mailbox-read 数据。


- 独立包 `packages/mailhub`，核心域不依赖 CAPlatform、CAACTRAINING 或 AeroLink。
- Provider Port 与能力描述：Sandbox（测试）、Gmail History API、Microsoft Graph delta、TLS IMAP/SMTP。
- M2/M3 activation primitives：Gmail/Graph connectors default to `read_only=true`, explicitly reject send in that mode, and keep `push_enabled=false` by default; when explicitly enabled, push is only a host-boundary capability declaration and requires subscription/verifier endpoints. Settings/env, server-pinned OAuth registration, `HttpOAuthStateStore`, `HttpOAuthCallbackAdapter` and offline `provider_preflight.py` are covered by tests.
- A3 notification primitives：`ProviderSubscriptionPort`/`HttpProviderSubscriptionAdapter` define host-owned ensure/renew/cancel metadata；`ProviderNotificationVerifierPort`/`HttpProviderNotificationVerifierAdapter` 负责 host 验证、账号映射和 body-free delivery；`POST /v1/mail/provider-notifications/{provider}` 持久化去重 receipt、推进水位并以通知 ID 幂等入队增量 sync job；`MailboxSyncState`（migration 0013）持久化 subscription ref/status/expiry/watermark/fencing，`MailSubscriptionCoordinator` 和 `MailWorker` 提供有界续期及撤权/删除前取消；`notifications.py` 解析 OIDC-gated Gmail Pub/Sub 和 clientState-bound Graph change/lifecycle payloads 为去重安全、无正文 metadata。`*_PUSH_ENABLED` 默认仍为 false；显式开启只在 Host subscription/verifier endpoint 已配置时声明 capability，且不等于真实 renewal/reconciliation 证据。
- Graph lifecycle disposition：已知 `reauthorizationRequired`、`subscriptionRemoved`、`missed` 在 host 验签后的 body-free delivery 中被分类；重授权/订阅移除将订阅 lease 置为 `renewal_required` 并立即到期，连接分别进入 `reauthorization_required`/`degraded`，重授权不排队不可执行的增量 job，移除/漏通知触发 `reconcile`；终态连接拒绝迟到通知重开。该层只证明本地状态、fencing 和 job policy，真实 Graph webhook/renewal/throttle/permission 证据仍由宿主提供。
  Owner-scoped receipt `:replay` API 与 Python/TypeScript SDK 只按 digest/idempotency 重新排队 `reconcile`；原 receipt append-only。
- Provider error disposition：Gmail 配额 reason 与 Graph 429/`TooManyRequests` 统一为 `rate_limited`，保留有限 reason/status 和数值或 HTTP-date `Retry-After`；已知权限/凭据错误统一为 `permission_denied` 并触发活动同步连接的重新授权状态；只读同步 5xx 保留可重试 Provider failure，未来写操作 5xx 保留 `outcome_unknown` 语义。该层只有本地 connector contract evidence，真实配额、退避、权限变化和撤权仍由 A2–A4 运行证据确认。
- Provider failure observability：同步失败的 audit/event/telemetry 现在只追加白名单校验的 `provider_status`、`provider_reason`、`retry_after_seconds`，支持后续限流告警/退避调度但不携带 Provider 原文；本地 connector/service contract 已覆盖，真实运行窗口仍需 A2–A4 证据。
- Provider read retry：Gmail/Graph 只读 GET transport 最多 3 次、单次等待不超过 30 秒，优先遵循 `Retry-After`，否则指数退避；POST/send 不自动重试，保留 outcome-unknown 边界。该行为有本地 connector contract test，真实 Provider 退避窗口仍待激活证据。
- Durable sync retry：migration `0021_mailhub_sync_retry.sql` 与 `MailSyncJob.retry_wait` 将 rate-limit、网络不可用和 Provider 5xx 转成最多 5 次总尝试的 durable retry；安全继承 `Retry-After`/指数退避、释放 lease、到期再 claim，并覆盖 cancel、lease fencing、SQLAlchemy/Memory round-trip。权限、scope、撤权和格式错误仍立即失败；真实 Provider 重试窗口和撤权零访问仍待 A2–A4。
- Clean-room recovery contracts：`MAIL-CORE-015` 的 CAACTRAINING/AeroLink 行为参考已映射到 MailHub 的 crash-after-accept、outcome reconciliation、cursor replay、lease fencing、retry、dead-letter 与 kill-switch 测试；两个 source ledger 记录固定 commit/path 和“未复制实现”边界。
- `MailMessageProjection`、线程/游标、内容 SHA-256、租户/主体范围、凭据只经 `CredentialBrokerPort` 短时注入。
- Agent 策略与 DelegationGrant 交集：账号/文件夹/域/data class/thread、过期/撤销、L3B/L4、Kill Switch。
- Agent 管理面提供 owner-scoped policy/delegation 列表 API；列表不会跨 tenant/subject 泄漏，BFF 与 Web SDK 同步转发范围参数。
- PostgreSQL SQLAlchemy Core repository：迁移、幂等 outbox、lease、fencing token、失败/重试/outcome unknown；关系表不保存 OAuth 密钥和原始正文。新增 0007 sender-lease 表与 PostgreSQL-backed `PostgresSenderInterlockAdapter`，按 tenant/account/purpose 原子 claim/release、owner 绑定和 fencing token；0018 将 provider deletion count 纳入 durable sync job 结果，0019 使运行中 sync job 的取消请求保留 worker lease 并安全收尾。
- PostgreSQL migration operator runner：`scripts/apply_migrations.py` 仅接受 PostgreSQL，维护 `mailhub_schema_migrations` checksum ledger，使用 advisory lock、单迁移事务和可指定 target 的 rollback；当前静态迁移链为 0001–0021，新增 `MailboxSyncState` subscription lease/status/expiry/watermark 字段、bounded `provider_metadata` 消息投影、body-free webhook route metadata、OAuth 返回的 provider account/tenant identity 与 credential version、normalized inbound Cc/Bcc/Reply-To 投影，以及 durable sync job 的 folder/label/date backfill filter/deletion count/cancelling/retry_wait 状态字段，并包含持久化 autonomy run、outbox lease expiry、sync/autonomy worker lease expiry recovery、bounded message read/attachment metadata 与事务性 quota lease；临时 PostgreSQL 的 0001–0019 migration smoke 记录在 `docs/reports/mailhub-sync-cancelling-migration-2026-07-29.md`，另有 0001–0021 全量 upgrade、0021 target rollback/re-upgrade 和 retry schema SQL 检查记录在 `docs/reports/mailhub-sync-retry-migration-2026-07-29.md`；此前 SQLAlchemy/RLS 隔离演练仍覆盖至 0017；生产托管数据库、PITR/备库/容量/故障注入与部署级 N/N-1 证据仍待完成。
- 2026-07-29 在专用临时 `postgres:16.4` 中完成 0001–0017 upgrade、回滚到 0005 后 re-upgrade、N-1（回滚到 0016）再升级；真实 SQLAlchemy 仓储验证两个租户的 transaction-local RLS 可见性与跨租户负向，19 张租户表的 FORCE RLS/policy 检查通过。报告：`docs/reports/mailhub-postgres-isolated-2026-07-29.md`。生产托管数据库的 PITR/备库/容量/故障注入仍由部署验收负责。
- `ObjectStorePort` 与测试实现：正文/草稿使用加密对象引用设计，分析/发送前按 scope hydration 并再次校验 digest；消息/草稿对象带 TTL，清理 worker 生成删除证据。
- 知识候选提交新增 `KnowledgeSafetyPort`：MailHub 在 Host AV/DLP/PII/rights gate 返回 `security_state=cleared` 且 `rights_state=approved` 前拒绝入知识 Sink；缺 gate、quarantine 或 rights pending 均 fail closed，并保留 gate ref/evidence。
- 知识生命周期与 Agent memory 隔离：`KnowledgeLifecyclePort` 只接收 tenant/subject/connection/message refs，连接撤销/删除在有知识候选而未配置 downstream lifecycle 时 fail closed；`ApprovedKnowledgeReference`/`AgentMemoryPort` 只接受 host 返回的 approved/published knowledge ref、approval、rights/security 状态和 content digest，绝不发送正文、MIME、提示词或原始 object ref。HTTP/内存 adapter 均按候选/请求稳定幂等。
- PostgreSQL repository 的每个连接/事务都会设置 transaction-local `mailhub.tenant_id`（并设置 subject context），与 SQL RLS 和应用 WHERE 双门禁配套；0002 down migration 不会误删 0001 核心列。
- Evidence-first deterministic analyzer：HTML active content、引用/签名、提示注入识别；只产生带 evidence 的候选。
- 结构化 AI 执行端口与严格结果 schema：未知字段、类型、置信度和长度均 fail closed；规则模式明确标记为 `mode=rules`。
- 版本化智能分析策略：`AnalysisPolicy` 固定 policy/prompt/parser/calibration 版本，正文输入与估算 token 有界，输出带截断/预算元数据但不把正文写入证据；evidence-support 校准、action/knowledge 独立阈值和 prompt-injection/低置信度 abstain 会在候选生成前 fail closed。真实模型离线校准、shadow/canary、成本/延迟计量和附件预算仍待部署门禁。
- Provider webhook ingress：大小上限、HMAC 验签、tenant+event 去重、24 小时 replay 窗口和 quarantine receipt；附件安全 gate 在未配置 AV/DLP 时保持 quarantine。
- Connector conformance kit：capability、sync page、message batch、send receipt 的身份/时间/摘要/限额验证；IMAP UIDVALIDITY 缺失时拒绝支持，SMTP send 默认关闭且需显式开关。IMAP adapter 另有有界连接并发、指数退避+jitter、部分 FETCH fail-closed、UIDVALIDITY/UID 与可选 CONDSTORE/MODSEQ 游标合同；IDLE、folder rename/delete 和真实服务器兼容矩阵仍未宣称完成。
- Connector conformance sequence：稳定重放/变更摘要拒绝、乱序、cursor reset、429/5xx、撤权错误分类和发送重试身份均有纯函数合同与负向测试；真实 Provider 兼容矩阵仍由外部验收决定。
- Provider reconciliation/normalization 基础：Gmail history cursor 失效时执行有界、分页的 messages.list 补偿回填并取得 profile historyId；Graph 使用 `/delta` 初始入口，分页并返回 continuation/delta cursor，失效 404/410 token 只重建一次并显式返回 `reset_required`；Gmail/Graph 统一投影保留 bounded To/Cc/Bcc/Reply-To 地址、body content type、labels/categories、folder/changeKey/history metadata，不携带 raw MIME/display names/headers/body/token；错误分类使用具体 provider reason，避免把通用错误类别当作撤权/限流原因。
- A2 真实观察工具：`scripts/run_provider_activation.py` 通过双重网络确认，并先要求远端 `/health/ready` 为注入式 `status=ready`，再调用已授权 MailHub API；它要求 active connection 的账号 identity（Graph 还要 tenant identity），轮询 durable sync job、用同一幂等键重放，并将 readiness、tenant/subject/cursor 与响应内容脱敏为摘要/hash/count/request ref；配套 `scripts/validate_provider_evidence.py` 使用 closed-world schema 拒绝未知字段、正文/凭据和身份原文；新增 `scripts/validate_provider_bundle.py` 对完整 A1–A4 目录交叉验证 OAuth、通知、撤权和删除证明；它不接收或写出 Provider token，未在当前环境伪造真实运行证据。
- 观测与配额边界：`TelemetryPort`/`HttpTelemetryAdapter`/`InMemoryTelemetryPort`/`OpenTelemetryTelemetryPort`/`StructuredRedactingLogger` 提供限长脱敏事件、span、低基数 metric 和结构化日志，sync/send/analyze 只写安全字段；`QuotaPort`/`HttpQuotaAdapter`/`InMemoryQuotaPort`/`PostgresQuotaPort` 提供账号并发、用户小时/租户日窗口和 lease 过期，Worker finally 释放；独立部署可使用 migration 0012 的 PostgreSQL 事务实现，宿主也可保留 HTTP quota authority，真实 Collector/压力/告警仍需部署证据。
- Provider-neutral OAuth state/nonce/PKCE：opaque signed state、redirect allowlist、server-side tenant/subject binding、一次性消费和过期/replay 拒绝；token exchange 仍由宿主/Secret Broker 持有。
- OAuth API begin/callback boundary：API 只返回授权 URL/state/challenge；callback 通过注入的 `OAuthCallbackPort` 完成宿主侧 code exchange，并只接收 `credential_ref` 与账号元数据。
- 持久同步作业：`MailSyncJob`、0003/0018/0019/0021 migrations、幂等 key、claim/lease/fencing、进度/失败/`retry_wait`/`cancelling`/取消状态和 `/v1/mail/sync-jobs` 查询；API 默认返回 `queued`，不会把未执行的同步伪报为成功。
- 有界 backfill：`backfill` sync job 持久化 `folder_ref`、`label_refs`、`received_after`、`received_before`，API/SDK/BFF 全链路透传并在 0017 migration 中建立约束与索引；0018 另外持久化 provider deletion count，0019 对运行中取消保留 lease/fencing 并在安全检查点完成；Gmail/Graph/IMAP/Sandbox 仅执行安全过滤翻译，无法表达的 Provider 条件 fail closed。backfill 不读取或推进 incremental cursor；消息数/耗时估算、真实账号对账和撤销后的零访问仍需部署证据。
- Worker 提供 `sync_job_one` durable job 入口；同步线程聚合在重放时保持参与者、最新时间和消息计数一致，不把重复 Provider 页膨胀为新消息。
- 新增持久化 `MailAutonomyRun`、`MailAutonomyCoordinator`/`MailWorker.autonomy_job_one`：以 durable worker 单元执行一次 owner-scoped 同步、去重、摘要/候选生成；默认 `recommend_only`，绝不直接写项目/知识、建草稿或发信，重放使用稳定 run id 和现有候选去重。API 支持 job 查询、run/pause/resume/cancel，状态与候选/同步引用写入审计；连接撤权/重授权/删除状态会在 worker fencing 下自动暂停，旧 worker 不能覆盖 owner 或新 lease 决策；生产调度、租约监控和真实 Provider 仍未宣称完成。
- Sandbox cursor 按位置重放而非消费式弹出，模拟 Worker 崩溃后未提交 cursor 的安全重试。
- Provider webhook HTTP receipt：快速 ACK、content-type/size/signature/tenant/event 校验、receipt hash 与重复记录持久化接口；提供受控 receipt 查询，以及 digest/idempotency/owner-scope 约束的 body-free `:replay` reconcile 入队；原 receipt append-only，丢弃/重路由和真实宿主 receipt 证据仍开放。
- 线程详情与正文安全出口：线程消息按宿主范围查询，详情只返回 metadata；正文通过授权纯文本端点返回，禁止直接暴露 governed object ref。
- 统一收件箱 metadata page：`MailThreadSummary`/scope-bound cursor 支持多账号账号标识、未读/重要/附件/项目/候选筛选；0011 只保存 bounded `is_read`/`attachment_count` 元数据，不保存 MIME/附件字节；生产级批量 SQL 查询、完整 folder/label 语义和真实 Provider 验收仍开放。
- Search contract now declares `metadata` coverage/completeness/incomplete reason and includes normalized To/Cc/Bcc/Reply-To recipient headers; `provider` search returns explicit `degraded` until a separately verified Provider search capability exists，避免把投影搜索伪报为全量邮箱搜索。
- 版本化声明式规则 DSL：只允许低风险 label/archive/mark_read 条件和动作，支持 dry-run/simulate、publish/pause、版本与有效期；新增 L3A 受控执行入口、policy/grant 交集、独立 kill switch、hour/day cap、稳定 action/execution id、0006 持久执行证据与 replay suppression；禁止任意代码、脚本、URL 和 webhook。
- Host-backed 动态 Kill Switch：`KillSwitchPort`/HTTP/内存 adapter 支持全局、tenant、provider 和 operation 决策；排队、L3A 规则与 Provider send 在副作用前检查，拒绝/检查失败写入受限审计，已 lease 的发送回到可重试状态；宿主仍负责四眼变更审批与权威开关账本。
- 可复用 SDK/部署起点：Python/TypeScript 薄 SDK、`mailhub-api` standalone CLI、sandbox Docker Compose、要求不可变镜像 digest 且注入 EventPublisher/Telemetry/Quota 的 Helm 合同、第二宿主 in-memory Host Port 示例、HTTP Host Port adapters（Identity/Secret/Object/Approval/AI/HostAction/Knowledge/Audit/Notification/Telemetry）、NOTICE/SBOM/provenance/license/import-boundary/secret gates。
- 版本化事件运行时：`EventEnvelope`、message-observed/action-operation builders 与脱敏输出，和 `docs/compatibility/mailhub-v1.md` 兼容矩阵；事件仍由宿主/部署的持久总线发布，核心不自建第二 Agent runtime。
- 发布/交接文档增补：管理员、用户、开发者、Security/Privacy、故障排查/数据生命周期手册已发布；Python/TypeScript SDK 安全示例位于 `packages/mailhub/examples/sdk/`，示例不携带 token 或真实邮箱标识。
- 许可边界增补：`packages/mailhub/LICENSE`、`NOTICE`、根 `LICENSE` 和两份 source ledger 明确 MailHub/UI/SDK 为 CAPlatform 专有许可；CAACTRAINING、AeroLink、Mail0、Inbox Zero 仅为固定行为/产品参考，未导入源文件。
- 关键生命周期已接入可选 `EventPublisherPort`：连接授权/撤销/删除、同步开始/完成/失败、新消息、候选提议/审核/应用、草稿创建/修订、delegation 授予/撤销、规则发布/暂停/执行/阻断及 outbox 排队/结果/对账均使用稳定 event id 和 body-free metadata；发布失败不改变已提交邮件状态，并记录安全 telemetry，生产可靠投递仍由宿主 durable bus/outbox 验收。
- 服务镜像运行时依赖显式包含 Uvicorn；Helm 通过外部 Secret 注入全部生产控制端点，并以 `outboundEnabled=false` 作为默认安全值。
- CAPlatform BFF 回归现为 128 tests；MailHub adapter、主路由与新审计/健康/导出/显式撤权/连接影响预览投影通过 Ruff 和 `python -m mypy src`（20 个 source files，无错误）。
- 项目、任务、知识候选的 proposal/review/apply 流程；review 无宿主副作用，apply 使用稳定 action id 并交给宿主项目/知识权威，核心不直接写宿主事实。
- 候选拒绝必须携带 bounded `review_reason` 并保存在 candidate payload/审计 lineage；项目改选和反馈模型仍由宿主补齐。
- 数据生命周期核心合同：`POST /v1/mail/connections/{id}:delete` 先进入 `DELETING`，通过无分页 `ConnectionCleanupRefs` 快照收集全部消息/知识/对象引用，删除连接正文/草稿对象与投影、调用 host `CredentialRevocationPort`，再写 `DELETED` tombstone；`GET /v1/mail/data-export` 输出 bounded `mailhub.data_export.v1`，默认不含正文、凭据、附件/ObjectRef（执行记录也纳入导出），含正文必须显式开启；删除/导出均写 append-only audit proof。201 条清理引用回归通过，真实 Secret/ObjectStore/Knowledge downstream 清理仍由部署验收。
- 审计/健康宿主投影：Core `/v1/mail/audit` 依赖 Host `mail.audit` entitlement；CAPlatform BFF `/api/mail/audit` 只接受 admin 或显式 `mail.audit` 权限，`/api/mail/admin/provider-health` 只接受 admin，返回均递归移除 credential/token-shaped 字段。两者仍是当前主体范围，不宣称租户级监控聚合。
- FastAPI v1 contract：连接、激活/撤销、同步、线程/消息、分析、候选审核/应用、草稿、受控发送、outbox 查询、Provider capability。
- 草稿支持 GET/PUT revision；发送命令必须绑定 `expected_revision`、正文 digest、To/Cc/Bcc 收件人 digest、附件引用和 approval；幂等 key 同时绑定草稿 revision、收件人/附件 envelope digest 与正文 digest，已批准草稿重放只返回既有 operation。
- 受控发信覆盖 To/CC/BCC、线程 participant 检查、外部域/大范围/BCC/附件/保守高风险词风险旗标和显式确认；Gmail/Graph/SMTP 当前只在能力未声明附件时 fail closed，CC/BCC 已映射到 HTTP/MIME Provider DTO，附件 governed ref 尚未宣称可发送。
- Agentctl product capabilities：`ca.mail.message.analyze`、`ca.mail.sync.enqueue`、`ca.mail.autonomy.enqueue`、`ca.mail.rule.execute`、候选审核/应用、回复草稿、回复发送；使用 product-owned JSON-in/JSON-out handlers，不新增 Agentctl Core route；自主运行 capability 只接受 `recommend_only`、要求 scope/idempotency/replay key 与显式确认，规则执行 capability 仍声明 explicit confirmation 与 L3A feature flag。
- Agentctl 受治理读取能力：`ca.mail.connection.list`、`ca.mail.thread.list`、`ca.mail.message.search`；仅返回租户/主体范围内的 metadata，正文 hydration 仍是独立授权端点，缺少 MailHub endpoint/身份时 fail closed。
- Agentctl sync enqueue 现在也接受与 HTTP/SDK 相同的 `backfill` folder/label/UTC date bounds；product handler 在发出请求前拒绝 incremental+filter、控制字符、重复 label 和无时区/倒序日期，manifest 保持 `additionalProperties: false`。Agent 仍只创建 durable job，不直接调用 Provider。
- AI 策略已接入 `MailHubSettings`：版本、模式、字符/token/evidence/action 预算和 action/knowledge 阈值可由环境变量配置，启动时按 `AnalysisPolicy` bounds 校验并注入默认应用图；M2/M3 开关仍默认为 false。
- CAACTRAINING/AeroLink 迁移辅助：tenant-scoped legacy ID map（旧 ID 重放可更新状态但禁止静默改指）、幂等 migration batch/sender claim、shadow compare、`SenderInterlockPort`/HTTP sender lease adapter 和安全 migration audit events；clean-room adapters 只接受 approved snapshot、对象引用和 digest，并提供 CAA delivery-result 与 AeroLink inbound metadata-only projection；AeroLink 仅映射 EMAIL，WEBHOOK/SOCKET 保持宿主所有；未知/outcome_unknown 永不投影为 sent。
- CAPlatform BFF 薄适配器 `apps/bff/src/caplatform_bff/mailhub_adapter.py`：只转发 MailHub v1 合同，不直连 Gmail/Graph/IMAP。
- CAPlatform MailHub UI `/mail`：统一收件箱、metadata-first 线程详情、纯文本按需读取、候选审核/批量审核/已批准候选显式 apply、连接健康、服务端固定注册的只读 OAuth 向导/回调、durable autonomy run 控制与受控草稿；fixture 仅演示，不代表真实 Provider。
- 可嵌入 React 源包 `packages/mailhub/ui`：`MailHubUiClient` 注入身份/路由/API，`MailHubWorkspace` 不保存 token、不渲染原始 HTML，并提供独立 CSS/TypeScript build contract。
- CAPlatform BFF MailHub 路由与 Python/TypeScript SDK 已补齐连接、服务端固定注册信息的 OAuth provider readiness/authorize/callback（不向浏览器返回 `credential_ref`；连接列表/刷新/范围/删除投影也做凭据字段脱敏）、同步、durable autonomy run 控制、线程/消息、搜索、候选 review/apply、草稿/受控发送的宿主投影；未配置 `MAILHUB_BASE_URL` 时 fail closed。
- 候选控制面现在提供 `/v1/mail/candidates/projects|knowledge|tasks` owner-scoped typed views、通用 `candidate_type` filter、candidate detail 与 revision-bound revoke，Python/TypeScript SDK 同步提供 typed list/detail/revoke 方法；已 apply 候选的下游撤回以及真实 Host project/EDM 事实仍按 M7/M8 外部验收开放。
- 邮件数据生命周期已补齐 BFF `/api/mail/data-export`（非缓存、固定下载文件名、verified session scope）和管理员 `/api/mail/admin/provider-health`（仅 admin、递归脱敏）；`/mail` 连接中心提供元数据导出与二次确认的含正文导出，fixture 模式按钮保持禁用。
- 连接撤权负向路径已贯通 Core `:revoke`、CAPlatform BFF、Python/TypeScript SDK 与 `/mail` 连接中心；撤权保留连接投影以便验证 Provider 零访问，`:delete` 仍是独立的数据清理流程。真实 Credential Broker 撤权、通知取消和撤权后零访问报告仍未宣称完成。
- 本次显式撤权补充后的复核为 BFF 127 tests、根项目 220 tests；旧版 125/218 计数仅代表上一轮收尾基线。
- MailHub 核心在撤权后 Provider health 不再调用 connector 的负向合同补充、OAuth authorize/state-bound scope drift 负向补充、直接 Provider connection scope gate、legacy state 缺失 scope 负向、Gmail/Graph deletion ref 传递、投影幂等删除、durable sync job deletion count 与运行中取消 fencing 负向补充后，独立门禁为 264 tests；这些测试证明 revoked/非 active 连接的健康投影不会成为隐式 Provider 访问路径，Host exchange 与 direct service path 也不能扩大只读 scope。
- 说明：下方较长摘要保留历史功能描述；其中 285/128/221、284/128/221、283/128/221、280/128/221、279/128/221、278/128/221、277/128/221、270/128/221、266/128/221 与 264/127/220 均仅代表前几轮基线；当前 MailHub 包计数以文档顶部的 327 tests 为准。
- EML/MBOX 只读离线导入 `mailhub.importers`：限额、摘要/附件 metadata、稳定 cursor，可作为迁移/测试入口，不宣称实时邮箱能力。
- CI 已加入独立 MailHub quality job，执行测试、Ruff、strict mypy、import-boundary、secret、migration、provenance 和 license gates；OCI 镜像 digest、签名、SBOM 漏洞扫描和真实发布审批仍由 release pipeline 提供。
- 本地验证：MailHub 306 tests（新增 201 条消息/知识候选/对象引用的无分页截断清理回归，以及 Graph lifecycle 分类、续期 required、连接状态 fencing、重授权阻断 job/移除与漏通知 reconcile 回归；本轮另覆盖 durable sync `retry_wait` 的有界尝试、Retry-After/退避、到期 claim、cancel、lease fencing、最多五次总 Provider 调用预算与 0021 round-trip；含 OAuth authorize/state/exchange、state-bound requested/granted scope 漂移与写 scope 负向、legacy real-provider state 缺失 scope 负向、直接 Gmail/Graph create/activate/update-scopes/reauthorize 只读 scope gate、重授权 state/connection revision 绑定、Host metadata-only refresh/credential version fencing、provider account/tenant identity 与 credential version round-trip/负向合同、resolve 前 Provider I/O credential binding 负向、revision-fenced scope 收窄与扩权阻断、MailboxConnection 构造层 scope 规范化/控制字符负向、server-pinned endpoint/client/scope、只读/推送门、ProviderSubscription/ProviderNotification host adapters、MailboxSyncState 续期/通知 watermark/fencing/撤权取消/失败过期事件、event bus outage watermark 保持、Gmail/Graph notification parser、Gmail/Graph bounded provider metadata normalization 与 To/Cc/Bcc/Reply-To 标准化、Gmail `messagesDeleted`/`labelsRemoved` 与 Graph `@removed` deletion refs、InMemory/PostgreSQL deletion contract 与 body-object cleanup、durable sync job deletion count round-trip/Worker/activation evidence、running sync cancellation/CANCELLING lease recovery/SQL round-trip、verified provider notification receipt/queue/idempotency、body-free receipt replay/digest/idempotency/owner-scope 合同、SQLAlchemy subscription-state/provider-metadata/message-recipient-header round-trip、route metadata unknown/control/empty-value negative contracts、typed candidate list/detail/revoke、provider preflight（含 read-only 下拒绝 Gmail/Graph 写 scope、`--require-config-ready` 禁止 disabled 误判为可激活）、严格拒绝 OAuth endpoint/redirect 的 query/凭据/空路径和公网显式端口、允许 loopback 开发端口、默认 OAuth 注册即使跳过 preflight 也拒绝写 scope/关闭只读门、默认图显式启用真实 Provider 时 readiness 报 degraded 并列出宿主依赖、真实激活采集器要求远端注入式 readiness=ready、拒绝带缺失依赖的伪 ready，Sandbox 只生成 activation gate failure 而非 real observation，并将 readiness 摘要写入脱敏证据、真实 API 证据采集器双重网络门/脱敏/幂等重放合同、单文件和完整目录证据校验器的 closed-world/schema/身份交叉负向、订阅生命周期 API/SDK 合同、Host 路由字段控制字符负向和 verifier 失败不落 receipt、bounded backfill filter/0017 persistence、sync deletion count/0018 persistence/activation redaction and connector translation contracts、Graph `$select` 字段完整性与 delta cursor 同源/路径负向、connection impact preview 的 owner scope/本地投影计数/远端 unknown 合同）、Agentctl handler 14 tests、CAPlatform BFF MailHub adapter 13 tests；CAPlatform BFF 全套 128 tests，根项目全套 221 tests，deployment contracts 全套 93 tests，Web 全套 10 tests（5 files）；Web `typecheck`/生产构建、可嵌入 MailHub UI `npm run typecheck`/`npm run build`、TypeScript SDK strict compile、合成评测脚本均通过。Ruff（含 `scripts`）、strict mypy、静态 0001–0021 migration 合同、migration runner dry-run、OpenAPI 3.1 导出、source provenance、license/prerequisite、import-boundary、secret scan 均通过；Agentctl manifest validate/apply 通过，doctor 的 assurance、manifest、binding 和 provider registration 检查通过。未注入测试凭据的 doctor 会显示既有 Lite valuation provider SecretRef 缺失；使用临时测试引用的完整基线已通过，仅保留 source-tree distribution warning。Agentctl smoke 已真实执行但因当前环境未提供 `CAPLATFORM_MODEL_API_KEY`/`CAPLATFORM_MODEL_BASE_URL` 而保持失败，未添加伪模型或 placeholder success；详见 [`mailhub-agentctl-gate-evidence-2026-07-29.md`](mailhub-agentctl-gate-evidence-2026-07-29.md)。
- 2026-07-29 本地门禁复核（历史基线）：MailHub 312 tests；A0 push endpoint 条件门、Host URL 负向、A3 push capability 的 Host subscription/verifier 依赖、默认图 readiness 缺失项和 loopback OAuth 端口合同、push bundle marker 门均已覆盖。后续 310/307 及 306/288/290/291/297/298 等行仅代表前几轮历史基线。
- 2026-07-30 A1 增量复核：MailHub 319 tests；OAuth callback 审计绑定实际 scope、PKCE、redirect、flow digest 和 Provider identity hash，同一 state 的 replay rejection 可由 `scripts/collect_oauth_evidence.py` 从 owner-scoped audit API 脱敏采集；缺少真实 replay/refresh/revoke 观察时仍 fail closed。
- 本轮增量复核：MailHub 288 tests；新增真实 Provider `:revoke` 的 Host credential revocation 顺序、缺 Port/秘密响应 fail-closed 与已 revoked delete 不重复撤权回归。上一行的 285 为前一轮基线。
- 本轮证据校验增量复核：MailHub 290 tests；A1–A4 bundle validator 新增 environment/run_id/identity
  交叉绑定与 A0 enabled/read-only/push-disabled 负向门禁。上一行的 288 为前一轮基线。
- 本轮测试结果增量：MailHub 291 tests；激活采集器输出的 `test-output.txt` 已版本化并由
  bundle validator 强制校验真实成功退出码。上一行的 290 为前一轮基线。
- 本轮安全审查增量：MailHub 297 tests；`security-review.md` 机器字段要求 approved、范围、
  DLP/AV、privacy、retention、deletion 和 signoff hash。上一行的 291 为前一轮基线。
- OAuth scope 证据增量：`oauth-consent.json` 必须携带 requested/granted scope 实际列表，
  bundle validator 独立验证只读必需 scope、子集关系和写权限拒绝；布尔字段不能单独通过。
- 通知证据增量：`webhook-receipts.json` 要求续期、receipt、非零 ACK p95 及合理重复计数，
  防止空通知窗口伪装成 A3/A4 连续运行证据。
- A4 窗口增量：通知证据新增带时区观察起止时间，validator 计算至少 168 小时；不接受
  单独手填持续时长。
- 未来窗口负向增量：validator 拒绝晚于当前时刻的 `observation_finished_at`，避免尚未
  发生的观察窗口伪装连续运行证据；本轮完整 MailHub 回归为 298 tests。
- 同步证据时间增量：`validate_provider_evidence.py` 拒绝倒置或未来的
  `started_at`/`finished_at`；本轮完整 MailHub 回归为 312 tests，仍不替代真实 Provider 证据。
- 安全审查证据增量：bundle validator 同时强制 `security-review.md` 的 approved/范围/AV-DLP/
  privacy/retention/deletion 元数据和 signoff hash；机器字段通过不替代真实 Security/Privacy
  owner 签字。
- 默认 FastAPI app 明确标记 `GET /health/ready` 为 `sandbox`；若开发默认图显式打开 Gmail/Graph，则改报 `degraded` 并列出 OAuth、Credential Broker、Host Identity、ObjectStore/KMS 或通知验签缺失项，不能把真实 Provider 伪装成 Sandbox。生产/预生产未注入 PostgreSQL、对象存储和 Host Port 时 fail closed，不会悄悄启动内存 repository。PostgreSQL sender lease 的 `check()` 可作为宿主启动自检。
- 为落实 M2/M3 受控激活，默认开发图只注册 Sandbox；只有显式设置 `MAILHUB_GMAIL_ENABLED=true` 或 `MAILHUB_MICROSOFT_GRAPH_ENABLED=true` 才会注册真实连接器。连接器默认 `read_only=true`、`push_enabled=false`；开关和离线预检本身不构成真实 Provider 验收证据。
- CAPlatform 根测试集：`python -m pytest -q --maxfail=1` 通过 221 tests；MailHub 包独立门禁另通过 319 tests（根 pytest 配置不递归执行 `packages/mailhub/tests`）。

> A3 capability note：`*_PUSH_ENABLED` 的显式声明不等于真实 renewal/reconciliation 证据；未配置
> Host subscription/verifier endpoint 时，预检和 `/health/ready` 均 fail closed，connector 不直接接收
> 未验证的 Gmail Pub/Sub 或 Graph callback。
>
> 运行时配置修正：生产环境的定时/拉取式只读同步（`push_enabled=false`）不再被错误要求
> subscription/verifier endpoint；只有显式启用 A3 push capability 时才要求两类 Host endpoint。

## 必须由部署/宿主补齐

- 生产托管 PostgreSQL 运行实例与 `asyncpg`，使用 `scripts/apply_migrations.py` 执行发布版本 upgrade/rollback/re-upgrade/N-1，并完成 PITR/备库/容量/故障注入及真实 RLS/租户负向测试；仓库级隔离演练已记录在 `docs/reports/mailhub-postgres-isolated-2026-07-29.md`。
- 真实加密对象存储、DLP/AV/HTML sanitizer、Webhook subscription renewal（宿主 watch/subscription API、调度和告警）、搜索索引、EDM rights downstream 和生产 OpenTelemetry exporter；本地 `KnowledgeSafetyPort`/`TelemetryPort` 仅为合同/测试适配器。
- Gmail/Graph OAuth 应用审核、受控测试账号、撤权/失效 cursor reconciliation；IMAP/SMTP 真实受控服务器。
- CAPlatform 生产 `HostIdentityPort`、Credential Broker、审批/四眼队列、加密 ObjectStore、AIProjectOPS `HostActionPort`、知识库 `KnowledgeSinkPort`、Audit/Notification、Telemetry/Quota 适配器（仓库已有通用 HTTPS JSON adapters，但真实宿主端点、认证和故障证据仍未接入）。
- 生产数据生命周期：Credential Broker 必须实现 `/v1/mail-host/credentials/revoke` 的真实 Provider 撤权/Secret 删除；对象存储删除证明、PostgreSQL 删除 job、知识 downstream revoke/reindex、法律保留与导出文件投递/过期仍需部署和隐私评审。
- Worker/Scheduler 独立部署、监控、死信/人工处置、outcome unknown 对账；不可用 API 进程内存定时器替代。
- 生产 quota store/worker concurrency leases、租户/用户/账号上限的 live 压力与告警演练；代码已提供 `PostgresQuotaPort`（migration 0012）和 `HttpQuotaAdapter`，`InMemoryQuotaPort` 仍只用于 sandbox/contract tests。
- 生产 OAuth callback handler、共享/加密 state store、Credential Broker、Provider webhook subscription renewal 和真实 webhook mapping。
- 生产规则执行 worker、分布式账号/租户配额、Kill Switch 管理面和 Host Action policy-decision 验证；当前 Core 已有 0006 执行历史与受控执行 API，但仍不能用 API 进程替代 durable worker/事故演练。
- SDK 的发布构建（TypeScript `dist`、OpenAPI 代码生成、N/N-1 兼容测试）、镜像签名、真实 SBOM/vulnerability/license 扫描；仓库脚本只生成 resolved-environment inventory，不替代这些外部证据。
- CAACTRAINING 与 AeroLink 的 sandbox → isolated mailbox → shadow → single sender 迁移证据；完成前禁止切换旧 sender authority。
- 真实安全、许可/SBOM、跨租户负向、10 万消息回放、备份恢复、RPO/RTO 和 L3B 独立试点。

## 复核命令

```powershell
cd packages/mailhub
python -m pytest
ruff format --check src tests scripts
ruff check src tests scripts
mypy src tests
./scripts/verify.ps1
cd ../..
agentctl integration validate --manifest deployment/agentctl.capabilities.yaml --json
agentctl integration doctor --manifest deployment/agentctl.capabilities.yaml --config deployment/runtime.config.yaml --json
```

Doctor 的 assurance、manifest、binding 和 provider registration 检查通过；不注入测试凭据时仅会暴露仓库既有 Lite valuation provider SecretRef 缺失，使用 `verify-agentctl-integration.ps1 -Apply -UseEphemeralCredentialReference` 的基线验证为 16 pass/0 fail/1 source-tree distribution warning，且不把临时引用当作生产认证证据。真实 MailHub capability smoke 仍需要 MailHub 服务 URL、宿主身份和受控测试 mailbox；未配置 URL 时 handler 保持 `mailhub_endpoint_not_configured` fail closed，而不是返回伪成功。
- 可复用 UI/SDK 增补：`packages/mailhub/ui` 现在提供统一收件箱筛选、metadata 搜索和 opaque cursor 加载更多；Python/TypeScript SDK 提供 scope-bound 线程 page/cursor/filter 方法。
