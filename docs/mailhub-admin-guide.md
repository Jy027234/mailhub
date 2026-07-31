# MailHub 管理员手册

本手册面向部署管理员、值班工程师和租户管理员。MailHub 是独立的
provider-neutral 服务；宿主负责身份、凭据、对象存储、审批、知识/项目动作、审计和
生产开关。当前发布默认是 Sandbox/合同验证模式，Gmail 真实只读接入（M2）和
Microsoft Graph 真实只读接入（M3）已进入受控 `preflight` 阶段；尚未通过真实
OAuth、隔离邮箱、撤权和安全评审时不得宣传为 GA。

## 启动前检查

1. 使用 `packages/mailhub/.env.example` 生成环境配置；生产环境不在 MailHub
   配置文件中写 OAuth token、密码或私钥。
2. 配置 PostgreSQL、加密对象存储、Credential Broker、Host Identity、Approval、
   HostAction、KnowledgeSafety/KnowledgeLifecycle、EventPublisher、Telemetry、Quota
   和 KillSwitch；任一必需 Port 缺失都应保持 `degraded`/`blocked`。
3. 先执行 `python packages/mailhub/scripts/apply_migrations.py --dry-run
   --direction upgrade`，确认 0001–0021 链和 checksum，再由 operator-only 流程执行真实迁移。
   API 启动不会自动迁移。
4. 执行 `packages/mailhub/scripts/verify.ps1`，保存命令、退出码、提交版本和报告；它
   只证明本地合同和 Sandbox，不证明 Provider 或生产可用性。
5. M2/M3 试点前执行 `python packages/mailhub/scripts/provider_preflight.py --provider all --json`；
   预检只读非秘密配置，失败时不得打开 Provider 开关。真实激活、watch/delta 对账、撤权和
   7 天运行按 [`mailhub-provider-activation-runbook.md`](mailhub-provider-activation-runbook.md) 留证。

## 日常运维

- 观察 `/health` 与 `/health/ready`、同步/自主运行队列、outbox、dead-letter、
  `outcome_unknown`、Provider capability、Quota、KillSwitch 和审计 trace。
- `queued`/`running`/`paused` 由持久 worker 处理；不要在 API 进程中添加内存队列或
  `setInterval`。连接撤权、删除、提示注入和 KillSwitch deny 后，运行必须暂停或阻断。
- 发送只接受 revision、正文 digest、recipient digest、approval/grant 和 idempotency
  绑定；`outcome_unknown` 必须先对账，禁止盲目重试。
- 任何真实 Provider 只读试点必须使用隔离账号、最小 scope、明确 feature flag 和回滚
  窗口。Sandbox/fixture 不得改写为“已接入”。

## 数据操作

- 用户可通过 `GET /v1/mail/data-export` 获取 bounded metadata 导出；正文必须显式
  `include_content=true`，导出不包含 credential 或 ObjectRef。
- CAPlatform BFF 将其投影为 `GET /api/mail/data-export`，固定下载文件名并设置
  `Cache-Control: no-store`；`/mail` 连接中心的含正文导出必须二次确认。管理员可通过
  `GET /api/mail/admin/provider-health` 查看当前主体范围的 bounded 健康，不返回正文或凭据，
  普通主体返回 403。
- 撤权先使用 `POST /v1/mail/connections/{id}:revoke`（CAPlatform 为
  `POST /api/mail/connections/{id}:revoke`）并携带 `expected_revision`；该操作停止
  Provider 访问但保留连接投影；真实 Provider 会先调用 Host `CredentialRevocationPort`，
  撤权失败时不会写入 `REVOKED`。数据清理使用独立的 `:delete` 操作，不能把“撤权请求已
  接受”当作删除完成；已 revoked 连接的 delete 重试不会重复调用 Host 撤权。
- 变更或删除前可调用 `GET /v1/mail/connections/{id}/impact-preview`（CAPlatform 为
  `/api/mail/connections/{id}/impact-preview`），或在连接中心点击“预览影响”，查看本地
  projection 影响。该接口不会查询 Provider；`remote_message_delta_estimate=null` 代表必须
  另行执行真实对账，不能据此假设远端新增/删除数量。
- 连接中心的“收窄本地权限”只会更新 MailHub 本地 projection 的可用 scope，并绑定当前
  `expected_revision`；扩权请求会 fail closed，必须重新走同一连接的 OAuth 重授权。
- 删除使用 `POST /v1/mail/connections/{id}:delete`，遵循 `DELETING → DELETED`
  状态；只有凭据撤销、对象引用清理和 downstream knowledge 处理完成后才能报告完成。
- 迁移使用 `docs/mailhub-migration-runbook.md`。同一 tenant/account/purpose 必须先
  取得 sender interlock；旧 sender 与 MailHub 不能同时写信。

## 事故处理

先暂停受影响 tenant/provider 的 KillSwitch 和出站规则，保留 operation/trace/audit
ref，隔离未知结果；不要删除证据或重发。按
[`mailhub-troubleshooting.md`](mailhub-troubleshooting.md) 分流，并记录影响范围、
数据类别、租户、时间线、处置人和回滚结果。凭据泄漏、跨租户读取、误发和知识污染应
升级给 Security/Privacy/业务事实所有者。

## 发布边界

每次发布必须附 OpenAPI、事件 schema、迁移/回滚计划、SDK/UI 版本、SBOM、provenance、
NOTICE、兼容矩阵和已知限制。未完成真实 Provider、PITR、容量、安全和连续运行证据时，
状态只能是 `pilot` 或 `contract_only`，不得标为 GA。
