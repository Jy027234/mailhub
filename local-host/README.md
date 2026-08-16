# MailHub B4 本地激活宿主

归档内自包含的最小宿主，用于执行 Gmail 真实只读激活（runbook A0–A2）。
它实现 `docs/host-adapter-sdk.md` 记载的 `/v1/mail-host/*` HTTP 合同，
OAuth 凭据代理直接复用归档中的参考实现
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
