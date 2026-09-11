"""Conformance tests: the reference host must satisfy the MailHub port kit."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from local_host.app import create_host_app
from local_host.broker import build_broker
from local_host.config import HostSettings
from local_host.stores import LocalStores

_TOKEN = "conformance-service-token-0123456789abcdef"
_SECRET = "conformance-encryption-secret-0123456789abcdef"


@pytest.fixture()
def settings(tmp_path: Path) -> HostSettings:
    return HostSettings(
        service_token=_TOKEN,
        encryption_secret=_SECRET,
        database_path=tmp_path / "host.db",
        mailhub_api_url="http://127.0.0.1:8000",
        gmail_enabled=False,
        gmail_client_id="",
        gmail_client_secret="",
        gmail_redirect_uri="http://127.0.0.1:8090/oauth/gmail/callback",
        gmail_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        ai_gateway_url=None,
        ai_gateway_model="",
        ai_gateway_api_key="",
    )


@pytest.fixture()
def stores(settings: HostSettings) -> LocalStores:
    store = LocalStores(settings.database_path, settings.encryption_secret)
    store.initialize()
    return store


@pytest.fixture()
async def client(settings: HostSettings, stores: LocalStores) -> httpx.AsyncClient:
    broker = build_broker(settings, settings.database_path)
    await broker.initialize()
    app = create_host_app(settings, stores, broker)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://host.test"
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _load_emitter() -> Any:
    path = (
        Path(__file__).resolve().parents[1] / "scripts" / "emit_conformance_bundle.py"
    )
    spec = importlib.util.spec_from_file_location("emit_conformance_bundle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_audit_endpoint_requires_the_service_token(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/mail-host/audit", json={"event": {"status": "ok"}}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_audit_endpoint_redacts_content_and_secrets(
    client: httpx.AsyncClient, stores: LocalStores
) -> None:
    response = await client.post(
        "/v1/mail-host/audit",
        headers=_auth(),
        json={
            "event": {
                "event_type": "mail.message.observed",
                "message_id": "message-1",
                "status": "observed",
                "body_text": "must not be stored",
                "raw_headers": "must not be stored",
                "access_token": "must not be stored",
                "nested": {"credential_ref": "must not be stored", "hash": "abc"},
            }
        },
    )
    assert response.status_code == 200
    retained = set(stores.audit_field_names(limit=5))
    assert {"event_type", "message_id", "status"} <= retained
    assert retained.isdisjoint(
        {"body_text", "raw_headers", "access_token", "credential_ref"}
    )
    # Redaction is recursive: an allowed nested key survives, a nested secret
    # does not.
    stored = stores.audit_records(limit=1)[0]
    assert stored["nested"] == {"hash": "abc"}


@pytest.mark.asyncio
async def test_audit_endpoint_rejects_a_non_object_event(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/mail-host/audit", headers=_auth(), json={"event": "nope"}
    )
    assert response.status_code == 422


def test_reference_host_emitter_passes_the_conformance_kit(tmp_path: Path) -> None:
    module = _load_emitter()
    bundle_path = tmp_path / "host-conformance.json"
    assert asyncio.run(module._run(bundle_path)) == 0
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert payload["host"] == "mailhub-reference-host"
    # every port area in the kit must be covered by a real observation
    assert len(payload["observations"]) == 11
    assert all(payload["observations"][area] for area in payload["observations"])


def test_reference_host_bundle_revalidates_offline(tmp_path: Path) -> None:
    from mailhub.hosts.conformance import load_bundle, run_host_conformance

    module = _load_emitter()
    bundle_path = tmp_path / "host-conformance.json"
    assert asyncio.run(module._run(bundle_path)) == 0
    report = run_host_conformance(load_bundle(bundle_path))
    assert report.passed, [f"{item.area}.{item.name}" for item in report.failures]
