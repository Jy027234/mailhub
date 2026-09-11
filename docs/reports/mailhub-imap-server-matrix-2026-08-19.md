# MailHub IMAP 服务器兼容矩阵证据（2026-08-19）

- 状态：**矩阵已完成 3 行**（163 企业邮箱、QQ 邮箱、标准 Dovecot 2.4）；自建 Exchange **仍开放**
- 对应条目：`MAIL-IMAP-005`（服务器矩阵）、相关：`MAIL-IMAP-002/004`
- 证据文件：`mailhub-imap-server-matrix-163-2026-08-19.json`、`-qq-…json`、`-dovecot-…json`
- 采集器：`packages/mailhub/scripts/imap_server_matrix_probe.py`（**只读**；`--validate` 可离线复核；`--ca-file` 支持私有 CA）
- 本地 Dovecot 夹具：`packages/mailhub/scripts/local_dovecot_fixture.py`（Docker）

## 1. 被测服务器

| 项 | 163 企业邮箱 | QQ 邮箱 | Dovecot 2.4.5（本地夹具） |
| --- | --- | --- | --- |
| 主机 | `imap.qiye.163.com:993` | `imap.qq.com:993` | `127.0.0.1:10993` |
| TLS | TLSv1.3 | TLSv1.3 | TLSv1.3 |
| 证书信任 | 系统信任库 | 系统信任库 | **私有 CA**（已记入证据 `certificate_trust: custom_ca`） |
| 认证 | 应用授权码 | 应用授权码 | 夹具静态口令 |

只读护栏由采集器写入证据并由校验器强制：`store_issued / expunge_issued / copy_or_move_issued /
body_fetched / flags_modified` 必须全为 `false`，`selected_readonly` 必须为 `true`。

## 2. 能力对比（矩阵的核心价值）

| 能力 | 163 | QQ | Dovecot | 影响 |
| --- | --- | --- | --- | --- |
| `IDLE` | ❌ | ✅ | ✅ | **受控 poll 必须自足**：163 无推送 |
| `CONDSTORE` | ❌ | ❌ | ❌（但有 `ENABLE`） | 见下方发现 #2 |
| `ENABLE` | ❌ | ❌ | ✅ | RFC 7162 允许经 `ENABLE` 打开 CONDSTORE |
| `UIDPLUS` | ❌ | ✅ | ❌（**但实际实现**） | 见下方发现 #1 |
| `MOVE` | ❌ | ✅ | ❌ | 移动语义不可通用 |
| `NAMESPACE` | ❌ | ✅ | ❌ | 前缀需按分隔符推断 |
| `QUOTA` | ❌ | ❌ | ❌ | 三家都读不到配额 |
| `AUTH=XOAUTH2` | ❌ | ✅ | ❌ | 国内两家都只保证 `AUTH=PLAIN` |
| `LITERAL+` | ❌ | ❌ | ✅ | — |
| `SPECIAL-USE`/`XLIST` | ✅/✅ | ❌/✅ | ❌/❌ | 文件夹特殊用途标记方式三家不同 |
| 能力数量 | 7 | 14 | 8 | — |

## 3. 三个真实发现

### 发现 #1：能力令牌**不可靠**——Dovecot 实现了 UIDPLUS 却不广告

夹具 `APPEND` 返回 `[APPENDUID 1789136049 1]`，**这是 UIDPLUS 的响应码**，但 CAPABILITY 列表里
**没有 `UIDPLUS` 令牌**。任何"没有令牌就禁用 UID 相关能力"的逻辑，都会在真实 Dovecot 上错误降级。

### 发现 #2：`ENABLE` 存在但 `CONDSTORE` 不广告

Dovecot 广告 `ENABLE` 而不广告 `CONDSTORE`；RFC 7162 允许经 `ENABLE CONDSTORE` 打开该扩展。
connector 目前在 `imap_smtp.py:206` **按字面令牌判定**：

```python
highest_modseq = _highest_modseq(client) if "CONDSTORE" in capability_names else None
```

→ 对 Dovecot 会跳过 MODSEQ 路径。**记录为待改进项**（安全改法：`ENABLE` 存在时尝试
`ENABLE CONDSTORE` 再读 `HIGHESTMODSEQ`，任何拒绝都回落到 UID-only 游标）。

### 发现 #3：层级分隔符不统一

| 服务器 | 分隔符 |
| --- | --- |
| 163 | `/` |
| QQ | `/` |
| Dovecot（Maildir++） | **`.`** |

文件夹前缀/父子关系必须按服务器返回的分隔符处理，不能写死 `/`。

## 4. 实测读数

| 项 | 163 | QQ | Dovecot |
| --- | --- | --- | --- |
| `UIDVALIDITY` | `1` | `1789113608` | `1789136049` |
| `UIDNEXT` | `1730701461` | `327` | `2` |
| 最大 UID | `1730701460` | `326` | `1` |
| 不变量 `UIDNEXT = max UID + 1` | ✅ | ✅ | ✅ |
| `EXISTS` / `UID SEARCH` | `178`/`178` | `324`/`324` | `1`/`1` |
| 元数据 FETCH | 5/5 | 5/5 | 1/1 |
| 文件夹 / 分隔符 | 7 / `/` | 7 / `/` | 1 / `.` |
| 跨会话稳定性 | ✅ | ✅ | ✅ |

Dovecot 行先以**空邮箱**采集（`messages=0` 干净通过，证明探针能处理空 INBOX），随后由夹具
`APPEND` 一封邮件再采一次，使 SEARCH/FETCH 路径也被覆盖。文件夹名在证据里只存 12 位摘要。

## 5. 矩阵当前状态

| 服务器 | 状态 |
| --- | --- |
| 网易企业邮箱 | ✅ 已实测 |
| QQ 邮箱 | ✅ 已实测 |
| 标准 Dovecot（2.4.5 夹具） | ✅ 已实测 |
| 自建 Exchange / 其他 | ⬜ **开放** |

> 三行实测**不等于**矩阵完成（Exchange 行缺）；`MAIL-IMAP-005` 保持未勾选。
> 采集器可复用：`python scripts/imap_server_matrix_probe.py --env-file <env> --label <name> --json <out>`

## 6. 对产品的结论

1. **受控 poll 必须自足**（163 无 IDLE）。
2. **`UIDVALIDITY + UID` 游标必须自足**：三家都没有可直接使用的 CONDSTORE 令牌。
3. **能力判定不能只看令牌**（发现 #1/#2）：UIDPLUS 可不广告而实现，CONDSTORE 可经 `ENABLE` 打开。
4. **分隔符必须按服务器取值**（发现 #3）。
5. **`STATUS` 必须按字段名解析**（163 乱序返回；已在采集器修复并加不变量交叉校验与回归测试）。
6. **`UID SEARCH` 与 `EXISTS` 都要读并交叉校验**：三行当前一致，设计上不能只信其一。
