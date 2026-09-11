"""Prove the reference host's Vault secret backend end to end.

`MAIL-ADOPT-008` asks for a production Secret backend instead of the encrypted
SQLite fallback.  The requirement is not "Vault is reachable" but that the host's
own database never holds a usable mailbox credential:

* after storing an application password the database row holds only a pointer;
* the password is readable from Vault and resolvable through the host API shape;
* revoking removes the material from Vault, not just the local pointer;
* a resolve after revoke fails closed.

Runs against a real Vault (dev server is fine) given by `HOST_VAULT_ADDR` /
`HOST_VAULT_TOKEN`, and never prints a secret.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_host.stores import LocalStores  # noqa: E402
from local_host.vault import VaultConfig, VaultKvClient, is_vault_pointer  # noqa: E402

SCHEMA_VERSION = "mailhub.vault_secret_backend_conformance.v1"
TENANT = "vault-tenant"
SUBJECT = "vault-subject"
USERNAME = "vault-user@example.test"
PASSWORD = "vault-application-password-value"


@dataclass
class Checks:
    values: dict[str, bool] = field(default_factory=dict)

    def check(self, name: str, passed: bool) -> None:
        self.values[name] = bool(passed)


def _files_containing(database_path: Path, needle: bytes) -> list[str]:
    """Scan the main database file and its WAL companions.

    SQLite runs in WAL mode, so a raw read of the main file alone would not prove
    the secret is absent.
    """

    found: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        companion = Path(str(database_path) + suffix)
        if companion.exists() and needle in companion.read_bytes():
            found.append(companion.name)
    return found


def _stored_blob(database_path: Path, credential_ref: str) -> bytes:
    with sqlite3.connect(database_path) as db:
        row = db.execute(
            "SELECT encrypted_password FROM host_imap_credentials WHERE credential_ref=?",
            (credential_ref,),
        ).fetchone()
    return bytes(row[0]) if row else b""


def run() -> dict[str, Any]:
    addr = os.environ.get("HOST_VAULT_ADDR", "")
    token = os.environ.get("HOST_VAULT_TOKEN", "")
    if not addr or not token:
        raise SystemExit(
            "vault_settings_missing: set HOST_VAULT_ADDR and HOST_VAULT_TOKEN"
        )

    checks = Checks()
    # SQLite keeps the database file handle open on Windows, so cleanup is
    # best-effort instead of failing an otherwise complete conformance run.
    with tempfile.TemporaryDirectory(
        prefix="mailhub-vault-", ignore_cleanup_errors=True
    ) as workdir:
        database_path = Path(workdir) / "host.db"
        vault = VaultKvClient(
            VaultConfig(
                addr=addr, token=token, prefix=f"mailhub-conformance-{os.getpid()}"
            )
        )
        stores = LocalStores(
            database_path, "vault-conformance-encryption-secret", vault=vault
        )
        stores.initialize()

        credential_ref = stores.store_imap_credential(
            tenant_id=TENANT, subject_id=SUBJECT, username=USERNAME, password=PASSWORD
        )
        stored = _stored_blob(database_path, credential_ref)

        checks.check("credential_ref_issued", credential_ref.startswith("imapcred_"))
        checks.check("database_holds_a_pointer", is_vault_pointer(stored))
        checks.check(
            "password_absent_from_database_row", PASSWORD.encode() not in stored
        )
        checks.check(
            "password_absent_from_every_database_file",
            not _files_containing(database_path, PASSWORD.encode()),
        )
        checks.check(
            "password_digest_absent_from_every_database_file",
            not _files_containing(
                database_path, hashlib.sha256(PASSWORD.encode()).hexdigest().encode()
            ),
        )

        in_vault = vault.get_secret(pointer=stored.decode("utf-8", "replace"))
        checks.check("secret_readable_from_vault", in_vault == PASSWORD)

        resolved = stores.resolve_imap_credential(
            credential_ref=credential_ref, tenant_id=TENANT, subject_id=SUBJECT
        )
        checks.check(
            "resolve_returns_username_and_password",
            resolved == {"username": USERNAME, "password": PASSWORD},
        )
        checks.check(
            "resolve_is_tenant_scoped",
            stores.resolve_imap_credential(
                credential_ref=credential_ref, tenant_id="other", subject_id=SUBJECT
            )
            is None,
        )

        rotated = stores.rotate_imap_credential(
            credential_ref=credential_ref,
            tenant_id=TENANT,
            subject_id=SUBJECT,
            password="rotated-application-password",
        )
        checks.check("rotation_bumps_version", rotated == 2)
        checks.check(
            "rotation_replaces_vault_material",
            vault.get_secret(pointer=stored.decode("utf-8", "replace"))
            == "rotated-application-password",
        )
        checks.check(
            "rotation_keeps_the_same_pointer",
            _stored_blob(database_path, credential_ref) == stored,
        )
        checks.check(
            "rotated_secret_resolves",
            stores.resolve_imap_credential(
                credential_ref=credential_ref, tenant_id=TENANT, subject_id=SUBJECT
            )
            == {"username": USERNAME, "password": "rotated-application-password"},
        )

        revoked = stores.revoke_imap_credential(
            credential_ref=credential_ref, tenant_id=TENANT, subject_id=SUBJECT
        )
        checks.check("revoke_reported_success", revoked is True)
        checks.check(
            "secret_removed_from_vault",
            vault.get_secret(pointer=stored.decode("utf-8", "replace")) is None,
        )
        checks.check(
            "resolve_after_revoke_fails_closed",
            stores.resolve_imap_credential(
                credential_ref=credential_ref, tenant_id=TENANT, subject_id=SUBJECT
            )
            is None,
        )

    return {
        "schema": SCHEMA_VERSION,
        "observed_at": datetime.now(UTC).isoformat(),
        "vault_addr_scheme": addr.split("://", 1)[0],
        "storage": "vault_kv_v2_pointer_in_sqlite",
        "checks": checks.values,
        "passed": all(checks.values.values()),
    }


def validate_bundle(bundle: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if bundle.get("schema") != SCHEMA_VERSION:
        issues.append("schema_mismatch")
    if bundle.get("passed") is not True:
        issues.append("not_passed")
    checks = bundle.get("checks")
    if not isinstance(checks, Mapping) or not checks:
        issues.append("checks_missing")
    else:
        failed = sorted(
            str(name) for name, value in checks.items() if value is not True
        )
        if failed:
            issues.append("checks_failed:" + ",".join(failed))
    for key in ("token", "password", "vault_token", "secret"):
        if key in bundle:
            issues.append("forbidden_key:" + key)
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Vault secret backend conformance.")
    parser.add_argument("--json", dest="json_path", type=Path, default=None)
    parser.add_argument("--validate", type=Path, default=None)
    args = parser.parse_args()

    if args.validate is not None:
        bundle: Any = json.loads(args.validate.read_text(encoding="utf-8"))
        if not isinstance(bundle, Mapping):
            print("evidence is not a JSON object")
            return 1
        issues = validate_bundle(bundle)
        for issue in issues:
            print("  [FAIL] " + issue)
        print(
            "vault evidence validation: "
            + ("ok" if not issues else f"{len(issues)} issue(s)")
        )
        return 0 if not issues else 1

    bundle = run()
    issues = validate_bundle(bundle)
    for name, value in bundle["checks"].items():
        print(f"  [{'PASS' if value else 'FAIL'}] {name}")
    for issue in issues:
        print("  [FAIL] " + issue)
    print("vault backend : " + ("pass" if not issues else f"{len(issues)} issue(s)"))
    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(
            json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print("wrote: " + str(args.json_path))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
