# MailHub 故障排查与数据生命周期

## 快速分流

| 现象 | 先看 | 处置 |
| --- | --- | --- |
| `/health/ready` 非 ready | 必需 Port、PostgreSQL、对象存储、EventPublisher/Telemetry/Quota | 保持 degraded；不要绕过 fail-closed 配置 |
| 运行停在 queued/running | worker lease、fencing token、quota、dead-letter | 等待有界重试；过期 lease 由 recovery 处理 |
| `reauthorization_required`/`revoked` | connection status、OAuth/凭据撤销审计 | 暂停运行，重新授权后由 owner 显式恢复 |
| `outcome_unknown` | operation/provider request ref、reconciliation | 对账或人工处置；禁止盲目重发 |
| Provider search degraded | response 的 mode/coverage/incomplete_reason | 使用 metadata 结果；M2/M3 尚未通过真实 Provider 门禁 |
| 候选不入库 | revision、approval、AV/DLP/PII/rights gate | 修正事实或完成审核，不降低门禁 |
| 内容/附件无法打开 | content policy、scan/quarantine、对象 TTL | 只显示 metadata；等待扫描/授权，不直接下载 |

## 数据导出与删除

1. 记录请求主体、tenant、connection、范围、原因和 trace；先确认请求者有权限。
2. 使用 bounded export；是否包含正文必须显式选择并记录，导出包不携带 credential 或
   ObjectStore 原始句柄。
3. 删除先进入 `DELETING`，撤销 Credential Broker 引用，停止/取消未开始任务，清理受治
   理对象引用，并要求 KnowledgeLifecycle/AgentMemory 返回下游 revoke/reindex proof。
4. 失败可重试；只有所有必需证据完成才进入 `DELETED`。保留最小 deletion proof，不保留
   被删除正文。按适用法律/合同保留不可删除的审计证据时，必须记录例外和期限。

## 证据要求

每次故障报告至少包含版本、配置摘要（不含 secret）、tenant/subject scope、operation/job
ref、时间线、错误码、重试次数、KillSwitch/审批状态、数据影响判断和回滚/恢复结果。
本地 Sandbox 测试只证明合同；生产报告还要附真实 Provider、持久数据库、对象存储、
扫描器和宿主权限证据。
