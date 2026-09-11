"""Probe one IMAP server and emit redacted server-matrix evidence.

`MAIL-IMAP-005` asks for a real compatibility matrix (QQ / 163 / Dovecot /
self-hosted Exchange).  This probe produces the evidence row for **one** server;
the matrix stays honest because a single verified row never implies the others.

Read-only by construction:

* the mailbox is selected with `readonly=True`, so no message can become \\Seen;
* only `BODY.PEEK`-free metadata is fetched (FLAGS / INTERNALDATE / RFC822.SIZE),
  so no header or body content enters memory or evidence;
* STORE, APPEND, COPY, MOVE and EXPUNGE are never issued, and the bundle records
  that guard explicitly so a reviewer can check it.

Credentials are read from the environment (or an env file) and are never written
to the bundle; `--validate` re-checks that fail-closed property afterwards.

Usage::

    python scripts/imap_server_matrix_probe.py \
        --env-file ../../local-host/.env --label 163-qiye --json evidence.json
    python scripts/imap_server_matrix_probe.py --validate evidence.json
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import imaplib
import json
import os
import re
import ssl
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "mailhub.imap_server_matrix_evidence.v1"

#: Key names (case-folded, substring match) that must never reach the bundle.
FORBIDDEN_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "body",
    "content",
    "snippet",
    "raw",
    "subject",
    "sender",
    "recipient",
)

#: The read-only guard is a fixed structure: every flag must be present and the
#: five mutation flags must be false.  Because the shape is validated exactly,
#: the secret scan can skip this subtree instead of flagging its own key names
#: (\`body_fetched\` legitimately contains the word "body").
READ_ONLY_GUARD_KEY = "read_only_guard"
READ_ONLY_GUARD_MUTATION_FLAGS: tuple[str, ...] = (
    "store_issued",
    "expunge_issued",
    "copy_or_move_issued",
    "body_fetched",
    "flags_modified",
)
READ_ONLY_GUARD_FLAGS: tuple[str, ...] = (*READ_ONLY_GUARD_MUTATION_FLAGS, "selected_readonly")

#: Capability tokens that matter to the MailHub connector contract.
FEATURE_TOKENS: Mapping[str, str] = {
    "idle": "IDLE",
    "condstore": "CONDSTORE",
    "enable": "ENABLE",
    "uidplus": "UIDPLUS",
    "move": "MOVE",
    "namespace": "NAMESPACE",
    "quota": "QUOTA",
    "utf8_accept": "UTF8=ACCEPT",
    "unselect": "UNSELECT",
    "special_use": "SPECIAL-USE",
    "literal_plus": "LITERAL+",
    "starttls": "STARTTLS",
    "auth_xoauth2": "AUTH=XOAUTH2",
    "auth_plain": "AUTH=PLAIN",
    "auth_login": "AUTH=LOGIN",
}


def parse_env_file(path: Path) -> dict[str, str]:
    """Read `KEY=VALUE` lines without echoing values.

    Deliberately tiny: no export syntax, no interpolation, no command
    substitution — a probe must not execute anything from an env file.
    """

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def resolve_settings(env_file: Path | None) -> dict[str, str]:
    file_values = parse_env_file(env_file) if env_file is not None else {}

    def pick(*names: str, default: str = "") -> str:
        for name in names:
            candidate = os.environ.get(name) or file_values.get(name)
            if candidate:
                return candidate
        return default

    host = pick("MAILHUB_IMAP_HOST", "HOST_IMAP_HOST")
    username = pick("HOST_IMAP_USERNAME", "MAILHUB_IMAP_USERNAME")
    password = pick("HOST_IMAP_APP_PASSWORD", "MAILHUB_IMAP_APP_PASSWORD", "HOST_IMAP_PASSWORD")
    port = pick("MAILHUB_IMAP_PORT", "HOST_IMAP_PORT", default="993")
    missing = [
        name
        for name, value in (("host", host), ("username", username), ("app password", password))
        if not value
    ]
    if missing:
        raise SystemExit("imap_probe_settings_missing: " + ", ".join(missing))
    return {"host": host, "username": username, "password": password, "port": port}


def authenticated_capabilities(client: imaplib.IMAP4_SSL) -> list[str]:
    """Ask the authenticated server for its capabilities explicitly.

    The answer is authoritative: a server may advertise a much wider set after
    login than before it, and RFC 3501 lets it do so by repeating CAPABILITY in
    the login response, which imaplib does not always pick up.
    """

    try:
        status, data = client.capability()
    except (imaplib.IMAP4.error, OSError):
        return []
    if status != "OK" or not isinstance(data, (list, tuple)):
        return []
    flattened: list[bytes | str] = []
    for item in data:
        if isinstance(item, (bytes, str)):
            flattened.extend(item.split())
    return normalize_capabilities(flattened)


def normalize_capabilities(raw: Sequence[bytes | str]) -> list[str]:
    tokens: set[str] = set()
    for entry in raw:
        text = entry.decode("utf-8", "replace") if isinstance(entry, bytes) else entry
        tokens.update(token.strip().upper() for token in text.split() if token.strip())
    return sorted(tokens)


def feature_flags(capabilities: Sequence[str]) -> dict[str, bool]:
    present = {token.upper() for token in capabilities}
    return {name: token in present for name, token in FEATURE_TOKENS.items()}


def parse_status_fields(text: str) -> dict[str, int]:
    """Parse a STATUS response into a name -> value map.

    Positional parsing is unsafe: 163 answers `STATUS INBOX (UIDVALIDITY UIDNEXT
    UNSEEN)` with `(UIDNEXT 1730701461 UIDVALIDITY 1 UNSEEN 82)`, so reading by
    index silently swaps UIDVALIDITY and UIDNEXT and fabricates a cursor reset.
    """

    return {
        match.group(1).upper(): int(match.group(2))
        for match in re.finditer(r"([A-Za-z][A-Za-z0-9]*)\s+(\d+)", text)
    }


def digest(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _iter_keys(node: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            keys.append(str(key))
            keys.extend(_iter_keys(value))
    elif isinstance(node, (list, tuple)):
        for item in node:
            keys.extend(_iter_keys(item))
    return keys


def find_forbidden_keys(bundle: Mapping[str, Any]) -> list[str]:
    """Scan every key except the validated read-only guard subtree.

    The guard is excluded only at the top level and only because
    `validate_bundle` pins its exact shape; anywhere else a key named
    `read_only_guard` is scanned like any other.
    """

    offenders: list[str] = []
    for key, value in bundle.items():
        if key == READ_ONLY_GUARD_KEY:
            continue
        for candidate in _iter_keys({key: value}):
            folded = candidate.strip().casefold()
            if any(marker in folded for marker in FORBIDDEN_KEY_MARKERS):
                offenders.append(candidate)
    return sorted(set(offenders))


def validate_bundle(bundle: Mapping[str, Any]) -> list[str]:
    """Fail-closed checks a reviewer (or CI) can re-run offline."""

    issues: list[str] = []
    if bundle.get("schema") != SCHEMA_VERSION:
        issues.append("schema_mismatch")
    offenders = find_forbidden_keys(bundle)
    if offenders:
        issues.append("forbidden_keys:" + ",".join(offenders))
    if not bundle.get("tls_version"):
        issues.append("tls_version_missing")
    capabilities = bundle.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        issues.append("capabilities_missing")
    pre_auth = bundle.get("capabilities_unauthenticated")
    if pre_auth is not None and not isinstance(pre_auth, list):
        issues.append("capabilities_unauthenticated_invalid")
    auth_features = bundle.get("auth_features")
    if auth_features is not None and not isinstance(auth_features, dict):
        issues.append("auth_features_invalid")
    guard = bundle.get(READ_ONLY_GUARD_KEY)
    if not isinstance(guard, Mapping):
        issues.append("read_only_guard_missing")
    else:
        if set(guard) != set(READ_ONLY_GUARD_FLAGS):
            issues.append("read_only_guard_shape_invalid")
        for name in READ_ONLY_GUARD_MUTATION_FLAGS:
            if guard.get(name) is not False:
                issues.append(f"read_only_guard_violated:{name}")
        if guard.get("selected_readonly") is not True:
            issues.append("read_only_guard_violated:selected_readonly")
    inbox = bundle.get("inbox")
    if not isinstance(inbox, Mapping):
        issues.append("inbox_missing")
    else:
        uidvalidity = inbox.get("uidvalidity")
        messages = inbox.get("messages")
        search_count = inbox.get("uid_search_count")
        max_uid = inbox.get("max_uid")
        uidnext = inbox.get("uidnext")
        if not isinstance(uidvalidity, int) or uidvalidity <= 0:
            issues.append("uidvalidity_invalid")
        if not isinstance(messages, int) or messages < 0:
            issues.append("message_count_invalid")
        if not isinstance(search_count, int) or search_count < 0:
            issues.append("search_count_invalid")
        # Cross-checks that a positional/partial parse cannot survive: these are
        # the invariants that a swapped UIDVALIDITY/UIDNEXT used to violate.
        if isinstance(search_count, int) and isinstance(messages, int):
            if search_count > messages:
                issues.append("search_exceeds_exists")
            delta = inbox.get("search_delta")
            if isinstance(delta, int) and delta != messages - search_count:
                issues.append("search_delta_inconsistent")
        if (
            isinstance(uidnext, int)
            and isinstance(max_uid, int)
            and uidnext != 0
            and max_uid > 0
            and uidnext <= max_uid
        ):
            issues.append("uidnext_not_above_max_uid")
        if isinstance(uidvalidity, int) and isinstance(uidnext, int) and uidvalidity == uidnext:
            issues.append("uidvalidity_equals_uidnext")
    second = bundle.get("second_session")
    if isinstance(second, Mapping) and "uidvalidity_stable_across_sessions" in bundle:
        stable = bundle["uidvalidity_stable_across_sessions"]
        expected = second.get("uidvalidity") == (inbox or {}).get("uidvalidity")
        if bool(stable) is not bool(expected):
            issues.append("uidvalidity_stability_inconsistent")
    return issues


def _tls_facts(client: imaplib.IMAP4_SSL) -> dict[str, Any]:
    sock = client.sock
    if sock is None:
        return {"tls_version": "", "tls_cipher": "", "peer_verified": False}
    version = sock.version()
    cipher = sock.cipher()
    return {
        "tls_version": str(version) if version else "",
        "tls_cipher": str(cipher[0]) if cipher else "",
        "peer_verified": True,
    }


def _folder_facts(client: imaplib.IMAP4_SSL, limit: int) -> dict[str, Any]:
    status, data = client.list()
    names: list[str] = []
    delimiter = ""
    if status == "OK":
        for entry in data:
            text = entry.decode("utf-8", "replace") if isinstance(entry, bytes) else str(entry)
            match = re.match(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$', text)
            if match is None:
                continue
            delimiter = delimiter or match.group("delim")
            name = match.group("name").strip().strip('"')
            names.append(name)
    return {
        "folder_count": len(names),
        "hierarchy_delimiter": delimiter,
        "inbox_present": any(name.upper() == "INBOX" for name in names),
        # Folder names are operator metadata, but they can leak business
        # structure, so evidence carries stable digests instead of plain names.
        "folder_name_digests": sorted(digest(name) for name in names[:limit]),
        "folder_names_truncated": len(names) > limit,
    }


def _tls_context(ca_file: Path | None) -> ssl.SSLContext:
    """Trust anchor for the probe.

    A self-hosted Dovecot or Exchange very often presents a private-CA
    certificate, so the matrix must be able to record which anchor was used
    instead of silently skipping those servers.
    """

    if ca_file is None:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=str(ca_file))


def probe(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str = "INBOX",
    fetch_limit: int = 5,
    folder_limit: int = 30,
    timeout: float = 30.0,
    ca_file: Path | None = None,
) -> dict[str, Any]:
    """Run the read-only probe and return a redacted evidence bundle."""

    context = _tls_context(ca_file)
    client = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=timeout)
    try:
        tls = _tls_facts(client)
        unauthenticated = normalize_capabilities(client.capabilities)
        client.login(username, password)
        # imaplib only refreshes client.capabilities when it happens to see an
        # untagged CAPABILITY, so on a server that restates capabilities in the
        # tagged login response it silently keeps the pre-auth list.  Dovecot
        # 2.4 reports 8 tokens that way and 41 through an explicit CAPABILITY,
        # which is how an earlier revision of this probe recorded CONDSTORE,
        # QRESYNC, UIDPLUS and MOVE as absent on a server that implements them.
        authenticated = authenticated_capabilities(client) or normalize_capabilities(
            client.capabilities
        )
        capabilities = authenticated or unauthenticated

        status, selected = client.select(mailbox, readonly=True)
        if status != "OK":
            raise SystemExit("imap_probe_select_failed")
        messages = int(selected[0]) if selected and selected[0] else 0

        status, status_data = client.status(mailbox, "(UIDVALIDITY UIDNEXT UNSEEN)")
        fields: dict[str, int] = {}
        if status == "OK" and status_data:
            fields = parse_status_fields(status_data[0].decode("utf-8", "replace"))
        uidvalidity = fields.get("UIDVALIDITY", 0)
        uidnext = fields.get("UIDNEXT", 0)
        unseen = fields.get("UNSEEN", 0)

        status, search_data = client.uid("SEARCH", None, "ALL")
        uids: list[bytes] = []
        if status == "OK" and search_data and search_data[0]:
            uids = search_data[0].split()

        fetched = 0
        internal_dates: list[str] = []
        if uids:
            sample = uids[-min(fetch_limit, len(uids)) :]
            # Metadata only: no BODY[...] item is requested, so nothing that
            # could set \Seen or pull content is ever fetched.
            status, fetch_data = client.uid(
                "FETCH", b",".join(sample), "(FLAGS INTERNALDATE RFC822.SIZE)"
            )
            if status == "OK":
                for entry in fetch_data:
                    # imaplib yields tuples for literal sections and bare bytes
                    # for a plain attribute list, so both shapes must count.
                    if isinstance(entry, tuple):
                        meta = b" ".join(part for part in entry if isinstance(part, bytes)).decode(
                            "utf-8", "replace"
                        )
                    elif isinstance(entry, bytes):
                        meta = entry.decode("utf-8", "replace")
                    else:
                        continue
                    if "RFC822.SIZE" not in meta and "FLAGS" not in meta:
                        continue
                    date_match = re.search(r'INTERNALDATE "([^"]+)"', meta)
                    if date_match:
                        internal_dates.append(date_match.group(1))
                    fetched += 1

        folders = _folder_facts(client, folder_limit)
        # Auth mechanisms live in the *pre-auth* list: RFC 3501 lets a server
        # drop them once the session is authenticated, and every server in the
        # matrix does.  Deriving the auth flags from the authenticated list
        # would report "no AUTH=PLAIN" for a server that just logged us in.
        read_only = feature_flags(capabilities)
        auth_features = {
            name: value
            for name, value in feature_flags(list(authenticated) + list(unauthenticated)).items()
            if name.startswith("auth_")
        }
        guard = {
            "store_issued": False,
            "expunge_issued": False,
            "copy_or_move_issued": False,
            "body_fetched": False,
            "flags_modified": False,
            "selected_readonly": True,
        }

        return {
            "schema": SCHEMA_VERSION,
            "observed_at": datetime.now(UTC).isoformat(),
            "host": host,
            "port": port,
            "tls_version": tls["tls_version"],
            "tls_cipher": tls["tls_cipher"],
            "peer_verified": tls["peer_verified"],
            # Recorded so a reviewer knows whether the system trust store or an
            # operator-supplied CA validated the server certificate.
            "certificate_trust": "custom_ca" if ca_file is not None else "system_store",
            "capabilities": capabilities,
            # Kept separately because the two lists answer different questions:
            # authenticated capabilities govern mailbox behaviour, pre-auth
            # capabilities govern which authentication mechanisms exist.
            "capabilities_unauthenticated": unauthenticated,
            "features": read_only,
            "auth_features": auth_features,
            "inbox": {
                "mailbox": mailbox,
                "uidvalidity": uidvalidity,
                "uidnext": uidnext,
                "messages": messages,
                "unseen": unseen,
                "uid_search_count": len(uids),
                "max_uid": int(uids[-1]) if uids else 0,
                "search_visibility_limited": len(uids) < messages,
                "search_delta": messages - len(uids),
            },
            "metadata_fetch": {
                "requested": min(fetch_limit, len(uids)),
                "fetched": fetched,
                "internal_date_samples": len(internal_dates),
            },
            "folders": folders,
            "read_only_guard": guard,
        }
    finally:
        # A logout failure must never mask the probe result.
        with contextlib.suppress(Exception):
            client.logout()


def second_session_facts(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str = "INBOX",
    timeout: float = 30.0,
    ca_file: Path | None = None,
) -> dict[str, int]:
    """Second session: proves UIDVALIDITY stability and re-measures visibility.

    The re-measurement matters because a server may report an EXISTS count that
    disagrees with `UID SEARCH`; recording both sessions is what turns that from
    an anecdote into evidence.
    """

    client = imaplib.IMAP4_SSL(host, port, ssl_context=_tls_context(ca_file), timeout=timeout)
    try:
        client.login(username, password)
        status, data = client.status(mailbox, "(UIDVALIDITY UIDNEXT)")
        fields = (
            parse_status_fields(data[0].decode("utf-8", "replace"))
            if status == "OK" and data
            else {}
        )
        exists = 0
        select_status, selected = client.select(mailbox, readonly=True)
        if select_status == "OK" and selected and selected[0]:
            exists = int(selected[0])
        search_count = 0
        search_status, search_data = client.uid("SEARCH", None, "ALL")
        if search_status == "OK" and search_data and search_data[0]:
            search_count = len(search_data[0].split())
        return {
            "uidvalidity": fields.get("UIDVALIDITY", 0),
            "uidnext": fields.get("UIDNEXT", 0),
            "exists": exists,
            "uid_search_count": search_count,
        }
    finally:
        with contextlib.suppress(Exception):
            client.logout()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only IMAP server-matrix probe.")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--label", default="")
    parser.add_argument("--mailbox", default="INBOX")
    parser.add_argument("--json", dest="json_path", type=Path, default=None)
    parser.add_argument("--validate", type=Path, default=None)
    parser.add_argument("--skip-reconnect", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--ca-file",
        type=Path,
        default=None,
        help="PEM trust anchor for a self-hosted server with a private CA",
    )
    args = parser.parse_args()

    if args.validate is not None:
        bundle: Any = json.loads(args.validate.read_text(encoding="utf-8"))
        if not isinstance(bundle, Mapping):
            print("evidence is not a JSON object")
            return 1
        issues = validate_bundle(bundle)
        for issue in issues:
            print("  [FAIL] " + issue)
        print("imap evidence validation: " + ("ok" if not issues else f"{len(issues)} issue(s)"))
        return 0 if not issues else 1

    settings = resolve_settings(args.env_file)
    bundle = probe(
        host=settings["host"],
        port=int(settings["port"]),
        username=settings["username"],
        password=settings["password"],
        mailbox=args.mailbox,
        timeout=args.timeout,
        ca_file=args.ca_file,
    )
    bundle["label"] = args.label or settings["host"]
    if not args.skip_reconnect:
        second = second_session_facts(
            host=settings["host"],
            port=int(settings["port"]),
            username=settings["username"],
            password=settings["password"],
            mailbox=args.mailbox,
            timeout=args.timeout,
            ca_file=args.ca_file,
        )
        inbox = bundle["inbox"]
        bundle["second_session"] = second
        bundle["uidvalidity_stable_across_sessions"] = second["uidvalidity"] == inbox["uidvalidity"]
        bundle["exists_stable_across_sessions"] = second["exists"] == inbox["messages"]
        bundle["uid_search_stable_across_sessions"] = (
            second["uid_search_count"] == inbox["uid_search_count"]
        )

    issues = validate_bundle(bundle)
    print(f"server        : {bundle['host']}:{bundle['port']} ({bundle['label']})")
    print(f"tls           : {bundle['tls_version']} {bundle['tls_cipher']}")
    print(f"capabilities  : {len(bundle['capabilities'])}")
    print(f"pre-auth caps : {len(bundle.get('capabilities_unauthenticated') or [])}")
    features = bundle["features"]
    active = sorted(name for name, enabled in features.items() if enabled)
    print("features      : " + ", ".join(active))
    inbox = bundle["inbox"]
    print(
        "inbox         : uidvalidity={uidvalidity} messages={messages} "
        "uid_search={uid_search_count} delta={search_delta}".format(**inbox)
    )
    folders = bundle["folders"]
    print(
        f"folders       : {folders['folder_count']} (delimiter {folders['hierarchy_delimiter']!r})"
    )
    if "uidvalidity_stable_across_sessions" in bundle:
        stable = bundle["uidvalidity_stable_across_sessions"]
        print(f"uidvalidity   : stable across sessions = {stable}")
    for issue in issues:
        print("  [FAIL] " + issue)
    print("evidence      : " + ("ok" if not issues else f"{len(issues)} issue(s)"))

    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(
            json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print("wrote         : " + str(args.json_path))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
