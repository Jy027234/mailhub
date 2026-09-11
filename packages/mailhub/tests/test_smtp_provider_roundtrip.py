"""Tests for the real-provider SMTP round-trip harness.

The live run needs a real mailbox; these tests lock down the evidence contract:
it cannot carry credentials, the account address is never stored verbatim, and a
failed or missing check cannot be published as a pass.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "smtp_provider_roundtrip.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("smtp_provider_roundtrip", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ROUNDTRIP = _module()


def _bundle(**overrides: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema": ROUNDTRIP.SCHEMA_VERSION,
        "passed": True,
        "checks": {
            "received_message_id_matches_receipt": True,
            "subject_marker_survived": True,
        },
        "received": {"message_id": "<mailhub-x@mailhub.invalid>"},
    }
    bundle.update(overrides)
    return bundle


def test_valid_bundle_passes() -> None:
    assert ROUNDTRIP.validate_bundle(_bundle()) == []


def test_module_imports_without_a_mailbox() -> None:
    """Everything provider-facing is imported lazily inside a function."""

    source = _SCRIPT.read_text(encoding="utf-8")
    head = source.split("def parse_env_file", 1)[0]

    assert "from mailhub" not in head
    assert "import mailhub" not in head


def test_failed_check_is_detected() -> None:
    bundle = _bundle()
    bundle["checks"]["subject_marker_survived"] = False

    assert "checks_failed:subject_marker_survived" in ROUNDTRIP.validate_bundle(bundle)


def test_missing_checks_are_rejected() -> None:
    assert "checks_missing" in ROUNDTRIP.validate_bundle(_bundle(checks={}))


@pytest.mark.parametrize("value", [False, None, "yes"])
def test_passed_flag_must_be_a_true_boolean(value: Any) -> None:
    assert "roundtrip_not_passed" in ROUNDTRIP.validate_bundle(_bundle(passed=value))


def test_received_message_is_required() -> None:
    assert "received_message_missing" in ROUNDTRIP.validate_bundle(_bundle(received={}))


@pytest.mark.parametrize(
    "leaky",
    [{"password": "x"}, {"access_token": "x"}, {"nested": {"credential_ref": "x"}}],
)
def test_secret_keys_are_detected(leaky: dict[str, Any]) -> None:
    bundle = _bundle()
    bundle.update(leaky)

    issues = ROUNDTRIP.validate_bundle(bundle)

    assert any(issue.startswith("forbidden_keys:") for issue in issues), issues


def test_account_facts_never_store_the_full_address() -> None:
    facts = ROUNDTRIP.account_facts("justin@bmshkb.com")

    assert facts["account_domain"] == "bmshkb.com"
    assert facts["account_digest"] == "0105bbfa262d"[:12]
    assert "justin" not in str(facts)


def test_env_file_parser_ignores_comments_and_odd_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# comment",
                "MAILHUB_IMAP_HOST=imap.qiye.163.com",
                'HOST_IMAP_APP_PASSWORD="quoted"',
                "not a key line",
                "1BAD=x",
            ]
        ),
        encoding="utf-8",
    )

    values = ROUNDTRIP.parse_env_file(env)

    assert values["MAILHUB_IMAP_HOST"] == "imap.qiye.163.com"
    assert values["HOST_IMAP_APP_PASSWORD"] == "quoted"
    assert "1BAD" not in values


def test_resolve_settings_reports_every_missing_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "MAILHUB_IMAP_HOST",
        "MAILHUB_SMTP_HOST",
        "HOST_IMAP_USERNAME",
        "HOST_IMAP_APP_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / ".env"
    empty.write_text("", encoding="utf-8")

    with pytest.raises(SystemExit, match="roundtrip_settings_missing"):
        ROUNDTRIP.resolve_settings(empty)
