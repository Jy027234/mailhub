from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from caplatform_bff.config import Settings
from caplatform_bff.contracts import PlatformPrincipal
from caplatform_bff.mailhub_host_actions import EncryptedSQLiteMailHostActionBroker
from caplatform_bff.main import create_app


class _PlatformCore:
    async def login_user(self, **_: object) -> dict[str, object]:
        return {"access_token": "platform-user-token"}

    async def introspect_user(self, token: str) -> PlatformPrincipal:
        assert token == "platform-user-token"
        return PlatformPrincipal(
            user_id="user-1",
            tenant_id="tenant-1",
            role="member",
            permissions=["project.write"],
            apps=["civil_aviation_workbench"],
        )

    async def get_user_profile(self, token: str) -> dict[str, object]:
        assert token == "platform-user-token"
        return {
            "user": {"id": "user-1"},
            "current_tenant": {"id": "tenant-1"},
        }


class _Assist:
    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    async def preflight_capability(self, **_: object) -> dict[str, object]:
        return {
            "allowed": True,
            "required_confirmation": "explicit",
            "side_effect_class": "external",
        }

    async def invoke_capability(self, **payload: Any) -> dict[str, object]:
        invocation = payload["invocation"]
        assert isinstance(invocation, dict)
        self.invocations.append(invocation)
        return {
            "output": {
                "task_ref": {
                    "task_id": "task-mail-1",
                    "project_id": "project-1",
                    "state": "PLANNED",
                    "source_system": "aiprojectops",
                    "creation_ref": "creation-mail-1",
                },
                "evidence": {
                    "kind": "aiprojectops_mcp",
                    "trace_id": "trace-mail-1",
                    "provider_status": "created",
                    "idempotency_replayed": False,
                },
            }
        }


def _action(candidate_id: str) -> dict[str, object]:
    return {
        "action_id": str(uuid4()),
        "action_type": "create_task",
        "tenant_id": "tenant-1",
        "agent_subject_id": "user-1",
        "connection_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "recipient_addresses": [],
        "risk_flags": {},
        "input_digest": "a" * 64,
        "source_message_ids": [str(uuid4())],
        "parameters": {
            "candidate_id": candidate_id,
            "candidate_revision": 2,
            "candidate_type": "task",
            "candidate_payload": {
                "project_refs": ["project-1"],
                "summary": "Review the delivery date",
            },
            "source_message_id": str(uuid4()),
        },
    }


def test_host_approval_and_task_action_are_scope_bound_and_replay_safe(tmp_path: Path) -> None:
    state_path = str(tmp_path / "state.sqlite3")
    service_token = "mailhub-host-service-token-" * 2
    broker = EncryptedSQLiteMailHostActionBroker(
        database_path=state_path,
        encryption_secret="mailhub-host-encryption-secret-" * 2,
    )
    asyncio.run(broker.initialize())
    candidate_id = uuid4()
    approval_ref = asyncio.run(
        broker.issue_candidate_approval(
            tenant_id="tenant-1",
            subject_id="user-1",
            candidate_id=candidate_id,
            candidate_revision=2,
        )
    )
    action = _action(str(candidate_id))
    assist = _Assist()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=state_path,
            mailhub_host_service_token=service_token,
        ),
        platform_core=_PlatformCore(),
        agentctl_assist=assist,
        mailhub_host_action_broker=broker,
    )
    headers = {"Authorization": f"Bearer {service_token}"}

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        )
        assert login.status_code == 200, login.text
        verified = client.post(
            "/v1/mail-host/approvals/verify",
            headers=headers,
            json={"confirmation_ref": approval_ref, "action": action},
        )
        assert verified.status_code == 200, verified.text
        assert verified.json() == {"verified": True}

        executed = client.post(
            "/v1/mail-host/actions/execute",
            headers=headers,
            json={"approval_ref": approval_ref, "action": action},
        )
        replayed = client.post(
            "/v1/mail-host/actions/execute",
            headers=headers,
            json={"approval_ref": approval_ref, "action": action},
        )

    assert executed.status_code == 200, executed.text
    assert replayed.json() == executed.json()
    assert executed.json()["result_ref"] == "task-mail-1"
    assert len(assist.invocations) == 1
    assert assist.invocations[0]["validated_arguments"] == {
        "project_id": "project-1",
        "title": "Review the delivery date",
        "description": "Review the delivery date",
        "stage_id": None,
    }


def test_local_knowledge_candidate_is_safety_gated_and_idempotent(tmp_path: Path) -> None:
    state_path = str(tmp_path / "state.sqlite3")
    service_token = "mailhub-host-service-token-" * 2
    broker = EncryptedSQLiteMailHostActionBroker(
        database_path=state_path,
        encryption_secret="mailhub-host-encryption-secret-" * 2,
    )
    asyncio.run(broker.initialize())
    candidate_id = str(uuid4())
    body: dict[str, object] = {
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
        "message_id": str(uuid4()),
        "candidate_id": candidate_id,
        "content_sha256": "d" * 64,
        "candidate": {
            "title": "Delivery date update",
            "summary": "The delivery date requires review.",
            "sensitivity": "internal",
            "requires_review": True,
            "security_state": "pending_scan",
            "rights_state": "awaiting_review",
            "suggested_scope": "personal",
        },
        "evidence": [{"locator": "body:1", "text_sha256": "e" * 64}],
    }
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=state_path,
            mailhub_host_service_token=service_token,
        ),
        platform_core=_PlatformCore(),
        agentctl_assist=_Assist(),
        mailhub_host_action_broker=broker,
    )
    headers = {"Authorization": f"Bearer {service_token}"}

    with TestClient(app) as client:
        safety = client.post(
            "/v1/mail-host/knowledge/safety/evaluate",
            headers=headers,
            json=body,
        )
        assert safety.status_code == 200, safety.text
        safety_result = safety.json()
        assert len(safety_result["gate_ref"]) <= 512
        candidate = body["candidate"]
        assert isinstance(candidate, dict)
        candidate.update(safety_result)

        tampered = client.post(
            "/v1/mail-host/knowledge/candidates",
            headers=headers,
            json={**body, "content_sha256": "f" * 64},
        )

        created = client.post(
            "/v1/mail-host/knowledge/candidates",
            headers=headers,
            json=body,
        )
        replayed = client.post(
            "/v1/mail-host/knowledge/candidates",
            headers=headers,
            json=body,
        )

    assert tampered.status_code == 403
    assert tampered.json()["detail"] == "mailhub_knowledge_safety_gate_invalid"
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "approved"
    assert created.json()["idempotency_replayed"] is False
    assert replayed.json()["knowledge_candidate_ref"] == created.json()["knowledge_candidate_ref"]
    assert replayed.json()["idempotency_replayed"] is True


def test_local_knowledge_safety_rejects_raw_mail_body(tmp_path: Path) -> None:
    state_path = str(tmp_path / "state.sqlite3")
    service_token = "mailhub-host-service-token-" * 2
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=state_path,
            mailhub_host_service_token=service_token,
        ),
        platform_core=_PlatformCore(),
        agentctl_assist=_Assist(),
    )
    body: dict[str, object] = {
        "tenant_id": "tenant-1",
        "subject_id": "user-1",
        "candidate_id": str(uuid4()),
        "content_sha256": "d" * 64,
        "candidate": {
            "sensitivity": "internal",
            "requires_review": True,
            "security_state": "pending_scan",
            "rights_state": "awaiting_review",
            "body_text": "raw sensitive email",
        },
        "evidence": [],
    }

    with TestClient(app) as client:
        response = client.post(
            "/v1/mail-host/knowledge/safety/evaluate",
            headers={"Authorization": f"Bearer {service_token}"},
            json=body,
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "mailhub_knowledge_safety_input_invalid"
