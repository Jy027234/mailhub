"""Minimal HashiCorp Vault KV v2 client for the reference host.

The reference host keeps only a **pointer** in its own database; the application
password itself lives in Vault.  A database dump, backup or replica therefore
never carries a usable mailbox credential, which is the production property the
encrypted-SQLite local backend cannot offer.

This is deliberately small: KV v2 read/write/delete plus bounded errors.  It is
a reference adapter, not a Vault SDK.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

POINTER_PREFIX = "vault:"


class VaultError(RuntimeError):
    """Bounded, redacted Vault transport error."""


@dataclass(frozen=True, slots=True)
class VaultConfig:
    addr: str
    token: str
    mount: str = "secret"
    prefix: str = "mailhub"
    timeout_seconds: float = 10.0


def is_vault_pointer(value: str | bytes) -> bool:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    return text.startswith(POINTER_PREFIX)


class VaultKvClient:
    """KV v2 client that returns opaque pointers instead of raw paths."""

    def __init__(
        self, config: VaultConfig, *, client: httpx.Client | None = None
    ) -> None:
        if not config.addr.strip() or not config.token.strip():
            raise VaultError("vault_config_incomplete")
        if not 0.1 <= config.timeout_seconds <= 60:
            raise VaultError("vault_timeout_invalid")
        self.config = config
        self._addr = config.addr.rstrip("/")
        # Injected for tests; production uses a real client with a bound timeout.
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    def _path(self, name: str) -> str:
        return f"{self.config.prefix.strip('/')}/{name.strip('/')}"

    def _url(self, name: str) -> str:
        path = self._path(name)
        return f"{self._addr}/v1/{self.config.mount.strip('/')}/data/{path}"

    def pointer_for(self, name: str) -> str:
        return f"{POINTER_PREFIX}{self.config.mount.strip('/')}/{self._path(name)}"

    def _name_from_pointer(self, pointer: str) -> str:
        text = (
            pointer.decode("utf-8", "replace")
            if isinstance(pointer, bytes)
            else pointer
        )
        if not text.startswith(POINTER_PREFIX):
            raise VaultError("vault_pointer_invalid")
        remainder = text[len(POINTER_PREFIX) :]
        _, _, path = remainder.partition("/")
        return path.rsplit("/", 1)[-1]

    def put_secret(self, *, name: str, value: str) -> str:
        """Store the secret and return the pointer the host may persist."""

        try:
            response = self._client.post(
                self._url(name),
                headers={"X-Vault-Token": self.config.token},
                json={"data": {"value": value}},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise VaultError("vault_unavailable") from exc
        if response.status_code not in {200, 204}:
            # Never reflect a response body: it can echo tenant data.
            raise VaultError(f"vault_write_failed_{response.status_code}")
        return self.pointer_for(name)

    def get_secret(self, *, pointer: str) -> str | None:
        name = self._name_from_pointer(pointer)
        try:
            response = self._client.get(
                self._url(name),
                headers={"X-Vault-Token": self.config.token},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise VaultError("vault_unavailable") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise VaultError(f"vault_read_failed_{response.status_code}")
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise VaultError("vault_response_invalid") from exc
        if not isinstance(payload, dict):
            raise VaultError("vault_response_invalid")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise VaultError("vault_response_invalid")
        inner = data.get("data")
        if not isinstance(inner, dict):
            raise VaultError("vault_response_invalid")
        value = inner.get("value")
        return value if isinstance(value, str) else None

    def delete_secret(self, *, pointer: str) -> bool:
        """Delete the secret version's data (revocation must remove material)."""

        name = self._name_from_pointer(pointer)
        try:
            response = self._client.request(
                "DELETE",
                self._url(name),
                headers={"X-Vault-Token": self.config.token},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise VaultError("vault_unavailable") from exc
        if response.status_code in {200, 204}:
            return True
        if response.status_code == 404:
            return False
        raise VaultError(f"vault_delete_failed_{response.status_code}")


def probe_vault(config: VaultConfig) -> dict[str, object]:
    """Unauthenticated health probe used by readiness checks."""

    try:
        response = httpx.get(
            f"{config.addr.rstrip('/')}/v1/sys/health",
            timeout=config.timeout_seconds,
        )
    except (httpx.TimeoutException, httpx.NetworkError):
        return {"reachable": False}
    detail: dict[str, object] = {"reachable": True, "status": response.status_code}
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return detail
    if isinstance(payload, dict):
        for key in ("initialized", "sealed", "version"):
            if key in payload:
                detail[key] = payload[key]
    return detail
