# MailHub real-provider revoke boundary report

日期：2026-07-29（Asia/Shanghai）
范围：MailHub `:revoke` 的 Host credential revocation 顺序、fail-closed 和幂等合同；不是
真实 Gmail/Graph 运行证据。

## 变更

- 对非 Sandbox Provider，`POST /v1/mail/connections/{id}:revoke` 先取消已持久化订阅，
  再调用 Host `CredentialRevocationPort`，最后才写入 `ConnectionStatus.REVOKED`。
- 缺少 Host Port、Host 调用失败、未知状态或 token/credential-ref-shaped 响应都会拒绝
  状态转换并保留非终态连接。
- 允许的 Host 状态证明为 `revoked`、`already_revoked`、`accepted` 或
  `revoke_requested`；MailHub 只保存 `provider_grant_status`，不保存 credential ref、
  token 或 Host 原始响应。
- 已 `REVOKED` 连接进入独立 `:delete` 清理流程时不重复调用 Host 撤权；删除仍需对象、
  知识 downstream 和 tombstone 清理证据。
- Sandbox 只跳过 Host 撤权调用，用于确定性合同测试，不得当作 Provider 撤权证据。

## 本地证据

- `packages/mailhub/tests/test_privacy_lifecycle.py`：真实 Provider 撤权成功顺序、缺少
  Port、秘密响应和 delete 不重复撤权。
- `packages/mailhub/tests/test_host_adapters.py`：Host revoke 响应解包、请求范围和秘密
  响应拒绝。
- MailHub 包回归：307 tests passed；Ruff 与 strict mypy 通过。

## 仍需部署证据

该报告不证明真实 Credential Broker 已撤销 Gmail/Graph grant，也不证明撤权后的 Provider
请求计数为零。A1–A4 必须在隔离邮箱和真实 Host 端点上保存 `revocation-negative.json`，
交叉验证订阅取消、sync 拒绝、Provider/Credential Broker 请求为零和后续删除证明后，才可
勾选 M2/M3 的撤权相关条目。
