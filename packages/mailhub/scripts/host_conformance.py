"""Validate a host product against the MailHub Host Port contract.

Usage::

    # print a starter bundle to record your product's port behaviour
    python scripts/host_conformance.py --emit-template

    # validate the recorded bundle (exit code 1 on any failed check)
    python scripts/host_conformance.py --bundle host-conformance.json

    # prove the kit itself still detects violations
    python scripts/host_conformance.py --self-test

The kit is offline: it validates what your ports returned, it never calls your
services or a provider.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mailhub.hosts.conformance import (
    load_bundle,
    run_host_conformance,
    sample_bundle,
    template_bundle,
)


def _validate(path: Path, *, as_json: bool) -> int:
    report = run_host_conformance(load_bundle(path))
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(f"host   : {report.host}")
        print(f"checks : {len(report.checks)}")
        for check in report.checks:
            marker = "PASS" if check.passed else "FAIL"
            detail = f" - {check.detail}" if check.detail else ""
            print(f"  [{marker}] {check.area}.{check.name}{detail}")
        print("result : " + ("pass" if report.passed else f"{len(report.failures)} failure(s)"))
    return 0 if report.passed else 1


def _self_test() -> int:
    compliant = run_host_conformance(sample_bundle(compliant=True))
    broken = run_host_conformance(sample_bundle(compliant=False))
    print(f"compliant bundle : {len(compliant.checks)} checks, passed={compliant.passed}")
    for failure in compliant.failures:
        print(f"  unexpected failure: {failure.area}.{failure.name} - {failure.detail}")
    print(f"broken bundle    : {len(broken.checks)} checks, passed={broken.passed}")
    for failure in broken.failures:
        print(f"  detected: {failure.area}.{failure.name} - {failure.detail}")
    if compliant.passed and len(broken.failures) >= 8:
        print("self-test: ok")
        return 0
    print("self-test: FAILED (the kit no longer separates compliant from broken hosts)")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="MailHub Host Port conformance kit.")
    parser.add_argument("--bundle", type=Path, default=None)
    parser.add_argument("--emit-template", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    actions = [bool(args.bundle), args.emit_template, args.self_test]
    if sum(actions) != 1:
        parser.error("choose exactly one of --bundle, --emit-template, --self-test")

    if args.emit_template:
        payload = json.dumps(template_bundle(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if args.output is not None:
            args.output.write_text(payload, encoding="utf-8")
            print(args.output)
        else:
            print(payload, end="")
        return 0
    if args.self_test:
        return _self_test()
    assert args.bundle is not None
    return _validate(args.bundle.resolve(), as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
