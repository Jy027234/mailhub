"""Small, dependency-light async client for the MailHub HTTP contract.

The client is intentionally provider-neutral: callers provide a host identity
context and an idempotency key for commands.  It never accepts provider tokens
and never logs request bodies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID

import httpx


class MailHubApiError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, details: object = None) -> None:
        super().__init__(f"mailhub_http_{status_code}:{code}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _normalize_sync_datetime(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"mailhub_sync_{field_name}_must_be_aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MailHubClient:
    base_url: str
    tenant_id: str
    subject_id: str
    timeout_seconds: float = 15.0

    def _headers(
        self, *, idempotency_key: str | None = None, trace_id: str | None = None
    ) -> dict[str, str]:
        headers = {
            "X-MailHub-Tenant": self.tenant_id,
            "X-MailHub-Subject": self.subject_id,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "//" in path:
            raise ValueError("mailhub_path_invalid")
        async with httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"), timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            response = await client.request(
                method,
                path,
                headers=self._headers(idempotency_key=idempotency_key, trace_id=trace_id),
                json=dict(json) if json is not None else None,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MailHubApiError(
                response.status_code, "invalid_response", "MailHub returned non-JSON"
            ) from exc
        if not response.is_success:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            if not isinstance(error, dict):
                error = {}
            raise MailHubApiError(
                response.status_code,
                str(error.get("code", "http_error")),
                str(error.get("message", "MailHub request failed")),
                error.get("details"),
            )
        if not isinstance(payload, dict):
            raise MailHubApiError(
                response.status_code, "invalid_response", "MailHub response shape invalid"
            )
        return payload

    async def list_connections(self) -> dict[str, Any]:
        return await self.request("GET", "/v1/mail/connections")

    async def provider_health(self) -> dict[str, Any]:
        """Return bounded provider health for a host-authorized admin subject."""

        return await self.request("GET", "/v1/mail/admin/provider-health")

    async def list_audit(self, *, limit: int = 100) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("mailhub_audit_limit_invalid")
        return await self.request("GET", f"/v1/mail/audit?limit={limit}")

    async def update_connection_scopes(
        self,
        connection_id: UUID | str,
        *,
        expected_revision: int,
        granted_scopes: tuple[str, ...],
    ) -> dict[str, Any]:
        """Narrow locally usable scopes; OAuth reauthorization is required to expand them."""

        if expected_revision < 1:
            raise ValueError("mailhub_connection_revision_invalid")
        if (
            not granted_scopes
            or len(granted_scopes) > 40
            or any(
                not scope.strip()
                or len(scope.strip()) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in scope)
                for scope in granted_scopes
            )
        ):
            raise ValueError("mailhub_connection_scopes_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/connections/{connection_id}:scopes",
            json={
                "expected_revision": expected_revision,
                "granted_scopes": list(granted_scopes),
            },
        )

    async def refresh_connection(
        self,
        connection_id: UUID | str,
        *,
        expected_revision: int,
        reason: str = "manual_refresh",
    ) -> dict[str, Any]:
        """Request host-owned OAuth refresh; no provider token crosses this client."""

        if expected_revision < 1:
            raise ValueError("mailhub_connection_revision_invalid")
        if not reason.strip() or len(reason) > 500:
            raise ValueError("mailhub_connection_refresh_reason_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/connections/{connection_id}:refresh",
            json={"expected_revision": expected_revision, "reason": reason},
        )

    async def begin_oauth(
        self,
        provider: str,
        *,
        authorization_endpoint: str,
        client_id: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
        connection_id: UUID | str | None = None,
        expected_revision: int | None = None,
        extra_parameters: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if provider not in {"gmail", "microsoft_graph"}:
            raise ValueError("mailhub_oauth_provider_invalid")
        if not authorization_endpoint.strip() or len(authorization_endpoint) > 2000:
            raise ValueError("mailhub_oauth_endpoint_invalid")
        if not client_id.strip() or len(client_id) > 512:
            raise ValueError("mailhub_oauth_client_id_invalid")
        if not redirect_uri.strip() or len(redirect_uri) > 2000:
            raise ValueError("mailhub_oauth_redirect_uri_invalid")
        if not scopes or len(scopes) > 20 or any(not scope.strip() for scope in scopes):
            raise ValueError("mailhub_oauth_scopes_invalid")
        if expected_revision is not None and (expected_revision < 1 or connection_id is None):
            raise ValueError("mailhub_oauth_expected_revision_invalid")
        payload: dict[str, object] = {
            "authorization_endpoint": authorization_endpoint,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scopes": list(scopes),
            "extra_parameters": dict(extra_parameters or {}),
        }
        if connection_id is not None:
            payload["connection_id"] = str(connection_id)
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        return await self.request("POST", f"/v1/mail/oauth/{provider}:authorize", json=payload)

    async def complete_oauth(
        self,
        provider: str,
        *,
        state: str,
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        if provider not in {"gmail", "microsoft_graph"}:
            raise ValueError("mailhub_oauth_provider_invalid")
        if not 20 <= len(state) <= 4000 or not code.strip():
            raise ValueError("mailhub_oauth_callback_invalid")
        if not redirect_uri.strip() or len(redirect_uri) > 2000:
            raise ValueError("mailhub_oauth_redirect_uri_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/oauth/{provider}:callback",
            json={"state": state, "code": code, "redirect_uri": redirect_uri},
        )

    async def create_connection(
        self,
        *,
        provider: str,
        email_address: str,
        credential_ref: str,
        granted_scopes: tuple[str, ...] = (),
        provider_account_id: str | None = None,
        provider_tenant_id: str | None = None,
        credential_version: int = 1,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/v1/mail/connections",
            json={
                "provider": provider,
                "email_address": email_address,
                "credential_ref": credential_ref,
                "granted_scopes": list(granted_scopes),
                "provider_account_id": provider_account_id,
                "provider_tenant_id": provider_tenant_id,
                "credential_version": credential_version,
            },
        )

    async def enqueue_sync(
        self,
        connection_id: UUID | str,
        *,
        mode: str = "incremental",
        limit: int = 50,
        folder_ref: str = "INBOX",
        label_refs: tuple[str, ...] = (),
        received_after: datetime | None = None,
        received_before: datetime | None = None,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if mode not in {"incremental", "backfill", "reconcile"}:
            raise ValueError("mailhub_sync_mode_invalid")
        if not 1 <= limit <= 500:
            raise ValueError("mailhub_sync_limit_invalid")
        if not folder_ref.strip() or len(folder_ref) > 200:
            raise ValueError("mailhub_sync_folder_invalid")
        if len(label_refs) > 20 or any(
            not label.strip() or len(label) > 200 for label in label_refs
        ):
            raise ValueError("mailhub_sync_labels_invalid")
        if any(
            any(ord(char) < 33 or ord(char) == 127 for char in value)
            for value in (folder_ref, *label_refs)
        ):
            raise ValueError("mailhub_sync_filter_invalid")
        normalized_after = _normalize_sync_datetime(received_after, "received_after")
        normalized_before = _normalize_sync_datetime(received_before, "received_before")
        if (
            normalized_after is not None
            or normalized_before is not None
            or label_refs
            or folder_ref != "INBOX"
        ) and mode != "backfill":
            raise ValueError("mailhub_sync_filter_requires_backfill")
        if (
            normalized_after is not None
            and normalized_before is not None
            and normalized_after >= normalized_before
        ):
            raise ValueError("mailhub_sync_date_range_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/connections/{connection_id}/sync-jobs",
            json={
                "mode": mode,
                "limit": limit,
                "folder_ref": folder_ref,
                "label_refs": list(label_refs),
                "received_after": normalized_after.isoformat().replace("+00:00", "Z")
                if normalized_after
                else None,
                "received_before": normalized_before.isoformat().replace("+00:00", "Z")
                if normalized_before
                else None,
            },
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def get_sync_job(self, job_id: UUID | str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/mail/sync-jobs/{job_id}")

    async def list_sync_states(self, connection_id: UUID | str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/mail/connections/{connection_id}/sync-state")

    async def connection_impact_preview(
        self,
        connection_id: UUID | str,
        *,
        folder_refs: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Return a projection-only scope/deletion impact preview."""

        if len(folder_refs) > 50 or any(
            not value.strip()
            or len(value.strip()) > 200
            or any(ord(char) < 33 or ord(char) == 127 for char in value.strip())
            for value in folder_refs
        ):
            raise ValueError("mailhub_impact_preview_folder_refs_invalid")
        query = urlencode([("folder_ref", value.strip()) for value in folder_refs])
        path = f"/v1/mail/connections/{connection_id}/impact-preview"
        if query:
            path += f"?{query}"
        return await self.request("GET", path)

    async def ensure_subscription(
        self,
        connection_id: UUID | str,
        *,
        callback_endpoint: str,
        desired_expiry: datetime,
        idempotency_key: str,
        folder_ref: str = "INBOX",
        client_state_ref: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not callback_endpoint.strip() or len(callback_endpoint) > 2000:
            raise ValueError("mailhub_subscription_callback_invalid")
        if not idempotency_key.strip() or len(idempotency_key) > 300:
            raise ValueError("mailhub_subscription_idempotency_key_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/connections/{connection_id}/subscription:ensure",
            json={
                "callback_endpoint": callback_endpoint,
                "desired_expiry": desired_expiry.isoformat(),
                "folder_ref": folder_ref,
                "client_state_ref": client_state_ref,
            },
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def renew_subscription(
        self,
        connection_id: UUID | str,
        *,
        idempotency_key: str | None = None,
        folder_ref: str = "INBOX",
        renewal_window_hours: int = 24,
        desired_expiry: datetime | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= renewal_window_hours <= 168:
            raise ValueError("mailhub_subscription_renewal_window_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/connections/{connection_id}/subscription:renew",
            json={
                "folder_ref": folder_ref,
                "renewal_window_hours": renewal_window_hours,
                "desired_expiry": desired_expiry.isoformat() if desired_expiry else None,
            },
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def cancel_subscription(
        self,
        connection_id: UUID | str,
        *,
        idempotency_key: str,
        folder_ref: str = "INBOX",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not idempotency_key.strip() or len(idempotency_key) > 300:
            raise ValueError("mailhub_subscription_idempotency_key_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/connections/{connection_id}/subscription:cancel",
            json={"folder_ref": folder_ref},
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def replay_webhook_receipt(
        self,
        provider: str,
        event_id: str,
        *,
        expected_body_sha256: str,
        reason: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if provider not in {"gmail", "microsoft_graph"}:
            raise ValueError("mailhub_receipt_provider_invalid")
        if not event_id.strip() or len(event_id) > 512:
            raise ValueError("mailhub_receipt_event_id_invalid")
        if len(expected_body_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in expected_body_sha256
        ):
            raise ValueError("mailhub_receipt_digest_invalid")
        if not reason.strip() or len(reason) > 500:
            raise ValueError("mailhub_receipt_reason_invalid")
        if not idempotency_key.strip() or len(idempotency_key) > 300:
            raise ValueError("mailhub_receipt_idempotency_key_invalid")
        return await self.request(
            "POST",
            (
                f"/v1/mail/webhooks/receipts/{quote(provider, safe='')}/"
                f"{quote(event_id, safe='')}:replay"
            ),
            json={"expected_body_sha256": expected_body_sha256, "reason": reason},
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def enqueue_autonomy(
        self,
        connection_id: UUID | str,
        *,
        replay_key: str,
        limit: int = 50,
        message_limit: int = 50,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not replay_key.strip() or len(replay_key) > 200:
            raise ValueError("mailhub_autonomy_replay_key_invalid")
        if not 1 <= limit <= 500 or not 1 <= message_limit <= 200:
            raise ValueError("mailhub_autonomy_limits_invalid")
        return await self.request(
            "POST",
            "/v1/mail/autonomy/runs",
            json={
                "connection_id": str(connection_id),
                "replay_key": replay_key,
                "limit": limit,
                "message_limit": message_limit,
                "run_inline": False,
            },
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def list_autonomy_runs(
        self, *, connection_id: UUID | str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        bounded = min(max(limit, 1), 200)
        path = f"/v1/mail/autonomy/runs?limit={bounded}"
        if connection_id is not None:
            path += f"&connection_id={connection_id}"
        return await self.request("GET", path)

    async def get_autonomy_run(self, run_id: UUID | str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/mail/autonomy/runs/{run_id}")

    async def control_autonomy_run(
        self,
        run_id: UUID | str,
        *,
        command: str,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if command not in {"run", "pause", "resume", "cancel"}:
            raise ValueError("mailhub_autonomy_command_invalid")
        if command == "pause" and not (reason and reason.strip()):
            raise ValueError("mailhub_autonomy_pause_reason_required")
        return await self.request(
            "POST",
            f"/v1/mail/autonomy/runs/{run_id}:{command}",
            json={"reason": reason} if command == "pause" else None,
            trace_id=trace_id,
        )

    async def create_draft(
        self,
        *,
        connection_id: UUID | str,
        recipient_addresses: tuple[str, ...],
        subject: str,
        body_text: str,
        idempotency_key: str,
        cc_addresses: tuple[str, ...] = (),
        bcc_addresses: tuple[str, ...] = (),
        attachment_refs: tuple[str, ...] = (),
        thread_id: UUID | str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/v1/mail/drafts",
            json={
                "connection_id": str(connection_id),
                "thread_id": str(thread_id) if thread_id else None,
                "recipient_addresses": list(recipient_addresses),
                "cc_addresses": list(cc_addresses),
                "bcc_addresses": list(bcc_addresses),
                "attachment_refs": list(attachment_refs),
                "subject": subject,
                "body_text": body_text,
            },
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def list_thread_page(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        connection_id: UUID | str | None = None,
        unread: bool = False,
        important: bool = False,
        has_attachment: bool = False,
        project: bool = False,
        candidate: bool = False,
    ) -> dict[str, Any]:
        """Fetch one scope-bound unified-inbox page.

        The cursor is opaque and must be replayed with the same filters and
        account scope.  The SDK only forwards it; it never decodes or
        manufactures a cursor.
        """

        bounded = min(max(limit, 1), 200)
        params: dict[str, str] = {"limit": str(bounded)}
        if cursor:
            params["cursor"] = cursor
        if connection_id is not None:
            params["connection_id"] = str(connection_id)
        for key, enabled in (
            ("unread", unread),
            ("important", important),
            ("attachment", has_attachment),
            ("project", project),
            ("candidate", candidate),
        ):
            if enabled:
                params[key] = "true"
        return await self.request("GET", f"/v1/mail/threads?{urlencode(params)}")

    async def list_threads(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        connection_id: UUID | str | None = None,
        unread: bool = False,
        important: bool = False,
        has_attachment: bool = False,
        project: bool = False,
        candidate: bool = False,
    ) -> dict[str, Any]:
        return await self.list_thread_page(
            limit=limit,
            cursor=cursor,
            connection_id=connection_id,
            unread=unread,
            important=important,
            has_attachment=has_attachment,
            project=project,
            candidate=candidate,
        )

    async def list_messages(self, *, limit: int = 50) -> dict[str, Any]:
        return await self.request("GET", f"/v1/mail/messages?limit={min(max(limit, 1), 200)}")

    async def search_messages(
        self, query: str, *, limit: int = 50, mode: str = "metadata"
    ) -> dict[str, Any]:
        normalized = query.strip()
        if not normalized or len(normalized) > 200:
            raise ValueError("mailhub_search_query_invalid")
        if mode not in {"metadata", "provider"}:
            raise ValueError("mailhub_search_mode_invalid")
        return await self.request(
            "GET",
            (
                f"/v1/mail/search?q={quote(normalized, safe='')}"
                f"&limit={min(max(limit, 1), 200)}&mode={mode}"
            ),
        )

    async def get_message_content(self, message_id: UUID | str) -> str:
        if not str(message_id):
            raise ValueError("mailhub_message_id_invalid")
        async with httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            timeout=self.timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.get(
                f"/v1/mail/messages/{message_id}/content",
                headers=self._headers(),
            )
        if not response.is_success:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            if not isinstance(error, dict):
                error = {}
            raise MailHubApiError(
                response.status_code,
                str(error.get("code", "http_error")),
                str(error.get("message", "MailHub request failed")),
                error.get("details"),
            )
        if len(response.content) > 1_000_000:
            raise MailHubApiError(
                response.status_code, "content_too_large", "Mail content exceeds limit"
            )
        return response.text

    async def get_thread(self, thread_id: UUID | str, *, limit: int = 200) -> dict[str, Any]:
        return await self.request("GET", f"/v1/mail/threads/{thread_id}?limit={limit}")

    async def list_candidates(
        self,
        *,
        status: str | None = None,
        candidate_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("mailhub_candidate_limit_invalid")
        query = "?" + urlencode(
            {
                "limit": limit,
                **({"status": status} if status else {}),
                **({"candidate_type": candidate_type} if candidate_type else {}),
            }
        )
        return await self.request("GET", "/v1/mail/candidates" + query)

    async def list_typed_candidates(
        self,
        candidate_type: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not candidate_type.strip() or len(candidate_type) > 40:
            raise ValueError("mailhub_candidate_type_invalid")
        if not 1 <= limit <= 200:
            raise ValueError("mailhub_candidate_limit_invalid")
        query = "?" + urlencode({"limit": limit, **({"status": status} if status else {})})
        return await self.request(f"/v1/mail/candidates/{quote(candidate_type, safe='')}" + query)

    async def get_candidate(self, candidate_id: UUID | str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/mail/candidates/{quote(str(candidate_id), safe='')}")

    async def revoke_candidate(
        self,
        candidate_id: UUID | str,
        *,
        expected_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip() or len(reason) > 500:
            raise ValueError("mailhub_candidate_revoke_reason_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/candidates/{quote(str(candidate_id), safe='')}:revoke",
            json={"expected_revision": expected_revision, "reason": reason},
        )

    async def review_candidate(
        self,
        candidate_id: UUID | str,
        *,
        approved: bool,
        expected_revision: int,
        review_reason: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/v1/mail/candidates/{candidate_id}:review",
            json={
                "approved": approved,
                "expected_revision": expected_revision,
                "review_reason": review_reason,
            },
            trace_id=trace_id,
        )

    async def apply_candidate(
        self,
        candidate_id: UUID | str,
        *,
        approval_ref: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not approval_ref.strip() or len(approval_ref) > 512:
            raise ValueError("mailhub_candidate_approval_ref_invalid")
        if not idempotency_key.strip() or len(idempotency_key) > 300:
            raise ValueError("mailhub_candidate_idempotency_key_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/candidates/{candidate_id}:apply",
            json={"approval_ref": approval_ref},
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def queue_draft_send(
        self,
        draft_id: UUID | str,
        *,
        expected_revision: int,
        expected_content_sha256: str,
        expected_recipient_digest: str,
        confirmation_ref: str | None = None,
        policy_id: UUID | str | None = None,
        grant_id: UUID | str | None = None,
        agent_subject_id: str | None = None,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, object] = {
            "expected_revision": expected_revision,
            "expected_content_sha256": expected_content_sha256,
            "expected_recipient_digest": expected_recipient_digest,
            "confirmation_ref": confirmation_ref,
            "policy_id": str(policy_id) if policy_id else None,
            "grant_id": str(grant_id) if grant_id else None,
            "agent_subject_id": agent_subject_id,
        }
        return await self.request(
            "POST",
            f"/v1/mail/drafts/{draft_id}:send",
            json=body,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def delete_connection(
        self,
        connection_id: UUID | str,
        *,
        expected_revision: int,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/v1/mail/connections/{connection_id}:delete",
            json={"expected_revision": expected_revision},
            trace_id=trace_id,
        )

    async def revoke_connection(
        self,
        connection_id: UUID | str,
        *,
        expected_revision: int,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Revoke Provider access without deleting the MailHub projection."""

        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("mailhub_connection_revision_invalid")
        if expected_revision < 1:
            raise ValueError("mailhub_connection_revision_invalid")
        return await self.request(
            "POST",
            f"/v1/mail/connections/{connection_id}:revoke",
            json={"expected_revision": expected_revision},
            trace_id=trace_id,
        )

    async def export_data(
        self, *, include_content: bool = False, limit: int = 200
    ) -> dict[str, Any]:
        if not isinstance(include_content, bool):
            raise ValueError("mailhub_export_include_content_invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("mailhub_export_limit_invalid")
        return await self.request(
            "GET",
            f"/v1/mail/data-export?include_content={str(include_content).lower()}&limit={limit}",
        )

    async def execute_rule(
        self,
        rule_id: UUID | str,
        *,
        message_ids: tuple[UUID | str, ...],
        dry_run: bool = True,
        policy_id: UUID | str | None = None,
        grant_id: UUID | str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/v1/mail/rules/{rule_id}:execute",
            json={
                "message_ids": [str(message_id) for message_id in message_ids],
                "policy_id": str(policy_id) if policy_id else None,
                "grant_id": str(grant_id) if grant_id else None,
                "dry_run": dry_run,
            },
            trace_id=trace_id,
        )

    async def list_rule_executions(
        self, rule_id: UUID | str, *, limit: int = 100
    ) -> dict[str, Any]:
        return await self.request("GET", f"/v1/mail/rules/{rule_id}/executions?limit={limit}")
