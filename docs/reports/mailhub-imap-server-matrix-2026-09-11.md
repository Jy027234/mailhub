# MailHub IMAP 服务器兼容矩阵证据（2026-09-11 修订）

- 状态：**矩阵已完成 3 行**（163 企业邮箱、QQ 邮箱、标准 Dovecot 2.4）；自建 Exchange **仍开放**
- 对应条目：`MAIL-IMAP-005`（服务器矩阵）、相关：`MAIL-IMAP-002/004`
- 证据文件：`mailhub-imap-server-matrix-163-2026-09-11.json`、`-qq-…json`、`-dovecot-…json`
- 采集器：`packages/mailhub/scripts/imap_server_matrix_probe.py`（**只读**；`--validate` 可离线复核；`--ca-file` 支持私有 CA）
- 本地 Dovecot 夹具：`packages/mailhub/scripts/local_dovecot_fixture.py`（Docker）

## 0. 本修订推翻了 2026-08-19 版的两条结论

2026-08-19 版记录 Dovecot 只有 8 项能力、且"实现了 UIDPLUS / CONDSTORE 却不广告"。
复测证明那是**采集缺陷造成的假象**：采集器当时读的是 `client.capabilities`，而 imaplib 只在
恰好看到未标记 CAPABILITY 时才刷新该属性。Dovecot 2.4 在 tagged login 响应里重申能力集，
imaplib 没有取用，于是**认证前**那份刻意收窄的列表被当成了最终列表。

同一会话内实测（`127.0.0.1:10993`）：

| 来源 | 令牌数 | 内容 |
| --- | --- | --- |
| 认证前 `client.capabilities` | 8 | `AUTH=PLAIN, ENABLE, ID, IDLE, IMAP4REV1, LITERAL+, LOGIN-REFERRALS, SASL-IR` |
| 认证后 `client.capabilities` | 8 | 同上（未刷新） |
| 认证后**显式** `CAPABILITY` | **41** | 含 `CONDSTORE、QRESYNC、UIDPLUS、MOVE、NAMESPACE、IDLE、SPECIAL-USE、ESEARCH、SORT、THREAD=*` … |

因此"服务端不广告某能力"这类结论**必须注明读取时机与读取方式**。采集器已改为认证后显式发起
`CAPABILITY`（`authenticated_capabilities`），旧的三份 JSON 与旧报告已作废删除。

## 1. 被测服务器

| 项 | 163 企业邮箱 | QQ 邮箱 | Dovecot 2.4.5（本地夹具） |
| --- | --- | --- | --- |
| 主机 | `imap.qiye.163.com:993` | `imap.qq.com:993` | `127.0.0.1:10993` |
| TLS | TLSv1.3 | TLSv1.3 | TLSv1.3 |
| 证书信任 | 系统信任库 | 系统信任库 | **私有 CA**（证据内 `certificate_trust: custom_ca`） |
| 认证 | 应用授权码 | 应用授权码 | 夹具静态口令 |

只读护栏由采集器写入证据并由校验器强制：`store_issued / expunge_issued / copy_or_move_issued /
body_fetched / flags_modified` 必须全为 `false`，`selected_readonly` 必须为 `true`。

## 2. 能力对比（认证后显式 CAPABILITY）

| 能力 | 163 | QQ | Dovecot | 影响 |
| --- | --- | --- | --- | --- |
| `IDLE` | ✅ | ✅ | ✅ | 三家都支持；受控 poll 仍需自足（IDLE 会断线） |
| `CONDSTORE` | ❌ | ❌ | ✅ | 仅本地 Dovecot 可用 MODSEQ 增量 |
| `ENABLE` | ❌ | ❌ | ✅ | RFC 7162 允许经 `ENABLE` 打开 CONDSTORE |
| `UIDPLUS` | ✅ | ✅ | ✅ | 三家都广告；APPENDUID 响应码可用 |
| `MOVE` | ❌ | ✅ | ✅ | 163 无 MOVE，移动语义不可通用 |
| `NAMESPACE` | ❌ | ✅ | ✅ | 163 前缀需按分隔符推断 |
| `QUOTA` | ❌ | ❌ | ❌ | 三家都读不到配额 |
| `AUTH=XOAUTH2` | ❌ | ❌ | ❌ | 国内两家都只保证 `AUTH=PLAIN` |
| `LITERAL+` | ❌ | ❌ | ✅ | — |
| `SPECIAL-USE / XLIST` | ✅ / ✅ | ❌ / ✅ | ✅ / ❌ | 文件夹特殊用途标记方式三家不同 |
| `能力数量` | **9** | **11** | **41** | 旧版记为 7 / 14 / 8，均已作废 |

## 3. 真实发现

### 发现 #1：能力集必须在**认证后显式**读取

见 §0。这是本次修订的根因，也是唯一一条会影响其它证据可信度的发现：
**认证前的能力列表是服务端刻意收窄的**，用它判断"服务端不支持 X"会系统性低估。
采集器已固化该顺序，并新增回归测试锁定"显式 CAPABILITY 优先于 `client.capabilities`"。

配套说明：连接器 `connectors/imap_smtp.py` 一直调用的是显式 `CAPABILITY`，因此
**从未**因该假象而降级；`_condstore_enabled` 中的 `ENABLE` 分支是防御性正确性
（RFC 7162 允许服务端仅在 `ENABLE CONDSTORE` 之后才生效，且 `QRESYNC` 蕴含 CONDSTORE），
目前尚无实测服务端走该分支，由单元测试覆盖。

### 发现 #2：163 的 `UID SEARCH ALL` 覆盖不全（新增，影响同步设计）

| 项 | 163 | QQ | Dovecot |
| --- | --- | --- | --- |
| `EXISTS `（`SELECT` 报的邮件数） | **1634** | 535 | 1 |
| `UID SEARCH ALL` 返回 | **182** | 535 | 1 |
| 差值 | **1452** | 0 | 0 |
| 证据位 | `search_visibility_limited: true` | false | false |

163 的 `UID SEARCH ALL` 只看到 1634 封中的 182 封。任何"用 SEARCH 枚举全部邮件"的
回填/对账逻辑在 163 上都会静默漏掉约 89% 的邮件。**增量同步必须同时依赖 UID 游标与
EXISTS/UIDNEXT 交叉校验**，不能用 SEARCH 结果数量代表邮箱真实规模。

### 发现 #3：层级分隔符不统一

| 服务器 | 分隔符 |
| --- | --- |
| 163 | `/` |
| QQ | `/` |
| Dovecot（Maildir++） | **`.`** |

文件夹前缀/父子关系必须按服务器返回的分隔符处理，不能写死 `/`。

### 发现 #4：`STATUS` 必须按字段名解析

163 曾乱序返回 `UIDVALIDITY`/`UIDNEXT`；采集器已改为按名解析并加不变量交叉校验与回归测试。

## 4. 实测读数

| 项 | 163 | QQ | Dovecot |
| --- | --- | --- | --- |
| `UIDVALIDITY` | `1` | `1789113608` | `1789138475` |
| `UIDNEXT` | `1730701468` | `589` | `2` |
| 最大 UID | `1730701467` | `588` | `1` |
| 不变量 `UIDNEXT = max UID + 1` | ✅ | ✅ | ✅ |
| `EXISTS` / `UID SEARCH` | 1634 / 182 | 535 / 535 | 1 / 1 |
| 元数据 FETCH | 5/5 | 5/5 | 1/1 |
| 文件夹 / 分隔符 | 7 / `/` | 7 / `/` | 1 / `.` |
| `UIDVALIDITY` 跨会话稳定 | ✅ | ✅ | ✅ |
| `UID SEARCH` 跨会话稳定 | ✅ | ✅ | ✅ |
| `EXISTS` 跨会话稳定 | ❌（邮箱在持续收信） | ✅ | ✅ |

Dovecot 行由夹具 `APPEND ` 一封邮件后采集，使 SEARCH/FETCH 路径也被覆盖。
文件夹名在证据里只存 12 位摘要。

## 5. 矩阵当前状态

| 服务器 | 状态 |
| --- | --- |
| 网易企业邮箱 | ✅ 已实测（2026-09-11 复测） |
| QQ 邮箱 | ✅ 已实测（2026-09-11 复测） |
| 标准 Dovecot（2.4.5 夹具） | ✅ 已实测（2026-09-11 复测） |
| 自建 Exchange / 其他 | ⬜ **开放** |

> 三行实测**不等于**矩阵完成（Exchange 行缺）；`MAIL-IMAP-005` 保持未勾选。
> 采集器可复用：`python scripts/imap_server_matrix_probe.py --env-file <env> --label <name> --json <out>`

## 6. 对产品的结论

1. **受控 poll 必须自足**：三家都广告 IDLE，但长连接会断，轮询仍是兜底路径。
2. **`UIDVALIDITY + UID` 游标必须自足**：只有本地 Dovecot 有 CONDSTORE，国内两家都没有。
3. **能力集只在认证后显式读取**（发现 #1）；`ENABLE` 分支是防御性正确性而非实测缺口。
4. **不能只信 SEARCH**（发现 #2）：163 上 SEARCH 只覆盖 11% 的邮件，必须与 EXISTS/UIDNEXT 交叉校验。
5. **分隔符必须按服务器取值**（发现 #3）。
6. **`STATUS` 必须按字段名解析**（发现 #4）。
7. **CONDSTORE 协商必须在 `SELECT` 之前**（RFC 7162 §3.1.8）：`HIGHESTMODSEQ` 只在扩展生效后
   才出现在 SELECT 响应里，晚协商会让服务端即使支持也退回 UID 游标。已由
   `scripts/imap_condstore_live_probe.py` 对真实 Dovecot 验证（8/8，游标 `1789138475:1:2`）。
