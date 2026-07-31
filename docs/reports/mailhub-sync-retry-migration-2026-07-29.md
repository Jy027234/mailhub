# MailHub 0021 sync retry migration drill

日期：2026-07-29（Asia/Shanghai）
范围：临时 `postgres:16.4` 容器中的 `0001`–`0021` migration runner smoke；不是生产数据库验收。

## 目的与边界

本报告验证 `scripts/apply_migrations.py` 对最新 durable sync retry schema 的全量
upgrade、target rollback 和 re-upgrade 路径。演练只使用一次性本地 PostgreSQL 容器，
没有连接现有 CAPlatform、CAACTRAINING、AeroLink 或其他项目数据库，也没有保存邮箱、
token、正文、附件或持久卷。

该证据覆盖 migration runner 的 SQL/ledger 可执行性，不等价于托管 PostgreSQL 的
PITR、备库切换、容量/并发、网络故障注入、RPO/RTO、真实 RLS 运行负向或生产发布审批。

## 执行记录

1. 启动临时 `postgres:16.4` 容器，容器内 `pg_isready` 报告 accepting connections；
   数据库只加入容器内部网络，未发布宿主端口。
2. 通过与数据库共享 network namespace 的临时 Python 运行容器执行：
   `scripts/apply_migrations.py --direction upgrade`，成功应用 `0001`–`0021`。
3. 执行 `--direction down --target 20`，成功回滚 `0021`；随后再次执行 upgrade，
   成功重新应用 `0021`。
4. 使用容器内 `psql` 做最终结构检查：

   - `mailhub_schema_migrations` 共 21 条记录，最大版本为 `21`；
   - `mail_sync_jobs` 存在 `attempt_count` 与 `next_attempt_at`；
   - `mail_sync_jobs_retry_due_idx` 存在；
   - `mail_sync_jobs_attempt_count_nonnegative` 约束存在。

5. 演练完成后用明确容器名删除临时容器；无残留容器或数据卷。

## 结果

- 全量 upgrade：通过（`0001`–`0021`）。
- 0021 target rollback/re-upgrade：通过。
- checksum ledger、advisory lock、单 migration 事务路径：由 runner 执行并通过。
- retry schema 字段、索引和非负约束：通过最终 SQL 检查。
- 生产托管数据库、PITR/RPO/RTO、容量/故障注入、发布回滚：未在本次演练中宣称完成，
  仍由部署/运维验收负责。

复现入口：`packages/mailhub/scripts/apply_migrations.py`、
`packages/mailhub/migrations/0021_mailhub_sync_retry.sql`、
`docs/mailhub-migration-runbook.md`。
