# MailHub IMAP 服务器兼容矩阵证据（2026-08-19）

- 状态：**矩阵已完成 2 行（163 企业邮箱、QQ 邮箱）**；Dovecot / 自建 Exchange **仍开放**
- 对应条目：`MAIL-IMAP-005`（服务器矩阵）、相关：`MAIL-IMAP-002/004`
- 证据文件：`mailhub-imap-server-matrix-163-2026-08-19.json`、`mailhub-imap-server-matrix-qq-2026-08-19.json`
- 采集器：`packages/mailhub/scripts/imap_server_matrix_probe.py`（**只读**；`--validate` 可离线复核）

## 1. 被测服务器

| 项 | 163 企业邮箱 | QQ 邮箱 |
| --- | --- | --- |
| 主机 | `imap.qiye.163.com:993` | `imap.qq.com:993` |
| TLS | TLSv1.3 / `TLS_AES_256_GCM_SHA384` | TLSv1.3 / `TLS_AES_256_GCM_SHA384` |
| 认证 | 应用授权码（`AUTH=PLAIN`） | 应用授权码（`AUTH=PLAIN`/`LOGIN`/`XOAUTH2`） |
| 采集方式 | `SELECT ... readonly=True`，仅取 `FLAGS/INTERNALDATE/RFC822.SIZE` 元数据，**不取正文** | 同左 |

只读护栏由采集器写入证据并由校验器强制：`store_issued / expunge_issued / copy_or_move_issued /
body_fetched / flags_modified` 必须全为 `false`，`selected_readonly` 必须为 `true`。

## 2. 能力对比（矩阵的核心价值）

| 能力 | 163 企业邮箱 | QQ 邮箱 | 影响 |
| --- | --- | --- | --- |
| `IDLE` | ❌ | ✅ | **163 无推送 → 受控 poll 是唯一路径**；不能把 IDLE 写成通用能力 |
| `CONDSTORE` / `ENABLE` | ❌ | ❌ | 两家都没有 → **MODSEQ 游标不可依赖**，`UIDVALIDITY+UID` 是唯一方案 |
| `UIDPLUS` | ❌ | ✅ | 163 无 `UID EXPUNGE`/`APPENDUID`/`COPYUID` → 安全部分删除不能作为通用能力 |
| `MOVE` | ❌ | ✅ | 同上，移动语义在 163 只能退化 |
| `NAMESPACE` | ❌ | ✅ | 163 需靠分隔符推断前缀 |
| `QUOTA` | ❌ | ❌ | 两家都读不到配额 |
| `AUTH=XOAUTH2` | ❌ | ✅ | **163 只提供 `AUTH=PLAIN`** → 授权码+TLS 是国内既定方式 |
| `SPECIAL-USE` / `XLIST` | ✅ / ✅ | ❌ / ✅ | 文件夹特殊用途标记方式不同 |
| 能力数量 | 7 | 14 | — |

**结论：能力必须按服务器协商，不能写死。** 任何"MailHub 支持 IDLE / UIDPLUS / MOVE"的说法，
对 163 都是假的；API 的 `capabilities` 与 UI 的能力展示必须由实际协商结果驱动。

## 3. 实测读数

| 项 | 163 企业邮箱 | QQ 邮箱 |
| --- | --- | --- |
| `UIDVALIDITY` | `1` | `1789113608` |
| `UIDNEXT` | `1730701461` | `327` |
| 最大 UID | `1730701460` | `326` |
| 不变量 `UIDNEXT = max UID + 1` | ✅ | ✅ |
| `EXISTS` / `STATUS MESSAGES` | `178` / `178` | `324` / `324` |
| `UID SEARCH ALL` | `178`（一致） | `324`（一致） |
| `UNSEEN` | `82` | — |
| 文件夹 / 分隔符 | 7 / `/` | 7 / `/` |
| 元数据 FETCH | 5/5 | 5/5 |
| 跨会话稳定性（UIDVALIDITY / EXISTS / SEARCH） | ✅ / ✅ / ✅ | ✅ / ✅ / ✅ |

文件夹名在证据里以 12 位摘要保存（不落明文）。

## 4. 一处**未解释读数**（明确排除、不作为证据）

163 首次采集时，采集器（当时按**位置**解析 `STATUS`）记录到 `messages=1630`、
`uidvalidity=1730701461`。随后查明：

1. 163 的 `STATUS` **不按请求顺序返回字段**——请求 `(UIDVALIDITY UIDNEXT UNSEEN)` 得到
   `(UIDNEXT 1730701461 UIDVALIDITY 1 UNSEEN 82)`。按位置解析会把 `UIDNEXT` 当成
   `UIDVALIDITY`，并**伪造出一次游标重置**。
2. 修复为**按字段名解析**后，163 在 **5 次独立会话**中读数完全一致（178/178/1/1730701461）。

`1630` 既不可复现也无可佐证读数，**不计入矩阵证据**，仅备案。最可能的解释是采集窗口内邮箱被
并发修改，但无证据支持，故不做结论。

## 5. 矩阵当前状态

| 服务器 | 状态 | 依据 |
| --- | --- | --- |
| 网易企业邮箱 | ✅ 已实测 | 只读探针 + 证据校验通过 |
| QQ 邮箱 | ✅ 已实测 | 只读探针 + 证据校验通过 |
| 标准 Dovecot | ⬜ **开放** | 未采集 |
| 自建 Exchange / 其他 | ⬜ **开放** | 未采集 |

> 两行实测**不等于**矩阵完成。`MAIL-IMAP-005` 保持未勾选，直到其余各行也有可复核证据。
> 采集器可复用：`python scripts/imap_server_matrix_probe.py --env-file <env> --label <name> --json <out>`

## 6. 对产品的结论

1. **受控 poll 必须自足**：163 无 IDLE，而它是国内主路径的参考服务器。
2. **`UIDVALIDITY + UID` 游标必须自足**：两家都没有 CONDSTORE/MODSEQ。
3. **能力协商结果必须驱动 UI 与 API**：`UIDPLUS`/`MOVE`/`IDLE` 在两家之间相反，
   写死任何一个都会在真实服务器上说谎。
4. **`STATUS` 必须按字段名解析**（已在采集器修复并写入回归测试）；connector 侧应遵循同一规则。
5. **`UID SEARCH` 与 `EXISTS` 都要读并交叉校验**：两行当前一致，设计上不能只信其一。
6. 国内两家都只保证 `AUTH=PLAIN`（QQ 额外支持 XOAUTH2）：**授权码 + TLS 是既定路径**。
