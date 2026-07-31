DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM mail_sender_leases) THEN
        RAISE EXCEPTION 'sender_leases_not_empty';
    END IF;
END $$;

DROP TABLE IF EXISTS mail_sender_leases;
