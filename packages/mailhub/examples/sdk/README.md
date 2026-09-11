# MailHub SDK examples

These snippets are host-side examples, not executable Provider integrations.
They require an already authenticated MailHub endpoint and host-owned
tenant/subject context.

Python can be run with the reviewed client directory on `PYTHONPATH`:

```powershell
$env:PYTHONPATH = "packages/mailhub/sdk/python"
python packages/mailhub/examples/sdk/python_client.py
```

TypeScript hosts should install/link `@fyjtech/mailhub-client` and compile
`typescript_client.ts` with their own runtime types. Neither example accepts
or prints OAuth/SMTP credentials, raw message content, or real mailbox data.
