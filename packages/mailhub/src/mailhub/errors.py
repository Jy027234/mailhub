"""Stable MailHub error taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    code: str
    message: str
    retryable: bool = False
    outcome_unknown: bool = False
    details: dict[str, Any] | None = None


class MailHubError(RuntimeError):
    code = "mailhub_error"
    retryable = False
    outcome_unknown = False

    def __init__(
        self, message: str | None = None, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}

    def detail(self) -> ErrorDetail:
        return ErrorDetail(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            outcome_unknown=self.outcome_unknown,
            details=self.details,
        )


class ValidationError(MailHubError):
    code = "validation_error"


class NotFoundError(MailHubError):
    code = "not_found"


class AuthorizationError(MailHubError):
    code = "authorization_denied"


class PolicyDeniedError(MailHubError):
    code = "policy_denied"


class ApprovalRequiredError(MailHubError):
    code = "approval_required"


class ConflictError(MailHubError):
    code = "conflict"


class SyncCancelledError(MailHubError):
    """A durable sync worker reached an operator-requested cancel checkpoint."""

    code = "sync_job_cancelled"


class ProviderFailureError(MailHubError):
    code = "provider_failure"
    retryable = True


class OutcomeUnknownError(MailHubError):
    code = "outcome_unknown"
    retryable = False
    outcome_unknown = True


class KnownNotSentError(MailHubError):
    """The provider call was not accepted; safe to surface without retry ambiguity."""

    code = "known_not_sent"
    retryable = True


class DegradedError(MailHubError):
    """The capability is temporarily degraded or sandbox-only."""

    code = "degraded"
    retryable = True


class RateLimitedError(MailHubError):
    code = "rate_limited"
    retryable = True


class KillSwitchError(MailHubError):
    code = "kill_switch_active"
