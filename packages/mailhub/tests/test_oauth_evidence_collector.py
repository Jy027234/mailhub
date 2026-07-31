from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "collect_oauth_evidence.py"
_SPEC = importlib.util.spec_from_file_location("mailhub_collect_oauth_evidence", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
collector_main = cast(Any, _MODULE.main)


class _FakeClient:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def readiness(self) -> dict[str, object]:
        return {"status": "ready"}

    def oauth_observation(self, **kwargs: object) -> dict[str, object]:
        return {
            "schema_version": "mailhub.provider_activation_oauth.v1",
            "network_access": True,
            "status": "succeeded",
            "credentials_observed": False,
            "provider": kwargs["provider"],
            "environment": kwargs["environment"],
            "run_id": kwargs["run_id"],
            "account_identity_sha256": "a" * 64,
            "scope_subset_verified": True,
            "state_replay_rejected": True,
            "redirect_allowlist_verified": True,
            "pkce_verified": True,
            "refresh_verified": True,
            "revoke_verified": True,
            "requested_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        }


def test_collector_writes_audit_derived_oauth_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", "true")
    monkeypatch.setattr(
        _MODULE,
        "_offline_preflight",
        lambda provider: {
            "schema_version": "mailhub.provider_preflight.v1",
            "provider": provider,
            "network_access": False,
            "real_provider_evidence": False,
            "config_ready": True,
        },
    )
    evidence_dir = tmp_path / "gmail" / "test" / "run-1"

    result = collector_main(
        [
            "--provider",
            "gmail",
            "--environment",
            "test",
            "--api-url",
            "https://mailhub.example.test",
            "--tenant-id",
            "tenant-1",
            "--subject-id",
            "subject-1",
            "--connection-id",
            str(uuid4()),
            "--run-id",
            "run-1",
            "--evidence-dir",
            str(evidence_dir),
            "--confirm-real-provider",
        ],
        client_factory=_FakeClient,
    )

    assert result == 0
    payload = json.loads((evidence_dir / "oauth-consent.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "mailhub.provider_activation_oauth.v1"
    assert payload["network_access"] is True
    assert payload["credentials_observed"] is False
    assert json.loads((evidence_dir / "preflight.json").read_text(encoding="utf-8"))["config_ready"]


def test_collector_keeps_network_gate_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", raising=False)
    result = collector_main(
        [
            "--provider",
            "gmail",
            "--api-url",
            "https://mailhub.example.test",
            "--tenant-id",
            "tenant-1",
            "--subject-id",
            "subject-1",
            "--connection-id",
            str(uuid4()),
            "--run-id",
            "run-gate",
            "--evidence-dir",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert not (tmp_path / "oauth-consent.json").exists()
