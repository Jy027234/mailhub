"""Thin CAPlatform adapter for the standalone MailHub service.

This module deliberately contains no provider SDK or mail business rules. The
BFF supplies its verified identity/approval context and forwards only the
versioned MailHub contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx


class MailHubAdapterError(RuntimeError):
    """Redacted, host-facing MailHub transport error."""


class MailHubClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 15.0) -> None:
        self.base_url = _validate_base_url(base_url)
        if not 0.1 <= timeout_seconds <= 120:
            raise ValueError("mailhub_timeout_invalid")
        self.timeout_seconds = timeout_seconds

    async def list_connections(
        self, *, tenant_id: str, subject_id: str
    ) -> Sequence[Mapping[str, Any]]:
        payload = await self._request(
            "GET", "/v1/mail/connections", tenant_id=tenant_id, subject_id=subject_id
        )
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def connection_impact_preview(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        folder_refs: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        """Return a projection-only scope/deletion preview without Provider I/O."""

        normalized = tuple(value.strip() for value in folder_refs)
        if len(normalized) > 50 or any(
            not value
            or len(value) > 200
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
            for value in normalized
        ):
            raise ValueError("mailhub_impact_preview_folder_refs_invalid")
        path = f"/v1/mail/connections/{connection_id}/impact-preview"
        if normalized:
            path += "?" + "&".join(f"folder_ref={quote(value, safe='')}" for value in normalized)
        return await self._request("GET", path, tenant_id=tenant_id, subject_id=subject_id)

    async def provider_health(
        self, *, tenant_id: str, subject_id: str
    ) -> Sequence[Mapping[str, Any]]:
        """Return bounded health facts; MailHub never returns body or secrets."""

        payload = await self._request(
            "GET",
            "/v1/mail/admin/provider-health",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def list_audit(
        self, *, tenant_id: str, subject_id: str, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        bounded_limit = min(max(limit, 1), 200)
        payload = await self._request(
            "GET",
            f"/v1/mail/audit?limit={bounded_limit}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def begin_oauth(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        provider: str,
        authorization_endpoint: str,
        client_id: str,
        redirect_uri: str,
        scopes: Sequence[str],
        extra_parameters: Mapping[str, str] | None = None,
        connection_id: UUID | None = None,
        expected_revision: int | None = None,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Start host-configured OAuth without accepting provider metadata from the UI."""

        if provider not in {"gmail", "microsoft_graph"}:
            raise ValueError("mailhub_oauth_provider_invalid")
        if not authorization_endpoint.strip() or len(authorization_endpoint) > 2000:
            raise ValueError("mailhub_oauth_endpoint_invalid")
        if not client_id.strip() or len(client_id) > 512:
            raise ValueError("mailhub_oauth_client_id_invalid")
        if not redirect_uri.strip() or len(redirect_uri) > 2000:
            raise ValueError("mailhub_oauth_redirect_uri_invalid")
        normalized_scopes = tuple(scope.strip() for scope in scopes)
        if (
            not normalized_scopes
            or len(normalized_scopes) > 20
            or any(not scope or len(scope) > 200 for scope in normalized_scopes)
        ):
            raise ValueError("mailhub_oauth_scopes_invalid")
        if expected_revision is not None and (expected_revision < 1 or connection_id is None):
            raise ValueError("mailhub_oauth_expected_revision_invalid")
        allowed_parameter_names = (
            {"access_type", "include_granted_scopes", "prompt"}
            if provider == "gmail"
            else {"prompt"}
        )
        parameters = dict(extra_parameters or {})
        if any(
            key not in allowed_parameter_names
            or not key
            or len(key) > 100
            or not isinstance(value, str)
            or not value
            or len(value) > 1000
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for key, value in parameters.items()
        ):
            raise ValueError("mailhub_oauth_extra_parameters_invalid")
        return await self._request(
            "POST",
            f"/v1/mail/oauth/{provider}:authorize",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            json_body={
                "authorization_endpoint": authorization_endpoint,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scopes": list(normalized_scopes),
                "extra_parameters": parameters,
                "connection_id": str(connection_id) if connection_id else None,
                "expected_revision": expected_revision,
            },
        )

    async def complete_oauth(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        provider: str,
        state: str,
        code: str,
        redirect_uri: str,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        if provider not in {"gmail", "microsoft_graph"}:
            raise ValueError("mailhub_oauth_provider_invalid")
        if not 20 <= len(state) <= 4000 or not code.strip():
            raise ValueError("mailhub_oauth_callback_invalid")
        if not redirect_uri.strip() or len(redirect_uri) > 2000:
            raise ValueError("mailhub_oauth_redirect_uri_invalid")
        return await self._request(
            "POST",
            f"/v1/mail/oauth/{provider}:callback",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            json_body={"state": state, "code": code, "redirect_uri": redirect_uri},
        )

    async def list_agent_policies(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> Sequence[Mapping[str, Any]]:
        payload = await self._request(
            "GET",
            f"/v1/mail/agent-policies?limit={min(max(limit, 1), 200)}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def list_delegations(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> Sequence[Mapping[str, Any]]:
        payload = await self._request(
            "GET",
            f"/v1/mail/delegations?limit={min(max(limit, 1), 200)}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def delete_connection(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        expected_revision: int,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/v1/mail/connections/{connection_id}:delete",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            json_body={"expected_revision": expected_revision},
        )

    async def revoke_connection(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        expected_revision: int,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Revoke Provider access while retaining the MailHub projection.

        Deletion is a separate, irreversible lifecycle operation.  Keeping an
        explicit revoke projection lets a host prove the post-revocation
        negative path before it starts data cleanup.
        """

        return await self._request(
            "POST",
            f"/v1/mail/connections/{connection_id}:revoke",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            json_body={"expected_revision": expected_revision},
        )

    async def update_connection_scopes(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        expected_revision: int,
        granted_scopes: Sequence[str],
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        if expected_revision < 1:
            raise ValueError("mailhub_connection_revision_invalid")
        scopes = tuple(scope.strip() for scope in granted_scopes)
        if (
            not scopes
            or len(scopes) > 40
            or any(
                not scope
                or len(scope) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in scope)
                for scope in scopes
            )
        ):
            raise ValueError("mailhub_connection_scopes_invalid")
        return await self._request(
            "POST",
            f"/v1/mail/connections/{connection_id}:scopes",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            json_body={
                "expected_revision": expected_revision,
                "granted_scopes": list(scopes),
            },
        )

    async def refresh_connection(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        expected_revision: int,
        reason: str = "manual_refresh",
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        if expected_revision < 1:
            raise ValueError("mailhub_connection_revision_invalid")
        if not reason.strip() or len(reason) > 500:
            raise ValueError("mailhub_connection_refresh_reason_invalid")
        return await self._request(
            "POST",
            f"/v1/mail/connections/{connection_id}:refresh",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            json_body={"expected_revision": expected_revision, "reason": reason},
        )

    async def export_data(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        include_content: bool = False,
        limit: int = 200,
    ) -> Mapping[str, Any]:
        bounded_limit = min(max(limit, 1), 200)
        export_path = (
            f"/v1/mail/data-export?include_content={'true' if include_content else 'false'}"
            f"&limit={bounded_limit}"
        )
        return await self._request(
            "GET",
            export_path,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    async def analyze_message(
        self, *, tenant_id: str, subject_id: str, message_id: UUID, trace_id: str
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/v1/mail/messages/{message_id}:analyze",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
        )

    async def enqueue_sync(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        idempotency_key: str,
        trace_id: str | None = None,
        mode: str = "incremental",
        limit: int = 50,
        folder_ref: str = "INBOX",
        label_refs: Sequence[str] = (),
        received_after: str | None = None,
        received_before: str | None = None,
    ) -> Mapping[str, Any]:
        if mode not in {"incremental", "backfill", "reconcile"}:
            raise ValueError("mailhub_sync_mode_invalid")
        if not 1 <= limit <= 500:
            raise ValueError("mailhub_sync_limit_invalid")
        if not folder_ref.strip() or len(folder_ref) > 200:
            raise ValueError("mailhub_sync_folder_invalid")
        labels = tuple(label.strip() for label in label_refs)
        if len(labels) > 20 or any(
            not label
            or len(label) > 200
            or any(ord(char) < 33 or ord(char) == 127 for char in label)
            for label in labels
        ):
            raise ValueError("mailhub_sync_labels_invalid")
        if (
            labels or folder_ref != "INBOX" or received_after or received_before
        ) and mode != "backfill":
            raise ValueError("mailhub_sync_filter_requires_backfill")
        normalized_after = _normalize_sync_datetime(received_after, "received_after")
        normalized_before = _normalize_sync_datetime(received_before, "received_before")
        if (
            normalized_after is not None
            and normalized_before is not None
            and normalized_after >= normalized_before
        ):
            raise ValueError("mailhub_sync_date_range_invalid")
        return await self._request(
            "POST",
            f"/v1/mail/connections/{connection_id}/sync-jobs",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            json_body={
                "mode": mode,
                "limit": limit,
                "folder_ref": folder_ref,
                "label_refs": list(dict.fromkeys(labels)),
                "received_after": normalized_after,
                "received_before": normalized_before,
            },
        )

    async def list_sync_jobs(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 50,
    ) -> Sequence[Mapping[str, Any]]:
        bounded_limit = min(max(limit, 1), 200)
        path = f"/v1/mail/sync-jobs?limit={bounded_limit}"
        if connection_id is not None:
            path += f"&connection_id={connection_id}"
        payload = await self._request("GET", path, tenant_id=tenant_id, subject_id=subject_id)
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def get_sync_job(
        self, *, tenant_id: str, subject_id: str, job_id: UUID
    ) -> Mapping[str, Any]:
        return await self._request(
            "GET", f"/v1/mail/sync-jobs/{job_id}", tenant_id=tenant_id, subject_id=subject_id
        )

    async def cancel_sync_job(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        job_id: UUID,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/v1/mail/sync-jobs/{job_id}:cancel",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
        )

    async def enqueue_autonomy(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        replay_key: str,
        idempotency_key: str,
        limit: int = 50,
        message_limit: int = 50,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            "/v1/mail/autonomy/runs",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            json_body={
                "connection_id": str(connection_id),
                "replay_key": replay_key,
                "limit": limit,
                "message_limit": message_limit,
                "run_inline": False,
            },
        )

    async def list_autonomy_runs(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID | None = None,
        limit: int = 50,
    ) -> Sequence[Mapping[str, Any]]:
        query = f"/v1/mail/autonomy/runs?limit={min(max(limit, 1), 200)}"
        if connection_id is not None:
            query += f"&connection_id={connection_id}"
        payload = await self._request("GET", query, tenant_id=tenant_id, subject_id=subject_id)
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def get_autonomy_run(
        self, *, tenant_id: str, subject_id: str, run_id: UUID
    ) -> Mapping[str, Any]:
        return await self._request(
            "GET",
            f"/v1/mail/autonomy/runs/{run_id}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    async def control_autonomy_run(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        run_id: UUID,
        command: str,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        if command not in {"run", "pause", "resume", "cancel"}:
            raise ValueError("autonomy_command_invalid")
        body = {"reason": reason} if command == "pause" else None
        return await self._request(
            "POST",
            f"/v1/mail/autonomy/runs/{run_id}:{command}",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            json_body=body,
        )

    async def get_thread(
        self, *, tenant_id: str, subject_id: str, thread_id: UUID, limit: int = 200
    ) -> Mapping[str, Any]:
        return await self._request(
            "GET",
            f"/v1/mail/threads/{thread_id}?limit={min(max(limit, 1), 500)}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    async def list_threads(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> Sequence[Mapping[str, Any]]:
        payload = await self.list_thread_page(
            tenant_id=tenant_id,
            subject_id=subject_id,
            limit=limit,
        )
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def list_thread_page(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        limit: int = 50,
        cursor: str | None = None,
        connection_id: UUID | None = None,
        unread: bool = False,
        important: bool = False,
        has_attachment: bool = False,
        project: bool = False,
        candidate: bool = False,
    ) -> Mapping[str, Any]:
        query = f"/v1/mail/threads?limit={min(max(limit, 1), 200)}"
        if cursor:
            query += f"&cursor={quote(cursor, safe='')}"
        if connection_id is not None:
            query += f"&connection_id={connection_id}"
        for key, enabled in (
            ("unread", unread),
            ("important", important),
            ("attachment", has_attachment),
            ("project", project),
            ("candidate", candidate),
        ):
            if enabled:
                query += f"&{key}=true"
        payload = await self._request(
            "GET",
            query,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        return payload

    async def list_messages(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> Sequence[Mapping[str, Any]]:
        payload = await self._request(
            "GET",
            f"/v1/mail/messages?limit={min(max(limit, 1), 200)}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def search_messages(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        query: str,
        limit: int = 50,
        mode: str = "metadata",
    ) -> Mapping[str, Any]:
        normalized = query.strip()
        if not normalized or len(normalized) > 200:
            raise ValueError("mailhub_search_query_invalid")
        if mode not in {"metadata", "provider"}:
            raise ValueError("mailhub_search_mode_invalid")
        encoded_query = quote(normalized, safe="")
        bounded_limit = min(max(limit, 1), 200)
        return await self._request(
            "GET",
            f"/v1/mail/search?q={encoded_query}&limit={bounded_limit}&mode={mode}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    async def get_message_content(
        self, *, tenant_id: str, subject_id: str, message_id: UUID
    ) -> str:
        return await self._request_text(
            "GET",
            f"/v1/mail/messages/{message_id}/content",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    async def list_candidates(
        self, *, tenant_id: str, subject_id: str, limit: int = 50
    ) -> Sequence[Mapping[str, Any]]:
        payload = await self._request(
            "GET",
            f"/v1/mail/candidates?limit={min(max(limit, 1), 200)}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        value = payload.get("data", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def get_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
    ) -> Mapping[str, Any]:
        payload = await self._request(
            "GET",
            f"/v1/mail/candidates/{candidate_id}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        value = payload.get("data")
        if not isinstance(value, dict):
            raise MailHubAdapterError("mailhub_candidate_response_invalid")
        return value

    async def review_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
        approved: bool,
        expected_revision: int,
        review_reason: str | None = None,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/v1/mail/candidates/{candidate_id}:review",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            json_body={
                "approved": approved,
                "expected_revision": expected_revision,
                "review_reason": review_reason,
            },
        )

    async def apply_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        candidate_id: UUID,
        approval_ref: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        if not approval_ref.strip() or len(approval_ref) > 512:
            raise ValueError("mailhub_candidate_approval_ref_invalid")
        if not idempotency_key.strip() or len(idempotency_key) > 300:
            raise ValueError("mailhub_candidate_idempotency_key_invalid")
        return await self._request(
            "POST",
            f"/v1/mail/candidates/{candidate_id}:apply",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            json_body={"approval_ref": approval_ref},
        )

    async def execute_rule(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID,
        message_ids: Sequence[UUID],
        dry_run: bool = True,
        policy_id: UUID | None = None,
        grant_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> Mapping[str, Any]:
        if not message_ids or len(message_ids) > 100:
            raise ValueError("mailhub_rule_message_ids_invalid")
        return await self._request(
            "POST",
            f"/v1/mail/rules/{rule_id}:execute",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            json_body={
                "message_ids": [str(message_id) for message_id in message_ids],
                "dry_run": dry_run,
                "policy_id": str(policy_id) if policy_id else None,
                "grant_id": str(grant_id) if grant_id else None,
            },
        )

    async def list_rule_executions(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        rule_id: UUID,
        limit: int = 100,
    ) -> Mapping[str, Any]:
        bounded_limit = min(max(limit, 1), 500)
        return await self._request(
            "GET",
            f"/v1/mail/rules/{rule_id}/executions?limit={bounded_limit}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    async def create_draft(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        thread_id: UUID | None,
        recipient_addresses: Sequence[str],
        subject: str,
        body_text: str,
        idempotency_key: str,
        cc_addresses: Sequence[str] = (),
        bcc_addresses: Sequence[str] = (),
        attachment_refs: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            "/v1/mail/drafts",
            tenant_id=tenant_id,
            subject_id=subject_id,
            idempotency_key=idempotency_key,
            json_body={
                "connection_id": str(connection_id),
                "thread_id": str(thread_id) if thread_id else None,
                "recipient_addresses": list(recipient_addresses),
                "cc_addresses": list(cc_addresses),
                "bcc_addresses": list(bcc_addresses),
                "attachment_refs": list(attachment_refs),
                "subject": subject,
                "body_text": body_text,
            },
        )

    async def get_draft(
        self, *, tenant_id: str, subject_id: str, draft_id: UUID
    ) -> Mapping[str, Any]:
        return await self._request(
            "GET",
            f"/v1/mail/drafts/{draft_id}",
            tenant_id=tenant_id,
            subject_id=subject_id,
        )

    async def send_draft(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        draft_id: UUID,
        confirmation_ref: str,
        expected_revision: int,
        expected_content_sha256: str,
        expected_recipient_digest: str,
        idempotency_key: str,
        trace_id: str | None = None,
        policy_id: UUID | None = None,
        grant_id: UUID | None = None,
        agent_subject_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/v1/mail/drafts/{draft_id}:send",
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            json_body={
                "expected_revision": expected_revision,
                "expected_content_sha256": expected_content_sha256,
                "expected_recipient_digest": expected_recipient_digest,
                "confirmation_ref": confirmation_ref,
                "policy_id": str(policy_id) if policy_id else None,
                "grant_id": str(grant_id) if grant_id else None,
                "agent_subject_id": agent_subject_id,
            },
        )

    async def update_draft(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        draft_id: UUID,
        expected_revision: int,
        connection_id: UUID,
        thread_id: UUID | None,
        recipient_addresses: Sequence[str],
        subject: str,
        body_text: str,
        idempotency_key: str,
        cc_addresses: Sequence[str] = (),
        bcc_addresses: Sequence[str] = (),
        attachment_refs: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        return await self._request(
            "PUT",
            f"/v1/mail/drafts/{draft_id}",
            tenant_id=tenant_id,
            subject_id=subject_id,
            idempotency_key=idempotency_key,
            json_body={
                "expected_revision": expected_revision,
                "connection_id": str(connection_id),
                "thread_id": str(thread_id) if thread_id else None,
                "recipient_addresses": list(recipient_addresses),
                "cc_addresses": list(cc_addresses),
                "bcc_addresses": list(bcc_addresses),
                "attachment_refs": list(attachment_refs),
                "subject": subject,
                "body_text": body_text,
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        tenant_id: str,
        subject_id: str,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "//" in path or ".." in path.split("/"):
            raise ValueError("mailhub_path_invalid")
        headers = {
            "Accept": "application/json",
            "X-MailHub-Tenant": tenant_id,
            "X-MailHub-Subject": subject_id,
        }
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.request(
                    method,
                    self.base_url + path,
                    headers=headers,
                    json=dict(json_body) if json_body is not None else None,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MailHubAdapterError("mailhub_unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise MailHubAdapterError(f"mailhub_http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MailHubAdapterError("mailhub_invalid_json") from exc
        if not isinstance(payload, dict):
            raise MailHubAdapterError("mailhub_invalid_payload")
        return payload

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str:
        if not path.startswith("/") or "//" in path or ".." in path.split("/"):
            raise ValueError("mailhub_path_invalid")
        headers = {
            "Accept": "text/plain",
            "X-MailHub-Tenant": tenant_id,
            "X-MailHub-Subject": subject_id,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.request(
                    method,
                    self.base_url + path,
                    headers=headers,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MailHubAdapterError("mailhub_unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise MailHubAdapterError(f"mailhub_http_{response.status_code}")
        if len(response.content) > 1_000_000:
            raise MailHubAdapterError("mailhub_content_too_large")
        return response.text


def _normalize_sync_datetime(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 80
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"mailhub_sync_{field_name}_invalid")
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"mailhub_sync_{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"mailhub_sync_{field_name}_must_be_aware")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold()
    local_http = parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not hostname
        or (parsed.scheme != "https" and not local_http)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("mailhub_base_url_must_be_tls")
    return value.rstrip("/")
