"""Minimal local MailHub host for the B4 provider-activation walk.

This package implements the documented ``/v1/mail-host/*`` HTTP contract with a
local SQLite/Fernet backend.  It is a controlled local/Beta adapter, not a
production host: identity is single-user, approvals are local confirmations and
the AI/AV/DLP surfaces are honest unconfigured stubs.  The OAuth credential
broker reuses the archive's reference implementation from
``caplatform_bff.mailhub_credentials``.
"""
