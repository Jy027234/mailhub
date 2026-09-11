# MailHub 参考宿主（reference host）

本目录是接入 MailHub 的**参考实现**：一个自包含宿主应用，完整实现
`docs/host-adapter-sdk.md` 记载的 `/v1/mail-host/*` HTTP 合同。它既可以直接照抄，
也可以当作"我的产品每个 Port 应该返回什么"的对照样板。

它同时承担本仓的真实邮箱验证（Gmail 只读激活 runbook A0–A2、网易企业邮箱 IMAP 切片、
受控发送闭环）。OAuth 凭据代理复用参考实现
`apps/bff/src/caplatform_bff/mailhub_credentials.py`（不复制、不分叉）。

**诚实边界（这不是生产宿主）**：

- OAuth 凭据代理是 CAPlatform 参考实现的受控本地 Beta 适配器
  （加密 SQLite），生产需替换为 Secret Manager/KMS 后端；
- identity 为单用户本地模式（有 scope 即允许）；approval 是本地确认；
- AI/AV/DLP 端点为 fail-closed 的 `503 unconfigured`；
- 本宿主生成的所有证据都是本地合同证据，**不等于**真实 Provider 激活证据。

## 目录

| 文件 | 作用 |
| --- | --- |
| `local_host/` | 宿主实现（FastAPI） |
| `tests/test_local_host.py` | 宿主合同测试（含真实 broker 生命周期 MockTransport 演练） |
| `scripts/load_env.ps1` | 加载 `.env` |
| `scripts/run_host.ps1` | 启动宿主（127.0.0.1:8090） |
| `scripts/run_mailhub.ps1` | 启动 MailHub durable 图（127.0.0.1:8000） |
| `scripts/b4_smoke.py` | 接线预检（不接触 Provider） |
| `scripts/b4_a1.py` | A1 走查：授权 → 重放拒绝 → refresh → revoke → 第二次授权 |
| `docker-compose.yml` | 本地 PostgreSQL 16.4（端口 5433） |
| `.env.example` | 共享环境模板（宿主 + MailHub + Google OAuth） |

## 生产 Secret 后端（Vault）

设置 `HOST_VAULT_ADDR` 与 `HOST_VAULT_TOKEN` 后，宿主**只在本地库保存指针**
（`vault:secret/<prefix>/<ref>`），应用授权码本体存放在 Vault KV v2；未配置时保持原有的加密
SQLite 本地/开发后端。轮换 = 同指针原地更新并升版本；撤销 = 从 Vault **删除材料**（不只是改本地状态）。

```powershell
docker run -d --name mh-vault -p 127.0.0.1:8200:8200 `
  -e VAULT_DEV_ROOT_TOKEN_ID=mh-dev-root-token hashicorp/vault:latest
$env:HOST_VAULT_ADDR = "http://127.0.0.1:8200"; $env:HOST_VAULT_TOKEN = "mh-dev-root-token"
python scripts\vault_backend_conformance.py --json vault.json
```

15 项检查覆盖：库内无明文/无摘要（**含 WAL 伴随文件扫描**）、Vault 可读、resolve 与跨租户拒绝、
轮换同指针升版本、revoke 删除材料、撤权后 fail-closed。

## 自证：conformance kit

参考宿主会驱动自己走完 Host Port 矩阵（真实 HTTP 契约、进程内 ASGI），并用 MailHub 的
conformance kit 校验，产出可复核的 bundle：

```powershell
python scripts\emit_conformance_bundle.py --json host-conformance.json
# host   : mailhub-reference-host
# areas  : 11
# checks : 40
# result : pass
```

覆盖 11 个 Port 区域共 40 项检查（含 `AuditPort` 的递归脱敏断言）。测试见
`tests/test_host_conformance.py`。这是本地开发证据，不是生产 Provider 证据：
生产宿主的 identity/approval/Secret 后端必须替换为真实实现。

## 快速开始

```powershell
cd local-host
pip install -r requirements.txt
Copy-Item .env.example .env   # 填入 MAILHUB_HOST_SERVICE_TOKEN、HOST_ENCRYPTION_SECRET 等
docker compose up -d          # PostgreSQL

# 迁移（一次性）
cd ..\packages\mailhub
python scripts/apply_migrations.py --direction upgrade

# 启动两个进程
cd ..\local-host
pwsh -File scripts\run_host.ps1
pwsh -File scripts\run_mailhub.ps1     # 另一个终端

# 预检接线
python scripts\b4_smoke.py
```

## 配置 Google OAuth 客户端（一次性）

1. 打开 [Google Cloud Console](https://console.cloud.google.com/) → 新建/选择一个**测试专用**项目。
2. 启用 **Gmail API**。
3. **APIs & Services → OAuth consent screen**：
   - User Type 选 **External**，发布状态 **Testing**（无需 Google 审核）；
   - 将隔离 Gmail 测试账号加入 **Test users**；
   - 不需要添加任何 scope（应用只在运行时请求 `gmail.readonly`）。
4. **Credentials → Create Credentials → OAuth client ID**：
   - Application type: **Web application**；
   - Authorized redirect URIs: `http://127.0.0.1:8090/oauth/gmail/callback`；
   - 保存后把 **Client ID** 和 **Client secret** 填入 `.env`：
     - `HOST_GMAIL_ENABLED=true`
     - `HOST_GMAIL_CLIENT_ID=...` / `MAILHUB_GMAIL_CLIENT_ID=...`（两处一致）
     - `HOST_GMAIL_CLIENT_SECRET=...`（**只**进宿主，绝不进 MailHub）
     - `MAILHUB_GMAIL_ENABLED=true`
5. 重启两个进程，重跑 `b4_smoke.py`，然后执行 A1 走查：

```powershell
python scripts\b4_a1.py
```

浏览器会打开 Google 同意页（测试账号、只读 scope）。完成后按脚本提示：
点击回调页的【故意重放同一 state】链接生成拒绝证据 → 回车 → 脚本执行
refresh 与 revoke → 再开第二次授权生成 A2 活动连接。

## IMAP 应用密码路径（免 Google Cloud，先验证真实邮箱）

不需要任何 Google Cloud 项目，适合先证明"真实邮箱 → durable 同步 → 分析"
整条价值链。**该路径是 IMAP 传输的本地工程证据，不关闭 Gmail REST (M2) 激活门。**

1. 隔离 Gmail 账号打开**两步验证**（[Google 账号安全页](https://myaccount.google.com/security)）。
2. 生成**应用专用密码**（Security → 2-Step Verification → App passwords），
   记录 16 位密码（只用于本地 `.env` 或脚本交互输入，不提交）。
3. `.env` 启用 IMAP：

   ```text
   MAILHUB_IMAP_ENABLED=true
   MAILHUB_IMAP_HOST=imap.gmail.com
   MAILHUB_SMTP_HOST=smtp.gmail.com
   MAILHUB_SMTP_SEND_ENABLED=false
   ```

4. 重启两个进程 → `b4_smoke.py`（9/9，`/health/ready=ready`）→ 走查：

   ```powershell
   $env:HOST_IMAP_USERNAME = "隔离邮箱@gmail.com"
   $env:HOST_IMAP_APP_PASSWORD = "16位应用密码"
   python scripts\b4_imap.py --received-after 2026-07-01T00:00:00Z --received-before 2026-08-31T00:00:00Z
   ```

   脚本依次执行：应用密码加密入库（宿主）→ 创建/激活连接 → 有界 backfill 同步
   （幂等键重放验证）→ 增量同步 → 线程/消息投影检查 → 规则模式智能分析 →
   写入 `b4-imap-state.json` 证据。发送保持关闭（`supports_send=false`）。

## 受控发送测试（草稿→审批→outbox→SMTP 真实发信）

默认发送关闭。走一次受控发送闭环（发一封测试邮件给自己）：

1. `.env` 开启三处（并重启 host 与 mailhub）：

   ```text
   MAILHUB_OUTBOUND_ENABLED=true
   MAILHUB_SMTP_SEND_ENABLED=true
   MAILHUB_KILL_SWITCH_ENDPOINT=http://127.0.0.1:8090
   ```

2. 启动本地出站 worker（宿主拥有的调度单元，租约/防重由 MailHub durable 合同保证）：

   ```powershell
   pwsh -File scripts\run_worker.ps1
   ```

3. 执行发送测试（宿主确认 → 策略/委托 → 草稿 → 审批绑定入队 → worker 发送）：

   ```powershell
   python scripts\b4_send.py
   ```

   脚本会轮询 durable operation 到 `succeeded`，并写 `b4-send-state.json`。
   邮件到达后重跑一次增量同步即可在本地投影中看到这封测试邮件（收发闭环）。
   本地宿主的 approval 是单用户本地确认、kill-switch 恒允许——生产宿主必须
   替换为四眼审批与真实开关权威。

## AI 模型网关 + 每日自主周期（智能部分）

宿主 AI 端口默认是确定性规则直通。配置 OpenAI 兼容网关后，分析变为
**规则基线 + 模型增强**：规则先跑（注入检测是硬门，命中注入绝不调模型），
模型输出经 MailHub 自己的 AI merge 合同校验，任何网关故障自动回退规则，
分析永不中断。正文会发送到网关端点——这是宿主的**数据出境决策**，由 `.env` 记录。

```text
HOST_AI_GATEWAY_URL=https://model-router.edu-aliyun.com/v1
HOST_AI_GATEWAY_MODEL=qwen/qwen3.6-plus-2026-04-02
HOST_AI_GATEWAY_API_KEY=sk-...
```

验证模型已接通（重新分析任一邮件应返回 `mode=ai_enriched` 与模型名）：

```powershell
python scripts\b4_autonomy.py
```

自主周期 = 增量同步 → 对最新消息逐封 AI 分析 → 产出**今日候选清单**
（项目/任务/知识/跟进，全部 `requires_review=true`，绝不自动执行外部动作）。
结果写入 `b4-autonomy-state.json`。

## 日常使用闭环（每日摘要 + 审核应用 + 定时）

```powershell
# 1. 每日摘要：自主周期 + 生成 reports/daily-YYYY-MM-DD.html 报告
python scripts\b4_daily.py

# 2. 审核候选（报告里有可直接复制的命令）
python scripts\b4_apply.py --candidate <id>          # 批准并应用（写入宿主任务/知识账本）
python scripts\b4_apply.py --candidate <id> --reject # 拒绝
#    未审核直接应用会被拒绝（approval_required），重复应用幂等。

# 3. 注册每天早上 8 点的自动摘要（Windows 任务计划）
pwsh -File scripts\install_daily_task.ps1

# 4. 全量历史回填（按周窗口、有界、不推进增量游标）
python scripts\b4_backfill.py --start 2025-01-01
```

## A2 有界只读同步

A1 完成后按脚本输出的命令运行（需要 `MAILHUB_ACTIVATION_ALLOW_NETWORK=true`）：

```powershell
cd packages\mailhub
$env:MAILHUB_ACTIVATION_ALLOW_NETWORK = "true"
python scripts\run_provider_activation.py `
  --provider gmail --mode backfill --folder-ref INBOX `
  --received-after 2026-07-01T00:00:00Z --received-before 2026-08-31T00:00:00Z `
  --api-url http://127.0.0.1:8000 --tenant-id b4-tenant --subject-id b4-user `
  --connection-id <A2连接ID> `
  --evidence-root ..\local-host\provider-activation `
  --confirm-real-provider
```

然后按 runbook 采集并校验证据包（`collect_oauth_evidence.py`、
`validate_provider_evidence.py --require-real`、`validate_provider_bundle.py --require-real`）。
