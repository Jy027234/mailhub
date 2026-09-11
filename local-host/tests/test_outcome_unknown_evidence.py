"""Tests for the OUTCOME_UNKNOWN reconciliation evidence contract.

The run itself needs Docker, a real SMTP capture peer and a real mailbox; what
is pinned here is the part that decides whether a run counts as evidence at all.
A bundle that silently lost the "indeterminate" case, or that accepted a
delivered-case probe answer of "absent", would look like a pass while proving
nothing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).parents[1] / "scripts" / "outcome_unknown_reconciliation.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "outcome_unknown_reconciliation", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EVIDENCE = _module()


def _case(case: str, **overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "case": case,
        "connector_error": "smtp_outcome_unknown",
        "peer_accepted_data": case == "delivered",
        "message_id_present_in_payload": case == "delivered",
        "append_status": "OK" if case == "delivered" else "",
        "probe_observations": [
            {"found": EVIDENCE.EXPECTED[case]["found"], "mailbox": "INBOX"}
        ],
        "final_status": EVIDENCE.EXPECTED[case]["final_status"],
    }
    return {**defaults, **overrides}


def _bundle(**overrides: dict[str, Any]) -> list[dict[str, Any]]:
    cases = {case: _case(case) for case in EVIDENCE.REQUIRED_CASES}
    cases.update(overrides)
    return list(cases.values())


def test_a_complete_run_has_no_issues() -> None:
    assert EVIDENCE.evaluate(_bundle()) == []


def test_a_missing_case_is_reported() -> None:
    issues = EVIDENCE.evaluate(_bundle(indeterminate=_case("indeterminate"))[:-1])
    assert any(item.startswith("case_missing:") for item in issues)


def test_a_connector_that_did_not_report_an_unknown_outcome_is_reported() -> None:
    issues = EVIDENCE.evaluate(
        _bundle(delivered=_case("delivered", connector_error=None))
    )
    assert any("connector_did_not_report" in item for item in issues)


def test_an_unexpected_observation_is_reported() -> None:
    issues = EVIDENCE.evaluate(
        _bundle(delivered=_case("delivered", probe_observations=[{"found": False}]))
    )
    assert any("observation_was" in item for item in issues)


def test_an_unexpected_final_status_is_reported() -> None:
    issues = EVIDENCE.evaluate(
        _bundle(absent=_case("absent", final_status="reconciled_succeeded"))
    )
    assert any("final_status" in item for item in issues)


def test_a_delivered_case_that_never_reached_the_mailbox_is_reported() -> None:
    issues = EVIDENCE.evaluate(
        _bundle(delivered=_case("delivered", append_status="NO"))
    )
    assert any("never_reached_the_mailbox" in item for item in issues)


def test_an_absent_case_whose_peer_accepted_the_message_is_reported() -> None:
    issues = EVIDENCE.evaluate(_bundle(absent=_case("absent", peer_accepted_data=True)))
    assert any("absent:the_peer_accepted" in item for item in issues)


def test_a_probe_consulted_more_than_once_is_reported() -> None:
    issues = EVIDENCE.evaluate(
        _bundle(
            delivered=_case(
                "delivered",
                probe_observations=[{"found": True}, {"found": True}],
            )
        )
    )
    assert any("probe_consulted_2_times" in item for item in issues)
