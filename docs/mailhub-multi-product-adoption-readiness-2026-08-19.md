# MailHub 多产品引入就绪缺口清单

- 状态：基线已建立；下列缺口全部开放，未验收
- 日期：2026-08-19
- 范围：MailHub 作为**可被 CAPlatform 之外产品引入**的独立智能邮件模块
- 不含：CAPlatform 回接本身（见 `docs/intelligent-mail-management-module-master-todo-2026-07-28.md` 的 B0–B6 与 MX）
- 关联：`docs/adr/0010-mailhub-development-release-standard.md`、`docs/compatibility/mailhub-v1.md`、`packages/mailhub/docs/provider-compatibility.yaml`、`packages/mailhub/docs/host-adapter-sdk.md`

## 1. 结论

架构地基已经具备"可被引入"的形态：provider-neutral 领域核心 + Host Port 契约 + Python/TypeScript SDK
+ 可嵌入 React UI + 第二宿主示例 + v1 兼容矩阵。但"另一个产品能真正引进去并上生产"还缺四件事：
**能被装上、能合法用、能接得动、能上生产**。这些缺口与 CAPlatform 无关，属于产品化本身。

### 引入深度与现状

| 引入方式 | 现状 | 主要缺口 |
| --- | --- | --- |
| 代码级复用（复制源码） | 基本可用：`check_import_boundaries.py` 已阻止反向依赖 | 无硬缺口；需明确公共/内部模块边界 |
| 包级依赖（`pip install` / `npm install`） | 不可用：无任何已发布产物 | 发布流水线、版本与变更日志、包完整性签名 |
| 服务级部署（部署 MailHub 服务） | 半成品：只有最小 Helm 与沙箱 compose | 生产镜像、完整 Helm、生产 Secret 后端 |
| 产品级集成（嵌 UI + SDK） | 半成品：UI 可注入但未产品化 | 中性包名、i18n、WCAG 2.2 AA |

判定规则沿用 ADR 0010：Sandbox / fixture 通过永远不能代替外部真实证据；未勾选项不得因本地测试变绿而勾选。

## 2. 已具备的基础（不重复建设）

| 能力 | 证据 |
| --- | --- |
| provider-neutral 领域核心与 Host Port 契约 | `packages/mailhub/README.md`、`docs/host-adapter-sdk.md` |
| 版本化 HTTP 合同与兼容承诺 | `schemas/mailhub.openapi.v1.json`、`docs/compatibility/mailhub-v1.md`（v1 内只增字段） |
| Python / TypeScript 薄客户端 | `packages/mailhub/sdk/`、`examples/sdk/` |
| 可嵌入 React UI 与 standalone shell | `packages/mailhub/ui/`（`MailHubWorkspace` / `MailHubStandalone`） |
| 第二宿主最小示例 | `packages/mailhub/examples/second_host/`、`tests/test_second_host.py` |
| 迁移适配器（宿主侧） | `hosts/legacy.py` 的 `AeroLinkMailAdapter` / `CAACTRAININGMailAdapter` |
| 部署粗糙件 | `deployment/helm/mailhub`、`deployment/docker-compose.sandbox.yml` |
| 迁移运行器与运行手册 | `scripts/apply_migrations.py`、`docs/mailhub-migration-runbook.md` |
| 运维/安全/用户文档集 | `docs/mailhub-{admin,user,developer,security-privacy,troubleshooting}-guide.md` |
| 既有门禁脚本 | `scripts/verify.ps1`、`generate_sbom.py`、`check_license_gate.py`、`check_provenance.py`、`check_secrets.py` |

## 3. 缺口清单

### 3.1 分发与版本（"能被装上"）

| ID | 现状 | 缺口 | 验收条件 |
| --- | --- | --- | --- |
| `MAIL-DIST-001` | 无 OCI 镜像 | 发布 API/Webhook/Worker/Scheduler 四个镜像，固定 digest | 镜像 + digest 记录 + SBOM/provenance |
| `MAIL-DIST-003` | Python SDK 为源码客户端（`docs/compatibility/mailhub-v1.md` 自述 pre-dist） | 发布 `mailhub` wheel/sdist 与 SDK 分发物 | 可 `pip install` 的产物 + 完整性哈希 |
| `MAIL-DIST-004` | UI 包未发布 | 发布 UI 包与 standalone shell | 可 `npm install` 的产物 + 版本记录 |
| `MAIL-DIST-009` | 无 N/N-1 兼容矩阵 | 数据库升级/回滚、蓝绿、feature flags、协议版本握手 | N/N-1 升级与回滚演练记录 |
| `MAIL-ADOPT-001` | 无 CHANGELOG、无版本策略、无发布流水线 | 版本语义、变更日志、发布流水线（构建→门禁→SBOM→签名→清单） | 一条命令产出可复现发布包 + `release-manifest.json` |

### 3.2 命名与许可解耦（"能合法用"）

| ID | 现状 | 缺口 | 验收条件 |
| --- | --- | --- | --- |
| `MAIL-ADOPT-002` | ✅ **已完成（2026-08-19）**：npm 作用域统一为 `@fyjtech`（`@fyjtech/mailhub-ui`、`@fyjtech/mailhub-client`）；Python 分发名 `mailhub` 中性 | 剩余：非许可类宿主专属 prose（`.env.example`、`docs/host-adapter-sdk.md`、`examples/second_host`、`src/mailhub/hosts/http.py`）按需中性化 | 对外产物无 `caplatform` 命名（已达成）；prose 中性化清单完成 |
| `MAIL-ADOPT-003` | **暂缓（owner 决定 2026-08-19：先不做许可管理）**：整包仍为 CAPlatform 专有（`UNLICENSED`），已从发布阻断项降为 deferral 记录 | 面向其他产品的许可模式（商业授权/双许可） | 决策记录 + 对外许可文件 |

> 命名与许可是**发布硬前置**：未决前不得向任何共享 registry 发布（`MAIL-ADOPT-001` 的构建产物可先落地但不发布）。

### 3.3 宿主接入套件（"能接得动"）

| ID | 现状 | 缺口 | 验收条件 |
| --- | --- | --- | --- |
| `MAIL-ADOPT-004` | ✅ **已完成（2026-08-19）**：`local-host/` 升格为参考宿主并补齐唯一缺失的必接 Port（`/v1/mail-host/audit` + durable 递归脱敏账本）；新增 `scripts/emit_conformance_bundle.py` 自证入口（11 区域/40 检查/pass）；`replayed`/`created`/租约时长/kill-switch 显式化 | 剩余：接入产品照抄后替换 identity/approval/Secret 后端 | 参考宿主可独立启动并通过端到端合同测试（已达成：23 tests + conformance pass） |
| `MAIL-ADOPT-005` | ✅ **已完成（2026-08-19）**：`src/mailhub/hosts/conformance.py` + `scripts/host_conformance.py`（`--bundle`/`--emit-template`/`--self-test`/`--json`），10 个 Port 区域 38 项检查；`tests/test_host_conformance.py` 15 项测试通过 | 剩余：接入产品按模板填真实返回值并留存报告 | 一条命令产出 conformance 报告（已达成） |
| `MAIL-ADOPT-006` | ✅ **初版已落地（2026-08-19）**：`packages/mailhub/docs/host-quickstart.md` —— 三层接入形态、最小/生产 Port 集、启动与迁移、前端接入、三条不可协商约定、自检清单、常见失败 | 剩余：用第二个真实产品从头走一遍并留存记录（含非 Python 宿主） | 按文档从零接入成功的记录 |

### 3.4 部署与密钥后端（"能上生产"）

| ID | 现状 | 缺口 | 验收条件 |
| --- | --- | --- | --- |
| `MAIL-ADOPT-007` | ✅ **已完成（2026-08-19，本地渲染验证）**：新增 `values.schema.json`（`additionalProperties: false` + 摘要正则 + sandbox `const:false`）、`_helpers.tpl`（标签/镜像/校验/env 复用）、`service.yaml`/`serviceaccount.yaml`/`ingress.yaml`/`hpa.yaml`/`pdb.yaml`/`networkpolicy.yaml`/`NOTES.txt`；容器加固（非 root、只读根、无 capability、seccomp、关 token 挂载）、探针、资源限额、`maxUnavailable: 0`；国内 IMAP/SMTP 开关为一等公民；`scripts/check_helm_chart.py` 40 项断言（含 5 个负向用例）已入 `--gates full`，helm 缺失即失败 | 剩余：真实集群部署与 `helm template` 之外的集群级验证 | 集群部署通过（本地渲染已验证：`helm chart gate: ok`） |
| `MAIL-ADOPT-008` | ⚠️ **Vault KV v2 后端已建成并实测（2026-08-19）**：新增 `local-host/local_host/vault.py`（KV v2 读写删 + 有界脱敏错误 + 可注入 transport）与 `HOST_VAULT_ADDR/TOKEN/MOUNT/PREFIX` 配置；宿主库里**只存指针** `vault:secret/<prefix>/<ref>`，口令本体在 Vault。**真实 Vault dev 下 15/15 检查通过**：库里无明文/无摘要（含 WAL 伴随文件扫描）、Vault 可读、resolve 返回口令且跨租户拒绝、**轮换原地更新同指针并升版本**、**revoke 真的从 Vault 删除材料**、撤权后 resolve fail-closed。证据 `docs/reports/mailhub-vault-secret-backend-2026-08-19.json`（离线 `--validate`）+ 12 项单测 | 剩余：KMS/OpenBao 变体、真实集群 Vault 的 HA/审计后端、轮换调度 | 真实 Secret 后端下的 resolve/rotate/revoke 证据（已达成） |
| `MAIL-ADOPT-009` | ⚠️ **备份/恢复 + 迁移 N-1 演练已完成（2026-08-19）**：`scripts/dr_drill.py` 在真实 PostgreSQL 16.4 上跑完整演练，**19/19 通过**——先取基线（表数/RLS 标记/迁移账本），`pg_dump -Fc` 取外部副本，`DROP SCHEMA` 造成灾难，再恢复并逐项比对；**RPO 边界是被证明的而非假定**（备份后写入的行在恢复后必须消失）；迁移窗口回滚到 0016（账本 + 后期 schema 同步消失）再升级复原。**RTO 实测 2.1–6.5s**（同一工作站多次运行）；证据 `docs/reports/mailhub-dr-drill-2026-08-19.json`（离线校验 + 9 项测试） **2026-09-11 PITR/WAL 归档已闭环**：BQscripts/pitr_drill.pyBQ 在真实 PostgreSQL 16.4 上跑时间点恢复，**10/10 通过**——开 BQarchive_modeBQ + BQarchive_commandBQ 并重启 → BQpg_basebackup -Fp -Xs -RBQ 取基线 → 写目标前行 → 隔 1.2s 取时间点（边界无歧义）→ 写目标后行 → 切 WAL 段 → 以 BQrecovery.signalBQ + Brestore_commandBQ + Brecovery_target_timeBQ 拉起恢复库。归档 5 段、**4 个 WAL 文件确实由归档恢复**（按日志 BQrestored log fileBQ 计数，不是看配置），日志有明确的 BQrecovery stopping before commitBQ，目标前写入存在、**目标后写入不存在**、恢复库总行数为 1（证明是按时间点恢复而非全有或全无）；证据 BQdocs/reports/mailhub-pitr-drill.jsonBQ（离线 BQ--validateBQ + 8 项契约测试） **2026-09-11 备库切换已闭环**：BQscripts/failover_drill.pyBQ 双容器真流复制 + 真切换，**14/14 通过**——复制槽 + BQpg_basebackup -R -SBQ 建备库、BQstate=streamingBQ、主库写入实时到达备库、**BQdocker killBQ 主库容器**（非重启）、备库 promote、**时间线 BQ00000001 → 00000002BQ**、切换前行存活且切换后可写入；证据 BQdocs/reports/mailhub-failover-drill.jsonBQ（离线校验 + 9 项契约测试）。单容器方案被证伪：官方镜像里 postgres 是 PID 1，内核忽略发给 PID 1 的 SIGQUIT，集群杀不掉 **2026-09-11 容量压测已闭环**：`scripts/capacity_drill.py` 驱动真实仓储打在真实 schema 上（1000 条 / 并发 8 / 读 60 次），**9/9 通过**——写入 **351.6 msg/s**、读 **p95 10.3 ms**、并发 0 错误、重放不重复、跨租户 0 行、库内实计 1000/1000；证据 `docs/reports/mailhub-capacity-drill.json`（离线校验 + 10 项契约测试） | 剩余：**真实故障注入（进程/网络/磁盘）**——需生产级数据库环境 | 演练报告 + 量化目标（备份恢复 + PITR + 备库切换 + 容量基线四项本地演练已达成） |

### 3.5 真实 Provider 证据（"敢用"，与宿主无关）

| ID | 现状 | 缺口 | 验收条件 |
| --- | --- | --- | --- |
| `MAIL-IMAP-002/004` | 有 UIDVALIDITY/UID/MODSEQ 游标与受控 poll | IDLE、folder rename/delete、断线/并发/部分 FETCH 的真实服务器行为 | 服务器矩阵下的行为记录 |
| `MAIL-IMAP-005` | ⚠️ **矩阵 3/4 行已实测（2026-09-11 复测修订）**——163、QQ、本地 Dovecot 2.4 夹具；采集器 `scripts/imap_server_matrix_probe.py`（只读、离线 `--validate`、`--ca-file` 支持私有 CA、37 项测试），证据 `docs/reports/mailhub-imap-server-matrix-2026-09-11.md` + 三份 JSON。**本次复测推翻旧版两条结论**：旧版"Dovecot 实现了 UIDPLUS/CONDSTORE 却不广告"是**采集缺陷造成的假象**——旧采集器读 `client.capabilities`，而 imaplib 登录后并未刷新它，于是把认证前那份刻意收窄的 8 项列表当成了最终结果；同一会话显式 `CAPABILITY` 返回 **41** 项（含 CONDSTORE/QRESYNC/UIDPLUS/MOVE/NAMESPACE）。能力集修正为 163 **9** 项（新增 IDLE、UIDPLUS）、QQ **11** 项（XOAUTH2 实为不支持）、Dovecot **41** 项；采集器已改为认证后显式 CAPABILITY 并加回归测试。**新发现**：163 的 `UID SEARCH ALL` 只覆盖 1634 封中的 182 封（证据位 `search_visibility_limited`），SEARCH 结果数不能代表邮箱真实规模，增量同步必须与 EXISTS/UIDNEXT 交叉校验。连接器 `connectors/imap_smtp.py` 一直发显式 CAPABILITY，**从未**因该假象降级；本次另补防御性正确性：`ENABLE CONDSTORE` 必须在 `SELECT` 之前协商（RFC 7162 §3.1.8，否则 HIGHESTMODSEQ 不出现在 SELECT 响应里），由 `scripts/imap_condstore_live_probe.py` 对真实 Dovecot 验证 8/8（游标 `1789138475:1:2`） | 剩余：自建 Exchange 一行**环境阻塞**（`blocked_by_environment`，2026-09-11 所有者决定）——Exchange Server 无容器镜像且非可容器化形态（Windows Server 角色 + 必须先有 AD DS + 宿主级前置组件 + 数十 GB ISO + 主机名/域绑定），本地 Docker 无法提供该行，唯一路径是真实 Windows Server 虚拟机或客户现场环境；**明确不做**替身冒充。`MAIL-IMAP-005` 保持未勾选 | 逐服务器兼容与差异记录（3/3 可测行已达成） |
| `MAIL-SMTP-001/002` | ⚠️ **线上夹具已建成（2026-08-19）**：`scripts/smtp_wire_conformance.py` 驱动**真实 connector** 打到本地隐式 TLS 抓包服务器（自签证书经 `SSL_CERT_FILE` 受信），**7/7 用例通过**并产出证据 `docs/reports/mailhub-smtp-wire-conformance-2026-08-19.json`（含离线 `--validate`、14 项测试）。已线上验证：信封发件人=认证账号、信封收件人=To∪Cc∪Bcc、**Bcc 不落传输头**、Message-ID 与回执一致、回复头正确、**正文/主题里夹带的地址进不了信封**、发送关闭与缺凭据 fail-closed | ✅ **真实 163 端到端往返已验证（2026-08-19）**：新增 `scripts/smtp_provider_roundtrip.py`，用**真实 connector** 向自己发一封带 `[mailhub-roundtrip <marker>]` 前缀的邮件，再经 incremental sync 读回并逐项核对：**收到的 Message-ID 与回执完全一致**、主题/正文标记存活、收件人集合存活、发件人=账号、单次通过无重复、二次通过只见一次（9/9 通过，3 次轮询 16.6s）；证据 `docs/reports/mailhub-smtp-provider-roundtrip-163-2026-08-19.json`（脱敏：账号只存域名+摘要，离线 `--validate` 通过，14 项测试）。剩余：`OUTCOME_UNKNOWN` 真实对账。**2026-09-11 四眼审批接口已落地（加法式，方案 A）**：查证发现该语义此前**并非缺失于文档，而是缺失于实现**——`ports.py` 的 `ApprovalPort.verify_confirmation` 根本没有审批人身份参数，端口层无法表达"由另一个人批准"；一致性套件只检查绑定/过期/跨作用域，不检查职责分离与重放；`local-host/stores.py` 的 `verify_approval` 还有一条 fail-open 旁路（`action_id`/`action_digest` 为空时对任意 action 直接 `True`），且审批行从不标记已消费、可无限重放。本次改动：`verify_confirmation` 新增**可选**关键字 `approver_subject_id`（缺省行为不变，下游产品无需改造）；核心在"提交确认"的两处调用点传主体身份，在发信前**再校验**处传 `None` 并注明"这不是一次新的批准行为"；`HttpApprovalAdapter` 仅在非空时把它放进请求体，未采用该策略的宿主线上格式逐字节不变；一致性套件新增三项检查（`replay_rejected` 必检、`distinct_approver_enforced` 由宿主声明 `four_eyes_required` 时必检、两项证据项），并引入 **`not_implemented` 第三态**——未声明四眼的宿主被判为"未实现职责分离"而**不是通过**，报告同时在 `failed`/`not_implemented` 上可区分。**2026-09-11 参考宿主侧已闭环**：`host_approvals` 新增 `consumed_at`/`consumed_by`（含 `ALTER TABLE` 就地升级迁移，故老库不会静默保留旧行为）；`verify_approval` 改为**首次校验即绑定并消费**（原子 UPDATE … WHERE consumed_at IS NULL + rowcount 比较），fail-open 旁路（绑定为空即对任意 action 返回 True）已移除——未绑定确认现在只授权**它遇到的第一封 action**，而非任意 action；新增 revalidation 模式，使「排队时的新批准」与「发信前的再校验」可区分（线上始终携带 `approver_subject_id`，字符串=新批准、显式 null=再校验）；四眼由 `HOST_REQUIRE_FOUR_EYES` 配置（默认关，单主体本地宿主开了就没法用），开启时自批与匿名批准**一律拒绝且不消费**。测试 +5，其中 5/6 对旧实现会失败（已用 stash 实证）。参考宿主的 conformance bundle 现以严格策略产生：44 项检查全通过、`not_implemented` 为空，四眼/自批/重放三项由「未实现」变为有证据的通过 **2026-09-11 审批人身份已持久化，四眼链路闭合**：`mail_outbox_operations` 新增 `approver_subject_id`（迁移 0022，含 down 脚本与列注释），排队时记录是谁批的；发信前的再校验改为携带**该持久化身份**并置 `revalidation=True`，宿主据此**重新断言**职责分离，而不是「确认已消费」就放行——「不知道谁批的」不再等于通过。端口与 HTTP 适配器同步增加显式的 `revalidation` 标志（此前靠「有没有传审批人」隐式区分，一旦再校验也要带身份就无法表达）。存储层再校验会校验三件事：确认已被消费、呈现的审批人等于记录中的 `consumed_by`、四眼开启时该审批人不等于请求者。测试：local-host 42（+1），mailhub 460 | **2026-09-11 已收口**：`OUTCOME_UNKNOWN` 自动对账完成并有**受控端到端实测**（`b38a003`/`dbcfb78` + 证据脚本）——自建原始 TLS SMTP 对端精确制造两种未知结果（收下 DATA 后断连＝确已接受；DATA 阶段断连＝从未接受），三情形实测：delivered→`reconciled_succeeded`、absent→`retry_wait`、indeterminate（邮箱不可达）→保持 `outcome_unknown`；证据 `docs/reports/mailhub-outcome-unknown-reconciliation.json` 离线可校验、账号脱敏。另补一条**此前无人断言的域风险测试**（`recipient_domain_not_allowed` 在全仓库没有任何测试覆盖，现已补正反两例）。故 `MAIL-SMTP-001/002` 已勾选**已补：报文大小上限**（`max_send_bytes` 默认 10 MiB，可经 `MAILHUB_SMTP_MAX_SEND_BYTES` 配置；在**建连前**拒绝、用不可重试错误，线上夹具验证"超限不发出任何字节"） | 生产审批语义下的发送与对账证据 |
| `MAIL-CONN-001` | 有 Python/TypeScript 源码 SDK 与纯函数 conformance kit | 第三方 Connector SDK 发布、certification 与版本兼容表 | 第三方按 SDK 接入通过的认证记录 |
| 附件安全 | 未配置 AV/DLP 时保持 quarantine | 附件 AV/DLP 扫描接入 | 真实扫描器的拦截/放行证据 |

### 3.6 UI 复用性（"能嵌"）

| ID | 现状 | 缺口 | 验收条件 |
| --- | --- | --- | --- |
| `MAIL-ADOPT-010` | ✅ **已完成（2026-08-19）**：`src/i18n.ts` 定义 53 键 `MailHubMessages` + `zh-CN`/`en-US` 内置包；`locale`/`messages` 可注入（部分覆盖或完整语言包）；`npm run build` 内置三道门禁（CJK 残留扫描 / 占位符完整性 / 双语言包键集一致），实测 `locale gate: ok (53 keys)` | 剩余：复数规则（当前为简单插值，已在文档标注由宿主覆盖）、RTL 评估 | 至少两种 locale 切换且无硬编码残留（已达成） |
| `MAIL-UX-010` | ⚠️ **渲染半程完成（2026-09-11）**：语义半程 `ui/src/a11y.test.tsx`（jsdom + axe-core + Testing Library，10 用例；真 ARIA tab、`aria-pressed` 筛选、`headingLevel` 注入、门禁活性自证）；渲染半程 `ui/src/a11y.browser.test.tsx`（Vitest browser mode + Playwright Chromium，17 用例），实测：对比度 1.4.3/1.4.11 五视图 0 违规且 0 `incomplete`（每视图实测 12–22 个元素）、回流 1.4.10（320 CSS px 无横向滚动且双列塌缩为单列）、缩放 1.4.4（640 CSS px 无横向滚动）、目标尺寸 2.5.8（最小短边 26.0 CSS px）、动效 2.3.3（CDP 模拟 `reduce` 后 3s 内联 transition 被压到 `1e-05s`）、CSP 基线（无 `theme` 时零内联 `style` 属性）；报告 `docs/reports/mailhub-ui-browser-a11y-2026-09-11.md`；发布门禁新增 `ui-browser-a11y`（`scripts/check_ui_browser_a11y.py`，反读实测值并禁止 npm/node_modules/浏览器缺失静默跳过，另加 11 个单元测试）。注：本文档此前把该项误标为 `MAIL-UX-011`（与主待办的功能项撞名），现更正为 `MAIL-UX-010` | 剩余：屏幕阅读器实机走查（NVDA/VoiceOver）、1.4.11 非文本对比度目视（焦点框/边框）、系统级高对比度模式、真实 200% UA 缩放复核 | 浏览器审计报告 + 修复闭环（报告已出，人工走查待补） |
| `MAIL-DIST-004` | 无独立可部署 UI shell | standalone shell 的打包与托管方案 | 可独立部署并被宿主嵌入 |

### 3.7 安全、合规与运维（"敢上线"）

| ID | 现状 | 缺口 | 验收条件 |
| --- | --- | --- | --- |
| `MAIL-SEC-001..007` | 本地负向测试、脱敏、删除/导出、Kill Switch 已通过 | 渗透测试、PIPL/GDPR、Provider 条款、生产扫描/签名 | 正式评审与签字 |
| `MAIL-OPS-001..006` | 有运行手册 | 30 天 SLO、告警、值班、事故演练 | 量化 SLO 与演练记录 |
| `MAIL-REL-001..012` | 有开发发布标准（ADR 0010） | kill-switch 责任人、回滚命令、迁移边界、事件联系人、已知限制页 | GA 发布审批包 |

### 3.8 旧邮件模块迁移工具包（"能替换旧的"）

| ID | 现状 | 缺口 | 验收条件 |
| --- | --- | --- | --- |
| `MAIL-ADOPT-011` | AeroLink/CAACTRAINING 适配器为按宿主定制的参考 | 通用迁移工具包：shadow-read 比对器、cursor 接管、sender interlock、回滚脚本 | 在第二个不同栈的旧邮件模块上完成 shadow→cutover→回滚演练 |
| `MAIL-MIG-001/003/005`、`MAIL-AERO-*`、`MAIL-CAA-*` | 固定快照、source ledger、clean-room projection、迁移映射 | 真实 secret 迁移、shadow-read、single-read/single-send、rollback | 许可与数据分类签字 + 差异报告 + 切换证据 |

## 4. 优先级与最小可被引入集

按"下一个产品要引入时最先被卡住的地方"排序：

| 序 | 事项 | 为什么在这个位置 | 阻断级别 |
| --- | --- | --- | --- |
| 1 | `MAIL-ADOPT-002/003` 中性命名 + 许可决策 | 不改这个，任何发布都无处落地 | 阻断 |
| 2 | `MAIL-ADOPT-001` + `MAIL-DIST-001/003/004` 发布流水线与产物 | "能被装上"的唯一路径 | 阻断 |
| 3 | `MAIL-ADOPT-004/005/006` 参考宿主 + conformance kit + quickstart | 决定别的产品"接得动"还是"接不动" | 阻断 |
| 4 | `MAIL-ADOPT-010` + `MAIL-UX-010` UI i18n + WCAG | 嵌入型产品的前置 | 高 |
| 5 | `MAIL-ADOPT-007/008` 完整 Helm + 生产 Secret 后端 | 决定"能不能上生产" | 高 |
| 6 | `MAIL-IMAP-005`、`MAIL-SMTP-001/002`、附件 AV/DLP | 国内主路径真实证据 | 高 |
| 7 | `MAIL-SEC-*`、`MAIL-OPS-*`、`MAIL-REL-*` | 上线审批 | 中 |
| 8 | `MAIL-ADOPT-011` 通用迁移工具包 | 替换旧模块时才需要 | 中 |

**最小可被引入集（MVP）**：第 1–3 项完成即可让下一个产品完成"装上 + 接通"；第 4–6 项完成才谈得上生产。

## 5. 启动项与验收（MAIL-ADOPT-001 发布流水线）

本轮启动：`MAIL-ADOPT-001` 发布流水线（构建→门禁→SBOM→完整性清单）。它不依赖命名/许可决策即可
先行落地，且是其余发布项的前置工具。

实现：`packages/mailhub/scripts/build_release.py`（只构建，从不发布）、
`packages/mailhub/scripts/publish_release.py`（发布，默认拒绝）。

验收：

- [x] 一条命令产出 `dist/release/<version>/`：Python wheel/sdist、TypeScript SDK `dist`、UI `dist`、SBOM、完整性哈希
- [x] 产出 `release-manifest.json`（版本、git commit、每个产物 SHA-256、SBOM 引用、兼容矩阵/provenance 引用、`publishable` 与阻断原因）
- [x] 流水线内嵌门禁：`--gates full` 依次执行 pytest、ruff format/check、mypy、import-boundary、secret scan、OpenAPI 导出、迁移合同、provenance、license gate
- [x] 发布动作与构建动作分离：manifest 未 `publishable` 时 `publish_release.py` 以退出码 2 拒绝并逐条列出原因；缺 `--approved-by`/`--target` 一律拒绝
- [x] 门禁对流水线自身生效：首次 `--gates full` 运行即拦下新脚本的 ruff 格式/E501 问题并已修复
- [ ] 在干净环境中复现同一份产物（可重复构建）

### 流水线立即发现并修复的缺陷

- `MAIL-ADOPT-001-F1`：hatchling sdist 未排除 `node_modules`。安装 UI/SDK 依赖后 sdist 由 358KB 涨到
  14.6MB（549 条目中 356 条为 node_modules）。已在 `pyproject.toml` 增加
  `[tool.hatch.build.targets.sdist] exclude`，并补齐 `packages/mailhub/.gitignore`。

### 决策记录（2026-08-19）

- `MAIL-ADOPT-002` **已决策并落地**：组织名 `fyjtech`；npm 作用域统一为 `@fyjtech`
  （`@fyjtech/mailhub-ui`、`@fyjtech/mailhub-client`）。发布阻断项已清空。
- `MAIL-ADOPT-003` **暂缓**：owner 明确"先不做许可管理"。许可从发布阻断项降为 manifest 的
  `publish_deferrals` 记录（保持可见、可审计，但不再阻断构建与发布）。

### 流水线新增缺陷修复

- `MAIL-ADOPT-001-F2`：npm 包只拷 `dist` 未带 `package.json`，产物无法安装/发布。已修：
  发布包同时携带 `package.json`/`README.md`/`LICENSE`/`NOTICE`。

### 本轮新增缺陷修复

- `MAIL-ADOPT-010-F1`：UI 构建产物不符合 ESM 规范。`tsc` 在 `moduleResolution: Bundler` 下保留无扩展名导入，
  `dist/index.js` 里是 `from "./i18n"`，Node ESM 直接 `ERR_MODULE_NOT_FOUND`——**包对外根本 import 不动**
  （打包器会掩盖这个问题）。已改为 `module/moduleResolution: NodeNext` + 源码显式 `.js` 扩展名；
  复验 `node --input-type=module -e "import('./dist/index.js')"` 与 `standalone.js` 均通过，
  导出 8 个符号（组件 + 完整 i18n 契约）。

### 接入套件进展

- `MAIL-ADOPT-006` 初版完成：`packages/mailhub/docs/host-quickstart.md`。
- `MAIL-ADOPT-004`/`MAIL-ADOPT-005` 待做：完整参考宿主与 Port 级 conformance kit 可执行入口
  （现有 `src/mailhub/connectors/conformance.py` 只覆盖 connector，未覆盖 Host Port）。

## 6. 边界（不做什么）

- 不把 CAPlatform 回接工作混入本清单；两者独立记账。
- 不为通过门禁而删除既有 connector（Gmail/Graph 保留代码，仅摘出国内发布阻断门，见 `provider-compatibility.yaml`）。
- 不把 Sandbox/fixture 结果当作任何 `MAIL-ADOPT-*` 项的验收证据。
- 不在命名/许可决策前向任何共享 registry 发布产物。
