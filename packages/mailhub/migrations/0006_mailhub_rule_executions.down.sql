-- Roll back only the additive bounded-rule execution evidence table.

BEGIN;
ALTER TABLE IF EXISTS mail_rule_executions DISABLE ROW LEVEL SECURITY;
DROP TABLE IF EXISTS mail_rule_executions;
COMMIT;
