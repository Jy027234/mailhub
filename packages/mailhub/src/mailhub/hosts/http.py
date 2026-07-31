"""Generic JSON Host Port adapters for CAPlatform and other hosts."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from mailhub.domain import (
    AgentActionRequest,
    MailActionCandidate,
    MailMessageProjection,
    PolicyDecision,
    ProviderName,
)
from mailhub.errors import AuthorizationError, ProviderFailureError, RateLimitedError
from mailhub.events import validate_event_payload
from mailhub.hosts.migration import SenderInterlockPort
from mailhub.memory import ApprovedKnowledgeReference, reference_payload
from mailhub.notifications import NotificationChangeKind, ProviderNotification
from mailhub.oauth import (
    OAuthCallbackContext,
    OAuthCallbackPort,
    OAuthProvider,
    OAuthStateRecord,
    OAuthStateStore,
    validate_granted_scopes,
)
from mailhub.observability import TelemetryPort, redact_event
from mailhub.ports import (
    AgentMemoryPort,
    AiExecutionPort,
    ApprovalPort,
    AuditPort,
    CredentialBrokerPort,
    CredentialRefreshPort,
    CredentialRevocationPort,
    HostIdentityPort,
    KillSwitchPort,
    KnowledgeLifecyclePort,
    KnowledgeSafetyPort,
    NotificationPort,
    ObjectStorePort,
    ProviderNotificationDelivery,
    ProviderNotificationVerifierPort,
    ProviderSubscriptionLease,
    ProviderSubscriptionPort,
)
from mailhub.quota import QuotaLease, QuotaLimits, QuotaPort


class HttpHostActionAdapter:
    """Thin host-owned JSON adapter; paths are configurable and no provider API leaks in."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        discover_path: str = "/v1/mail-host/actions/discover",
        propose_path: str = "/v1/mail-host/actions/propose",
        execute_path: str = "/v1/mail-host/actions/execute",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.base_url = _validate_host_base_url(base_url)
        self.headers = _validate_headers(headers)
        self.discover_path = _validate_path(discover_path)
        self.propose_path = _validate_path(propose_path)
        self.execute_path = _validate_path(execute_path)
        self.timeout_seconds = _validate_timeout(timeout_seconds)

    async def discover(
        self, *, tenant_id: str, subject_id: str, query: str | None = None
    ) -> Sequence[Mapping[str, object]]:
        payload = await self._request(
            "POST",
            self.discover_path,
            {"tenant_id": tenant_id, "subject_id": subject_id, "query": query},
        )
        value = payload.get("objects", [])
        return (
            tuple(item for item in value if isinstance(item, dict))
            if isinstance(value, list)
            else ()
        )

    async def propose(self, *, action: AgentActionRequest) -> Mapping[str, object]:
        return await self._request("POST", self.propose_path, {"action": _action_json(action)})

    async def execute(
        self, *, action: AgentActionRequest, approval_ref: str
    ) -> Mapping[str, object]:
        return await self._request(
            "POST",
            self.execute_path,
            {"action": _action_json(action), "approval_ref": approval_ref},
            request_headers={"Idempotency-Key": str(action.action_id)},
        )

    async def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
        *,
        request_headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.request(
                    method,
                    self.base_url + path,
                    headers={
                        "Accept": "application/json",
                        **self.headers,
                        **(dict(request_headers) if request_headers is not None else {}),
                    },
                    json=dict(payload),
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderFailureError("host_action_unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ProviderFailureError(f"host_action_http_{response.status_code}")
        try:
            value = response.json()
        except ValueError as exc:
            raise ProviderFailureError("host_action_invalid_json") from exc
        if not isinstance(value, dict):
            raise ProviderFailureError("host_action_invalid_payload")
        return value


class HttpKnowledgeSink:
    """Review-only knowledge candidate adapter; publication remains host-owned."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        submit_path: str = "/v1/mail-host/knowledge/candidates",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.base_url = _validate_host_base_url(base_url)
        self.headers = _validate_headers(headers)
        self.submit_path = _validate_path(submit_path)
        self.timeout_seconds = _validate_timeout(timeout_seconds)

    async def submit_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        message: MailMessageProjection,
        candidate: MailActionCandidate,
    ) -> Mapping[str, object]:
        if message.tenant_id != tenant_id or candidate.subject_id != subject_id:
            raise ProviderFailureError("knowledge_scope_mismatch")
        payload = {
            "tenant_id": tenant_id,
            "subject_id": subject_id,
            "candidate_id": str(candidate.candidate_id),
            "message_id": str(message.message_id),
            "content_sha256": message.content_sha256,
            "candidate": dict(candidate.payload),
            "evidence": [dict(item) for item in candidate.evidence],
        }
        if message.body_object_ref is not None:
            # The host receives a governed object handle, never the hydrated
            # body.  It remains responsible for rights, scan and publication.
            payload["body_object_ref"] = message.body_object_ref
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.post(
                    self.base_url + self.submit_path,
                    headers={
                        "Accept": "application/json",
                        **self.headers,
                        "Idempotency-Key": f"mailhub-knowledge:{candidate.candidate_id}",
                    },
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderFailureError("knowledge_sink_unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ProviderFailureError(f"knowledge_sink_http_{response.status_code}")
        try:
            value: Any = response.json()
        except ValueError as exc:
            raise ProviderFailureError("knowledge_sink_invalid_json") from exc
        if not isinstance(value, dict):
            raise ProviderFailureError("knowledge_sink_invalid_payload")
        return value


class _HttpPortBase:
    """Shared bounded JSON transport for host adapters.

    The adapter never logs request payloads.  Authentication headers are
    supplied by the host deployment and are not persisted by MailHub.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None,
        timeout_seconds: float,
        error_prefix: str,
    ) -> None:
        self.base_url = _validate_host_base_url(base_url)
        if not 0.1 <= timeout_seconds <= 120:
            raise ValueError("host_timeout_invalid")
        self.headers = _validate_headers(headers)
        self.timeout_seconds = timeout_seconds
        self.error_prefix = error_prefix

    async def _json_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        request_headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        _validate_path(path)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.request(
                    method,
                    self.base_url + path,
                    headers={
                        "Accept": "application/json",
                        **self.headers,
                        **(dict(request_headers) if request_headers is not None else {}),
                    },
                    json=dict(payload) if payload is not None else None,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderFailureError(f"{self.error_prefix}_unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            code = (
                "authorization_denied"
                if response.status_code in {401, 403}
                else f"{self.error_prefix}_http_{response.status_code}"
            )
            if code == "authorization_denied":
                raise AuthorizationError(code)
            raise ProviderFailureError(code)
        if response.status_code == 204:
            return response.status_code, {}
        try:
            value: Any = response.json()
        except ValueError as exc:
            raise ProviderFailureError(f"{self.error_prefix}_invalid_json") from exc
        if not isinstance(value, dict):
            raise ProviderFailureError(f"{self.error_prefix}_invalid_payload")
        return response.status_code, value


class HttpOAuthStateStore(_HttpPortBase, OAuthStateStore):
    """Shared encrypted/TTL OAuth state store owned by the host.

    The store persists only the short-lived PKCE binding.  It must never
    return provider tokens or accept them in this contract; the state record
    is consumed exactly once by the host endpoint.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        save_path: str = "/v1/mail-host/oauth/state",
        consume_path: str = "/v1/mail-host/oauth/state/consume",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="oauth_state",
        )
        self.save_path = _validate_path(save_path)
        self.consume_path = _validate_path(consume_path)

    async def save(self, record: OAuthStateRecord) -> None:
        _validate_oauth_text(record.state_id, "state_id", maximum=512)
        _validate_oauth_text(record.tenant_id, "tenant_id", maximum=200)
        _validate_oauth_text(record.subject_id, "subject_id", maximum=200)
        _validate_oauth_text(record.redirect_uri, "redirect_uri", maximum=2000)
        _validate_oauth_text(record.code_verifier, "code_verifier", maximum=512)
        _validate_oauth_text(record.nonce, "nonce", maximum=512)
        if record.expected_revision is not None and record.connection_id is None:
            raise ProviderFailureError("oauth_state_connection_id_required")
        if record.expected_revision is not None and record.expected_revision < 1:
            raise ProviderFailureError("oauth_state_expected_revision_invalid")
        if record.expires_at.tzinfo is None:
            raise ProviderFailureError("oauth_state_expiry_timezone_missing")
        payload: dict[str, object] = {
            "state_id": record.state_id,
            "tenant_id": record.tenant_id,
            "subject_id": record.subject_id,
            "provider": record.provider.value,
            "redirect_uri": record.redirect_uri,
            "code_verifier": record.code_verifier,
            "nonce": record.nonce,
            "expires_at": record.expires_at.astimezone(UTC).isoformat(),
        }
        if record.connection_id is not None:
            payload["connection_id"] = str(record.connection_id)
        if record.expected_revision is not None:
            payload["expected_revision"] = record.expected_revision
        if record.requested_scopes:
            payload["requested_scopes"] = list(record.requested_scopes)
        await self._json_request(
            "POST",
            self.save_path,
            payload,
            request_headers={"Idempotency-Key": f"mailhub-oauth-state:{record.state_id}"},
        )

    async def consume(self, state_id: str) -> OAuthStateRecord | None:
        _validate_oauth_text(state_id, "state_id", maximum=512)
        _, payload = await self._json_request("POST", self.consume_path, {"state_id": state_id})
        raw = payload.get("record", payload.get("data", payload))
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ProviderFailureError("oauth_state_response_invalid")
        if raw.get("state_id") is None:
            # A host may use a 200/empty response for a missing or replayed
            # state.  Treat it as a normal one-time-consume miss.
            return None
        try:
            provider = OAuthProvider(str(raw["provider"]))
            expires_at = datetime.fromisoformat(str(raw["expires_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderFailureError("oauth_state_response_invalid") from exc
        if expires_at.tzinfo is None:
            raise ProviderFailureError("oauth_state_response_invalid")
        state_value = _required_oauth_value(raw, "state_id", maximum=512)
        tenant_value = _required_oauth_value(raw, "tenant_id", maximum=200)
        subject_value = _required_oauth_value(raw, "subject_id", maximum=200)
        redirect_value = _required_oauth_value(raw, "redirect_uri", maximum=2000)
        verifier_value = _required_oauth_value(raw, "code_verifier", maximum=512)
        nonce_value = _required_oauth_value(raw, "nonce", maximum=512)
        if state_value != state_id:
            raise ProviderFailureError("oauth_state_binding_mismatch")
        connection_value = raw.get("connection_id")
        connection_id = None
        if connection_value is not None:
            connection_text = _required_oauth_value(raw, "connection_id", maximum=100)
            try:
                connection_id = UUID(connection_text)
            except ValueError as exc:
                raise ProviderFailureError("oauth_state_response_invalid") from exc
        expected_revision_value = raw.get("expected_revision")
        expected_revision = None
        if expected_revision_value is not None:
            if (
                isinstance(expected_revision_value, bool)
                or not isinstance(expected_revision_value, int)
                or expected_revision_value < 1
            ):
                raise ProviderFailureError("oauth_state_response_invalid")
            expected_revision = expected_revision_value
        if expected_revision is not None and connection_id is None:
            raise ProviderFailureError("oauth_state_response_invalid")
        raw_scopes = raw.get("requested_scopes", ())
        if not isinstance(raw_scopes, (list, tuple, set, frozenset)) or len(raw_scopes) > 40:
            raise ProviderFailureError("oauth_state_response_invalid")
        requested_scopes: list[str] = []
        for item in raw_scopes:
            if (
                not isinstance(item, str)
                or not item.strip()
                or len(item) > 200
                or any(ord(char) < 33 or ord(char) == 127 for char in item)
            ):
                raise ProviderFailureError("oauth_state_response_invalid")
            normalized = item.strip()
            if normalized not in requested_scopes:
                requested_scopes.append(normalized)
        if provider in {OAuthProvider.GMAIL, OAuthProvider.MICROSOFT_GRAPH}:
            if not requested_scopes:
                raise ProviderFailureError("oauth_state_requested_scopes_missing")
            try:
                validate_granted_scopes(provider, requested_scopes, tuple(requested_scopes))
            except ValueError as exc:
                raise ProviderFailureError("oauth_state_requested_scopes_invalid") from exc
        return OAuthStateRecord(
            state_id=state_value,
            tenant_id=tenant_value,
            subject_id=subject_value,
            provider=provider,
            redirect_uri=redirect_value,
            code_verifier=verifier_value,
            nonce=nonce_value,
            expires_at=expires_at.astimezone(UTC),
            connection_id=connection_id,
            expected_revision=expected_revision,
            requested_scopes=tuple(requested_scopes),
        )


class HttpOAuthCallbackAdapter(_HttpPortBase, OAuthCallbackPort):
    """Host-owned authorization-code exchange and Secret Manager handoff.

    Provider access/refresh tokens stay inside the host credential broker.  A
    successful response is intentionally reduced to mailbox identity,
    granted scopes and an opaque credential reference.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        exchange_path: str = "/v1/mail-host/oauth/exchange",
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="oauth_exchange",
        )
        self.exchange_path = _validate_path(exchange_path)

    async def exchange(self, *, context: OAuthCallbackContext, code: str) -> Mapping[str, object]:
        _validate_oauth_text(code, "code", maximum=8192)
        _validate_oauth_text(context.tenant_id, "tenant_id", maximum=200)
        _validate_oauth_text(context.subject_id, "subject_id", maximum=200)
        _validate_oauth_text(context.redirect_uri, "redirect_uri", maximum=2000)
        _validate_oauth_text(context.code_verifier, "code_verifier", maximum=512)
        _validate_oauth_text(context.nonce, "nonce", maximum=512)
        if context.expected_revision is not None and context.connection_id is None:
            raise ProviderFailureError("oauth_exchange_connection_id_required")
        exchange_payload: dict[str, object] = {
            "tenant_id": context.tenant_id,
            "subject_id": context.subject_id,
            "provider": context.provider.value,
            "redirect_uri": context.redirect_uri,
            "code": code,
            "code_verifier": context.code_verifier,
            "nonce": context.nonce,
        }
        if context.connection_id is not None:
            exchange_payload["connection_id"] = str(context.connection_id)
        if context.expected_revision is not None:
            exchange_payload["expected_revision"] = context.expected_revision
        if context.requested_scopes:
            exchange_payload["requested_scopes"] = list(context.requested_scopes)
        _, payload = await self._json_request(
            "POST",
            self.exchange_path,
            exchange_payload,
        )
        raw = payload.get("metadata", payload.get("data", payload))
        if not isinstance(raw, dict):
            raise ProviderFailureError("oauth_exchange_metadata_invalid")
        if _contains_oauth_secret(raw):
            raise ProviderFailureError("oauth_exchange_secret_leak")
        email_address = raw.get("email_address")
        credential_ref = raw.get("credential_ref")
        if not isinstance(email_address, str) or not 3 <= len(email_address) <= 320:
            raise ProviderFailureError("oauth_exchange_email_invalid")
        if not isinstance(credential_ref, str) or not 1 <= len(credential_ref) <= 512:
            raise ProviderFailureError("oauth_exchange_credential_ref_invalid")
        try:
            scopes = validate_granted_scopes(
                context.provider,
                raw.get("granted_scopes", ()),
                context.requested_scopes,
            )
        except ValueError as exc:
            raise ProviderFailureError(str(exc)) from exc
        result: dict[str, object] = {
            "email_address": email_address,
            "credential_ref": credential_ref,
            "granted_scopes": scopes,
        }
        for key in ("provider_account_id", "provider_tenant_id"):
            value = raw.get(key)
            if value is not None:
                if not isinstance(value, str) or not 1 <= len(value) <= 512:
                    raise ProviderFailureError("oauth_exchange_metadata_invalid")
                if any(ord(char) < 33 or ord(char) == 127 for char in value):
                    raise ProviderFailureError("oauth_exchange_metadata_invalid")
                result[key] = value
        if context.provider in {OAuthProvider.GMAIL, OAuthProvider.MICROSOFT_GRAPH} and (
            "provider_account_id" not in result
        ):
            raise ProviderFailureError("oauth_exchange_provider_account_id_required")
        credential_version = raw.get("credential_version")
        if credential_version is not None:
            if (
                isinstance(credential_version, bool)
                or not isinstance(credential_version, int)
                or not 1 <= credential_version <= 2_147_483_647
            ):
                raise ProviderFailureError("oauth_exchange_credential_version_invalid")
            result["credential_version"] = credential_version
        return result


class HttpProviderSubscriptionAdapter(_HttpPortBase, ProviderSubscriptionPort):
    """Host-owned Gmail watch/Graph subscription lifecycle adapter.

    Provider access tokens, Pub/Sub OIDC verification, Graph clientState or
    certificate validation, durable renewal and scheduler leases stay in the
    host. MailHub receives only bounded, non-secret lease metadata.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        ensure_path: str = "/v1/mail-host/provider-subscriptions/ensure",
        cancel_path: str = "/v1/mail-host/provider-subscriptions/cancel",
        timeout_seconds: float = 20.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="provider_subscription",
        )
        self.ensure_path = _validate_path(ensure_path)
        self.cancel_path = _validate_path(cancel_path)

    async def ensure_subscription(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        provider: ProviderName,
        callback_endpoint: str,
        desired_expiry: datetime,
        client_state_ref: str | None = None,
        idempotency_key: str,
    ) -> ProviderSubscriptionLease:
        _validate_subscription_provider(provider)
        _validate_oauth_text(tenant_id, "tenant_id", maximum=200)
        _validate_oauth_text(subject_id, "subject_id", maximum=200)
        callback = _validate_callback_endpoint(callback_endpoint)
        expiry = _validate_subscription_expiry(desired_expiry)
        _validate_oauth_text(idempotency_key, "idempotency_key", maximum=512)
        if client_state_ref is not None:
            _validate_oauth_text(client_state_ref, "client_state_ref", maximum=512)
        _, payload = await self._json_request(
            "POST",
            self.ensure_path,
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "connection_id": str(connection_id),
                "provider": provider.value,
                "callback_endpoint": callback,
                "desired_expiry": expiry.isoformat(),
                "client_state_ref": client_state_ref,
            },
            request_headers={"Idempotency-Key": idempotency_key},
        )
        raw = payload.get("subscription", payload.get("data", payload))
        if not isinstance(raw, dict) or _contains_oauth_secret(raw):
            raise ProviderFailureError("provider_subscription_response_invalid")
        raw_provider = raw.get("provider", provider.value)
        if raw_provider != provider.value:
            raise ProviderFailureError("provider_subscription_provider_mismatch")
        subscription_ref = raw.get("subscription_ref")
        status_value = raw.get("status")
        expires_value = raw.get("expires_at")
        callback_value = raw.get("callback_endpoint", callback)
        if not isinstance(subscription_ref, str) or not 1 <= len(subscription_ref) <= 1000:
            raise ProviderFailureError("provider_subscription_ref_invalid")
        if not isinstance(status_value, str) or not 1 <= len(status_value) <= 100:
            raise ProviderFailureError("provider_subscription_status_invalid")
        if not isinstance(expires_value, str):
            raise ProviderFailureError("provider_subscription_expiry_invalid")
        try:
            expires_at = datetime.fromisoformat(expires_value)
        except ValueError as exc:
            raise ProviderFailureError("provider_subscription_expiry_invalid") from exc
        if expires_at.tzinfo is None:
            raise ProviderFailureError("provider_subscription_expiry_invalid")
        try:
            expires_at = _validate_subscription_expiry(expires_at)
        except ProviderFailureError as exc:
            if exc.args and exc.args[0] == "provider_subscription_expiry_out_of_bounds":
                raise
            raise ProviderFailureError("provider_subscription_expired") from exc
        if (
            not isinstance(callback_value, str)
            or _validate_callback_endpoint(callback_value) != callback
        ):
            raise ProviderFailureError("provider_subscription_callback_mismatch")
        provider_request_id = raw.get("provider_request_id")
        if provider_request_id is not None and (
            not isinstance(provider_request_id, str) or len(provider_request_id) > 512
        ):
            raise ProviderFailureError("provider_subscription_request_id_invalid")
        returned_state_ref = raw.get("client_state_ref", client_state_ref)
        if returned_state_ref is not None:
            if not isinstance(returned_state_ref, str) or not 1 <= len(returned_state_ref) <= 512:
                raise ProviderFailureError("provider_subscription_client_state_ref_invalid")
            if client_state_ref is not None and returned_state_ref != client_state_ref:
                raise ProviderFailureError("provider_subscription_client_state_ref_mismatch")
        return ProviderSubscriptionLease(
            provider=provider,
            connection_id=connection_id,
            subscription_ref=subscription_ref,
            status=status_value,
            expires_at=expires_at,
            callback_endpoint=callback,
            provider_request_id=provider_request_id,
            client_state_ref=returned_state_ref,
        )

    async def cancel_subscription(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        provider: ProviderName,
        subscription_ref: str,
        request_id: str,
    ) -> Mapping[str, object]:
        _validate_subscription_provider(provider)
        _validate_oauth_text(tenant_id, "tenant_id", maximum=200)
        _validate_oauth_text(subject_id, "subject_id", maximum=200)
        _validate_oauth_text(subscription_ref, "subscription_ref", maximum=1000)
        _validate_oauth_text(request_id, "request_id", maximum=512)
        _, payload = await self._json_request(
            "POST",
            self.cancel_path,
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "connection_id": str(connection_id),
                "provider": provider.value,
                "subscription_ref": subscription_ref,
            },
            request_headers={"Idempotency-Key": request_id},
        )
        if _contains_oauth_secret(payload):
            raise ProviderFailureError("provider_subscription_response_invalid")
        result: dict[str, object] = {}
        for key in ("status", "cancel_ref", "provider_request_id"):
            value = payload.get(key)
            if value is not None:
                if not isinstance(value, str) or len(value) > 1000:
                    raise ProviderFailureError("provider_subscription_response_invalid")
                result[key] = value
        result.setdefault("status", "cancel_requested")
        return result


class HttpProviderNotificationVerifierAdapter(_HttpPortBase, ProviderNotificationVerifierPort):
    """Host-owned provider callback verification and account routing adapter.

    The callback body is forwarded only to the configured host verifier as a
    bounded base64 payload.  Sensitive inbound headers such as ``Authorization``
    are deliberately stripped; the edge/host must perform OIDC/clientState or
    certificate verification and return only body-free, scoped deliveries.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        verify_path: str = "/v1/mail-host/provider-notifications/verify",
        timeout_seconds: float = 20.0,
        max_body_bytes: int = 10_000_000,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="provider_notification",
        )
        if not 1024 <= max_body_bytes <= 10_000_000:
            raise ValueError("provider_notification_body_limit_invalid")
        self.verify_path = _validate_path(verify_path)
        self.max_body_bytes = max_body_bytes

    async def verify_and_route(
        self,
        *,
        provider: ProviderName,
        body: bytes,
        headers: Mapping[str, str],
    ) -> tuple[ProviderNotificationDelivery, ...]:
        if provider not in {ProviderName.GMAIL, ProviderName.MICROSOFT_GRAPH}:
            raise ProviderFailureError("provider_notification_provider_unsupported")
        if len(body) > self.max_body_bytes:
            raise ProviderFailureError("provider_notification_body_too_large")
        if not isinstance(headers, Mapping):
            raise ProviderFailureError("provider_notification_headers_invalid")
        safe_headers = _safe_provider_notification_headers(headers)
        _, payload = await self._json_request(
            "POST",
            self.verify_path,
            {
                "provider": provider.value,
                "body_base64": base64.b64encode(body).decode("ascii"),
                "headers": safe_headers,
            },
        )
        if _contains_oauth_secret(payload):
            raise ProviderFailureError("provider_notification_secret_leak")
        raw = payload.get("notifications", payload.get("data", payload))
        if isinstance(raw, dict):
            raw = raw.get("notifications", ())
        if not isinstance(raw, list) or len(raw) > 100:
            raise ProviderFailureError("provider_notification_response_invalid")
        return tuple(
            _provider_notification_delivery(item, expected_provider=provider) for item in raw
        )


class HttpKnowledgeLifecycleAdapter(_HttpPortBase, KnowledgeLifecyclePort):
    """Host adapter for revoking or reindexing knowledge by governed source refs."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        revoke_path: str = "/v1/mail-host/knowledge/lifecycle/revoke",
        timeout_seconds: float = 15.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="knowledge_lifecycle",
        )
        self.revoke_path = _validate_path(revoke_path)

    async def revoke_source(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        connection_id: UUID,
        message_ids: Sequence[UUID],
        reason: str,
        request_id: str,
    ) -> Mapping[str, object]:
        if not tenant_id.strip() or not subject_id.strip() or not reason.strip():
            raise ProviderFailureError("knowledge_lifecycle_scope_invalid")
        if not request_id.strip() or len(request_id) > 300:
            raise ProviderFailureError("knowledge_lifecycle_request_id_invalid")
        if len(message_ids) > 10_000:
            raise ProviderFailureError("knowledge_lifecycle_message_limit")
        _, payload = await self._json_request(
            "POST",
            self.revoke_path,
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "connection_id": str(connection_id),
                "message_ids": [str(message_id) for message_id in message_ids],
                "reason": reason,
                "request_id": request_id,
            },
            request_headers={"Idempotency-Key": request_id},
        )
        return payload


class HttpAgentMemoryAdapter(_HttpPortBase, AgentMemoryPort):
    """Host adapter for storing approved knowledge refs without email bodies."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        store_path: str = "/v1/mail-host/memory/approved-references",
        timeout_seconds: float = 15.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="agent_memory",
        )
        self.store_path = _validate_path(store_path)

    async def store_approved_reference(
        self, *, reference: ApprovedKnowledgeReference
    ) -> Mapping[str, object]:
        payload = reference_payload(reference)
        _, result = await self._json_request(
            "POST",
            self.store_path,
            payload,
            request_headers={"Idempotency-Key": f"mailhub-memory:{reference.candidate_id}"},
        )
        return result


class HttpKillSwitchAdapter(_HttpPortBase, KillSwitchPort):
    """Host-owned global/tenant/provider kill-switch decision adapter.

    The endpoint is read-only from MailHub's perspective.  Switch mutations,
    four-eyes approval, and the authoritative audit ledger remain in the host.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        check_path: str = "/v1/mail-host/kill-switch/check",
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="kill_switch",
        )
        self.check_path = _validate_path(check_path)

    async def check(
        self,
        *,
        tenant_id: str,
        subject_id: str | None,
        provider: ProviderName | None,
        operation: str,
    ) -> Mapping[str, object]:
        if not tenant_id.strip() or not operation.strip():
            raise ProviderFailureError("kill_switch_scope_invalid")
        provider_value = provider.value if provider is not None else None
        if provider_value is not None and (
            not isinstance(provider_value, str) or not provider_value.strip()
        ):
            raise ProviderFailureError("kill_switch_provider_invalid")
        _, payload = await self._json_request(
            "POST",
            self.check_path,
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "provider": provider_value,
                "operation": operation,
            },
        )
        allowed = payload.get("allowed")
        if not isinstance(allowed, bool):
            raise ProviderFailureError("kill_switch_response_invalid")
        reason = payload.get("reason")
        if not allowed and (not isinstance(reason, str) or not reason.strip()):
            raise ProviderFailureError("kill_switch_response_invalid")
        return payload


class HttpSenderInterlockAdapter(_HttpPortBase, SenderInterlockPort):
    """Host-owned durable sender lease for shadow/cutover migrations."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        acquire_path: str = "/v1/mail-host/migration/sender-lease/acquire",
        release_path: str = "/v1/mail-host/migration/sender-lease/release",
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="migration_interlock",
        )
        self.acquire_path = _validate_path(acquire_path)
        self.release_path = _validate_path(release_path)

    async def claim(
        self, *, tenant_id: str, account_ref: str, owner: str, purpose: str = "email"
    ) -> bool:
        try:
            _, payload = await self._json_request(
                "POST",
                self.acquire_path,
                {
                    "tenant_id": tenant_id,
                    "account_ref": account_ref,
                    "owner": owner,
                    "purpose": purpose,
                },
            )
        except ProviderFailureError as exc:
            # A conflict is a normal negative claim, not a transport failure.
            if exc.message == "migration_interlock_http_409":
                return False
            raise
        acquired = payload.get("acquired", payload.get("claimed"))
        if not isinstance(acquired, bool):
            raise ProviderFailureError("migration_interlock_response_invalid")
        return acquired

    async def release(
        self, *, tenant_id: str, account_ref: str, owner: str, purpose: str = "email"
    ) -> None:
        await self._json_request(
            "POST",
            self.release_path,
            {
                "tenant_id": tenant_id,
                "account_ref": account_ref,
                "owner": owner,
                "purpose": purpose,
            },
        )


class HttpTelemetryAdapter(_HttpPortBase, TelemetryPort):
    """OpenTelemetry/structured-event bridge owned by the host deployment."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        record_path: str = "/v1/mail-host/telemetry/events",
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="telemetry",
        )
        self.record_path = _validate_path(record_path)

    async def record(self, *, name: str, fields: Mapping[str, object]) -> None:
        if not name.strip() or len(name) > 200 or len(fields) > 50:
            raise ProviderFailureError("telemetry_event_invalid")
        safe_fields = redact_event(fields)
        await self._json_request(
            "POST",
            self.record_path,
            {"name": name, "fields": safe_fields},
        )


class HttpEventPublisherAdapter(_HttpPortBase):
    """Host-owned durable event bus adapter.

    The event envelope is validated before transport and the envelope's event
    id is used as the idempotency key.  The host, rather than MailHub, owns
    durable replay, subscriber authorization and delivery semantics.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        publish_path: str = "/v1/mail-host/events",
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="event_publisher",
        )
        self.publish_path = _validate_path(publish_path)

    async def publish(self, event: Mapping[str, object]) -> None:
        validate_event_payload(event)
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ProviderFailureError("event_publisher_event_id_invalid")
        await self._json_request(
            "POST",
            self.publish_path,
            dict(event),
            request_headers={"Idempotency-Key": f"mailhub-event:{event_id}"},
        )


class HttpQuotaAdapter(_HttpPortBase, QuotaPort):
    """Durable host quota lease adapter; the host owns atomic counters."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        acquire_path: str = "/v1/mail-host/quota/acquire",
        release_path: str = "/v1/mail-host/quota/release",
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="quota",
        )
        self.acquire_path = _validate_path(acquire_path)
        self.release_path = _validate_path(release_path)

    async def acquire(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        account_id: UUID | None,
        operation: str,
        limits: QuotaLimits,
    ) -> QuotaLease:
        try:
            _, payload = await self._json_request(
                "POST",
                self.acquire_path,
                {
                    "tenant_id": tenant_id,
                    "subject_id": subject_id,
                    "account_id": str(account_id) if account_id is not None else None,
                    "operation": operation,
                    "limits": {
                        "max_concurrent": limits.max_concurrent,
                        "max_per_hour": limits.max_per_hour,
                        "max_per_day": limits.max_per_day,
                    },
                },
            )
        except ProviderFailureError as exc:
            if exc.message == "quota_http_429":
                raise RateLimitedError("quota_remote_limit") from exc
            raise
        raw = payload.get("lease", payload)
        if not isinstance(raw, dict):
            raise ProviderFailureError("quota_response_invalid")
        lease_id = raw.get("lease_id")
        acquired_at = raw.get("acquired_at")
        if not isinstance(lease_id, str) or not isinstance(acquired_at, str):
            raise ProviderFailureError("quota_response_invalid")
        try:
            parsed_id = UUID(lease_id)
            parsed_at = datetime.fromisoformat(acquired_at)
        except (TypeError, ValueError) as exc:
            raise ProviderFailureError("quota_response_invalid") from exc
        return QuotaLease(
            lease_id=parsed_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            account_id=account_id,
            operation=operation,
            acquired_at=parsed_at,
        )

    async def release(self, lease: QuotaLease, *, consume: bool = True) -> None:
        await self._json_request(
            "POST",
            self.release_path,
            {"lease_id": str(lease.lease_id), "consume": consume},
        )


class HttpKnowledgeSafetyAdapter(_HttpPortBase, KnowledgeSafetyPort):
    """AV/DLP/rights decision adapter; no candidate is released on missing data."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        evaluate_path: str = "/v1/mail-host/knowledge/safety/evaluate",
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="knowledge_safety",
        )
        self.evaluate_path = evaluate_path

    async def evaluate_candidate(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        message: MailMessageProjection,
        candidate: MailActionCandidate,
    ) -> Mapping[str, object]:
        if message.tenant_id != tenant_id or candidate.subject_id != subject_id:
            raise ProviderFailureError("knowledge_scope_mismatch")
        _, payload = await self._json_request(
            "POST",
            self.evaluate_path,
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "message_id": str(message.message_id),
                "content_sha256": message.content_sha256,
                "candidate_id": str(candidate.candidate_id),
                "candidate": dict(candidate.payload),
                "evidence": [dict(item) for item in candidate.evidence],
            },
        )
        result = payload.get("decision", payload.get("data", payload))
        if not isinstance(result, dict):
            raise ProviderFailureError("knowledge_safety_response_invalid")
        return result


class HttpHostIdentityAdapter(_HttpPortBase, HostIdentityPort):
    """Host-owned identity/entitlement check; deny on malformed responses."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        authorize_path: str = "/v1/mail-host/identity/authorize",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="host_identity",
        )
        self.authorize_path = authorize_path

    async def authorize(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        capability: str,
        connection_id: UUID | None = None,
    ) -> bool:
        _, payload = await self._json_request(
            "POST",
            self.authorize_path,
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "capability": capability,
                "connection_id": str(connection_id) if connection_id is not None else None,
            },
        )
        allowed = payload.get("allowed")
        if not isinstance(allowed, bool):
            raise ProviderFailureError("host_identity_response_invalid")
        return allowed


class HttpCredentialBrokerAdapter(
    _HttpPortBase, CredentialBrokerPort, CredentialRefreshPort, CredentialRevocationPort
):
    """Resolve short-lived provider material without persisting the response."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        resolve_path: str = "/v1/mail-host/credentials/resolve",
        refresh_path: str = "/v1/mail-host/credentials/refresh",
        revoke_path: str = "/v1/mail-host/credentials/revoke",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="credential_broker",
        )
        self.resolve_path = _validate_path(resolve_path)
        self.refresh_path = _validate_path(refresh_path)
        self.revoke_path = _validate_path(revoke_path)

    async def resolve(
        self, *, credential_ref: str, tenant_id: str, subject_id: str
    ) -> Mapping[str, str]:
        _, payload = await self._json_request(
            "POST",
            self.resolve_path,
            {
                "credential_ref": credential_ref,
                "tenant_id": tenant_id,
                "subject_id": subject_id,
            },
        )
        raw = payload.get("credentials", payload.get("data"))
        if isinstance(raw, dict) and isinstance(raw.get("credentials"), dict):
            raw = raw["credentials"]
        if not isinstance(raw, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in raw.items()
        ):
            raise AuthorizationError("credential_broker_response_invalid")
        return {str(key): str(value) for key, value in raw.items()}

    async def refresh(
        self,
        *,
        credential_ref: str,
        tenant_id: str,
        subject_id: str,
        reason: str,
    ) -> Mapping[str, str]:
        if not credential_ref.strip():
            raise AuthorizationError("credential_ref_required")
        if (
            not reason.strip()
            or len(reason) > 500
            or any(ord(char) < 32 or ord(char) == 127 for char in reason)
        ):
            raise AuthorizationError("credential_refresh_reason_invalid")
        _, payload = await self._json_request(
            "POST",
            self.refresh_path,
            {
                "credential_ref": credential_ref,
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "reason": reason,
            },
        )
        raw = payload.get("metadata", payload.get("data", payload))
        if not isinstance(raw, dict):
            raise ProviderFailureError("credential_refresh_metadata_invalid")
        if _contains_oauth_secret(raw):
            raise ProviderFailureError("credential_refresh_secret_leak")
        allowed = {
            "credential_ref",
            "email_address",
            "provider_account_id",
            "provider_tenant_id",
            "credential_version",
        }
        if not raw or any(key not in allowed for key in raw):
            raise ProviderFailureError("credential_refresh_metadata_invalid")
        result: dict[str, str] = {}
        for key in allowed:
            value = raw.get(key)
            if value is None:
                continue
            if key == "credential_version":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 1 <= value <= 2_147_483_647
                ):
                    raise ProviderFailureError("credential_refresh_credential_version_invalid")
                result[key] = str(value)
                continue
            if not isinstance(value, str) or not value.strip():
                raise ProviderFailureError("credential_refresh_metadata_invalid")
            maximum = 320 if key == "email_address" else 512
            if len(value) > maximum or any(ord(char) < 33 or ord(char) == 127 for char in value):
                raise ProviderFailureError("credential_refresh_metadata_invalid")
            result[key] = value
        if not result:
            raise ProviderFailureError("credential_refresh_metadata_invalid")
        return result

    async def revoke(
        self, *, credential_ref: str, tenant_id: str, subject_id: str
    ) -> Mapping[str, object]:
        if not credential_ref.strip():
            raise AuthorizationError("credential_ref_required")
        _, payload = await self._json_request(
            "POST",
            self.revoke_path,
            {
                "credential_ref": credential_ref,
                "tenant_id": tenant_id,
                "subject_id": subject_id,
            },
        )
        raw = payload.get("data", payload)
        if not isinstance(raw, dict) or not raw or _contains_oauth_secret(raw):
            raise ProviderFailureError("credential_revoke_response_invalid")
        allowed = {"status", "provider_request_id", "revocation_id", "revoke_ref"}
        if any(not isinstance(key, str) or key not in allowed for key in raw):
            raise ProviderFailureError("credential_revoke_response_invalid")
        return dict(raw)


class HttpApprovalAdapter(_HttpPortBase, ApprovalPort):
    """Four-eyes/approval queue adapter; MailHub never self-approves."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        request_path: str = "/v1/mail-host/approvals/request",
        verify_path: str = "/v1/mail-host/approvals/verify",
        timeout_seconds: float = 15.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="approval",
        )
        self.request_path = request_path
        self.verify_path = verify_path

    async def require_confirmation(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        action: AgentActionRequest,
        decision: PolicyDecision,
    ) -> str:
        _, payload = await self._json_request(
            "POST",
            self.request_path,
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "action": _action_json(action),
                "decision": _decision_json(decision),
            },
        )
        reference = payload.get("confirmation_ref")
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 512:
            raise ProviderFailureError("approval_reference_invalid")
        return reference

    async def verify_confirmation(
        self, *, confirmation_ref: str, action: AgentActionRequest
    ) -> bool:
        _, payload = await self._json_request(
            "POST",
            self.verify_path,
            {"confirmation_ref": confirmation_ref, "action": _action_json(action)},
        )
        verified = payload.get("verified")
        if not isinstance(verified, bool):
            raise ProviderFailureError("approval_response_invalid")
        return verified


class HttpObjectStoreAdapter(_HttpPortBase, ObjectStorePort):
    """Encrypted object-store facade; raw body bytes stay outside MailHub DB."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        put_path: str = "/v1/mail-host/objects",
        get_path: str = "/v1/mail-host/objects/read",
        delete_path: str = "/v1/mail-host/objects/delete",
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="object_store",
        )
        self.put_path = put_path
        self.get_path = get_path
        self.delete_path = delete_path

    async def put_text(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        purpose: str,
        content: str,
        content_sha256: str,
        expires_at: datetime | None = None,
    ) -> str:
        _, payload = await self._json_request(
            "POST",
            self.put_path,
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "purpose": purpose,
                "content": content,
                "content_sha256": content_sha256,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )
        object_ref = payload.get("object_ref")
        if not isinstance(object_ref, str) or not object_ref.strip():
            raise ProviderFailureError("object_store_reference_invalid")
        return object_ref

    async def get_text(self, *, tenant_id: str, subject_id: str, object_ref: str) -> str:
        _, payload = await self._json_request(
            "POST",
            self.get_path,
            {"tenant_id": tenant_id, "subject_id": subject_id, "object_ref": object_ref},
        )
        content = payload.get("content")
        if not isinstance(content, str):
            raise ProviderFailureError("object_store_content_invalid")
        return content

    async def delete(self, *, tenant_id: str, subject_id: str, object_ref: str) -> None:
        await self._json_request(
            "POST",
            self.delete_path,
            {"tenant_id": tenant_id, "subject_id": subject_id, "object_ref": object_ref},
        )


class HttpAiExecutionAdapter(_HttpPortBase, AiExecutionPort):
    """Structured model gateway adapter; model output is still schema-checked by MailHub."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        structure_path: str = "/v1/mail-host/ai/structure",
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="ai_execution",
        )
        self.structure_path = structure_path

    async def structure(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        operation: str,
        source: Mapping[str, object],
        schema: Mapping[str, object],
    ) -> Mapping[str, object]:
        _, payload = await self._json_request(
            "POST",
            self.structure_path,
            {
                "tenant_id": tenant_id,
                "subject_id": subject_id,
                "operation": operation,
                "source": dict(source),
                "schema": dict(schema),
            },
        )
        result = payload.get("result", payload.get("data", payload))
        if not isinstance(result, dict):
            raise ProviderFailureError("ai_execution_result_invalid")
        return result


class HttpAuditAdapter(_HttpPortBase, AuditPort):
    """Metadata/evidence audit sink with a transport-level redaction guard."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        append_path: str = "/v1/mail-host/audit",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="audit",
        )
        self.append_path = append_path

    async def append_audit(self, event: Mapping[str, object]) -> None:
        await self._json_request("POST", self.append_path, {"event": redact_event(event)})


class HttpNotificationAdapter(_HttpPortBase, NotificationPort):
    """Host notification adapter; content is bounded and host-rendered."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        notify_path: str = "/v1/mail-host/notifications",
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            error_prefix="notification",
        )
        self.notify_path = notify_path

    async def notify(self, notification: Mapping[str, object]) -> None:
        await self._json_request("POST", self.notify_path, {"notification": dict(notification)})


def _validate_host_base_url(value: str) -> str:
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
        raise ValueError("host_base_url_must_be_tls")
    return value.rstrip("/")


def _validate_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    result = dict(headers or {})
    for key, value in result.items():
        if (
            not key.strip()
            or len(key) > 200
            or len(value) > 8192
            or any(ord(char) < 33 or ord(char) == 127 for char in key)
            # Header values may contain SP (for example ``Bearer <token>``)
            # but never other controls or DEL.
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("host_headers_invalid")
    return result


def _validate_timeout(value: float) -> float:
    if not 0.1 <= value <= 120:
        raise ValueError("host_timeout_invalid")
    return value


def _validate_subscription_provider(provider: ProviderName) -> None:
    if provider not in {ProviderName.GMAIL, ProviderName.MICROSOFT_GRAPH}:
        raise ProviderFailureError("provider_subscription_provider_invalid")


def _validate_subscription_expiry(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderFailureError("provider_subscription_expiry_invalid")
    normalized = value.astimezone(UTC)
    now = datetime.now(UTC)
    if normalized <= now + timedelta(seconds=30) or normalized > now + timedelta(days=31):
        raise ProviderFailureError("provider_subscription_expiry_out_of_bounds")
    return normalized


def _validate_callback_endpoint(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ProviderFailureError("provider_subscription_callback_invalid")
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
        or not parsed.path
        or any(ord(char) < 33 for char in value)
    ):
        raise ProviderFailureError("provider_subscription_callback_invalid")
    return value


def _validate_oauth_text(value: str, field_name: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ProviderFailureError(f"oauth_{field_name}_invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProviderFailureError(f"oauth_{field_name}_invalid")


def _required_oauth_value(payload: Mapping[str, object], field_name: str, *, maximum: int) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise ProviderFailureError("oauth_state_response_invalid")
    _validate_oauth_text(value, field_name, maximum=maximum)
    return value


def _safe_provider_notification_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Forward only non-secret callback metadata to the host verifier."""

    allowed = {
        "content-type",
        "x-goog-resource-state",
        "x-goog-channel-id",
        "x-goog-message-number",
        "validationtoken",
        "client-request-id",
        "x-ms-signature",
        "x-mailhub-provider-attestation",
        "x-mailhub-request-id",
        "x-mailhub-verification",
    }
    result: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            continue
        name = raw_name.casefold()
        if name not in allowed:
            continue
        if len(raw_value) > 2_000 or any(ord(char) < 32 for char in raw_value):
            raise ProviderFailureError("provider_notification_header_invalid")
        result[name] = raw_value
        if len(result) > 32:
            raise ProviderFailureError("provider_notification_headers_too_many")
    return result


def _provider_notification_delivery(
    value: object, *, expected_provider: ProviderName
) -> ProviderNotificationDelivery:
    if not isinstance(value, dict):
        raise ProviderFailureError("provider_notification_response_item_invalid")
    if value.get("verified", True) is not True:
        raise ProviderFailureError("provider_notification_unverified")
    route = value.get("route", value)
    notification_value = value.get("notification", value)
    if not isinstance(route, dict) or not isinstance(notification_value, dict):
        raise ProviderFailureError("provider_notification_response_item_invalid")
    tenant_id = route.get("tenant_id")
    subject_id = route.get("subject_id")
    connection_value = route.get("connection_id")
    folder_ref = route.get("folder_ref", "INBOX")
    if (
        not isinstance(tenant_id, str)
        or not isinstance(subject_id, str)
        or not isinstance(connection_value, str)
        or not isinstance(folder_ref, str)
    ):
        raise ProviderFailureError("provider_notification_route_invalid")
    try:
        connection_id = UUID(connection_value)
    except ValueError as exc:
        raise ProviderFailureError("provider_notification_connection_invalid") from exc
    provider_value = notification_value.get("provider", expected_provider.value)
    if provider_value != expected_provider.value:
        raise ProviderFailureError("provider_notification_provider_mismatch")
    notification_id = notification_value.get("notification_id")
    subscription_ref = notification_value.get("subscription_ref")
    resource_ref = notification_value.get("resource_ref")
    change_value = notification_value.get("change_kind")
    received_value = notification_value.get("received_at")
    if not isinstance(notification_id, str):
        raise ProviderFailureError("provider_notification_metadata_invalid")
    if not isinstance(subscription_ref, str):
        raise ProviderFailureError("provider_notification_metadata_invalid")
    if not isinstance(resource_ref, str):
        raise ProviderFailureError("provider_notification_metadata_invalid")
    if not isinstance(change_value, str) or not isinstance(received_value, str):
        raise ProviderFailureError("provider_notification_metadata_invalid")
    try:
        change_kind = NotificationChangeKind(change_value)
        received_at = datetime.fromisoformat(received_value)
    except ValueError as exc:
        raise ProviderFailureError("provider_notification_metadata_invalid") from exc
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ProviderFailureError("provider_notification_received_at_invalid")
    cursor_hint = notification_value.get("cursor_hint")
    lifecycle_event = notification_value.get("lifecycle_event")
    if cursor_hint is not None and not isinstance(cursor_hint, str):
        raise ProviderFailureError("provider_notification_cursor_invalid")
    if lifecycle_event is not None and not isinstance(lifecycle_event, str):
        raise ProviderFailureError("provider_notification_lifecycle_invalid")
    try:
        notification = ProviderNotification(
            provider=expected_provider,
            notification_id=notification_id,
            subscription_ref=subscription_ref,
            resource_ref=resource_ref,
            change_kind=change_kind,
            received_at=received_at,
            cursor_hint=cursor_hint,
            lifecycle_event=lifecycle_event,
        )
        return ProviderNotificationDelivery(
            tenant_id=tenant_id,
            subject_id=subject_id,
            connection_id=connection_id,
            folder_ref=folder_ref,
            notification=notification,
        )
    except ValueError as exc:
        raise ProviderFailureError("provider_notification_metadata_invalid") from exc


def _contains_oauth_secret(value: object) -> bool:
    """Reject token-shaped fields anywhere in a host response payload."""

    secret_keys = {
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "id_token",
        "idtoken",
        "client_secret",
        "clientsecret",
        "token",
        "token_type",
        "tokentype",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold().replace("-", "_") in secret_keys:
                return True
            if _contains_oauth_secret(child):
                return True
    elif isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_oauth_secret(child) for child in value)
    return False


def _validate_path(value: str) -> str:
    path_parts = value.split("/")
    if (
        not value.startswith("/")
        or "//" in value
        or "?" in value
        or "#" in value
        or ".." in path_parts
        or any(ord(char) < 33 for char in value)
    ):
        raise ValueError("host_path_invalid")
    return value


def _action_json(action: AgentActionRequest) -> dict[str, object]:
    return {
        "action_id": str(action.action_id),
        "action_type": action.action_type.value,
        "tenant_id": action.context.tenant_id,
        "agent_subject_id": action.context.agent_subject_id,
        "connection_id": str(action.context.connection_id)
        if action.context.connection_id
        else None,
        "thread_id": str(action.context.thread_id) if action.context.thread_id else None,
        "recipient_addresses": list(action.context.recipient_addresses),
        "risk_flags": {
            "has_new_recipient": action.context.has_new_recipient,
            "has_attachment": action.context.has_attachment,
            "has_bcc": action.context.has_bcc,
            "has_group_recipient": action.context.has_group_recipient,
            "has_external_recipient": action.context.has_external_recipient,
            "has_large_recipient_set": action.context.has_large_recipient_set,
        },
        "input_digest": action.input_digest,
        "source_message_ids": [str(item) for item in action.source_message_ids],
        "parameters": dict(action.parameters),
    }


def _decision_json(decision: PolicyDecision) -> dict[str, object]:
    return {
        "allowed": decision.allowed,
        "reason_code": decision.reason_code,
        "policy_id": (str(decision.policy_id) if decision.policy_id is not None else None),
        "grant_id": (str(decision.grant_id) if decision.grant_id is not None else None),
        "required_approval": decision.required_approval,
        "automation_level": decision.automation_level.value,
    }
