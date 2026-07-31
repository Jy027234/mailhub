"""Provider adapters."""

from mailhub.connectors.conformance import (
    ConformanceReport,
    validate_capabilities,
    validate_message,
    validate_message_batch,
    validate_send_receipt,
    validate_sync_page,
)
from mailhub.connectors.http_providers import GmailConnector, MicrosoftGraphConnector
from mailhub.connectors.imap_smtp import ImapSmtpConnector
from mailhub.connectors.sandbox import SandboxConnector

__all__ = [
    "ConformanceReport",
    "GmailConnector",
    "ImapSmtpConnector",
    "MicrosoftGraphConnector",
    "SandboxConnector",
    "validate_capabilities",
    "validate_message",
    "validate_message_batch",
    "validate_send_receipt",
    "validate_sync_page",
]
