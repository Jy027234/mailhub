-- Roll back only the additive rule table.

BEGIN;
ALTER TABLE IF EXISTS mail_rules DISABLE ROW LEVEL SECURITY;
DROP TABLE IF EXISTS mail_rules;
COMMIT;
