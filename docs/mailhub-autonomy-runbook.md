# MailHub 自主运行操作手册

本手册描述 `MailAutonomyRun` 的可恢复 L0/L1 运行边界。它不是通用 Agent
循环，也不授予模型 Provider 凭据或自动发信权限。

## 运行边界

- `mode` 固定为 `recommend_only`。
- Worker 可以同步、去重、读取受治理元数据、分析消息并生成项目/任务/知识候选。
- 候选必须经过现有 review/apply 流程；项目事实、知识发布、草稿写入和发送不由自主运行直接完成。
- Gmail/Graph 真实 OAuth、watch/history/delta 和真实邮箱证据已进入受控 preflight；Sandbox/fixture 不能替代 Provider 验收。详见 [`mailhub-provider-activation-runbook.md`](mailhub-provider-activation-runbook.md)。

## 生命周期

```text
queued -> running -> completed
   |         |  \-> failed
   v         v
 paused <----+
   |
   +----> queued (resume)
queued/running/paused -> cancelled
```

每次运行由 `(tenant_id, subject_id, connection_id, replay_key)` 唯一确定。Worker
claim 时获得 lease owner 和 fencing token；完成或失败必须用同一 token 提交。暂停、
恢复、取消可由 owner-scoped API 发起；并发控制不得覆盖已经生效的 owner 决策。

如果 Worker 发现连接进入 `pending_authorization`、`reauthorization_required`、
`revoked`、`deleting` 或 `deleted`，会在同一 fencing token 下自动暂停运行，
`pause_reason` 形如 `connection_<status>_requires_attention`，并写入自动暂停审计。
撤权后的运行不能继续访问 Provider；重新授权并由 owner 显式恢复前，不会自动重试。
如果消息分析检测到 prompt injection，运行同样会在首个命中处暂停，
`pause_reason=prompt_injection_detected_requires_review`；已产生的候选仍须走人工审核。

## 调度器合同

生产调度器应从数据库读取 `queued` 或 lease 已过期的运行，调用：

```text
POST /v1/mail/autonomy/runs/{run_id}:run
```

API 创建运行只负责持久化并返回 `202`/`queued`。调度器必须记录 job/run/trace
evidence，使用有界重试和 dead-letter；不得使用 API 进程内 `setInterval` 或内存队列
替代持久 worker。Provider 连接、Secret Broker、对象存储、Host Action、Knowledge、
KnowledgeLifecycle、AgentMemory、KillSwitch 和 Telemetry 均由宿主注入，缺失时保持 fail closed。

出站排队、L3A 规则执行和 Provider send 在副作用前都会查询宿主
`KillSwitchPort`；宿主可按全局、tenant 或 provider 阻断。宿主负责开关变更的四眼
审批与权威审计，MailHub 只保存 bounded deny/check evidence。发送在已取得 lease 后
若开关切换为 deny，会进入可重试状态，不会报告为已发送。

## 控制与排障

```text
GET  /v1/mail/autonomy/runs?connection_id=<uuid>
GET  /v1/mail/autonomy/runs/<run_id>
POST /v1/mail/autonomy/runs/<run_id>:pause   {"reason":"..."}
POST /v1/mail/autonomy/runs/<run_id>:resume
POST /v1/mail/autonomy/runs/<run_id>:cancel
```

优先查看 `status`、`error_code`、`pause_reason`、`sync_result`、消息/候选引用和
审计 trace。`failed` 不得被当成部分成功；`outcome_unknown` 只适用于出站操作，
自主运行自身失败应由调度器重试或进入人工处置。删除连接或撤销授权后，尚未开始的
运行应由撤权处理器取消或阻止 claim；如果运行已经被 claim，Worker 会自动暂停并只能
完成安全收尾，不能继续访问 Provider。
如果 owner 同时取消或另一 Worker 已取得新 fencing token，旧 Worker 的自动暂停不会
覆盖新的 durable 决策。

## 验收要求

本地验证使用 Sandbox：迁移 0008/0009/0010/0011/0012、replay、owner scope、pause/resume/cancel、
outbox outcome-unknown、sync/autonomy lease expiry、lease/fencing 和候选去重必须通过。生产启用前还需要持久 PostgreSQL/RLS、分布式
quota、Worker crash/lease expiry、Secret/KMS/扫描器不可用、账号撤权和 prompt
injection 演练；这些证据未完成前不得把界面文案改成“Agent 自动发信”。
