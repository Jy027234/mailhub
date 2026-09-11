"""Tests for the SMTP wire-conformance harness.

The live harness needs a local capture server; these tests lock down the parts
that must never regress: the evidence cannot carry secrets, a missing or failed
case cannot be published as a pass, and a check *name* mentioning a credential
is not mistaken for a leaked credential.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "smtp_wire_conformance.py"

REQUIRED_CASES = (
    "happy_path",
    "cc_and_bcc",
    "reply_headers",
    "send_disabled_refused",
    "missing_password_refused",
    "audience_injection_blocked",
    "size_behaviour_measured",
)


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("smtp_wire_conformance", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: dataclasses resolves annotations through
    # sys.modules[cls.__module__], which is None for an unregistered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARNESS = _module()


def _bundle(**overrides: Any) -> dict[str, Any]:
    cases = [
        {
            "name": name,
            "passed": True,
            "checks": {"raised_smtp_password_required": True}
            if name == "missing_password_refused"
            else {"ok": True},
            "detail": "",
        }
        for name in REQUIRED_CASES
    ]
    bundle: dict[str, Any] = {
        "schema": HARNESS.SCHEMA_VERSION,
        "implicit_tls": True,
        "cases": cases,
        "summary": {"total": len(cases), "passed": len(cases)},
    }
    bundle.update(overrides)
    return bundle


def test_harness_module_imports_without_aiosmtpd() -> None:
    """The capture dependency is optional: importing must not require it."""

    source = _SCRIPT.read_text(encoding="utf-8")

    assert "import aiosmtpd" not in source.split("def start_capture", 1)[0]
    assert "from aiosmtpd.controller import Controller" in source


def test_valid_bundle_passes() -> None:
    assert HARNESS.validate_bundle(_bundle()) == []


def test_check_names_mentioning_credentials_are_not_treated_as_secrets() -> None:
    """Regression: a check named raised_smtp_password_required is a code, not a leak."""

    bundle = _bundle()
    assert HARNESS.find_forbidden_keys(bundle) == []
    assert HARNESS.validate_bundle(bundle) == []


@pytest.mark.parametrize(
    "leaky",
    [
        {"password": "x"},
        {"token": "x"},
        {"nested": {"credential_secret": "x"}},
        {"cases": [{"name": "c", "passed": True, "body_text": "x"}]},
    ],
)
def test_secret_keys_are_still_detected(leaky: dict[str, Any]) -> None:
    bundle = _bundle()
    bundle.update(leaky)

    issues = HARNESS.validate_bundle(bundle)

    assert any(issue.startswith("forbidden_keys:") for issue in issues), issues


def test_missing_case_is_detected() -> None:
    bundle = _bundle()
    bundle["cases"] = [case for case in bundle["cases"] if case["name"] != "cc_and_bcc"]
    bundle["summary"] = {"total": len(bundle["cases"]), "passed": len(bundle["cases"])}

    assert "case_missing:cc_and_bcc" in HARNESS.validate_bundle(bundle)


def test_failed_case_is_detected() -> None:
    bundle = _bundle()
    bundle["cases"][0]["passed"] = False

    assert "cases_failed:happy_path" in HARNESS.validate_bundle(bundle)


def test_non_boolean_checks_are_rejected() -> None:
    """A free-form checks map would smuggle payload past the secret scan."""

    bundle = _bundle()
    bundle["cases"][0]["checks"] = {"password": "secret-value"}

    issues = HARNESS.validate_bundle(bundle)

    assert "checks_not_boolean:happy_path" in issues
    assert any(issue.startswith("forbidden_keys:") for issue in issues), issues


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"schema": "other.v1"}, "schema_mismatch"),
        ({"implicit_tls": False}, "implicit_tls_not_observed"),
        ({"cases": []}, "cases_missing"),
        ({"summary": {"total": 7, "passed": 6}}, "summary_inconsistent"),
    ],
)
def test_malformed_bundles_fail_closed(mutation: dict[str, Any], expected: str) -> None:
    bundle = _bundle(**mutation)

    assert expected in HARNESS.validate_bundle(bundle)
