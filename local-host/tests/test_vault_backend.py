"""Tests for the Vault secret backend and its reference-host wiring.

The live conformance run needs a real Vault; these tests pin the contract with a
mock transport: pointer shape, error redaction, fail-closed resolution and the
rule that revocation removes the material rather than the local pointer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from local_host.stores import LocalStores
from local_host.vault import VaultConfig, VaultError, VaultKvClient, is_vault_pointer

ADDR = "http://127.0.0.1:8200"
TOKEN = "test-root-token"


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, response: httpx.Response) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                response.status_code,
                json=response.json() if response.content else None,
                request=request,
            )

        return httpx.MockTransport(handle)


def _client(handler: httpx.MockTransport) -> VaultKvClient:
    return VaultKvClient(
        VaultConfig(addr=ADDR, token=TOKEN, prefix="mailhub-test"),
        client=httpx.Client(transport=handler, timeout=5.0),
    )


def test_config_requires_addr_and_token() -> None:
    with pytest.raises(VaultError, match="vault_config_incomplete"):
        VaultKvClient(VaultConfig(addr="", token=TOKEN))
    with pytest.raises(VaultError, match="vault_config_incomplete"):
        VaultKvClient(VaultConfig(addr=ADDR, token=""))


def test_pointer_shape() -> None:
    client = _client(
        httpx.MockTransport(lambda request: httpx.Response(204, request=request))
    )

    pointer = client.pointer_for("imapcred_abc")

    assert pointer == "vault:secret/mailhub-test/imapcred_abc"
    assert is_vault_pointer(pointer)
    assert not is_vault_pointer(b"gAAAAA-encrypted-blob")


def test_put_secret_sends_the_token_and_returns_a_pointer() -> None:
    recorder = _Recorder()
    client = _client(recorder.handler(httpx.Response(200, json={"data": {}})))

    pointer = client.put_secret(name="imapcred_abc", value="s3cret")

    assert pointer == "vault:secret/mailhub-test/imapcred_abc"
    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{ADDR}/v1/secret/data/mailhub-test/imapcred_abc"
    assert request.headers["X-Vault-Token"] == TOKEN


def test_get_secret_reads_kv_v2_shape() -> None:
    body = {"data": {"data": {"value": "s3cret"}, "metadata": {"version": 1}}}
    client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=body, request=request)
        )
    )

    assert (
        client.get_secret(pointer="vault:secret/mailhub-test/imapcred_abc") == "s3cret"
    )


def test_get_secret_missing_returns_none() -> None:
    client = _client(
        httpx.MockTransport(lambda request: httpx.Response(404, request=request))
    )

    assert client.get_secret(pointer="vault:secret/mailhub-test/gone") is None


@pytest.mark.parametrize("status", [403, 500, 503])
def test_read_failures_are_bounded_and_redacted(status: int) -> None:
    client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                status, json={"errors": ["tenant-secret-leak"]}, request=request
            )
        )
    )

    with pytest.raises(VaultError) as excinfo:
        client.get_secret(pointer="vault:secret/mailhub-test/imapcred_abc")

    assert str(excinfo.value) == f"vault_read_failed_{status}"
    assert "tenant-secret-leak" not in str(excinfo.value)


def test_delete_secret_status_semantics() -> None:
    ok = _client(
        httpx.MockTransport(lambda request: httpx.Response(204, request=request))
    )
    missing = _client(
        httpx.MockTransport(lambda request: httpx.Response(404, request=request))
    )

    assert ok.delete_secret(pointer="vault:secret/mailhub-test/a") is True
    assert missing.delete_secret(pointer="vault:secret/mailhub-test/a") is False


def test_invalid_pointer_is_rejected() -> None:
    client = _client(
        httpx.MockTransport(lambda request: httpx.Response(204, request=request))
    )

    with pytest.raises(VaultError, match="vault_pointer_invalid"):
        client.get_secret(pointer="not-a-pointer")


def test_stores_keep_only_a_pointer_and_revoke_deletes_it(tmp_path: Path) -> None:
    """The database must never hold the password, and revoke must delete it."""

    state: dict[str, str] = {}
    secret = "app-password-value"

    def handle(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if request.method == "POST":
            # Store the KV value, not the wrapper envelope the client sends.
            state[name] = json.loads(request.content.decode())["data"]["value"]
            return httpx.Response(200, json={"data": {}}, request=request)
        if request.method == "DELETE":
            state.pop(name, None)
            return httpx.Response(204, request=request)
        if name not in state:
            return httpx.Response(404, request=request)
        return httpx.Response(
            200, json={"data": {"data": {"value": state[name]}}}, request=request
        )

    vault = _client(httpx.MockTransport(handle))
    database = tmp_path / "host.db"
    stores = LocalStores(
        database, "vault-wiring-test-encryption-secret-value", vault=vault
    )
    stores.initialize()

    credential_ref = stores.store_imap_credential(
        tenant_id="t1", subject_id="u1", username="user@example.test", password=secret
    )

    # SQLite runs in WAL mode, so the row may live in the -wal companion: the
    # "no secret on disk" claim must scan every file, not just the main one.
    with sqlite3.connect(database) as db:
        stored = bytes(
            db.execute(
                "SELECT encrypted_password FROM host_imap_credentials"
            ).fetchone()[0]
        )
    assert stored.startswith(b"vault:secret/mailhub-test/")
    for suffix in ("", "-wal", "-shm"):
        companion = Path(str(database) + suffix)
        if companion.exists():
            assert secret.encode() not in companion.read_bytes(), suffix
    assert stores.resolve_imap_credential(
        credential_ref=credential_ref, tenant_id="t1", subject_id="u1"
    ) == {"username": "user@example.test", "password": secret}

    assert stores.revoke_imap_credential(
        credential_ref=credential_ref, tenant_id="t1", subject_id="u1"
    )
    assert state == {}
    assert (
        stores.resolve_imap_credential(
            credential_ref=credential_ref, tenant_id="t1", subject_id="u1"
        )
        is None
    )


def test_rotation_replaces_material_and_keeps_the_reference(tmp_path: Path) -> None:
    state: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if request.method == "POST":
            state[name] = json.loads(request.content.decode())["data"]["value"]
            return httpx.Response(200, json={"data": {}}, request=request)
        if name not in state:
            return httpx.Response(404, request=request)
        return httpx.Response(
            200, json={"data": {"data": {"value": state[name]}}}, request=request
        )

    vault = _client(httpx.MockTransport(handle))
    stores = LocalStores(
        tmp_path / "host.db", "rotation-test-encryption-secret-value", vault=vault
    )
    stores.initialize()
    credential_ref = stores.store_imap_credential(
        tenant_id="t1",
        subject_id="u1",
        username="user@example.test",
        password="first-secret",
    )

    assert (
        stores.rotate_imap_credential(
            credential_ref=credential_ref,
            tenant_id="t1",
            subject_id="u1",
            password="second-secret",
        )
        == 2
    )
    assert state == {credential_ref: "second-secret"}
    assert stores.resolve_imap_credential(
        credential_ref=credential_ref, tenant_id="t1", subject_id="u1"
    ) == {"username": "user@example.test", "password": "second-secret"}
    # A rotation attempt from another tenant must not touch the material.
    assert (
        stores.rotate_imap_credential(
            credential_ref=credential_ref,
            tenant_id="other",
            subject_id="u1",
            password="hijack",
        )
        is None
    )
    assert state == {credential_ref: "second-secret"}


def test_local_backend_still_works_without_vault(tmp_path: Path) -> None:
    """Omitting the Vault client must keep the encrypted local fallback intact."""

    database = tmp_path / "host.db"
    stores = LocalStores(database, "local-fallback-encryption-secret-value")
    stores.initialize()

    credential_ref = stores.store_imap_credential(
        tenant_id="t1",
        subject_id="u1",
        username="user@example.test",
        password="local-secret",
    )

    assert b"vault:" not in database.read_bytes()
    assert stores.resolve_imap_credential(
        credential_ref=credential_ref, tenant_id="t1", subject_id="u1"
    ) == {"username": "user@example.test", "password": "local-secret"}
