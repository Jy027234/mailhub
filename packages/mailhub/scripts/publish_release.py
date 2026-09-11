"""Publish an already-built MailHub release bundle.

Publishing is deliberately separated from building.  This script refuses to act
unless all of the following hold:

1. the manifest was produced by build_release.py and is marked publishable;
2. the release gates ran in full (--gates full);
3. every recorded artifact still matches its SHA-256 on disk;
4. an explicit approver is named with --approved-by.

Without --execute the script only prints the publish commands it would run, so a
release can be reviewed before anything leaves the machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

MANIFEST_SCHEMA = "mailhub.release_manifest.v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _refuse(reasons: list[str]) -> int:
    print("publish refused:")
    for reason in reasons:
        print("  - " + reason)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a MailHub release bundle.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--target", default="", help="registry/index target identifier")
    parser.add_argument("--execute", action="store_true", help="actually run the publish commands")
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    if not manifest_path.is_file():
        return _refuse(["manifest not found: " + str(manifest_path)])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    release_dir = manifest_path.parent

    reasons: list[str] = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        reasons.append("unexpected manifest schema: " + str(manifest.get("schema")))
    if not manifest.get("publishable"):
        reasons.extend(
            str(item) for item in manifest.get("publish_blockers") or ["publishable=false"]
        )
    if manifest.get("gates") != "full":
        reasons.append("release gates were not run in full")
    if not args.approved_by.strip():
        reasons.append("--approved-by is required (no implicit release authority)")
    if not args.target.strip():
        reasons.append("--target is required (registry/index must be explicit)")

    mismatched: list[str] = []
    missing: list[str] = []
    for artifact in manifest.get("artifacts") or []:
        item = release_dir / str(artifact.get("path"))
        if not item.is_file():
            missing.append(str(artifact.get("path")))
            continue
        if _sha256(item) != artifact.get("sha256"):
            mismatched.append(str(artifact.get("path")))
    if missing:
        reasons.append("artifacts missing: " + ", ".join(sorted(missing)))
    if mismatched:
        reasons.append("artifact digest mismatch: " + ", ".join(sorted(mismatched)))

    if reasons:
        return _refuse(reasons)

    commands = [
        "twine upload --repository "
        + args.target
        + " "
        + str((release_dir / "python").as_posix())
        + "/*",
        "npm publish " + str((release_dir / "ui").as_posix()) + " --registry " + args.target,
        "npm publish "
        + str((release_dir / "typescript-sdk").as_posix())
        + " --registry "
        + args.target,
    ]
    print("release     : " + str(manifest.get("version")))
    print("git commit  : " + str(manifest.get("git_commit")))
    print("approved by : " + args.approved_by.strip())
    print("target      : " + args.target.strip())
    print("artifacts   : " + str(len(manifest.get("artifacts") or [])))
    if not args.execute:
        print("dry run; commands that would run:")
        for command in commands:
            print("  " + command)
        print("re-run with --execute to publish")
        return 0
    print("execution requires a configured registry client; no upload was attempted")
    return 3


if __name__ == "__main__":
    sys.exit(main())
