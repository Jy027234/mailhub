# MailHub 开发者手册

MailHub core 只依赖 `ports`，不导入 CAPlatform、CAACTRAINING、AeroLink 或 Provider
SDK。宿主通过 Host Port 和薄适配器接入；参考
[`packages/mailhub/docs/host-adapter-sdk.md`](../packages/mailhub/docs/host-adapter-sdk.md)。

## 合同入口

- HTTP/OpenAPI：`packages/mailhub/schemas/mailhub.openapi.v1.json`。
- 事件：`packages/mailhub/src/mailhub/events.py` 和
  `schemas/mailhub.contract.v1.json`；只发布 metadata/ref，不发布正文、MIME、token。
- Python SDK：`packages/mailhub/sdk/python`；TypeScript SDK：
  `packages/mailhub/sdk/typescript`。
- UI：`@caplatform/mailhub-ui` 的 `MailHubWorkspace` 或
  `@caplatform/mailhub-ui/standalone` 的 `MailHubStandalone`；client、身份、主题和路由
  均由宿主注入。

## 扩展规则

1. 新能力先运行 `agentctl integration assess --root . --json`，按
   `deployment/agentctl.capabilities.yaml` 的 manifest/handler 合同接入；不要增加产品
   专用 Agentctl Core route。
2. 新副作用必须有稳定 operation/action/idempotency key、revision/digest 检查、Host
   approval、KillSwitch 和审计/telemetry evidence。Provider 返回未知时使用
   `outcome_unknown`，不能返回成功。
3. 新 Port 先写协议/负向测试，再写 HTTP adapter；缺字段、越权、超时、重定向、非 JSON
   和 401/403 均 fail closed。
4. 连接、同步、候选、草稿和事件必须带 tenant/subject scope。原始正文只能在授权读取
   或受治理 AI 输入边界短暂出现，不能进入日志、事件、Agent memory 或业务 HostAction。
5. Gmail/Graph 初始连接必须保持 `read_only=true`、`push_enabled=false`。OAuth state 使用
   `HttpOAuthStateStore`，授权码交换使用 `HttpOAuthCallbackAdapter`；state 绑定
   `requested_scopes`，callback 只接受其子集且拒绝 Gmail/Graph 写 scope；Host 只能返回
   `credential_ref` 和非敏感账号元数据，禁止把 token 放入 MailHub response、测试 fixture 或日志。
   设置完整的 Provider registration 后，默认开发图会对 authorization endpoint、client id、
   redirect URI 和 scope 做服务端 allowlist；缺配置时 OAuth endpoint 保持 503，而不是接受
   浏览器提交的任意 Provider 参数。
6. 真实 A2 同步只使用 `packages/mailhub/scripts/run_provider_activation.py` 这类受控入口；
   命令需要双重网络确认，只接收 tenant/subject/connection scope，不接收 provider token，
   并将 cursor、正文、邮箱地址和原始 response 脱敏后写入 evidence bundle。随后必须用
   `packages/mailhub/scripts/validate_provider_evidence.py --require-real` 验证 bundle；backfill
   可额外传入 folder/label/UTC date bounds；它们必须随 `mode=backfill` 持久化，且不得
   推进 incremental cursor；校验器采用 closed-world schema，未知字段、正文/凭据或
   未审计的手工扩展均 fail closed；Provider 无法安全表达时也必须 fail closed。
   A1–A4 交接前还必须运行 `packages/mailhub/scripts/validate_provider_bundle.py --require-real`
   对完整目录做 OAuth、通知、撤权、删除证明和跨文件 identity hash 复核；该命令离线运行，
   不会把本地合同或 gate failure 当成真实 Provider 证据。
   A0/CI 使用 `provider_preflight.py --require-config-ready`；兼容字段 `ready=true` 在
   Provider 关闭时不代表可以激活，必须同时看到 `readiness=config_ready` 和
   `config_ready=true`。
7. 收件 Provider DTO 只允许 bounded normalized `sender/to/cc/bcc/reply-to` 地址、主题、
   labels/folder/category/changeKey/historyId 等 projection；connector 必须丢弃 display name、
   raw RFC 5322 headers/MIME 和 token，并在 conformance test 中拒绝未解析地址。migration
   `0020_mailhub_message_recipient_headers.sql` 只保存三类可见的 header 地址；Bcc 缺失不等于
   Provider 证明“没有 Bcc”。
8. A3 订阅生命周期只能经 `MailSubscriptionCoordinator`/`MailWorker` 和
   `ProviderSubscriptionPort`；`MailboxSyncState` 的 lease/status/expiry/watermark 必须在
   PostgreSQL migration 0013 后持久化。续期由外部 durable scheduler 触发，撤权/删除先取消
   subscription；verified notification 才能推进 watermark，不能直接写 cursor 或把 lifecycle
   payload 当成邮件正文/变更。
9. 连接撤权/删除不得用用户列表分页收集清理对象。调用仓储的
   `list_connection_cleanup_refs` 获取完整的消息 ID、知识候选来源和正文/草稿 object refs
   快照；该返回值只含 opaque refs，随后才执行 ObjectStore/KnowledgeLifecycle 清理。任何
   新持久化实现都必须覆盖超过 API page limit 的回归测试，避免删除完成后残留受治理内容。

## 验证与提交

```powershell
cd packages/mailhub
python -m pytest
ruff format --check src tests scripts
ruff check src tests scripts
mypy src tests
./scripts/verify.ps1
npm --prefix ui run typecheck
npm --prefix ui run build
npm --prefix sdk/typescript run build
```

更改迁移时必须同时更新 checksum/静态合同/回滚说明；更改 OpenAPI、事件或 SDK 时必须
更新兼容矩阵和示例。真实 Provider、PostgreSQL/RLS、对象存储、AV/DLP、模型 endpoint
和宿主权限证据不能由本地 fixture 代替。
