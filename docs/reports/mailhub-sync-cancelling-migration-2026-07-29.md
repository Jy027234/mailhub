# MailHub 0018/0019 migration drill

日期：2026-07-29（Asia/Shanghai）
范围：临时 `postgres:16.4` 容器中的 SQL migration smoke，不是生产数据库验收。

## 证据

- 在隔离临时 PostgreSQL 中按顺序执行 `0001`–`0019` upgrade，19 个 migration 均以
  `ON_ERROR_STOP=1` 成功完成。
- `mail_sync_jobs` 的最终约束包含：
  `mail_sync_jobs_deleted_count_nonnegative`，以及允许
  `queued/running/cancelling/succeeded/failed/cancelled` 的
  `mail_sync_jobs_status_values`。
- 执行 `0019_mailhub_sync_cancelling.down.sql` 后再次执行 up migration，回滚/重升级均成功，
  最终约束恢复为 `mail_sync_jobs_status_values`。
- 容器不开放宿主端口，结束后已删除；没有保存连接字符串、邮箱、token、正文或附件。

## 边界

该演练证明 migration SQL、状态约束和 0019 rollback/re-upgrade 可执行；没有证明托管
PostgreSQL 的 PITR、备库、容量、故障注入、RLS 运行负向或生产发布审批。此前的真实
SQLAlchemy/RLS 隔离报告仍覆盖至 0017；生产数据库和 0018/0019 部署级证据仍需由部署方补齐。
