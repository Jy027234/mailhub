# Deployment examples

## Helm chart（`helm/mailhub`）

生产部署契约。chart 只引用 operator 拥有的 Secret，从不内联凭据；它拒绝可变镜像，
并在不安全组合下**拒绝渲染**。

### 渲染与校验

```powershell
$env:HELM_BIN = "C:\tools\helm.exe"      # 或把 helm 放进 PATH
helm template my-release .\helm\mailhub --set image.digest=sha256:<64 hex>
python scripts\check_helm_chart.py        # 40 项断言，含负向用例
```

`check_helm_chart.py` 已纳入 `build_release.py --gates full`。**helm 缺失即失败**——
"chart 未验证"绝不能被算成通过。

### 默认渲染出的资源

| 资源 | 默认 | 说明 |
| --- | --- | --- |
| Deployment（api） | 是 | 打包的 `mailhub-api` 唯一入口 |
| Service / ServiceAccount | 是 | ServiceAccount 关闭 token 自动挂载 |
| PodDisruptionBudget | 是 | `minAvailable: 1` |
| NetworkPolicy | 是 | 入站仅同命名空间；出站默认只放 DNS，其余需显式列出 |
| Ingress / HPA | 否 | `ingress.enabled` / `autoscaling.enabled` 打开 |

### 容器加固（由门禁断言，不靠评审）

`runAsNonRoot` + `runAsUser: 65534`（与镜像 `USER nobody` 对齐）、`readOnlyRootFilesystem`、
`allowPrivilegeEscalation: false`、`capabilities.drop: [ALL]`、`seccompProfile: RuntimeDefault`、
`automountServiceAccountToken: false`、requests+limits 必填、`maxUnavailable: 0` 滚动更新、
`/health` 存活 + `/health/ready` 就绪。

### fail-closed 保证

| 场景 | 行为 |
| --- | --- |
| `image.digest` 为空或写成 tag | 被 `values.schema.json` 的 `^sha256:[a-f0-9]{64}$` 拒绝 |
| `runtime.allowSandbox=true` | 拒绝（schema `const: false`） |
| `runtime.outboundEnabled=true` 但 kill-switch 键缺失或为空 | 拒绝渲染 |
| `providers.imap.enabled=true` 但 host/smtpHost 为空 | 拒绝渲染 |
| `providers.imap.smtpSendEnabled=true` 但 outbound 未开 | 拒绝渲染（发送双重门） |
| values 里出现未声明的键（拼写错误） | 拒绝（`additionalProperties: false`） |

### 国内主路径（IMAP/SMTP + 授权码）

```powershell
helm template my-release .\helm\mailhub --set image.digest=sha256:<64 hex> \
  --set providers.imap.enabled=true \
  --set providers.imap.host=imap.qiye.163.com \
  --set providers.imap.smtpHost=smtp.qiye.163.com
```

Gmail / Microsoft Graph 默认关闭，与 `docs/provider-compatibility.yaml` 的出范围/可选口径一致。

### 明确未包含（诚实边界）

- **worker / scheduler 进程**：`MailWorker` 是库式单元（`src/mailhub/worker.py` 开头即声明
  "调度与进程监管属于部署基础设施"），本仓没有打包的 worker 入口，因此 chart **不模板
  worker Deployment**——凭空造一个假进程比不做更糟。这是 `MAIL-DIST-001` 的剩余项。
- **数据库迁移**不由 chart 执行：发布前由 operator 运行
  `python scripts/apply_migrations.py --direction upgrade`。
- **Secret 对象**由平台 Secret Manager 集成创建，chart 只按名引用。
- 镜像中的 **worker/scheduler 镜像**（MAIL-DIST-001 的四镜像目标）尚未发布。

## Sandbox compose

`docker-compose.sandbox.yml` 仅本地沙箱：出站关闭，不宣称 PostgreSQL 生产能力。

## 运行契约

镜像使用打包的 `mailhub-api` 入口，`--host`/`--port` 是仅有的进程级绑定开关。在
`production`/`staging` 下入口会选择 fail-closed 的 durable 图：PostgreSQL、带鉴权的 Host Ports、
且只注册显式启用的真实 Provider connector——它永不注册 Sandbox connector，也不回退到内存状态。
