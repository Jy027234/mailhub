# MailHub 宿主接入 quickstart

把 MailHub 接进一个**新产品**的最小路径。本文只讲"怎么接上"，Port 的完整语义见
`host-adapter-sdk.md`，兼容承诺见 `docs/compatibility/mailhub-v1.md`。

## 0. 先选接入形态

| 形态 | 适用 | 依赖 |
| --- | --- | --- |
| 进程内嵌入 | 宿主是 Python 服务，想少一个进程 | `MailService` + Port 实现 + `create_app` |
| 独立服务（推荐） | 宿主语言不限，多产品共享一个邮件服务 | MailHub HTTP 服务 + Python/TypeScript SDK |
| 生产部署 | 多租户、真实邮箱、合规 | PostgreSQL、Secret 后端、全部强制 Port |

**结论**：产品接入优先选"独立服务"，宿主只实现 Port 并注入 tenant/subject，不复制 MailHub 领域代码。

## 1. 拿到产物

从发布包安装（`packages/mailhub/dist/release/<version>/`）：

```powershell
# 后端
python -m pip install .\dist\release\0.1.0\python\mailhub-0.1.0-py3-none-any.whl
# 前端（宿主是 Node 工程时）
npm install .\dist\release\0.1.0\ui
npm install .\dist\release\0.1.0\typescript-sdk
```

包名：Python `mailhub`；npm `@fyjtech/mailhub-ui`、`@fyjtech/mailhub-client`。

## 2. 实现 Host Port

MailHub 不认识宿主的框架和产品名词，只认 Port。**最小可跑集**（本地/演示）：

```python
from mailhub.api import create_app
from mailhub.hosts.second_host import build_second_host

_service, _connector, _host_actions, _knowledge = build_second_host()
app = create_app(_service)
```

这用的是内存仓储 + Sandbox connector，**只能验证合同，不能作为生产证据**。

**生产必接集**（缺一即 fail closed，不会退化为本地状态）：

| Port | 谁实现 | 关键约束 |
| --- | --- | --- |
| `HostIdentityPort` | 宿主身份系统 | 校验 tenant/subject/capability；**不得信任浏览器传来的 tenant 头** |
| `CredentialBrokerPort` | 宿主 Secret/KMS | 只返回短期凭据；token 不进 MailHub、不进日志 |
| `ObjectStorePort` | 宿主对象存储 | 加密、租户隔离、带 digest/TTL |
| `ApprovalPort` | 宿主审批 | 绑定 revision/action；不得自审批 |
| `AiExecutionPort` | 宿主模型网关 | 只处理有界、已分类输入 |
| `HostActionPort` | 宿主业务（项目/任务） | 按稳定 `action_id` 去重；失败不得重放已生效动作 |
| `KnowledgeSinkPort` | 宿主知识库 | 只收已审核引用；重试不得产生重复知识 |
| `AuditPort` | 宿主审计 | 只落脱敏 ID/hash/状态/证据引用 |
| `QuotaPort` | 宿主配额 | 原子租约 + 过期；进程内计数不是生产权威 |
| `KillSwitchPort` | 宿主开关 | 每个副作用前实时判定；不得从邮件内容推断 |

按需追加：`KnowledgeLifecyclePort`、`AgentMemoryPort`、`KnowledgeSafetyPort`（AV/DLP）、
`NotificationPort`、`EventPublisherPort`、`TelemetryPort`、`OAuthStateStore`、`OAuthCallbackPort`、
`ProviderSubscriptionPort`、`ProviderNotificationVerifierPort`、`SenderInterlockPort`。

不想逐个写 Python Protocol 时，用 `mailhub.hosts.http` 的通用 HTTP 适配器，只暴露宿主端点：

```text
MAILHUB_EVENT_PUBLISHER_ENDPOINT=...
MAILHUB_TELEMETRY_ENDPOINT=...
MAILHUB_QUOTA_ENDPOINT=...
MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT=...          # 仅在启用真实 push 时必需
MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT=... # 仅在启用真实 push 时必需
MAILHUB_HOST_SERVICE_TOKEN=...                      # 服务间鉴权，MailHub 不持久化
```

## 3. 启动

```powershell
# 本地/开发（Sandbox 默认，无真实 Provider）
mailhub-api --host 127.0.0.1 --port 8000

# 生产/预发：显式环境触发 durable runtime
$env:MAILHUB_ENV = "production"      # production | prod | staging
$env:MAILHUB_DATABASE_URL = "postgresql+asyncpg://..."
python scripts/apply_migrations.py   # 迁移是独立的运维步骤，服务不自动迁移
mailhub-api --host 0.0.0.0 --port 8000
```

生产图强制：PostgreSQL + 全部强制 Port + `MAILHUB_HOST_SERVICE_TOKEN`，**不注册 Sandbox connector**，
缺任何一项直接启动失败，而不是降级成内存实现。

## 4. 接入前端

```tsx
import { MailHubWorkspace } from "@fyjtech/mailhub-ui";
import "@fyjtech/mailhub-ui/styles.css";

<MailHubWorkspace client={mailHubClient} identityLabel="当前工作空间" />
```

宿主自持页面外壳时用 `MailHubWorkspace`；想要一个不绑定宿主 router/session 的完整外壳时用
`@fyjtech/mailhub-ui/standalone` 的 `MailHubStandalone`。主题通过 CSS 变量注入。
组件不保存 token、不渲染原始 HTML、不自行扩大 OAuth scope。文案不含硬编码：
用 `locale="en-US"` 切内置语言，或用 `messages={{ ... }}` 部分覆盖、传完整语言包做新语言；
`npm run build` 会拦住残留硬编码文案与丢失的占位符。可访问性用 `headingLevel={2}` 适配宿主
页面标题层级（standalone 已默认 2，保证只有一个 h1）；`npm test` 跑无头语义门禁（axe-core），
但颜色对比度/缩放仍需真实浏览器审计（详见 `ui/README.md` 覆盖表）。

## 5. 不可协商的三条约定

1. **作用域由宿主注入**：每个请求带 tenant/subject；浏览器不能选租户，MailHub 每次读/写前重新校验。
2. **幂等键由宿主生成并持久化**：每个 command 一个键，宿主必须保存"键→结果"关联；
   `outcome_unknown` 不得当成成功。
3. **副作用要审批**：propose/review 无副作用；apply/send 必须带已批准、revision 绑定的 action 与稳定幂等键。

## 6. 用 conformance kit 自证（推荐先做）

Port 矩阵已经变成可执行的检查。先让产品把"各 Port 实际返回了什么"记录成 bundle，再跑一遍：

```powershell
# 1) 生成起始模板
python scripts/host_conformance.py --emit-template --output host-conformance.json

# 2) 按模板填写你的产品真实返回值（每个 Port 区域都要有 deny/重复/过期观测）

# 3) 校验：任一检查失败即退出码 1
python scripts/host_conformance.py --bundle host-conformance.json
python scripts/host_conformance.py --bundle host-conformance.json --json   # 机器可读，可入 CI

# 4) 确认工具本身仍能识别违规（无需你的产品）
python scripts/host_conformance.py --self-test
```

覆盖 10 个 Port 区域共 38 项检查，重点不是"happy path 能跑通"，而是这些**必须失败**的行为：

| Port 区域 | 关键检查 |
| --- | --- |
| `HostIdentityPort` | 空 tenant/subject/capability 永不授权；必须有拒绝观测 |
| `CredentialBrokerPort` | 凭据有效期有界；返回字段不得含 token/secret/credential_ref |
| `ApprovalPort` | 未绑定/过期/跨作用域的确认必须被拒；同时要有一次合法接受观测 |
| `HostActionPort` | 同一 `action_id` 重放不得二次执行，且返回同一 result_ref |
| `KnowledgeSinkPort` | 同一 candidate 重试不得产生第二条知识；security/rights 状态必须显式 |
| `AuditPort` | 字段不得含正文/MIME/token/凭据引用 |
| `QuotaPort` | 租约必须有界且会过期 |
| `KillSwitchPort` | 每个副作用都要显式 allow/deny，不能靠推断默认 |
| `EventPublisherPort` | 事件 ID 稳定、类型明确、信封仅元数据 |
| `TelemetryPort` | 属性仅元数据；不得含按地址的高基数标签 |

Python 宿主也可以直接调用：`from mailhub.hosts import run_host_conformance, bundle_from_mapping`。
bundle 校验是离线的——它只检查你的返回值，不会调用你的服务或 Provider。

## 7. 人工自检清单

- [ ] 未登录/越权请求被拒绝，且不是"降级放行"
- [ ] 缺 Port 时启动失败（而不是退回内存实现）
- [ ] 重复提交同一幂等键不产生第二条业务事实
- [ ] 审批 ref 被篡改/跨租户复用时拒绝
- [ ] 审计里没有正文、MIME、token、凭据引用
- [ ] 前端列表只渲染 metadata，正文按需读取
- [ ] 撤权后新同步立即失败，且保留可核查的投影

## 8. 常见失败

| 现象 | 原因 |
| --- | --- |
| 服务启动即失败 | 生产环境缺 PostgreSQL 或强制 Port |
| `mailhub_host_service_unauthorized` | `MAILHUB_HOST_SERVICE_TOKEN` 不匹配或未注入 |
| Provider 显示 not-ready | Provider 未配置或未通过 preflight；这是 fail closed，不是 bug |
| 候选无法 apply | 审批 ref 非本租户/本 revision 签发，或浏览器自造 |
| 邮件列表为空 | 只有 metadata 投影；正文需按需读取，且未同步时不会伪造 |

## 9. 参考

- **可运行的参考宿主**：`local-host/`（完整实现 `/v1/mail-host/*`，自证通过 conformance kit；
  直接照抄或作为对照样板）

- Port 完整语义与 HTTP 契约：`host-adapter-sdk.md`
- 兼容承诺与破坏性变更策略：`docs/compatibility/mailhub-v1.md`
- Provider 能力与激活阶段：`provider-compatibility.yaml`、`docs/mailhub-provider-activation-runbook.md`
- 生产运维：`docs/mailhub-admin-guide.md`、`docs/mailhub-migration-runbook.md`
- 最小第二宿主示例：`examples/second_host/`
