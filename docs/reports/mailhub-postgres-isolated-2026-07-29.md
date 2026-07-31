# MailHub 隔离 PostgreSQL 演练报告

日期：2026-07-29
范围：`MAIL-CORE-002` 的仓库级 PostgreSQL 迁移、RLS 与租户负向证据
环境：临时 Docker `postgres:16.4` 容器（本机 `127.0.0.1:15434`，无持久卷，演练后销毁）

## 目的与边界

本报告记录一次可复核的真实 PostgreSQL 演练。它验证迁移 runner、事务边界、回滚路径、
N-1 重放以及 SQLAlchemy 仓储的 transaction-local RLS scope；不等价于生产托管数据库的
PITR、备库切换、容量、网络故障、备份恢复或发布审批证据。

演练使用专用临时数据库和仅用于本次演练的本地凭据；没有读取或写入现有 CAPlatform、
CAACTRAINING、AeroLink 或其他项目数据库，也没有记录任何秘密。

## 执行记录

1. `postgres:16.4` 临时容器启动并通过 `pg_isready`。
2. `scripts/apply_migrations.py --direction upgrade`：一次事务一个 migration，完成
   `0001`–`0017`。
3. `--direction down --target 5`：完成 `0017`–`0006` 的逆序回滚；随后再次 upgrade，
   完成 `0006`–`0017`。
4. `--direction down --target 16`：回滚 `0017`（N-1）；随后再次 upgrade，重新应用
   `0017`。
5. 使用 `SqlAlchemyMailRepository` 写入两个不同租户的最小连接投影，并在各自
   transaction-local scope 下读取：

   - `tenant-alpha/subject-alpha` 只返回 alpha 连接（1 条）；
   - `tenant-beta/subject-beta` 只返回 beta 连接（1 条）；
   - 以 alpha scope 查 beta `connection_id` 返回 `None`（跨租户负向通过）。

6. 迁移完成后的真实 SQL 检查确认：19 张 MailHub 租户表均启用 `FORCE ROW LEVEL SECURITY`，
   每张表均存在 `mailhub_tenant_isolation` policy；ledger 保留完整 `0001`–`0017`。

## 结果

- 迁移 upgrade/rollback/re-upgrade/N-1：通过。
- checksum ledger、advisory lock、单迁移事务路径：由 runner 执行并通过。
- 应用 WHERE + PostgreSQL RLS 的双门禁仓储负向：通过。
- 生产数据库、PITR/RPO/RTO、故障注入、容量/并发与发布回滚：未在本次演练中宣称完成，
  仍由部署/运维验收负责。

复现入口：`packages/mailhub/scripts/apply_migrations.py`、
`packages/mailhub/src/mailhub/persistence/sqlalchemy.py`、
`docs/mailhub-migration-runbook.md`。
