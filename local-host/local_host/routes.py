"""FastAPI routes implementing the documented ``/v1/mail-host/*`` contract.

The OAuth state/exchange and credential broker routes delegate to the
archive's reference broker.  The remaining ports are honest local
implementations: identity always allows a present scope, approvals are local
confirmations, AI/AV/DLP surfaces fail closed as unconfigured, and every
side-effect ledger is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from local_host import _bff  # noqa: F401  (bootstrap apps/bff/src on sys.path)
from local_host.ai import (
    build_model_prompt,
    call_chat_model,
    parse_model_json,
    serialize_ai_result,
)
from local_host.auth import make_service_auth
from local_host.config import HostSettings
from local_host.dlp import evaluate_text
from local_host.stores import LocalStores, StoreError

logger = logging.getLogger("local-host")

from caplatform_bff.mailhub_credentials import (  # noqa: E402
    CredentialRefresh,
    CredentialResolve,
    MailHubCredentialBrokerError,
    OAuthExchange,
    OAuthStateConsume,
    OAuthStateWrite,
)

_HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>MailHub B4 OAuth</title>
<style>body{{font-family:system-ui;max-width:56rem;margin:2rem auto;padding:0 1rem;}}
pre{{background:#f4f4f4;padding:1rem;overflow:auto;border-radius:6px;}}</style></head>
<body><h1>{title}</h1><p>{message}</p>{detail}</body></html>"""


def _html(title: str, message: str, detail: str = "") -> HTMLResponse:
    return HTMLResponse(_HTML_PAGE.format(title=title, message=message, detail=detail))


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_body(request: Request) -> dict[str, Any]:
    try:
        raw: Any = request.state.body
    except AttributeError:
        raise HTTPException(status_code=400, detail={"code": "body_required"}) from None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail={"code": "body_invalid"})
    return raw


async def _mailhub_post(
    settings: HostSettings,
    path: str,
    body: Mapping[str, object],
    *,
    tenant_id: str,
    subject_id: str,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        return await client.post(
            settings.mailhub_api_url + path,
            headers={
                "Accept": "application/json",
                "X-MailHub-Tenant": tenant_id,
                "X-MailHub-Subject": subject_id,
                **(
                    {"Authorization": f"Bearer {os.environ['MAILHUB_API_AUTH_TOKEN']}"}
                    if os.environ.get("MAILHUB_API_AUTH_TOKEN")
                    else {}
                ),
            },
            json=dict(body),
        )


def build_routes(settings: HostSettings, stores: LocalStores, broker: Any) -> APIRouter:
    router = APIRouter()
    service_auth = make_service_auth(settings.service_token)

    @router.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "gmail_ready": settings.gmail_enabled}

    # ---- identity / approvals ---------------------------------------------

    @router.post("/v1/mail-host/identity/authorize", dependencies=[service_auth])
    async def identity_authorize(request: Request) -> dict[str, object]:
        body = _json_body(request)
        allowed = bool(
            isinstance(body.get("tenant_id"), str)
            and body["tenant_id"].strip()
            and isinstance(body.get("subject_id"), str)
            and body["subject_id"].strip()
            and isinstance(body.get("capability"), str)
            and body["capability"].strip()
        )
        return {"allowed": allowed}

    @router.post("/v1/mail-host/approvals/request", dependencies=[service_auth])
    async def approval_request(request: Request) -> dict[str, object]:
        body = _json_body(request)
        action = body.get("action")
        # Local single-user semantics mirror the reference in-memory port: a
        # confirmation may be created without an action binding (the caller
        # cannot know the service-constructed action id in advance).  When an
        # action IS bound, verification enforces the binding.
        action_id = str(action.get("action_id", "")) if isinstance(action, dict) else ""
        action_digest = (
            _digest_bytes(_canonical_json(action)) if isinstance(action, dict) else ""
        )
        confirmation_ref = stores.create_approval(
            tenant_id=str(body.get("tenant_id", "")),
            subject_id=str(body.get("subject_id", "")),
            action_id=action_id,
            action_digest=action_digest,
        )
        return {"confirmation_ref": confirmation_ref}

    @router.post("/v1/mail-host/approvals/verify", dependencies=[service_auth])
    async def approval_verify(request: Request) -> dict[str, object]:
        body = _json_body(request)
        confirmation_ref = body.get("confirmation_ref")
        action = body.get("action")
        if not isinstance(confirmation_ref, str) or not isinstance(action, dict):
            raise HTTPException(
                status_code=422, detail={"code": "approval_verify_invalid"}
            )
        action_id = str(action.get("action_id", ""))
        action_digest = _digest_bytes(_canonical_json(action))
        verified = stores.verify_approval(
            confirmation_ref=confirmation_ref,
            action_id=action_id,
            action_digest=action_digest,
        )
        return {"verified": verified}

    # ---- OAuth state / exchange / credentials (reference broker) -----------

    @router.post("/v1/mail-host/oauth/state", dependencies=[service_auth])
    async def oauth_state_save(request: Request) -> dict[str, object]:
        body = _json_body(request)
        try:
            await broker.save_state(OAuthStateWrite.model_validate(body))
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise _broker_error(exc) from exc
        return {}

    @router.post("/v1/mail-host/oauth/state/consume", dependencies=[service_auth])
    async def oauth_state_consume(request: Request) -> dict[str, object]:
        body = _json_body(request)
        try:
            consume = OAuthStateConsume.model_validate(body)
            record = await broker.consume_state(consume.state_id)
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise _broker_error(exc) from exc
        return {"record": record} if record is not None else {}

    @router.post("/v1/mail-host/oauth/exchange", dependencies=[service_auth])
    async def oauth_exchange(request: Request) -> dict[str, object]:
        body = _json_body(request)
        try:
            metadata = await broker.exchange(OAuthExchange.model_validate(body))
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise _broker_error(exc) from exc
        return {"metadata": metadata}

    @router.post("/v1/mail-host/credentials/resolve", dependencies=[service_auth])
    async def credential_resolve(request: Request) -> dict[str, object]:
        body = _json_body(request)
        credential_ref = body.get("credential_ref")
        if isinstance(credential_ref, str) and stores.is_imap_credential_ref(
            credential_ref
        ):
            try:
                resolved = stores.resolve_imap_credential(
                    credential_ref=credential_ref,
                    tenant_id=str(body.get("tenant_id", "")),
                    subject_id=str(body.get("subject_id", "")),
                )
            except StoreError as exc:
                raise HTTPException(
                    status_code=exc.status_code, detail={"code": exc.code}
                ) from exc
            if resolved is None:
                raise HTTPException(
                    status_code=404, detail={"code": "credential_not_found"}
                )
            return {"credentials": resolved}
        try:
            credentials = await broker.resolve(CredentialResolve.model_validate(body))
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise _broker_error(exc) from exc
        return {"credentials": credentials}

    @router.post("/v1/mail-host/credentials/refresh", dependencies=[service_auth])
    async def credential_refresh(request: Request) -> dict[str, object]:
        body = _json_body(request)
        credential_ref = body.get("credential_ref")
        if isinstance(credential_ref, str) and stores.is_imap_credential_ref(
            credential_ref
        ):
            # Application passwords do not rotate; the durable answer is
            # "same credential, unchanged version" so MailHub can persist its
            # metadata-only refresh observation.
            resolved = stores.resolve_imap_credential(
                credential_ref=credential_ref,
                tenant_id=str(body.get("tenant_id", "")),
                subject_id=str(body.get("subject_id", "")),
            )
            if resolved is None:
                raise HTTPException(
                    status_code=404, detail={"code": "credential_not_found"}
                )
            return {
                "metadata": {
                    "credential_ref": credential_ref,
                    "email_address": resolved["username"],
                    "provider_account_id": resolved["username"],
                    "credential_version": 1,
                    # An application password is rotated by an operator instead
                    # of expiring per call; the projection says so rather than
                    # leaving the lifetime unstated.
                    "long_lived": True,
                    "rotation": "operator_managed",
                }
            }
        try:
            metadata = await broker.refresh(CredentialRefresh.model_validate(body))
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise _broker_error(exc) from exc
        return {"metadata": metadata}

    @router.post("/v1/mail-host/credentials/revoke", dependencies=[service_auth])
    async def credential_revoke(request: Request) -> dict[str, object]:
        body = _json_body(request)
        credential_ref = body.get("credential_ref")
        if isinstance(credential_ref, str) and stores.is_imap_credential_ref(
            credential_ref
        ):
            revoked = stores.revoke_imap_credential(
                credential_ref=credential_ref,
                tenant_id=str(body.get("tenant_id", "")),
                subject_id=str(body.get("subject_id", "")),
            )
            if not revoked:
                return {"status": "already_revoked"}
            return {
                "status": "revoked",
                "revocation_id": f"imaprevoke_{secrets.token_urlsafe(16)}",
            }
        try:
            result = await broker.revoke(CredentialResolve.model_validate(body))
        except (MailHubCredentialBrokerError, ValueError) as exc:
            raise _broker_error(exc) from exc
        return dict(result)

    @router.post("/v1/mail-host/admin/credentials", dependencies=[service_auth])
    async def admin_store_imap_credential(request: Request) -> dict[str, object]:
        """Service-token-authenticated intake for an IMAP application password.

        Local/Beta only: a production host would keep this behind an operator
        console with four-eyes approval.  The password is Fernet-encrypted at
        rest and returned to callers only as an opaque credential ref.
        """

        body = _json_body(request)
        provider = body.get("provider")
        username = body.get("username")
        password = body.get("password")
        if (
            provider != "imap_smtp"
            or not isinstance(username, str)
            or not isinstance(password, str)
        ):
            raise HTTPException(status_code=422, detail={"code": "imap_intake_invalid"})
        try:
            credential_ref = stores.store_imap_credential(
                tenant_id=str(body.get("tenant_id", "")),
                subject_id=str(body.get("subject_id", "")),
                username=username.strip(),
                password=password,
            )
        except StoreError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code}
            ) from exc
        return {"credential_ref": credential_ref, "provider": "imap_smtp"}

    # ---- host actions / knowledge ------------------------------------------

    @router.post("/v1/mail-host/actions/discover", dependencies=[service_auth])
    async def actions_discover(request: Request) -> dict[str, object]:
        del request
        return {"objects": []}

    @router.post("/v1/mail-host/actions/propose", dependencies=[service_auth])
    async def actions_propose(request: Request) -> dict[str, object]:
        body = _json_body(request)
        action = body.get("action")
        action_id = str(action.get("action_id", "")) if isinstance(action, dict) else ""
        if not action_id:
            raise HTTPException(status_code=422, detail={"code": "host_action_invalid"})
        return {
            "proposal_id": f"prop_{action_id}",
            "status": "proposed",
            "action_id": action_id,
        }

    @router.post("/v1/mail-host/actions/execute", dependencies=[service_auth])
    async def actions_execute(request: Request) -> dict[str, object]:
        body = _json_body(request)
        action = body.get("action")
        if not isinstance(action, dict) or not isinstance(action.get("action_id"), str):
            raise HTTPException(status_code=422, detail={"code": "host_action_invalid"})
        idempotency_key = request.headers.get("Idempotency-Key") or str(
            action["action_id"]
        )
        return stores.record_action(
            idempotency_key=idempotency_key,
            tenant_id=str(body.get("tenant_id", "")),
            subject_id=str(body.get("subject_id", "")),
            action_id=str(action["action_id"]),
        )

    @router.post("/v1/mail-host/knowledge/candidates", dependencies=[service_auth])
    async def knowledge_submit(request: Request) -> dict[str, object]:
        body = _json_body(request)
        candidate_id = str(body.get("candidate_id", ""))
        if not candidate_id:
            raise HTTPException(
                status_code=422, detail={"code": "knowledge_candidate_invalid"}
            )
        idempotency_key = (
            request.headers.get("Idempotency-Key")
            or f"mailhub-knowledge:{candidate_id}"
        )
        return stores.record_knowledge(
            idempotency_key=idempotency_key,
            tenant_id=str(body.get("tenant_id", "")),
            subject_id=str(body.get("subject_id", "")),
            candidate_id=candidate_id,
        )

    @router.post("/v1/mail-host/knowledge/safety/evaluate", dependencies=[service_auth])
    async def knowledge_safety_evaluate(request: Request) -> dict[str, object]:
        body = _json_body(request)
        if not isinstance(body.get("candidate_id"), str):
            raise HTTPException(
                status_code=422, detail={"code": "knowledge_candidate_invalid"}
            )
        candidate = body.get("candidate")
        if not isinstance(candidate, dict):
            raise HTTPException(
                status_code=422, detail={"code": "knowledge_candidate_invalid"}
            )
        # Real local DLP: pattern-detect sensitive data in the candidate
        # payload.  A hit quarantines the candidate (apply fails closed).
        text = json.dumps(candidate, ensure_ascii=False, default=str)
        decision = evaluate_text(
            text, clamav_host=os.environ.get("HOST_CLAMAV_HOST") or None
        )
        decision_payload: dict[str, object] = {
            "security_state": decision.security_state,
            "rights_state": decision.rights_state,
            "gate_ref": f"gate_{_digest_bytes(_canonical_json(body))[:16]}",
        }
        if decision.categories:
            decision_payload["dlp_categories"] = list(decision.categories)
            decision_payload["dlp_counts"] = dict(decision.counts)
        decision_payload["scanner"] = decision.scanner
        return {"decision": decision_payload}

    @router.post(
        "/v1/mail-host/knowledge/lifecycle/revoke", dependencies=[service_auth]
    )
    async def knowledge_lifecycle_revoke(request: Request) -> dict[str, object]:
        body = _json_body(request)
        request_id = str(body.get("request_id", ""))
        if not request_id:
            raise HTTPException(
                status_code=422, detail={"code": "knowledge_revoke_request_invalid"}
            )
        return stores.record_knowledge_revocation(
            request_id=request_id,
            tenant_id=str(body.get("tenant_id", "")),
            subject_id=str(body.get("subject_id", "")),
        )

    # ---- objects / events / telemetry / quota --------------------------------

    @router.post("/v1/mail-host/objects", dependencies=[service_auth])
    async def objects_put(request: Request) -> dict[str, object]:
        body = _json_body(request)
        content = body.get("content")
        content_sha256 = body.get("content_sha256")
        if not isinstance(content, str) or not isinstance(content_sha256, str):
            raise HTTPException(
                status_code=422, detail={"code": "object_store_payload_invalid"}
            )
        raw_expires = body.get("expires_at")
        expires_at: datetime | None = None
        if raw_expires is not None:
            try:
                expires_at = datetime.fromisoformat(str(raw_expires)).astimezone(UTC)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail={"code": "object_store_expiry_invalid"}
                ) from exc
        try:
            object_ref = stores.put_object(
                tenant_id=str(body.get("tenant_id", "")),
                subject_id=str(body.get("subject_id", "")),
                purpose=str(body.get("purpose", "")),
                content=content,
                content_sha256=content_sha256,
                expires_at=expires_at,
            )
        except StoreError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code}
            ) from exc
        return {"object_ref": object_ref}

    @router.post("/v1/mail-host/objects/read", dependencies=[service_auth])
    async def objects_read(request: Request) -> dict[str, object]:
        body = _json_body(request)
        object_ref = body.get("object_ref")
        if not isinstance(object_ref, str):
            raise HTTPException(
                status_code=422, detail={"code": "object_store_reference_invalid"}
            )
        try:
            content = stores.get_object(
                tenant_id=str(body.get("tenant_id", "")),
                subject_id=str(body.get("subject_id", "")),
                object_ref=object_ref,
            )
        except StoreError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code}
            ) from exc
        return {"content": content}

    @router.post("/v1/mail-host/objects/delete", dependencies=[service_auth])
    async def objects_delete(request: Request) -> Response:
        body = _json_body(request)
        object_ref = body.get("object_ref")
        if not isinstance(object_ref, str):
            raise HTTPException(
                status_code=422, detail={"code": "object_store_reference_invalid"}
            )
        stores.delete_object(
            tenant_id=str(body.get("tenant_id", "")),
            subject_id=str(body.get("subject_id", "")),
            object_ref=object_ref,
        )
        return Response(status_code=204)

    @router.post("/v1/mail-host/events", dependencies=[service_auth])
    async def events_publish(request: Request) -> dict[str, object]:
        body = _json_body(request)
        event_id = body.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise HTTPException(status_code=422, detail={"code": "event_id_invalid"})
        stores.record_event(event_id=event_id, envelope=body)
        return {}

    @router.post("/v1/mail-host/telemetry/events", dependencies=[service_auth])
    async def telemetry_record(request: Request) -> dict[str, object]:
        body = _json_body(request)
        name = body.get("name")
        fields = body.get("fields")
        if not isinstance(name, str) or not isinstance(fields, dict):
            raise HTTPException(
                status_code=422, detail={"code": "telemetry_payload_invalid"}
            )
        stores.record_telemetry(name=name, fields=fields)
        return {}

    @router.post("/v1/mail-host/audit", dependencies=[service_auth])
    async def audit_append(request: Request) -> dict[str, object]:
        body = _json_body(request)
        event = body.get("event")
        if not isinstance(event, dict):
            raise HTTPException(status_code=422, detail={"code": "audit_event_invalid"})
        stores.record_audit(event={str(key): value for key, value in event.items()})
        return {}

    @router.post("/v1/mail-host/quota/acquire", dependencies=[service_auth])
    async def quota_acquire(request: Request) -> JSONResponse:
        body = _json_body(request)
        limits = body.get("limits")
        if not isinstance(limits, dict):
            raise HTTPException(
                status_code=422, detail={"code": "quota_limits_invalid"}
            )
        raw_account = body.get("account_id")
        account_id: UUID | None = None
        if raw_account is not None:
            try:
                account_id = UUID(str(raw_account))
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail={"code": "quota_account_invalid"}
                ) from exc
        lease = stores.acquire_quota(
            tenant_id=str(body.get("tenant_id", "")),
            subject_id=str(body.get("subject_id", "")),
            account_id=account_id,
            operation=str(body.get("operation", "")),
            max_concurrent=int(limits.get("max_concurrent", 0) or 0),
            max_per_hour=int(limits.get("max_per_hour", 0) or 0),
            max_per_day=int(limits.get("max_per_day", 0) or 0),
        )
        if lease is None:
            return JSONResponse(
                status_code=429, content={"code": "quota_limit_exceeded"}
            )
        return JSONResponse(
            content={
                "lease": {
                    "lease_id": str(lease.lease_id),
                    "acquired_at": lease.acquired_at.astimezone(UTC).isoformat(),
                    # QuotaLease carries no expiry field; the durable expiry is
                    # the same now + lease_ttl value written to the lease row.
                    "expires_at": (lease.acquired_at + stores.lease_ttl)
                    .astimezone(UTC)
                    .isoformat(),
                    "lease_seconds": int(stores.lease_ttl.total_seconds()),
                }
            }
        )

    @router.post("/v1/mail-host/quota/release", dependencies=[service_auth])
    async def quota_release(request: Request) -> dict[str, object]:
        body = _json_body(request)
        lease_id = body.get("lease_id")
        if not isinstance(lease_id, str):
            raise HTTPException(status_code=422, detail={"code": "quota_lease_invalid"})
        stores.release_quota(lease_id=lease_id, consume=bool(body.get("consume", True)))
        return {}

    # ---- honest unconfigured surfaces ----------------------------------------

    @router.post("/v1/mail-host/kill-switch/check", dependencies=[service_auth])
    async def kill_switch_check(request: Request) -> dict[str, object]:
        """Local kill-switch authority.

        The local host has no four-eyes switch ledger; it always reports
        ``allowed`` so the durable graph can start with outbound enabled.
        A production host must replace this with a real switch authority.
        """

        del request
        # The local host has no four-eyes switch ledger, but it still answers
        # explicitly so a caller can tell "allowed" apart from "nobody replied".
        return {
            "allowed": True,
            "decision": "allow",
            "explicit": True,
            "reason": "local_development_no_switch_ledger",
        }

    @router.post("/v1/mail-host/ai/structure", dependencies=[service_auth])
    async def ai_structure(request: Request) -> JSONResponse:
        """Rules-first model gateway for the local host.

        The deterministic rules engine always runs first: it is the safety
        baseline and the injection gate.  When a model gateway is configured
        and the baseline is clean, the body (strictly as data) is sent to the
        gateway; the reply is validated against MailHub's own AI merge
        contract and any failure falls back to the rules result.  Without a
        gateway the endpoint is the pure rules pass-through.
        """

        body = _json_body(request)
        operation = body.get("operation")
        source = body.get("source")
        if operation != "mail.message.analyze" or not isinstance(source, dict):
            raise HTTPException(
                status_code=422, detail={"code": "ai_execution_input_invalid"}
            )
        message_id = source.get("message_id")
        subject = source.get("subject")
        body_text = source.get("body_text")
        content_sha256 = source.get("content_sha256")
        if not all(
            isinstance(value, str)
            for value in (message_id, subject, body_text, content_sha256)
        ):
            raise HTTPException(
                status_code=422, detail={"code": "ai_execution_source_invalid"}
            )
        try:
            parsed_id = UUID(str(message_id))
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail={"code": "ai_execution_source_invalid"}
            ) from exc

        from mailhub.domain import MailMessageProjection  # noqa: PLC0415 (bounded local import)
        from mailhub.intelligence import (  # noqa: PLC0415
            analyze_message as rules_analyze,
            merge_ai_result,
        )

        projection = MailMessageProjection(
            message_id=parsed_id,
            tenant_id=str(body.get("tenant_id", "b4-tenant")),
            connection_id=uuid5(NAMESPACE_URL, f"mailhub:host-ai:{parsed_id}"),
            thread_id=uuid5(NAMESPACE_URL, f"mailhub:host-ai-thread:{parsed_id}"),
            provider_message_ref=f"host-ai:{parsed_id}",
            internet_message_id=None,
            sender_address="rules@mailhub.invalid",
            recipient_addresses=("rules@mailhub.invalid",),
            subject=str(subject)[:1000],
            received_at=datetime.now(UTC),
            body_text=str(body_text),
            content_sha256=str(content_sha256),
        )
        baseline = rules_analyze(projection)

        if baseline.injection_detected:
            # Never send injection-carrying content to the model; the rules
            # result already abstains.
            return JSONResponse(
                content={
                    "result": serialize_ai_result(
                        baseline, model_ref="mailhub-rules-abstain-v1"
                    )
                }
            )

        if settings.ai_gateway_enabled:
            try:
                system_prompt, user_prompt = build_model_prompt(
                    subject=str(subject), body_text=str(body_text), baseline=baseline
                )
                reply = await call_chat_model(
                    base_url=settings.ai_gateway_url or "",
                    api_key=settings.ai_gateway_api_key,
                    model=settings.ai_gateway_model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
                model_output = parse_model_json(reply)
                if model_output is None:
                    raise RuntimeError("model_gateway_unparseable")
                merged = merge_ai_result(baseline, model_output)
                return JSONResponse(
                    content={
                        "result": serialize_ai_result(
                            merged, model_ref=settings.ai_gateway_model
                        )
                    }
                )
            except Exception:
                # Gateway problems must never break analysis: fall back to the
                # deterministic result with an explicit ref.
                logger.exception("ai gateway failed; falling back to rules")
                return JSONResponse(
                    content={
                        "result": serialize_ai_result(
                            baseline, model_ref="mailhub-rules-fallback-v1"
                        )
                    }
                )

        return JSONResponse(
            content={
                "result": serialize_ai_result(
                    baseline, model_ref="mailhub-rules-pass-through-v1"
                )
            }
        )

    @router.post("/v1/mail-host/security/av-scan", dependencies=[service_auth])
    async def av_scan(request: Request) -> JSONResponse:
        body = _json_body(request)
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(
                status_code=422, detail={"code": "av_scan_input_invalid"}
            )
        decision = evaluate_text(
            content, clamav_host=os.environ.get("HOST_CLAMAV_HOST") or None
        )
        return JSONResponse(
            content={
                "status": "clean"
                if decision.security_state == "cleared"
                else "quarantined",
                "scanner": decision.scanner,
                "categories": list(decision.categories),
            }
        )

    @router.post("/v1/mail-host/security/dlp-check", dependencies=[service_auth])
    async def dlp_check(request: Request) -> JSONResponse:
        body = _json_body(request)
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=422, detail={"code": "dlp_input_invalid"})
        decision = evaluate_text(
            content, clamav_host=os.environ.get("HOST_CLAMAV_HOST") or None
        )
        return JSONResponse(
            content={
                "security_state": decision.security_state,
                "categories": list(decision.categories),
                "counts": dict(decision.counts),
                "scanner": decision.scanner,
            }
        )

    # ---- browser-facing OAuth walk ------------------------------------------

    @router.get("/oauth/gmail/start")
    async def gmail_start(
        tenant: str = Query(default="b4-tenant", min_length=1, max_length=200),
        subject: str = Query(default="b4-user", min_length=1, max_length=200),
    ) -> Response:
        if not settings.gmail_enabled:
            return _html(
                "Gmail 未启用",
                "宿主未启用 Gmail：请设置 HOST_GMAIL_ENABLED=true 与客户端凭据。",
            )
        response = await _mailhub_post(
            settings,
            "/v1/mail/oauth/gmail:authorize",
            {
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "client_id": settings.gmail_client_id,
                "redirect_uri": settings.gmail_redirect_uri,
                "scopes": list(settings.gmail_scopes),
            },
            tenant_id=tenant,
            subject_id=subject,
        )
        if response.status_code >= 400:
            return _html(
                "授权启动失败",
                f"MailHub :authorize 返回 {response.status_code}",
                f"<pre>{response.text[:2000]}</pre>",
            )
        payload: Any = response.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        authorization_url = (
            data.get("authorization_url") if isinstance(data, dict) else None
        )
        state_value = data.get("state") if isinstance(data, dict) else None
        if not isinstance(authorization_url, str) or not isinstance(state_value, str):
            return _html("授权启动失败", "MailHub 响应缺少 authorization_url/state")
        stores.record_oauth_flow(
            state=state_value,
            tenant_id=tenant,
            subject_id=subject,
            provider="gmail",
            ttl=timedelta(minutes=10),
        )
        return RedirectResponse(authorization_url, status_code=302)

    @router.get("/oauth/gmail/callback")
    async def gmail_callback(
        code: str = Query(default="", max_length=8192),
        state: str = Query(default="", max_length=4000),
        replay: bool = Query(default=False),
    ) -> Response:
        if not code or not state:
            return _html("回调缺少参数", "Google 回调缺少 code/state。")
        routing = stores.lookup_oauth_flow(state=state, provider="gmail")
        if routing is None:
            return _html("回调路由缺失", "该 state 没有本地路由记录（可能已过期）。")
        tenant_id, subject_id = routing
        response = await _mailhub_post(
            settings,
            "/v1/mail/oauth/gmail:callback",
            {"state": state, "code": code, "redirect_uri": settings.gmail_redirect_uri},
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        if response.status_code >= 400:
            title = "重放已被拒绝（预期证据）" if replay else "回调被拒绝"
            return _html(
                title,
                f"MailHub :callback 返回 {response.status_code}",
                f"<pre>{response.text[:2000]}</pre>",
            )
        payload: Any = response.json()
        body = payload.get("data", payload) if isinstance(payload, dict) else {}
        connection = body.get("connection") if isinstance(body, dict) else None
        rendered = json.dumps(connection or body, indent=2, ensure_ascii=False)
        replay_link = (
            f'<p><a href="/oauth/gmail/replay?code={code}&amp;state={state}">'
            "故意重放同一 state（生成 A1 拒绝证据）</a></p>"
            if not replay
            else ""
        )
        return _html(
            "Gmail 连接已创建",
            "OAuth 交换完成，MailHub 已持久化连接（仅保存 credential_ref）。",
            f"<pre>{rendered}</pre>{replay_link}",
        )

    return router


def _broker_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MailHubCredentialBrokerError):
        return HTTPException(status_code=exc.status_code, detail={"code": exc.code})
    return HTTPException(status_code=422, detail={"code": str(exc)})
