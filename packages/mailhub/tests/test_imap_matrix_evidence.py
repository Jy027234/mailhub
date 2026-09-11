"""Tests for the read-only IMAP server-matrix evidence probe.

The network probe itself is exercised against a real server by an operator; what
this suite locks down is the part that must never regress: the bundle cannot
carry secrets or content, the read-only guard is enforced, and a malformed or
leaky bundle fails validation.
"""

from __future__ import annotations

import ast
import importlib.util
import ssl
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "imap_server_matrix_probe.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("imap_server_matrix_probe", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = _module()


def test_status_parser_is_name_based_not_positional() -> None:
    """Regression: 163 answers STATUS in a different order than requested.

    A positional parse swapped UIDVALIDITY (1) with UIDNEXT (1730701461) and
    fabricated a uidvalidity change between sessions.
    """

    fields = PROBE.parse_status_fields('"INBOX" (UIDNEXT 1730701461 UIDVALIDITY 1 UNSEEN 82)')

    assert fields == {"UIDNEXT": 1730701461, "UIDVALIDITY": 1, "UNSEEN": 82}
    assert fields["UIDVALIDITY"] == 1
    assert fields["UIDNEXT"] == 1730701461


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ('"INBOX" (UIDVALIDITY 1)', {"UIDVALIDITY": 1}),
        (
            '"INBOX" (UIDVALIDITY 1730701461 UIDNEXT 1730701461)',
            {"UIDVALIDITY": 1730701461, "UIDNEXT": 1730701461},
        ),
        ("", {}),
    ],
)
def test_status_parser_handles_partial_responses(response: str, expected: dict[str, int]) -> None:
    assert PROBE.parse_status_fields(response) == expected


def test_validator_rejects_the_former_uidvalidity_uidnext_swap() -> None:
    swapped = _bundle(
        inbox={
            "uidvalidity": 1730701461,
            "uidnext": 1,
            "messages": 178,
            "uid_search_count": 178,
            "search_delta": 0,
            "max_uid": 1730701460,
        }
    )

    assert "uidnext_not_above_max_uid" in PROBE.validate_bundle(swapped)


def test_validator_rejects_search_exceeding_exists() -> None:
    impossible = _bundle(
        inbox={
            "uidvalidity": 5,
            "messages": 10,
            "uid_search_count": 99,
            "search_delta": -89,
        }
    )

    assert "search_exceeds_exists" in PROBE.validate_bundle(impossible)


def test_validator_rejects_inconsistent_search_delta() -> None:
    inconsistent = _bundle(
        inbox={
            "uidvalidity": 5,
            "messages": 100,
            "uid_search_count": 40,
            "search_delta": 7,
        }
    )

    assert "search_delta_inconsistent" in PROBE.validate_bundle(inconsistent)


def test_validator_rejects_uidvalidity_uidnext_collision() -> None:
    collapsed = _bundle(
        inbox={
            "uidvalidity": 7,
            "uidnext": 7,
            "messages": 1,
            "uid_search_count": 1,
            "search_delta": 0,
            "max_uid": 1,
        }
    )

    assert "uidvalidity_equals_uidnext" in PROBE.validate_bundle(collapsed)


def _bundle(**overrides: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema": PROBE.SCHEMA_VERSION,
        "host": "imap.example.test",
        "port": 993,
        "tls_version": "TLSv1.3",
        "tls_cipher": "TLS_AES_256_GCM_SHA384",
        "capabilities": ["IDLE", "IMAP4REV1", "UIDPLUS"],
        "inbox": {"uidvalidity": 1234, "messages": 57, "uid_search_count": 56},
        "folders": {"folder_count": 3},
        "read_only_guard": {
            "store_issued": False,
            "expunge_issued": False,
            "copy_or_move_issued": False,
            "body_fetched": False,
            "flags_modified": False,
            "selected_readonly": True,
        },
    }
    bundle.update(overrides)
    return bundle


def test_valid_bundle_passes() -> None:
    assert PROBE.validate_bundle(_bundle()) == []


def test_env_file_parser_ignores_comments_and_odd_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# comment",
                "",
                "MAILHUB_IMAP_HOST=imap.qiye.163.com",
                'HOST_IMAP_APP_PASSWORD="quoted-value"',
                "not a key line",
                "1INVALID=x",
                "EMPTY=",
                "SPACED = trimmed ",
            ]
        ),
        encoding="utf-8",
    )

    values = PROBE.parse_env_file(env)

    assert values["MAILHUB_IMAP_HOST"] == "imap.qiye.163.com"
    assert values["HOST_IMAP_APP_PASSWORD"] == "quoted-value"
    assert values["EMPTY"] == ""
    assert values["SPACED"] == "trimmed"
    assert "1INVALID" not in values


def test_capability_normalisation_and_features() -> None:
    capabilities = PROBE.normalize_capabilities([b"IMAP4rev1 idle", "UIDPLUS CondStore"])

    assert capabilities == ["CONDSTORE", "IDLE", "IMAP4REV1", "UIDPLUS"]
    features = PROBE.feature_flags(capabilities)
    assert features["idle"] is True
    assert features["condstore"] is True
    assert features["uidplus"] is True
    assert features["move"] is False


@pytest.mark.parametrize(
    "leaky",
    [
        {"password": "x"},
        {"app_password": "x"},
        {"access_token": "x"},
        {"nested": {"body_text": "x"}},
        {"items": [{"snippet": "x"}]},
        {"authorization": "x"},
        {"folder": {"raw_headers": "x"}},
    ],
)
def test_secret_and_content_keys_are_detected(leaky: dict[str, Any]) -> None:
    bundle = _bundle()
    bundle.update(leaky)

    issues = PROBE.validate_bundle(bundle)

    assert any(issue.startswith("forbidden_keys:") for issue in issues), issues


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"schema": "other.v1"}, "schema_mismatch"),
        ({"tls_version": ""}, "tls_version_missing"),
        ({"capabilities": []}, "capabilities_missing"),
        ({"inbox": {"uidvalidity": 0, "messages": 1}}, "uidvalidity_invalid"),
        ({"inbox": {"uidvalidity": 5, "messages": -1}}, "message_count_invalid"),
        ({"read_only_guard": None}, "read_only_guard_missing"),
    ],
)
def test_malformed_bundles_fail_closed(mutation: dict[str, Any], expected: str) -> None:
    issues = PROBE.validate_bundle(_bundle(**mutation))

    assert expected in issues, issues


@pytest.mark.parametrize(
    "violation",
    ["store_issued", "expunge_issued", "copy_or_move_issued", "body_fetched", "flags_modified"],
)
def test_read_only_guard_violations_are_rejected(violation: str) -> None:
    guard = dict(_bundle()["read_only_guard"])
    guard[violation] = True

    issues = PROBE.validate_bundle(_bundle(read_only_guard=guard))

    assert f"read_only_guard_violated:{violation}" in issues, issues


MUTATING_IMAP_METHODS = frozenset(
    {
        "store",
        "append",
        "copy",
        "move",
        "expunge",
        "create",
        "delete",
        "rename",
        "subscribe",
        "unsubscribe",
    }
)
READ_ONLY_UID_COMMANDS = frozenset({"SEARCH", "FETCH", "SORT", "THREAD"})


def _calls() -> list[ast.Call]:
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def test_probe_calls_no_mutating_imap_method() -> None:
    """Static guard: the probe cannot gain a mutating call by accident.

    Capability tokens such as "MOVE" are legitimate evidence, so the check looks
    at the *call graph* rather than at string literals in general.
    """

    for call in _calls():
        if not isinstance(call.func, ast.Attribute):
            continue
        # Only the IMAP client matters; list.append() style calls are fine.
        target = call.func.value
        if not (isinstance(target, ast.Name) and target.id == "client"):
            continue
        attribute = call.func.attr
        assert attribute not in MUTATING_IMAP_METHODS, attribute
        if attribute != "uid":
            continue
        command = call.args[0] if call.args else None
        assert isinstance(command, ast.Constant), "client.uid needs a literal command"
        assert command.value in READ_ONLY_UID_COMMANDS, command.value


def test_probe_never_requests_a_body_section() -> None:
    for node in ast.walk(ast.parse(_SCRIPT.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.startswith("BODY["), node.value
            assert "BODY.PEEK[" not in node.value, node.value


def test_tls_context_defaults_to_the_system_store() -> None:
    context = PROBE._tls_context(None)

    assert context.verify_mode is not None
    assert context.check_hostname is True


def test_tls_context_accepts_an_operator_ca_for_self_hosted_servers(tmp_path: Path) -> None:
    """A self-hosted Dovecot/Exchange very often presents a private-CA cert."""

    ca = tmp_path / "ca.pem"
    ca.write_text("not a real certificate", encoding="utf-8")

    # load_verify_locations raises on a malformed file, which proves the path is
    # actually consulted rather than silently ignored.
    with pytest.raises(ssl.SSLError):
        PROBE._tls_context(ca)


def test_probe_selects_every_mailbox_read_only() -> None:
    selects = [
        call
        for call in _calls()
        if isinstance(call.func, ast.Attribute) and call.func.attr == "select"
    ]

    assert selects, "the probe must select a mailbox"
    for call in selects:
        readonly = [keyword for keyword in call.keywords if keyword.arg == "readonly"]
        assert readonly, "client.select must pass readonly explicitly"
        value = readonly[0].value
        assert isinstance(value, ast.Constant) and value.value is True, "readonly must be True"
