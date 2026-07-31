from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from caplatform_bff.agentctl_service import (
    AgentctlIntegrationError,
    PersonalAgentShareSnapshot,
)
from caplatform_bff.config import Settings
from caplatform_bff.contracts import (
    AgentDraftProjection,
    AgentDraftWriteRequest,
    PlatformPrincipal,
    ProjectProjection,
    ProjectTreeProjection,
)
from caplatform_bff.fixtures import build_fixture_turn
from caplatform_bff.mailhub_adapter import MailHubAdapterError
from caplatform_bff.main import create_app


class FakePlatformCore:
    def __init__(self) -> None:
        self.uploaded_content = b""
        self.upload_body: dict[str, Any] | None = None
        self.auth_call: dict[str, Any] | None = None
        self.account_calls: list[dict[str, Any]] = []
        self.display_name = "测试工程师"
        self.skill_installation_status: str | None = None
        self.skill_installed_version: str | None = None
        self.skill_latest_version = "1.0.0"
        self.skill_permission_refs: list[str] = []
        self.usage_events: list[dict[str, Any]] = []

    async def readiness(self) -> dict[str, Any]:
        return {"status": "ready"}

    async def write_product_usage_event(self, **payload: Any) -> dict[str, Any]:
        self.usage_events.append(payload)
        return {"id": "usage-1", **payload}

    async def get_product_usage_summary(self, **payload: Any) -> list[dict[str, Any]]:
        assert payload["tenant_id"] == "ten-1"
        return [
            {
                "metric_code": "product.conversation_started",
                "unit": "event",
                "total_quantity": 4,
                "event_count": 4,
            },
            {
                "metric_code": "document.parse.pages",
                "unit": "page",
                "total_quantity": 99,
                "event_count": 1,
            },
        ]

    async def create_conversation(self, **payload: Any) -> dict[str, Any]:
        return {"status": "active", **payload}

    async def introspect_user(self, token: str) -> PlatformPrincipal:
        assert token in {
            "platform-user-token",
            "platform-password-token",
            "platform-tenant-2-token",
        }
        return PlatformPrincipal(
            user_id="usr-1",
            tenant_id="ten-2" if token == "platform-tenant-2-token" else "ten-1",
            role="member" if token == "platform-tenant-2-token" else "engineer",
            permissions=["civil_aviation_workbench.use", "project.read"],
            apps=["civil_aviation_workbench"],
        )

    async def get_user_profile(self, token: str) -> dict[str, Any]:
        assert token in {
            "platform-user-token",
            "platform-password-token",
            "platform-tenant-2-token",
        }
        current_tenant = (
            {"id": "ten-2", "slug": "caplatform", "name": "CAPLATFORM", "tenant_type": "team"}
            if token == "platform-tenant-2-token"
            else {
                "id": "ten-1",
                "slug": "personal-usr-1",
                "name": "个人空间",
                "tenant_type": "personal",
            }
        )
        return {
            "user": {
                "id": "usr-1",
                "email": "engineer@example.com",
                "phone": None,
                "display_name": self.display_name,
            },
            "current_tenant": current_tenant,
            "memberships": [
                {
                    "id": "ten-1",
                    "slug": "personal-usr-1",
                    "name": "个人空间",
                    "tenant_type": "personal",
                },
                {"id": "ten-2", "slug": "caplatform", "name": "CAPLATFORM", "tenant_type": "team"},
            ],
        }

    async def login_user(
        self,
        *,
        identity_type: str,
        identifier: str,
        password: str,
        totp_code: str | None,
    ) -> dict[str, Any]:
        self.auth_call = {
            "kind": "login",
            "identity_type": identity_type,
            "identifier": identifier,
            "password": password,
            "totp_code": totp_code,
        }
        return {"access_token": "platform-user-token"}

    async def register_user(
        self,
        *,
        identity_type: str,
        identifier: str,
        password: str,
        display_name: str,
        tenant_name: str,
    ) -> dict[str, Any]:
        self.auth_call = {
            "kind": "register",
            "identity_type": identity_type,
            "identifier": identifier,
            "password": password,
            "display_name": display_name,
            "tenant_name": tenant_name,
        }
        return {"access_token": "platform-user-token"}

    async def update_user_profile(
        self,
        *,
        platform_user_token: str,
        display_name: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        self.display_name = display_name
        self.account_calls.append({"kind": "profile", "display_name": display_name})
        return await self.get_user_profile(platform_user_token)

    async def change_user_password(
        self,
        *,
        platform_user_token: str,
        current_password: str,
        new_password: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        self.account_calls.append(
            {
                "kind": "password",
                "current_password": current_password,
                "new_password": new_password,
            }
        )
        return {"access_token": "platform-password-token"}

    async def deactivate_user_account(
        self,
        *,
        platform_user_token: str,
        current_password: str,
        totp_code: str | None,
    ) -> None:
        assert platform_user_token == "platform-user-token"
        self.account_calls.append(
            {
                "kind": "deactivate",
                "current_password": current_password,
                "totp_code": totp_code,
            }
        )

    async def get_user_totp_status(self, token: str) -> dict[str, Any]:
        assert token == "platform-user-token"
        self.account_calls.append({"kind": "totp_status"})
        return {"enabled": False, "pending_setup": False, "enabled_at": None}

    async def setup_user_totp(self, token: str) -> dict[str, Any]:
        assert token == "platform-user-token"
        self.account_calls.append({"kind": "totp_setup"})
        return {
            "secret": "TESTSECRET",
            "otpauth_uri": "otpauth://totp/CAPlatform:test",
            "issuer": "CAPlatform",
            "account_name": "engineer@example.com",
        }

    async def enable_user_totp(
        self,
        *,
        platform_user_token: str,
        code: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        self.account_calls.append({"kind": "totp_enable", "code": code})
        return {"enabled": True, "pending_setup": False, "enabled_at": "2026-07-26T00:00:00Z"}

    async def disable_user_totp(
        self,
        *,
        platform_user_token: str,
        current_password: str,
        code: str | None,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        self.account_calls.append(
            {"kind": "totp_disable", "current_password": current_password, "code": code}
        )
        return {"enabled": False, "pending_setup": False, "enabled_at": None}

    async def switch_user_tenant(
        self,
        *,
        platform_user_token: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        assert tenant_id == "ten-2"
        self.account_calls.append({"kind": "workspace", "tenant_id": tenant_id})
        return {"access_token": "platform-tenant-2-token"}

    async def list_instruction_skills(self, token: str) -> dict[str, Any]:
        assert token == "platform-user-token"
        installation = (
            {
                "id": "install-1",
                "status": self.skill_installation_status,
                "subject_type": "user",
                "package_version": self.skill_installed_version or "1.0.0",
                "package_digest": self._skill_digest(self.skill_installed_version or "1.0.0"),
            }
            if self.skill_installation_status is not None
            else None
        )
        versions = ["1.0.0"]
        if self.skill_latest_version != "1.0.0":
            versions.append(self.skill_latest_version)
        return {
            "packages": [
                {
                    "manifest": {
                        "id": "aviation-evidence-review",
                        "version": version,
                        "digest": self._skill_digest(version),
                        "title": "民航证据复核",
                        "description": "形成带引用的证据复核草稿。",
                        "owner": "CAPlatform",
                        "publisher": "CAPlatform",
                        "source": "CAPlatform 受治理技能目录",
                        "source_ref": (
                            "ref://github.com/Jy027234/CAPLATFORM/skills/aviation-evidence-review"
                        ),
                        "license": "LicenseRef-Proprietary",
                        "signature_ref": "signature://caplatform/aviation-evidence-review",
                        "when_to_use": ["需要核对证据时"],
                        "when_not_to_use": ["不得代替批准"],
                        "dependencies": {
                            "tool_refs": [],
                            "capability_refs": (
                                self.skill_permission_refs
                                if version == self.skill_latest_version
                                else []
                            ),
                            "model_refs": [],
                            "data_scope_refs": [],
                        },
                        "governance": {"risk_ceiling": "read", "sensitivity": "D1"},
                    },
                    "trust_state": "trusted",
                    "status": "active",
                    "installation": installation,
                }
                for version in versions
            ]
        }

    @staticmethod
    def _skill_digest(version: str) -> str:
        return "sha256:" + ("2" if version == "1.1.0" else "1") * 64

    async def list_tool_packages(self, token: str) -> list[dict[str, Any]]:
        assert token == "platform-user-token"
        return []

    async def list_knowledge_sources(self, token: str) -> dict[str, Any]:
        assert token == "platform-user-token"
        return {
            "sources": [
                {
                    "source_id": "knowledge-1",
                    "title": "个人研究资料",
                    "resource_scope": "personal",
                    "status": "active",
                    "index_status": "ready",
                    "source_app": "platform-core",
                    "reference_count": 2,
                    "updated_at": "2026-07-26T00:00:00Z",
                }
            ]
        }

    async def change_instruction_skill_installation(
        self,
        *,
        platform_user_token: str,
        package_id: str,
        version: str,
        action: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        assert package_id == "aviation-evidence-review"
        transitions = {
            "install": "installed",
            "enable": "enabled",
            "disable": "disabled",
            "update": "installed",
            "uninstall": "uninstalled",
        }
        self.skill_installation_status = transitions[action]
        if action in {"install", "update"}:
            self.skill_installed_version = version
        return {
            "id": "install-1",
            "package_id": package_id,
            "package_version": version,
            "package_digest": self._skill_digest(version),
            "status": self.skill_installation_status,
        }

    async def issue_tool_package_runtime_context(
        self,
        *,
        platform_user_token: str,
        package_id: str,
        product_id: str,
        capability_id: str,
        user_confirmation: bool,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        assert product_id == "civil_aviation_workbench"
        assert user_confirmation is False
        return {
            "tenant_id": "ten-1",
            "caller_user_id": "usr-1",
            "package_id": package_id,
            "capability_id": capability_id,
            "pinned_version": "1.0.0",
            "pinned_digest": "sha256:" + "1" * 64,
            "grant_id": "grant-1",
            "platform_tool_context": {
                "jti": "signed-context-1",
                "platform_context_signature": "not-returned-by-bff",
            },
        }

    async def list_apps(self, token: str) -> list[dict[str, Any]]:
        assert token == "platform-user-token"
        return [
            {
                "app_id": "aiprojectops",
                "name": "AIProjectOPS",
                "description": "项目协同",
                "status": "active",
                "manifest": {
                    "permissions": ["project.read"],
                    "capabilities": ["project.query"],
                },
            },
            {
                "app_id": "admin-only",
                "name": "管理员应用",
                "description": "不可用",
                "status": "active",
                "manifest": {
                    "permissions": ["platform.admin"],
                },
            },
        ]

    async def list_review_inbox(
        self,
        *,
        platform_user_token: str,
        product_id: str,
        after_cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        assert product_id == "civil_aviation_workbench"
        assert limit == 1
        if after_cursor is None:
            return {
                "schema_version": "aios.approval_inbox.v1",
                "items": [
                    {
                        "schema_version": "aios.approval_card.v1",
                        "request_id": "review-1",
                        "product_id": product_id,
                        "capability_id": "ca.project.create_task",
                        "requester_actor_id": "usr-1",
                        "status": "pending",
                        "risk_level": "R3",
                        "review_mode": "four_eyes",
                        "separation_required": True,
                        "request_version": 3,
                        "requirement_brief_version": "brief-v2",
                        "expires_at": "2026-07-25T12:00:00Z",
                        "review_surface_ref": "conversation://conv-1/review-1",
                        "deep_link": "/reviews/review-1",
                        "notification_ref": "review:review-1:3",
                        "status_cursor": "review-1:3:pending",
                        "can_decide": False,
                        "decision_block_reasons": ["requester_cannot_self_approve"],
                        "evidence_refs": ["artifact://brief-1"],
                    }
                ],
                "count": 1,
                "has_more": True,
                "next_cursor": "review-1",
            }
        assert after_cursor == "review-1"
        return {
            "schema_version": "aios.approval_inbox.v1",
            "items": [],
            "count": 0,
            "has_more": False,
            "next_cursor": None,
        }

    async def list_application_work_items(
        self,
        *,
        platform_user_token: str,
        product_id: str,
        after_cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        assert product_id == "civil_aviation_workbench"
        assert limit == 1
        if after_cursor is None:
            return {
                "schema_version": "aios.application_work_item_page.v1",
                "tenant_id": "ten-1",
                "subject_user_id": "usr-1",
                "product_id": product_id,
                "items": [
                    {
                        "schema_version": "aios.application_work_item.v1",
                        "tenant_id": "ten-1",
                        "subject_user_id": "usr-1",
                        "product_id": product_id,
                        "conversation_id": "conv-1",
                        "work_item_id": "work-1",
                        "request_id": "request-1",
                        "trace_id": "trace-1",
                        "status": "outcome_unknown",
                        "terminal_status": "outcome_unknown",
                        "latest_event_type": "execution.terminal",
                        "updated_at": "2026-07-24T12:00:00Z",
                        "source_cursor": "4:event-4",
                        "source_sequence": 4,
                        "producer": "agentctl.frontdesk",
                        "fact_owner": "agentctl",
                        "source_system": "platform-core",
                        "run_ref": {"run_id": "run-1"},
                        "approval_ref": None,
                        "artifact_refs": [],
                        "evidence_refs": [],
                        "error_summary": {"code": "provider_timeout"},
                        "authority_ref": {
                            "schema_version": "aios.application_event_authority_ref.v1",
                            "product_id": product_id,
                            "conversation_id": "conv-1",
                            "cursor": "4:event-4",
                        },
                    }
                ],
                "count": 2,
                "summary": {
                    "total": 2,
                    "active": 1,
                    "attention": 1,
                    "completed": 0,
                },
                "has_more": True,
                "next_cursor": "work-page-1",
                "source_system": "platform-core",
                "fact_owner": "agentctl",
            }
        assert after_cursor == "work-page-1"
        return {
            "schema_version": "aios.application_work_item_page.v1",
            "tenant_id": "ten-1",
            "subject_user_id": "usr-1",
            "product_id": product_id,
            "items": [],
            "count": 2,
            "summary": {
                "total": 2,
                "active": 1,
                "attention": 1,
                "completed": 0,
            },
            "has_more": False,
            "next_cursor": None,
            "source_system": "platform-core",
            "fact_owner": "agentctl",
        }

    async def create_storage_upload_session(
        self,
        *,
        platform_user_token: str,
        body: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        assert idempotency_key
        self.upload_body = body
        return {
            "session_id": "upload-1",
            "status": "pending",
            "upload_method": "POST",
            "expires_at": "2026-07-24T12:00:00Z",
            "trace_id": "upload-1",
        }

    async def upload_storage_content(
        self,
        *,
        platform_user_token: str,
        session_id: str,
        content: Any,
        content_type: str,
        idempotency_key: str,
    ) -> None:
        assert platform_user_token == "platform-user-token"
        assert session_id == "upload-1"
        assert content_type == "text/plain"
        assert idempotency_key
        chunks = [chunk async for chunk in content]
        self.uploaded_content = b"".join(chunks)

    async def commit_storage_upload_session(
        self,
        *,
        platform_user_token: str,
        session_id: str,
        checksum: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        assert session_id == "upload-1"
        assert checksum is None
        assert idempotency_key
        assert self.upload_body is not None
        return {
            "object_id": "object-1",
            "owner_type": "personal_user",
            "name": self.upload_body["name"],
            "content_type": self.upload_body["content_type"],
            "size_bytes": len(self.uploaded_content),
            "status": "active",
            "metadata": self.upload_body["metadata"],
            "updated_at": "2026-07-24T11:00:00Z",
        }

    async def preview_storage_file(
        self,
        *,
        platform_user_token: str,
        object_id: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        assert object_id == "object-1"
        return {
            "name": "说明.txt",
            "content_type": "text/plain",
            "preview_kind": "text",
            "text": self.uploaded_content.decode(),
            "truncated": False,
            "limit": 3200,
            "trace_id": "preview-1",
        }

    async def create_storage_download_url(
        self,
        *,
        platform_user_token: str,
        object_id: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        assert object_id == "object-1"
        return {
            "object_id": object_id,
            "signed_url": "http://127.0.0.1:8010/storage/local/download?token=opaque",
            "method": "GET",
            "expires_at": "2026-07-24T12:00:00Z",
            "trace_id": "download-1",
        }


class FakeRemotePlatformCore(FakePlatformCore):
    def __init__(self) -> None:
        super().__init__()
        self.decision: tuple[str, int, str] | None = None

    async def decide_review(
        self,
        *,
        platform_user_token: str,
        request_id: str,
        expected_request_version: int,
        decision: str,
    ) -> dict[str, Any]:
        assert platform_user_token == "platform-user-token"
        self.decision = (request_id, expected_request_version, decision)
        return {
            "schema_version": "aios.review_decision.v0.1",
            "decision_id": "decision-1",
        }

    async def replay_conversation(
        self,
        *,
        platform_user_token: str,
        product_id: str,
        conversation_id: str,
        after_cursor: str | None,
    ) -> Any:
        assert platform_user_token == "platform-user-token"
        assert after_cursor is None
        page = build_fixture_turn(
            principal=PlatformPrincipal(user_id="usr-1", tenant_id="ten-1"),
            product_id=product_id,
            conversation_id=conversation_id,
            text="审批后恢复",
        )
        return page.model_copy(
            update={
                "stream_scope": "conversation_replay",
                "extensions": {"authoritative": True},
            }
        )


class FakeAIProjectOps:
    async def list_projects(self, platform_user_token: str) -> list[ProjectProjection]:
        assert platform_user_token == "platform-user-token"
        return [
            ProjectProjection(
                id="project-1",
                goal="质量体系年审",
                status="active",
                created_at="2026-07-24T10:00:00Z",
            )
        ]

    async def project_tree(
        self,
        *,
        platform_user_token: str,
        project_id: str,
    ) -> ProjectTreeProjection:
        assert platform_user_token == "platform-user-token"
        return ProjectTreeProjection(
            id=project_id,
            goal="质量体系年审",
            status="active",
            stages=[],
            task_counts={},
        )


def test_readiness_checks_platform_core_and_remote_agentctl(tmp_path: Path) -> None:
    class ReadyAssist:
        async def readiness(self) -> None:
            return None

    app = create_app(
        settings=Settings(
            ai_mode="remote",
            session_cookie_secure=False,
            platform_connection_id="conn-1",
            platform_service_token="service-token",
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
        agentctl_assist=ReadyAssist(),
    )
    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["issues"] == []


def test_pilot_readiness_rejects_placeholder_encryption_secret() -> None:
    settings = Settings(
        environment="pilot",
        session_encryption_secret=(
            "replace-with-random-session-encryption-secret-at-least-32-characters"
        ),
    )

    assert "session_encryption_secret_insecure" in settings.readiness_issues()


def test_data_structuring_invoke_keeps_signed_context_server_side(tmp_path: Path) -> None:
    class InvokeAssist:
        async def invoke_tool_package(
            self,
            *,
            principal: PlatformPrincipal,
            package_id: str,
            capability_id: str,
            input_payload: dict[str, Any],
            platform_tool_context: dict[str, Any],
        ) -> dict[str, Any]:
            assert principal.tenant_id == "ten-1"
            assert input_payload == {
                "domain": "aviation",
                "source_ref": "obj://sample",
            }
            assert platform_tool_context["jti"] == "signed-context-1"
            return {
                "status": "completed",
                "tenant_id": "ten-1",
                "package_id": package_id,
                "capability_id": capability_id,
                "pinned_version": "1.0.0",
                "pinned_digest": "sha256:" + "1" * 64,
                "grant_id": "grant-1",
                "output": {
                    "capability_id": capability_id,
                    "result": {"document_blocks": []},
                },
                "latency_ms": 3,
                "execution_mode": "explicit_toolhost_only",
            }

    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
        agentctl_assist=InvokeAssist(),
    )
    with TestClient(app) as client:
        exchange = client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        assert exchange.status_code == 200
        response = client.post(
            "/api/data-structuring/invoke",
            json={
                "packageId": "data-ingest-parse",
                "capabilityId": "document.parse",
                "input": {
                    "domain": "aviation",
                    "source_ref": "obj://sample",
                },
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["factOwner"] == "agentctl"
    assert payload["grantId"] == "grant-1"
    assert "platform_tool_context" not in response.text
    assert "not-returned-by-bff" not in response.text


def test_login_and_register_create_browser_session(tmp_path: Path) -> None:
    core = FakePlatformCore()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
    )

    with TestClient(app) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "identity_type": "phone",
                "identifier": "13800138000",
                "password": "password-123",
                "display_name": "手机用户",
            },
        )
        assert registered.status_code == 201, registered.text
        assert core.auth_call is not None
        assert core.auth_call["identity_type"] == "phone"
        assert str(core.auth_call["tenant_name"]).startswith("CAPLATFORM 个人空间 ")
        assert client.get("/api/me").status_code == 200

        client.delete("/api/session")
        logged_in = client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        )
        assert logged_in.status_code == 200, logged_in.text
        assert core.auth_call == {
            "kind": "login",
            "identity_type": "email",
            "identifier": "owner@example.com",
            "password": "password-123",
            "totp_code": None,
        }


def test_account_profile_security_and_session_rotation(tmp_path: Path) -> None:
    core = FakePlatformCore()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
    )

    with TestClient(app) as client:
        exchanged = client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        assert exchanged.status_code == 200
        original_cookie = client.cookies.get("caplatform_session")
        assert original_cookie

        profile = client.patch(
            "/api/account/profile",
            json={"display_name": "  新姓名  "},
        )
        assert profile.status_code == 200, profile.text
        assert profile.json()["profile"]["user"]["display_name"] == "新姓名"
        refreshed_cookie = client.cookies.get("caplatform_session")
        assert refreshed_cookie and refreshed_cookie != original_cookie
        assert "platform-user-token" not in profile.text

        client.cookies.set(
            "caplatform_session",
            original_cookie,
            domain="testserver.local",
            path="/",
        )
        assert client.get("/api/me").status_code == 401
        client.cookies.set(
            "caplatform_session",
            refreshed_cookie,
            domain="testserver.local",
            path="/",
        )

        status = client.get("/api/account/totp")
        assert status.status_code == 200
        assert status.headers["cache-control"] == "no-store"
        setup = client.post("/api/account/totp/setup")
        assert setup.status_code == 200
        assert setup.json()["secret"] == "TESTSECRET"
        assert setup.headers["cache-control"] == "no-store"
        enabled = client.post("/api/account/totp/enable", json={"code": "123456"})
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        disabled = client.post(
            "/api/account/totp/disable",
            json={"current_password": "password-123", "code": "123456"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        password = client.post(
            "/api/account/change-password",
            json={
                "current_password": "password-123",
                "new_password": "password-456",
            },
        )
        assert password.status_code == 200, password.text
        password_cookie = client.cookies.get("caplatform_session")
        assert password_cookie and password_cookie != refreshed_cookie
        assert "platform-password-token" not in password.text

        client.cookies.set("caplatform_session", refreshed_cookie)
        assert client.get("/api/me").status_code == 401
        client.cookies.set("caplatform_session", password_cookie)
        assert client.get("/api/me").status_code == 200


def test_account_can_review_and_revoke_other_browser_sessions(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
    )

    with TestClient(app) as client:
        first = client.post(
            "/api/session/exchange",
            headers={
                "Authorization": "Bearer platform-user-token",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Edg/130.0",
            },
        )
        assert first.status_code == 200
        first_cookie = client.cookies.get("caplatform_session")

        second = client.post(
            "/api/session/exchange",
            headers={
                "Authorization": "Bearer platform-user-token",
                "User-Agent": "Mozilla/5.0 (Linux; Android 15) Chrome/130.0",
            },
        )
        assert second.status_code == 200
        second_cookie = client.cookies.get("caplatform_session")
        assert first_cookie != second_cookie

        listed = client.get("/api/account/sessions")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 2
        assert {item["device_label"] for item in items} == {
            "Windows · Edge",
            "Android · Chrome",
        }
        assert sum(bool(item["current"]) for item in items) == 1

        revoked = client.delete("/api/account/sessions/others")
        assert revoked.status_code == 200
        assert revoked.json() == {"revoked": 1}
        remaining = client.get("/api/account/sessions").json()["items"]
        assert [item["session_id"] for item in remaining] == [second_cookie]


def test_account_export_is_scoped_and_never_returns_session_secrets(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
    )

    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        response = client.get("/api/account/export")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "caplatform.account_export.v1"
    assert payload["scope"] == {
        "user_id": "usr-1",
        "tenant_id": "ten-1",
        "product_id": "civil_aviation_workbench",
    }
    assert "platform-user-token" not in response.text
    assert "password" not in response.text.lower()


def test_product_analytics_accepts_only_allowlisted_content_free_events(tmp_path: Path) -> None:
    core = FakePlatformCore()
    app = create_app(
        settings=Settings(
            ai_mode="remote",
            session_cookie_secure=False,
            platform_service_token="service-token",
            platform_connection_id="connection-1",
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
    )

    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        accepted = client.post(
            "/api/analytics/events",
            json={
                "event_name": "answer_completed",
                "event_id": "event-analytics-001",
                "surface": "conversation",
                "outcome": "success",
                "latency_bucket": "2_10s",
                "has_citations": True,
            },
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json() == {
            "accepted": True,
            "event_name": "answer_completed",
        }
        assert core.usage_events == [
            {
                "tenant_id": "ten-1",
                "app_id": "civil_aviation_workbench",
                "metric_code": "product.answer_completed",
                "source_event_id": "analytics:event-analytics-001",
                "metadata": {
                    "surface": "conversation",
                    "outcome": "success",
                    "latency_bucket": "2_10s",
                    "has_citations": True,
                },
            }
        ]

        rejected = client.post(
            "/api/analytics/events",
            json={
                "event_name": "answer_completed",
                "event_id": "event-analytics-002",
                "surface": "conversation",
                "prompt": "sensitive user content",
            },
        )
        assert rejected.status_code == 422

        summary = client.get("/api/analytics/summary?days=30")
        assert summary.status_code == 200, summary.text
        assert summary.json() == {
            "window_days": 30,
            "items": [{"event_name": "conversation_started", "total": 4}],
        }


def test_account_deactivation_cleans_memory_before_revoking_all_sessions(tmp_path: Path) -> None:
    class DeactivationAssist:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, str]] = []

        async def memory_center(
            self,
            *,
            principal: PlatformPrincipal,
            agent_id: str | None,
        ) -> dict[str, Any]:
            assert principal.user_id == "usr-1"
            assert agent_id is None
            return {
                "nodes": [
                    {"id": "mem-1", "namespace": {"agent_id": "frontdesk"}},
                    {"id": "mem-2", "namespace": {"agent_id": "expert-1"}},
                ]
            }

        async def delete_memory(
            self,
            *,
            principal: PlatformPrincipal,
            node_id: str,
            agent_id: str,
            mode: str,
            reason: str,
        ) -> dict[str, Any]:
            assert principal.user_id == "usr-1"
            assert mode == "hard_delete"
            assert reason == "account_deactivation"
            self.deleted.append((node_id, agent_id))
            return {"status": "deleted"}

    core = FakePlatformCore()
    assist = DeactivationAssist()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
        agentctl_assist=assist,
    )

    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        response = client.request(
            "DELETE",
            "/api/account",
            json={
                "current_password": "password-123",
                "totp_code": "123456",
                "confirmation": "DELETE",
            },
        )
        assert response.status_code == 204, response.text
        assert client.get("/api/me").status_code == 401

    assert assist.deleted == [("mem-1", "frontdesk"), ("mem-2", "expert-1")]
    assert core.account_calls[-1] == {
        "kind": "deactivate",
        "current_password": "password-123",
        "totp_code": "123456",
    }


def test_workspace_switch_rotates_session_and_checks_target_tenant(tmp_path: Path) -> None:
    core = FakePlatformCore()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
    )

    with TestClient(app) as client:
        assert (
            client.post(
                "/api/session/exchange",
                headers={"Authorization": "Bearer platform-user-token"},
            ).status_code
            == 200
        )
        original_cookie = client.cookies.get("caplatform_session")
        switched = client.post(
            "/api/account/workspaces/switch",
            json={"tenant_id": "ten-2"},
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["principal"]["user_id"] == "usr-1"
        assert switched.json()["principal"]["tenant_id"] == "ten-2"
        assert switched.json()["profile"]["current_tenant"]["name"] == "CAPLATFORM"
        assert "platform-tenant-2-token" not in switched.text
        switched_cookie = client.cookies.get("caplatform_session")
        assert switched_cookie and switched_cookie != original_cookie

        assert original_cookie is not None
        client.cookies.set("caplatform_session", original_cookie)
        assert client.get("/api/me").status_code == 401


def test_public_skill_catalog_and_personal_installation_are_authoritative(
    tmp_path: Path,
) -> None:
    core = FakePlatformCore()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
    )

    with TestClient(app) as client:
        assert (
            client.post(
                "/api/session/exchange",
                headers={"Authorization": "Bearer platform-user-token"},
            ).status_code
            == 200
        )

        catalog = client.get("/api/skills")
        assert catalog.status_code == 200, catalog.text
        item = catalog.json()[0]
        assert item["id"] == "aviation-evidence-review"
        assert item["installable"] is True
        assert item["installationStatus"] is None
        assert item["whenToUse"] == ["需要核对证据时"]

        missing_key = client.put(
            "/api/skills/aviation-evidence-review/versions/1.0.0/installation",
            json={
                "action": "install",
                "acknowledged_digest": "sha256:" + "1" * 64,
                "confirmed_permission_refs": [],
            },
        )
        assert missing_key.status_code == 400

        installed = client.put(
            "/api/skills/aviation-evidence-review/versions/1.0.0/installation",
            headers={"Idempotency-Key": "skill-install-1"},
            json={
                "action": "install",
                "acknowledged_digest": "sha256:" + "1" * 64,
                "confirmed_permission_refs": [],
            },
        )
        assert installed.status_code == 200, installed.text
        assert installed.json()["status"] == "installed"
        replayed = client.put(
            "/api/skills/aviation-evidence-review/versions/1.0.0/installation",
            headers={"Idempotency-Key": "skill-install-1"},
            json={
                "action": "install",
                "acknowledged_digest": "sha256:" + "1" * 64,
                "confirmed_permission_refs": [],
            },
        )
        assert replayed.json() == installed.json()

        refreshed = client.get("/api/skills")
        assert refreshed.status_code == 200
        assert refreshed.json()[0]["installationStatus"] == "installed"


def test_skill_update_is_versioned_and_requires_manifest_permission_confirmation(
    tmp_path: Path,
) -> None:
    core = FakePlatformCore()
    core.skill_installation_status = "enabled"
    core.skill_installed_version = "1.0.0"
    core.skill_latest_version = "1.1.0"
    core.skill_permission_refs = ["capability://external.read"]
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
    )

    with TestClient(app) as client:
        assert (
            client.post(
                "/api/session/exchange",
                headers={"Authorization": "Bearer platform-user-token"},
            ).status_code
            == 200
        )
        catalog = client.get("/api/skills")
        assert catalog.status_code == 200, catalog.text
        assert len(catalog.json()) == 1
        skill = catalog.json()[0]
        assert skill["version"] == "1.1.0"
        assert skill["installedVersion"] == "1.0.0"
        assert skill["updateAvailable"] is True
        assert skill["permissionRefs"] == ["capability://external.read"]

        changed = client.put(
            "/api/skills/aviation-evidence-review/versions/1.1.0/installation",
            headers={"Idempotency-Key": "skill-update-stale"},
            json={
                "action": "update",
                "acknowledged_digest": "sha256:" + "1" * 64,
                "confirmed_permission_refs": ["capability://external.read"],
            },
        )
        assert changed.status_code == 409
        assert changed.json()["detail"] == "skill_manifest_changed"

        unconfirmed = client.put(
            "/api/skills/aviation-evidence-review/versions/1.1.0/installation",
            headers={"Idempotency-Key": "skill-update-unconfirmed"},
            json={
                "action": "update",
                "acknowledged_digest": "sha256:" + "2" * 64,
                "confirmed_permission_refs": [],
            },
        )
        assert unconfirmed.status_code == 409
        assert unconfirmed.json()["detail"] == "skill_permissions_not_confirmed"

        updated = client.put(
            "/api/skills/aviation-evidence-review/versions/1.1.0/installation",
            headers={"Idempotency-Key": "skill-update-1"},
            json={
                "action": "update",
                "acknowledged_digest": "sha256:" + "2" * 64,
                "confirmed_permission_refs": ["capability://external.read"],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["packageVersion"] == "1.1.0"
        assert updated.json()["status"] == "installed"


def test_personal_agent_draft_accepts_only_enabled_skills_and_visible_knowledge(
    tmp_path: Path,
) -> None:
    class DraftAssist:
        received: AgentDraftWriteRequest | None = None

        async def upsert_personal_agent_draft(
            self,
            *,
            principal: PlatformPrincipal,
            configuration: AgentDraftWriteRequest,
            agent_id: str | None = None,
        ) -> AgentDraftProjection:
            assert principal.user_id == "usr-1"
            assert agent_id is None
            self.received = configuration
            return AgentDraftProjection(
                id="personal-agent-1",
                name=configuration.name,
                version="0.1.0",
                status="draft",
                description=configuration.description,
                system_prompt=configuration.system_prompt,
                scope_of_use=configuration.scope_of_use,
                forbidden=configuration.forbidden,
                skill_ids=configuration.skill_ids,
                knowledge_source_ids=configuration.knowledge_source_ids,
            )

    core = FakePlatformCore()
    core.skill_installation_status = "enabled"
    assist = DraftAssist()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
        agentctl_assist=assist,
    )
    body = {
        "name": "我的研究专家",
        "description": "帮助整理可追溯的研究资料。",
        "system_prompt": "请基于用户提供的事实形成带依据的研究摘要。",
        "scope_of_use": "只读资料研究",
        "forbidden": "不得形成批准结论",
        "skill_ids": ["aviation-evidence-review"],
        "knowledge_source_ids": ["knowledge-1"],
    }
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/session/exchange",
                headers={"Authorization": "Bearer platform-user-token"},
            ).status_code
            == 200
        )
        missing_key = client.post("/api/agents", json=body)
        assert missing_key.status_code == 400

        created = client.post(
            "/api/agents",
            json=body,
            headers={"Idempotency-Key": "agent-draft-create-1"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["skillIds"] == ["aviation-evidence-review"]
        assert created.json()["knowledgeSourceIds"] == ["knowledge-1"]
        assert assist.received is not None

        replayed = client.post(
            "/api/agents",
            json=body,
            headers={"Idempotency-Key": "agent-draft-create-1"},
        )
        assert replayed.status_code == 201
        assert replayed.json() == created.json()

        core.skill_installation_status = "disabled"
        rejected = client.post(
            "/api/agents",
            json=body,
            headers={"Idempotency-Key": "agent-draft-create-2"},
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"] == "agent_skill_not_enabled"


def test_session_and_fixture_conversation_vertical_slice(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
    )

    with TestClient(app) as client:
        exchange = client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        assert exchange.status_code == 200
        assert exchange.json()["principal"]["tenant_id"] == "ten-1"
        assert "platform-user-token" not in exchange.text

        me = client.get("/api/me")
        assert me.status_code == 200

        missing_idempotency = client.post("/api/conversations", json={})
        assert missing_idempotency.status_code == 400
        assert missing_idempotency.json()["detail"] == "idempotency_key_required"

        conversation = client.post(
            "/api/conversations",
            headers={"Idempotency-Key": "conversation-1"},
            json={"title": "法规检索"},
        )
        assert conversation.status_code == 200
        conversation_id = conversation.json()["conversation_id"]
        repeated_conversation = client.post(
            "/api/conversations",
            headers={"Idempotency-Key": "conversation-1"},
            json={"title": "法规检索"},
        )
        assert repeated_conversation.json()["conversation_id"] == conversation_id
        conflicting_conversation = client.post(
            "/api/conversations",
            headers={"Idempotency-Key": "conversation-1"},
            json={"title": "不同标题"},
        )
        assert conflicting_conversation.status_code == 409
        assert conflicting_conversation.json()["detail"] == "idempotency_key_reused"

        message = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "message-1"},
            json={"text": "检索 CCAR-145 要求"},
        )
        assert message.status_code == 200
        payload = message.json()
        assert payload["schema_version"] == "aios.application_interaction_event_page.v1"
        assert payload["events"][-1]["terminal_status"] == "completed"
        repeated_message = client.post(
            f"/api/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "message-1"},
            json={"text": "检索 CCAR-145 要求"},
        )
        assert repeated_message.json() == payload

        replay = client.get(f"/api/conversations/{conversation_id}/events")
        assert replay.status_code == 200
        assert replay.json()["extensions"]["fixture"] is True

        stream = client.get(
            f"/api/conversations/{conversation_id}/events/stream",
        )
        assert stream.status_code == 200
        assert "event: assistant.turn" in stream.text
        assert "event: heartbeat" in stream.text

        cursor = payload["events"][1]["cursor"]
        resumed = client.get(
            f"/api/conversations/{conversation_id}/events/stream",
            headers={"Last-Event-ID": cursor},
        )
        assert resumed.status_code == 200
        assert "event: assistant.turn" not in resumed.text
        assert "event: turn.terminal" in resumed.text

        decision = client.post(
            "/api/approvals/review-fixture/decisions",
            headers={"Idempotency-Key": "decision-fixture-1"},
            json={
                "conversation_id": conversation_id,
                "expected_request_version": 1,
                "decision": "allow",
            },
        )
        assert decision.status_code == 409
        assert decision.json()["detail"] == "fixture_has_no_authoritative_approval"


def test_disabled_ai_fails_closed(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        conversation = client.post(
            "/api/conversations",
            headers={"Idempotency-Key": "conversation-disabled-1"},
            json={},
        ).json()
        response = client.post(
            f"/api/conversations/{conversation['conversation_id']}/messages",
            headers={"Idempotency-Key": "message-disabled-1"},
            json={"text": "执行任务"},
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "ai_integration_disabled"


def test_remote_approval_is_decided_by_core_and_returns_replay(tmp_path: Path) -> None:
    core = FakeRemotePlatformCore()
    app = create_app(
        settings=Settings(
            ai_mode="remote",
            session_cookie_secure=False,
            platform_connection_id="conn-1",
            platform_service_token="service-token",
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
        agentctl_assist=object(),
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        response = client.post(
            "/api/approvals/review-1/decisions",
            headers={"Idempotency-Key": "decision-remote-1"},
            json={
                "conversation_id": "conv-1",
                "expected_request_version": 3,
                "decision": "deny",
            },
        )

    assert response.status_code == 200
    assert response.json()["stream_scope"] == "conversation_replay"
    assert core.decision == ("review-1", 3, "deny")


def test_project_reads_forward_only_the_server_side_user_token(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
        aiprojectops=FakeAIProjectOps(),
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        projects = client.get("/api/projects")
        tree = client.get("/api/projects/project-1/tree")

    assert projects.status_code == 200
    assert projects.json()[0]["goal"] == "质量体系年审"
    assert tree.status_code == 200
    assert "platform-user-token" not in projects.text


def test_review_inbox_is_core_owned_product_scoped_and_paginated(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        first = client.get("/api/reviews?limit=1")
        second = client.get("/api/reviews?limit=1&after_cursor=review-1")
        invalid_limit = client.get("/api/reviews?limit=101")

    assert first.status_code == 200
    assert first.json() == {
        "schema_version": "aios.approval_inbox.v1",
        "items": [
            {
                "schema_version": "aios.approval_card.v1",
                "requestId": "review-1",
                "productId": "civil_aviation_workbench",
                "capabilityId": "ca.project.create_task",
                "requesterActorId": "usr-1",
                "status": "pending",
                "riskLevel": "R3",
                "reviewMode": "four_eyes",
                "separationRequired": True,
                "requestVersion": 3,
                "requirementBriefVersion": "brief-v2",
                "expiresAt": "2026-07-25T12:00:00Z",
                "reviewSurfaceRef": "conversation://conv-1/review-1",
                "deepLink": "/reviews/review-1",
                "notificationRef": "review:review-1:3",
                "statusCursor": "review-1:3:pending",
                "canDecide": False,
                "decisionBlockReasons": ["requester_cannot_self_approve"],
                "evidenceRefs": ["artifact://brief-1"],
            }
        ],
        "count": 1,
        "hasMore": True,
        "nextCursor": "review-1",
        "sourceSystem": "platform-core",
    }
    assert second.status_code == 200
    assert second.json()["items"] == []
    assert second.json()["hasMore"] is False
    assert invalid_limit.status_code == 422
    assert "platform-user-token" not in first.text


def test_review_inbox_fails_closed_on_cross_product_projection(tmp_path: Path) -> None:
    class CrossProductCore(FakePlatformCore):
        async def list_review_inbox(
            self,
            *,
            platform_user_token: str,
            product_id: str,
            after_cursor: str | None,
            limit: int,
        ) -> dict[str, Any]:
            payload = await super().list_review_inbox(
                platform_user_token=platform_user_token,
                product_id=product_id,
                after_cursor=after_cursor,
                limit=limit,
            )
            payload["items"][0]["product_id"] = "another-product"
            return payload

    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=CrossProductCore(),
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        response = client.get("/api/reviews?limit=1")

    assert response.status_code == 502
    assert response.json()["detail"] == "platform_core_review_inbox_invalid"


def test_personal_agent_share_uses_core_review_and_publishes_organization_copy(
    tmp_path: Path,
) -> None:
    class AgentShareCore:
        def __init__(self) -> None:
            self.review_payload: dict[str, Any] | None = None
            self.review_status = "pending"
            self.decision_id: str | None = None

        async def introspect_user(self, token: str) -> PlatformPrincipal:
            assert token in {"owner-token", "reviewer-token"}
            return PlatformPrincipal(
                user_id="owner-user" if token == "owner-token" else "reviewer-user",
                tenant_id="tenant-1",
                role="member" if token == "owner-token" else "admin",
                permissions=(
                    ["review.read", "review.request", "knowledge.read"]
                    if token == "owner-token"
                    else ["review.read", "review.decide", "knowledge.read"]
                ),
                apps=["civil_aviation_workbench"],
            )

        async def get_user_profile(self, token: str) -> dict[str, Any]:
            principal = await self.introspect_user(token)
            return {
                "user": {"id": principal.user_id},
                "current_tenant": {"id": "tenant-1"},
                "memberships": [],
            }

        async def list_knowledge_sources(self, token: str) -> dict[str, Any]:
            assert token == "owner-token"
            return {
                "sources": [
                    {
                        "source_id": "knowledge-org",
                        "title": "组织研究资料",
                        "resource_scope": "organization",
                        "status": "active",
                        "index_status": "ready",
                        "source_app": "platform-core",
                        "reference_count": 3,
                        "updated_at": "2026-07-26T00:00:00Z",
                    }
                ]
            }

        async def create_review_request(
            self,
            *,
            platform_user_token: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            assert platform_user_token == "owner-token"
            self.review_payload = payload
            return payload

        def card(self, token: str) -> dict[str, Any]:
            assert self.review_payload is not None
            return {
                "schema_version": "aios.approval_card.v1",
                "request_id": self.review_payload["request_id"],
                "product_id": self.review_payload["product_id"],
                "capability_id": self.review_payload["capability_id"],
                "requester_actor_id": "owner-user",
                "status": self.review_status,
                "risk_level": "R2",
                "review_mode": "full",
                "separation_required": True,
                "request_version": 1,
                "requirement_brief_version": "0.3.0",
                "expires_at": self.review_payload["expires_at"],
                "review_surface_ref": self.review_payload["review_surface_ref"],
                "deep_link": self.review_payload["review_surface_ref"],
                "notification_ref": "review:agent-share:1",
                "status_cursor": f"1:{self.review_status}:1",
                "can_decide": token == "reviewer-token" and self.review_status == "pending",
                "decision_block_reasons": (
                    [] if token == "reviewer-token" else ["self_approval_forbidden"]
                ),
                "evidence_refs": self.review_payload["evidence_refs"],
            }

        async def get_review_approval_card(
            self,
            *,
            platform_user_token: str,
            request_id: str,
        ) -> dict[str, Any]:
            assert self.review_payload is not None
            assert request_id == self.review_payload["request_id"]
            return self.card(platform_user_token)

        async def list_review_inbox(
            self,
            *,
            platform_user_token: str,
            product_id: str,
            after_cursor: str | None,
            limit: int,
        ) -> dict[str, Any]:
            assert product_id == "civil_aviation_workbench"
            assert after_cursor is None
            assert limit == 20
            items = [self.card(platform_user_token)] if self.review_status == "pending" else []
            return {
                "schema_version": "aios.approval_inbox.v1",
                "items": items,
                "count": len(items),
                "has_more": False,
                "next_cursor": None,
            }

        async def list_review_inbox_by_status(
            self,
            *,
            platform_user_token: str,
            product_id: str,
            review_status: str,
            after_cursor: str | None,
            limit: int,
        ) -> dict[str, Any]:
            assert product_id == "civil_aviation_workbench"
            assert review_status == "allow"
            assert after_cursor is None
            assert limit == 20
            items = [self.card(platform_user_token)] if self.review_status == review_status else []
            return {
                "schema_version": "aios.approval_inbox.v1",
                "items": items,
                "count": len(items),
                "has_more": False,
                "next_cursor": None,
            }

        async def decide_review(
            self,
            *,
            platform_user_token: str,
            request_id: str,
            expected_request_version: int,
            decision: str,
        ) -> dict[str, Any]:
            assert platform_user_token == "reviewer-token"
            assert self.review_payload is not None
            assert request_id == self.review_payload["request_id"]
            assert expected_request_version == 1
            self.review_status = decision
            self.decision_id = "review-decision-agent-share-1"
            return {"decision_id": self.decision_id}

        async def get_review_request(
            self,
            *,
            platform_user_token: str,
            request_id: str,
        ) -> dict[str, Any]:
            assert platform_user_token in {"owner-token", "reviewer-token"}
            assert self.review_payload is not None
            assert request_id == self.review_payload["request_id"]
            return {"decision_id": self.decision_id, "status": self.review_status}

    class AgentShareAssist:
        def __init__(self) -> None:
            snapshot_digest = "sha256:" + "a" * 64
            organization_seed = "\0".join(
                (
                    "tenant-1",
                    "owner-user",
                    "personal-research",
                    "0.3.0",
                    snapshot_digest,
                )
            )
            self.snapshot = PersonalAgentShareSnapshot(
                agent_id="personal-research",
                agent_name="我的研究专家",
                agent_version="0.3.0",
                owner_user_id="owner-user",
                snapshot_digest=snapshot_digest,
                organization_agent_id=(
                    "shared-" + hashlib.sha256(organization_seed.encode("utf-8")).hexdigest()[:32]
                ),
                skill_ids=("aviation-evidence-review",),
                knowledge_source_ids=("knowledge-org",),
                trial_run_id="run-trial-share-1",
                raw={},
            )
            self.published = False
            self.publish_attempts = 0
            self.publish_call: dict[str, Any] | None = None

        async def prepare_personal_agent_share(
            self,
            *,
            principal: PlatformPrincipal,
            agent_id: str,
        ) -> PersonalAgentShareSnapshot:
            assert principal.user_id == "owner-user"
            assert agent_id == self.snapshot.agent_id
            return self.snapshot

        async def get_organization_share_publication(self, **kwargs: Any) -> object | None:
            assert kwargs["organization_agent_id"] == self.snapshot.organization_agent_id
            return object() if self.published else None

        async def publish_personal_agent_to_organization(self, **kwargs: Any) -> object:
            assert kwargs["reviewer"].user_id == "reviewer-user"
            assert kwargs["source_owner_user_id"] == "owner-user"
            assert kwargs["review_decision_id"] == "review-decision-agent-share-1"
            self.publish_attempts += 1
            if self.publish_attempts == 1:
                raise AgentctlIntegrationError("agentctl_share_publish_gate_unavailable")
            self.publish_call = kwargs
            self.published = True
            return object()

    core = AgentShareCore()
    assist = AgentShareAssist()
    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
        agentctl_assist=assist,
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer owner-token"},
        )
        created = client.post(
            "/api/agents/personal-research/share-requests",
            headers={"Idempotency-Key": "agent-share-create-1"},
            json={"reason": "希望团队共同使用这套证据整理方法。"},
        )
        assert created.status_code == 201, created.text
        request_id = created.json()["requestId"]
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer reviewer-token"},
        )
        inbox = client.get("/api/reviews?limit=20")
        first_decision = client.post(
            f"/api/agent-share-requests/{request_id}/decisions",
            headers={"Idempotency-Key": "agent-share-decide-1"},
            json={"expected_request_version": 1, "decision": "allow"},
        )
        followups = client.get("/api/agent-share-publication-followups?limit=20")
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer owner-token"},
        )
        forbidden_retry = client.post(
            f"/api/agent-share-requests/{request_id}/decisions",
            headers={"Idempotency-Key": "agent-share-owner-retry"},
            json={"expected_request_version": 1, "decision": "allow"},
        )
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer reviewer-token"},
        )
        decided = client.post(
            f"/api/agent-share-requests/{request_id}/decisions",
            headers={"Idempotency-Key": "agent-share-decide-1"},
            json={"expected_request_version": 1, "decision": "allow"},
        )
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer owner-token"},
        )
        status = client.get("/api/agents/personal-research/share-request")

    assert created.json()["status"] == "pending"
    assert created.json()["publicationStatus"] == "not_applicable"
    assert inbox.status_code == 200, inbox.text
    assert inbox.json()["items"][0]["agentShare"] == {
        "agentId": "personal-research",
        "agentName": "我的研究专家",
        "agentVersion": "0.3.0",
        "reason": "希望团队共同使用这套证据整理方法。",
        "organizationAgentId": assist.snapshot.organization_agent_id,
        "skillIds": ["aviation-evidence-review"],
        "knowledgeSourceIds": ["knowledge-org"],
        "publicationRetryAllowed": True,
    }
    assert first_decision.status_code == 502, first_decision.text
    assert followups.status_code == 200, followups.text
    assert followups.json()["count"] == 1
    assert followups.json()["items"][0]["status"] == "allow"
    assert followups.json()["items"][0]["agentShare"]["publicationRetryAllowed"] is True
    assert forbidden_retry.status_code == 403, forbidden_retry.text
    assert forbidden_retry.json()["detail"] == "review_decide_permission_required"
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "allow"
    assert decided.json()["publicationStatus"] == "published"
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "allow"
    assert status.json()["publicationStatus"] == "published"
    assert assist.publish_call is not None
    assert assist.publish_attempts == 2


def test_work_item_page_is_core_aggregated_identity_scoped_and_paginated(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        first = client.get("/api/work-items?limit=1")
        second = client.get("/api/work-items?limit=1&after_cursor=work-page-1")
        invalid_limit = client.get("/api/work-items?limit=101")

    assert first.status_code == 200, first.text
    assert first.json()["tenantId"] == "ten-1"
    assert first.json()["subjectUserId"] == "usr-1"
    assert first.json()["productId"] == "civil_aviation_workbench"
    assert first.json()["count"] == 2
    assert first.json()["summary"] == {
        "total": 2,
        "active": 1,
        "attention": 1,
        "completed": 0,
    }
    assert first.json()["items"][0]["workItemId"] == "work-1"
    assert first.json()["items"][0]["status"] == "outcome_unknown"
    assert first.json()["items"][0]["factOwner"] == "agentctl"
    assert first.json()["items"][0]["sourceSystem"] == "platform-core"
    assert first.json()["hasMore"] is True
    assert first.json()["nextCursor"] == "work-page-1"
    assert second.status_code == 200
    assert second.json()["items"] == []
    assert second.json()["hasMore"] is False
    assert invalid_limit.status_code == 422
    assert "platform-user-token" not in first.text


def test_work_item_page_fails_closed_on_cross_user_projection(tmp_path: Path) -> None:
    class CrossUserCore(FakePlatformCore):
        async def list_application_work_items(
            self,
            *,
            platform_user_token: str,
            product_id: str,
            after_cursor: str | None,
            limit: int,
        ) -> dict[str, Any]:
            payload = await super().list_application_work_items(
                platform_user_token=platform_user_token,
                product_id=product_id,
                after_cursor=after_cursor,
                limit=limit,
            )
            payload["subject_user_id"] = "usr-other"
            return payload

    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=CrossUserCore(),
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        response = client.get("/api/work-items?limit=1")

    assert response.status_code == 502
    assert response.json()["detail"] == "platform_core_work_item_page_invalid"


def test_internal_project_service_is_not_user_pinnable_or_listed(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "preferences.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )

        missing_key = client.put("/api/preferences/apps/aiprojectops/pin")
        assert missing_key.status_code == 400
        assert missing_key.json()["detail"] == "idempotency_key_required"

        pin = client.put(
            "/api/preferences/apps/aiprojectops/pin",
            headers={"Idempotency-Key": "pin-1"},
        )
        repeat = client.put(
            "/api/preferences/apps/aiprojectops/pin",
            headers={"Idempotency-Key": "pin-1"},
        )
        assert pin.status_code == 403
        assert repeat.status_code == 403
        assert all(item["id"] != "aiprojectops" for item in client.get("/api/apps").json())
        forbidden = client.put(
            "/api/preferences/apps/admin-only/pin",
            headers={"Idempotency-Key": "pin-2"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"] == "app_not_available"

        unpin = client.delete(
            "/api/preferences/apps/aiprojectops/pin",
            headers={"Idempotency-Key": "unpin-1"},
        )
        assert unpin.status_code == 204
        assert all(item["id"] != "aiprojectops" for item in client.get("/api/apps").json())


def test_runtime_observability_and_version_contract(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "runtime.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
    )
    with TestClient(app) as client:
        health = client.get("/healthz", headers={"X-Trace-Id": "trace-runtime"})
        assert health.status_code == 200
        assert health.json()["contract"] == "caplatform.project-bff.v1"
        assert health.headers["X-Trace-Id"] == "trace-runtime"
        assert health.headers["X-Service-Version"]

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "caplatform_http_requests_total" in metrics.text


def test_drive_upload_stream_commit_preview_and_signed_download(tmp_path: Path) -> None:
    core = FakePlatformCore()
    app = create_app(
        settings=Settings(
            ai_mode="disabled",
            session_cookie_secure=False,
            platform_core_public_base_url="https://core.example",
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
    )
    with TestClient(app) as client:
        client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )

        missing_key = client.post(
            "/api/drive/uploads",
            json={"name": "说明.txt", "content_type": "text/plain"},
        )
        assert missing_key.status_code == 400

        created = client.post(
            "/api/drive/uploads",
            headers={"Idempotency-Key": "create-upload-1"},
            json={
                "name": "说明.txt",
                "content_type": "text/plain",
                "size_bytes": 12,
                "sensitivity": "internal",
            },
        )
        assert created.status_code == 201
        assert created.json()["sessionId"] == "upload-1"

        content = client.put(
            "/api/drive/uploads/upload-1/content",
            headers={
                "Content-Type": "text/plain",
                "Idempotency-Key": "upload-content-1",
            },
            content="可追溯内容".encode(),
        )
        assert content.status_code == 204
        assert core.uploaded_content == "可追溯内容".encode()

        committed = client.post(
            "/api/drive/uploads/upload-1/commit",
            headers={"Idempotency-Key": "commit-upload-1"},
            json={"checksum": None},
        )
        assert committed.status_code == 201
        assert committed.json()["objectRef"] == "obj://core/object-1"
        assert committed.json()["ownerScope"] == "personal"

        preview = client.get("/api/drive/files/object-1/preview")
        assert preview.status_code == 200
        assert preview.json()["text"] == "可追溯内容"

        download = client.post("/api/drive/files/object-1/download-url")
        assert download.status_code == 200
        assert download.json()["signedUrl"].startswith(
            "https://core.example/storage/local/download"
        )


def test_encrypted_web_session_survives_bff_restart_without_plaintext_token(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "host-state.sqlite3"
    old_secret = "test-session-encryption-secret-with-32-chars"
    new_secret = "rotated-session-encryption-secret-with-32-chars"
    settings = Settings(
        ai_mode="disabled",
        session_cookie_secure=False,
        state_database_path=str(database_path),
        session_encryption_secret=old_secret,
    )
    first_app = create_app(settings=settings, platform_core=FakePlatformCore())
    with TestClient(first_app) as first_client:
        exchange = first_client.post(
            "/api/session/exchange",
            headers={"Authorization": "Bearer platform-user-token"},
        )
        session_cookie = exchange.cookies["caplatform_session"]
        conversation = first_client.post(
            "/api/conversations",
            headers={"Idempotency-Key": "rotation-conversation"},
            json={"title": "密钥轮换"},
        )
        conversation_id = conversation.json()["conversation_id"]

    assert b"platform-user-token" not in database_path.read_bytes()

    rotation_settings = Settings(
        ai_mode="disabled",
        session_cookie_secure=False,
        state_database_path=str(database_path),
        session_encryption_secret=new_secret,
        previous_session_encryption_secrets=(old_secret,),
    )
    rotated_app = create_app(
        settings=rotation_settings,
        platform_core=FakePlatformCore(),
    )
    with TestClient(rotated_app) as rotated_client:
        rotated_client.cookies.set("caplatform_session", session_cookie)
        restored = rotated_client.get("/api/me")
        replayed = rotated_client.post(
            "/api/conversations",
            headers={"Idempotency-Key": "rotation-conversation"},
            json={"title": "密钥轮换"},
        )

    assert restored.status_code == 200
    assert restored.json()["principal"]["tenant_id"] == "ten-1"
    assert replayed.json()["conversation_id"] == conversation_id

    final_settings = Settings(
        ai_mode="disabled",
        session_cookie_secure=False,
        state_database_path=str(database_path),
        session_encryption_secret=new_secret,
    )
    restarted_app = create_app(
        settings=final_settings,
        platform_core=FakePlatformCore(),
    )
    with TestClient(restarted_app) as restarted_client:
        restarted_client.cookies.set("caplatform_session", session_cookie)
        final_restored = restarted_client.get("/api/me")
        final_replayed = restarted_client.post(
            "/api/conversations",
            headers={"Idempotency-Key": "rotation-conversation"},
            json={"title": "密钥轮换"},
        )

    assert final_restored.status_code == 200
    assert final_replayed.json()["conversation_id"] == conversation_id


def test_mailhub_candidate_apply_route_issues_host_approval_and_maps_upstream_422(
    tmp_path: Path,
) -> None:
    class FakeMailHub:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.raise_upstream_422 = False

        async def get_candidate(self, **payload: Any) -> dict[str, Any]:
            del payload
            return {
                "candidate_id": str(candidate_id),
                "candidate_type": "task",
                "state": "approved",
                "revision": 2,
            }

        async def apply_candidate(self, **payload: Any) -> dict[str, Any]:
            if self.raise_upstream_422:
                raise MailHubAdapterError("mailhub_http_422")
            self.calls.append(payload)
            return {"data": {"status": "applied", "candidate_id": str(payload["candidate_id"])}}

    core = FakePlatformCore()
    mailhub = FakeMailHub()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=core,
        mailhub=mailhub,
    )
    candidate_id = uuid4()
    with TestClient(app) as client:
        logged_in = client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        )
        assert logged_in.status_code == 200, logged_in.text

        missing_revision = client.post(
            f"/api/mail/candidates/{candidate_id}:apply",
            headers={"Idempotency-Key": "candidate-apply-1"},
            json={},
        )
        assert missing_revision.status_code == 422

        missing_key = client.post(
            f"/api/mail/candidates/{candidate_id}:apply",
            json={"expected_revision": 2},
        )
        assert missing_key.status_code == 400

        applied = client.post(
            f"/api/mail/candidates/{candidate_id}:apply",
            headers={"Idempotency-Key": "candidate-apply-1"},
            json={"expected_revision": 2},
        )
        assert applied.status_code == 200, applied.text
        assert mailhub.calls[0]["approval_ref"].startswith("mailapprove_")
        assert mailhub.calls[0]["idempotency_key"] == "candidate-apply-1"

        mailhub.raise_upstream_422 = True
        mapped = client.post(
            f"/api/mail/candidates/{candidate_id}:apply",
            headers={"Idempotency-Key": "candidate-apply-2"},
            json={"expected_revision": 2},
        )
    assert mapped.status_code == 422


def test_mailhub_oauth_routes_keep_registration_server_side_and_redact_credential_ref(
    tmp_path: Path,
) -> None:
    class FakeMailHub:
        def __init__(self) -> None:
            self.begin_payload: dict[str, Any] | None = None
            self.complete_payload: dict[str, Any] | None = None

        async def list_connections(self, **payload: Any) -> list[dict[str, Any]]:
            del payload
            return [{"connection_id": "connection-1", "credential_ref": "cred://hidden"}]

        async def begin_oauth(self, **payload: Any) -> dict[str, Any]:
            self.begin_payload = payload
            return {
                "data": {
                    "provider": payload["provider"],
                    "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?state=opaque",
                    "state": "s" * 32,
                    "code_challenge": "challenge",
                    "expires_at": "2026-07-29T00:00:00Z",
                }
            }

        async def complete_oauth(self, **payload: Any) -> dict[str, Any]:
            self.complete_payload = payload
            return {
                "data": {
                    "provider": payload["provider"],
                    "connection": {"connection_id": "connection-1", "status": "active"},
                    "credential_ref": "cred://must-not-cross-browser",
                }
            }

    mailhub = FakeMailHub()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
            mailhub_gmail_enabled=True,
            mailhub_gmail_client_id="gmail-client",
            mailhub_gmail_client_secret="g" * 32,
            mailhub_gmail_redirect_uri="http://localhost:5180/mail/oauth/gmail/callback",
            mailhub_gmail_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            mailhub_host_service_token="h" * 32,
            mailhub_credential_encryption_secret="e" * 32,
        ),
        platform_core=FakePlatformCore(),
        mailhub=mailhub,
    )
    with TestClient(app) as client:
        logged_in = client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        )
        assert logged_in.status_code == 200, logged_in.text

        providers = client.get("/api/mail/oauth/providers")
        connections = client.get("/api/mail/connections")
        started = client.post(
            "/api/mail/oauth/gmail:authorize",
            json={"client_id": "attacker-controlled", "scopes": ["mail.send"]},
        )
        completed = client.post(
            "/api/mail/oauth/gmail:callback",
            json={"state": "s" * 32, "code": "code-from-provider"},
        )

    assert providers.status_code == 200
    assert connections.status_code == 200
    assert "credential_ref" not in connections.text
    provider_rows = providers.json()["data"]
    gmail = next(row for row in provider_rows if row["provider"] == "gmail")
    assert gmail["ready"] is True
    assert "client_id" not in gmail
    assert "authorization_endpoint" not in gmail
    assert providers.headers["cache-control"] == "no-store"
    assert started.status_code == 200, started.text
    assert mailhub.begin_payload is not None
    assert mailhub.begin_payload["client_id"] == "gmail-client"
    assert mailhub.begin_payload["scopes"] == (
        "https://www.googleapis.com/auth/gmail.readonly",
    )
    assert mailhub.begin_payload["extra_parameters"] == {
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    assert completed.status_code == 200, completed.text
    assert started.headers["cache-control"] == "no-store"
    assert completed.headers["cache-control"] == "no-store"
    assert "credential_ref" not in completed.text
    assert mailhub.complete_payload is not None
    assert mailhub.complete_payload["redirect_uri"] == (
        "http://localhost:5180/mail/oauth/gmail/callback"
    )


def test_mailhub_oauth_routes_fail_closed_when_registration_is_incomplete(
    tmp_path: Path,
) -> None:
    class FakeMailHub:
        async def begin_oauth(self, **payload: Any) -> dict[str, Any]:
            del payload
            return {"data": {}}

    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
            mailhub_gmail_enabled=True,
        ),
        platform_core=FakePlatformCore(),
        mailhub=FakeMailHub(),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        ).status_code == 200
        response = client.post("/api/mail/oauth/gmail:authorize", json={})

    assert response.status_code == 503
    assert response.json()["detail"] == "mailhub_oauth_not_ready"


def test_mailhub_oauth_routes_fail_closed_without_host_secrets(tmp_path: Path) -> None:
    class FakeMailHub:
        async def begin_oauth(self, **payload: Any) -> dict[str, Any]:
            del payload
            raise AssertionError("incomplete host secret boundary must not be called")

    settings = Settings(
        ai_mode="fixture",
        session_cookie_secure=False,
        state_database_path=str(tmp_path / "state.sqlite3"),
        mailhub_gmail_enabled=True,
        mailhub_gmail_client_id="gmail-client",
        mailhub_gmail_client_secret="g" * 32,
        mailhub_gmail_redirect_uri="http://localhost:5180/mail/oauth/gmail/callback",
        mailhub_gmail_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    app = create_app(
        settings=settings,
        platform_core=FakePlatformCore(),
        mailhub=FakeMailHub(),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        ).status_code == 200
        providers = client.get("/api/mail/oauth/providers")
        response = client.post("/api/mail/oauth/gmail:authorize", json={})

    gmail = next(
        row for row in providers.json()["data"] if row["provider"] == "gmail"
    )
    assert gmail["enabled"] is True
    assert gmail["ready"] is False
    assert response.status_code == 503
    assert response.json()["detail"] == "mailhub_oauth_not_ready"


def test_mailhub_oauth_readiness_rejects_write_scope_with_read_only_registration(
    tmp_path: Path,
) -> None:
    class FakeMailHub:
        async def begin_oauth(self, **payload: Any) -> dict[str, Any]:
            del payload
            return {"data": {}}

    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
            mailhub_gmail_enabled=True,
            mailhub_gmail_client_id="gmail-client",
            mailhub_gmail_redirect_uri="http://localhost:5180/mail/oauth/gmail/callback",
            mailhub_gmail_scopes=(
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            ),
        ),
        platform_core=FakePlatformCore(),
        mailhub=FakeMailHub(),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        ).status_code == 200
        response = client.post("/api/mail/oauth/gmail:authorize", json={})

    assert response.status_code == 503
    assert response.json()["detail"] == "mailhub_oauth_not_ready"


def test_mailhub_data_export_is_user_scoped_and_non_cacheable(tmp_path: Path) -> None:
    class FakeMailHub:
        def __init__(self) -> None:
            self.payload: dict[str, Any] | None = None

        async def export_data(self, **payload: Any) -> dict[str, Any]:
            self.payload = payload
            return {
                "data": {
                    "schema_version": "mailhub.data_export.v1",
                    "messages": [],
                    "credential_ref": None,
                }
            }

    mailhub = FakeMailHub()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
        mailhub=mailhub,
    )
    with TestClient(app) as client:
        logged_in = client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        )
        assert logged_in.status_code == 200, logged_in.text
        response = client.get("/api/mail/data-export?include_content=true&limit=7")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (
        'attachment; filename="mailhub-data-export.json"'
    )
    assert response.json()["data"]["schema_version"] == "mailhub.data_export.v1"
    assert mailhub.payload == {
        "tenant_id": "ten-1",
        "subject_id": "usr-1",
        "include_content": True,
        "limit": 7,
    }


def test_mailhub_revoke_route_is_revision_fenced_and_redacts_credentials(tmp_path: Path) -> None:
    class FakeMailHub:
        def __init__(self) -> None:
            self.payload: dict[str, Any] | None = None

        async def revoke_connection(self, **payload: Any) -> dict[str, Any]:
            self.payload = payload
            return {
                "data": {
                    "connection_id": str(payload["connection_id"]),
                    "status": "revoked",
                    "revision": payload["expected_revision"] + 1,
                    "credential_ref": "cred://must-not-cross-browser",
                }
            }

    mailhub = FakeMailHub()
    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
        mailhub=mailhub,
    )
    connection_id = uuid4()
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        ).status_code == 200
        response = client.post(
            f"/api/mail/connections/{connection_id}:revoke",
            json={"expected_revision": 4},
        )
        invalid = client.post(
            f"/api/mail/connections/{connection_id}:revoke",
            json={"expected_revision": True},
        )

    assert response.status_code == 200, response.text
    assert "credential_ref" not in response.text
    assert response.json()["data"]["status"] == "revoked"
    assert invalid.status_code == 422
    assert mailhub.payload is not None
    assert mailhub.payload["tenant_id"] == "ten-1"
    assert mailhub.payload["subject_id"] == "usr-1"
    assert mailhub.payload["connection_id"] == connection_id
    assert mailhub.payload["expected_revision"] == 4
    assert str(mailhub.payload["trace_id"]).startswith("tr-")


def test_mailhub_provider_health_is_admin_only_and_secret_redacted(tmp_path: Path) -> None:
    class AdminCore(FakePlatformCore):
        async def introspect_user(self, token: str) -> PlatformPrincipal:
            principal = await super().introspect_user(token)
            return principal.model_copy(update={"role": "admin"})

    class FakeMailHub:
        async def provider_health(self, **payload: Any) -> list[dict[str, Any]]:
            assert payload == {"tenant_id": "ten-1", "subject_id": "usr-1"}
            return [
                {
                    "connection_id": "connection-1",
                    "provider": "gmail",
                    "status": "healthy",
                    "report": {"credential_ref": "cred://must-not-cross-browser"},
                }
            ]

    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=AdminCore(),
        mailhub=FakeMailHub(),
    )
    with TestClient(app) as client:
        logged_in = client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        )
        assert logged_in.status_code == 200, logged_in.text
        response = client.get("/api/mail/admin/provider-health")

    assert response.status_code == 200, response.text
    assert "credential_ref" not in response.text
    assert response.json()["data"][0]["status"] == "healthy"


def test_mailhub_provider_health_rejects_non_admin(tmp_path: Path) -> None:
    class FakeMailHub:
        async def provider_health(self, **payload: Any) -> list[dict[str, Any]]:
            del payload
            return []

    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
        mailhub=FakeMailHub(),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        ).status_code == 200
        response = client.get("/api/mail/admin/provider-health")

    assert response.status_code == 403
    assert response.json()["detail"] == "integration_role_required"


def test_mailhub_audit_requires_host_permission_and_redacts_secret_shapes(tmp_path: Path) -> None:
    class AuditCore(FakePlatformCore):
        async def introspect_user(self, token: str) -> PlatformPrincipal:
            principal = await super().introspect_user(token)
            return principal.model_copy(
                update={"permissions": [*principal.permissions, "mail.audit"]}
            )

    class FakeMailHub:
        async def list_audit(self, **payload: Any) -> list[dict[str, Any]]:
            assert payload == {"tenant_id": "ten-1", "subject_id": "usr-1", "limit": 9}
            return [
                {
                    "event_type": "mail.connection.authorized",
                    "metadata": {"refresh_token": "must-not-cross-browser"},
                }
            ]

    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=AuditCore(),
        mailhub=FakeMailHub(),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        ).status_code == 200
        response = client.get("/api/mail/audit?limit=9")

    assert response.status_code == 200, response.text
    assert "refresh_token" not in response.text
    assert response.json()["data"][0]["event_type"] == "mail.connection.authorized"


def test_mailhub_audit_rejects_without_host_permission(tmp_path: Path) -> None:
    class FakeMailHub:
        async def list_audit(self, **payload: Any) -> list[dict[str, Any]]:
            del payload
            return []

    app = create_app(
        settings=Settings(
            ai_mode="fixture",
            session_cookie_secure=False,
            state_database_path=str(tmp_path / "state.sqlite3"),
        ),
        platform_core=FakePlatformCore(),
        mailhub=FakeMailHub(),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={
                "identity_type": "email",
                "identifier": "owner@example.com",
                "password": "password-123",
            },
        ).status_code == 200
        response = client.get("/api/mail/audit")

    assert response.status_code == 403
    assert response.json()["detail"] == "mail_audit_required"


def test_mailhub_graph_authority_tenant_must_match_authority_endpoint() -> None:
    settings = Settings(
        mailhub_graph_enabled=True,
        mailhub_graph_client_id="graph-client",
        mailhub_graph_authorization_endpoint=(
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        ),
        mailhub_graph_authority_tenant="organizations",
        mailhub_graph_redirect_uri=(
            "https://app.example.test/mail/oauth/microsoft_graph/callback"
        ),
        mailhub_graph_scopes=("openid", "offline_access", "Mail.Read"),
    )

    assert settings.mailhub_oauth_registration("microsoft_graph").ready is False
