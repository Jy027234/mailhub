# MailHub Security / Privacy 指南

本文是工程控制清单，不是 PIPL、GDPR、Provider 条款或跨境传输法律意见。上线前必须由
组织 Security/Privacy/法务确认适用地区、处理者角色、保留期、跨境和用户同意。

## 数据分类与边界

| 数据 | MailHub 处理方式 | 禁止事项 |
| --- | --- | --- |
| 账号、thread/message metadata | tenant/subject scope、digest、bounded projection；To/Cc/Bcc/Reply-To 仅保留 normalized address projection | 用地址或正文作为高基数 telemetry 标签；保存 raw headers/display names |
| 正文/MIME/附件 | 加密 ObjectStore ref，按需授权读取 | 写入普通 DB、事件、日志、Agent memory |
| OAuth/SMTP 凭据 | 仅保存 credential ref，由宿主 Credential Broker 解析 | MailHub 接收、刷新或打印 token |
| 项目/知识候选 | revision、source refs、rights/security/approval evidence | 未审核直接写权威项目或组织知识 |
| 审计/遥测 | 脱敏 ID/hash/status/error/trace | 记录正文、密钥、原始 Provider 响应 |

## 必须通过的控制

- OAuth state/nonce/PKCE、redirect allowlist、CSRF/session fixation、token rotation/revoke
  由宿主注册与审计；真实渗透测试仍是发布门禁。
- 所有 tenant/user/account/folder/object/vector/trace 查询都要负向授权测试；缺 scope
  或身份上下文必须拒绝。
- HTML 使用安全纯文本/清洗策略，屏蔽 tracking pixel 和自动 URL 访问；MIME、解压、宏、
  zip bomb、AV/DLP/PII/rights 检查失败时进入 quarantine。
- 发送前检查外部域、BCC、群组、新增收件人、敏感附件、域风险、额度、approval、
  KillSwitch 和 sender interlock。
- 真实 Provider 连接撤权必须先调用 Host CredentialRevocationPort 并在确认有界状态证明后
  才写入 `REVOKED`；随后连接删除再清理 ObjectStore 引用、处理 TTL、审计 deletion proof，
  并通知 KnowledgeLifecycle/AgentMemory 下游执行 revoke/reindex；缺下游确认不得报告完成。
- 清理引用必须由仓储级全量、无正文快照提供，不能复用收件箱分页上限；删除前应覆盖所有
  message IDs、知识来源和 draft/message object refs，并在审计 proof 中记录实际清理数量。

## 日志与响应

日志、trace、metric、备份和 dead-letter 采用相同 secret/正文/PII 扫描规则；错误只返回
稳定 code、bounded detail 和 trace ref。发生泄漏、越权、误发或知识污染时立即暂停相关
出站/规则，保留 evidence，隔离凭据，执行对账和删除/撤权流程，按组织 incident policy
评估通知义务。

当前 M2/M3 真实 Provider、AV/DLP/PII、Secret Manager、生产 RLS/PITR、法律评审和渗透
证据尚未完成；配置或 UI 不得把 `contract_only`/`preflight_*` 显示为安全或 GA。
