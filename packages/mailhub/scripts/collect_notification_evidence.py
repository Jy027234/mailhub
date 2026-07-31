"""Collect a redacted A3/A4 notification artifact from MailHub observations.

The command is an authenticated observation client, not a fixture importer.
It must run against a real, injected MailHub runtime after the operator has
exercised subscription renewal, a duplicate notification, ACK handling and a
reconciliation sync over the required observation window.  It accepts no
provider callback body, token, subscription client state or hand-written
counts.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path
from uuid import UUID

try:
    from run_provider_activation import (
        ActivationError,
        MailHubActivationClient,
        _api_url,
        _offline_preflight,
        _run_id,
        _slug,
        _text,
        _write_json,
    )
except ModuleNotFoundError as exc:
    if exc.name != "run_provider_activation":
        raise
    _activation_path = Path(__file__).with_name("run_provider_activation.py")
    _activation_spec = importlib.util.spec_from_file_location(
        "mailhub_run_provider_activation", _activation_path
    )
    if _activation_spec is None or _activation_spec.loader is None:
        raise RuntimeError("activation_client_unavailable") from exc
    _activation_module = importlib.util.module_from_spec(_activation_spec)
    _activation_spec.loader.exec_module(_activation_module)
    ActivationError = _activation_module.ActivationError
    MailHubActivationClient = _activation_module.MailHubActivationClient
    _api_url = _activation_module._api_url
    _offline_preflight = _activation_module._offline_preflight
    _run_id = _activation_module._run_id
    _slug = _activation_module._slug
    _text = _activation_module._text
    _write_json = _activation_module._write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("gmail", "microsoft_graph"), required=True)
    parser.add_argument("--environment", default=os.getenv("MAILHUB_ACTIVATION_ENV", "test"))
    parser.add_argument("--api-url", default=os.getenv("MAILHUB_ACTIVATION_API_URL"))
    parser.add_argument("--tenant-id", default=os.getenv("MAILHUB_ACTIVATION_TENANT_ID"))
    parser.add_argument("--subject-id", default=os.getenv("MAILHUB_ACTIVATION_SUBJECT_ID"))
    parser.add_argument("--connection-id", default=os.getenv("MAILHUB_ACTIVATION_CONNECTION_ID"))
    parser.add_argument("--run-id", default=os.getenv("MAILHUB_ACTIVATION_RUN_ID"))
    parser.add_argument(
        "--evidence-dir",
        default=os.getenv("MAILHUB_ACTIVATION_EVIDENCE_DIR"),
        help=(
            "existing provider-activation bundle directory; defaults to "
            "evidence-root/provider/env/run"
        ),
    )
    parser.add_argument(
        "--evidence-root",
        default=os.getenv("MAILHUB_ACTIVATION_EVIDENCE_ROOT", "provider-activation"),
    )
    parser.add_argument(
        "--confirm-real-provider",
        action="store_true",
        help="required together with MAILHUB_ACTIVATION_ALLOW_NETWORK=true",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client_factory: type[MailHubActivationClient] = MailHubActivationClient,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.confirm_real_provider:
            raise ActivationError("activation_confirmation_required")
        if os.getenv("MAILHUB_ACTIVATION_ALLOW_NETWORK", "").casefold() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise ActivationError("activation_network_gate_required")
        provider = _text(args.provider, "provider", 40)
        environment = _slug(args.environment, "environment")
        run_id = _run_id(args.run_id or "")
        api_url = _api_url(args.api_url)
        tenant_id = _text(args.tenant_id, "tenant_id", 200)
        subject_id = _text(args.subject_id, "subject_id", 200)
        connection_id = UUID(_text(args.connection_id, "connection_id", 80))
        preflight = _offline_preflight(provider)
        if args.evidence_dir:
            evidence_dir = Path(_text(args.evidence_dir, "evidence_dir", 2000)).resolve()
        else:
            evidence_dir = (
                Path(_text(args.evidence_root, "evidence_root", 2000)).resolve()
                / provider
                / environment
                / run_id
            )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        with client_factory(
            api_url=api_url,
            tenant_id=tenant_id,
            subject_id=subject_id,
            trace_id=f"mailhub-activation:{run_id}:notifications",
            authorization=os.getenv("MAILHUB_ACTIVATION_AUTHORIZATION"),
        ) as client:
            client.readiness()
            notification_payload = client.notification_observation(
                connection_id=connection_id,
                provider=provider,
                environment=environment,
                run_id=run_id,
            )
        _write_json(evidence_dir / "webhook-receipts.json", notification_payload)
        preflight_path = evidence_dir / "preflight.json"
        if not preflight_path.exists():
            _write_json(preflight_path, preflight)
        print(f"notification evidence: {evidence_dir / 'webhook-receipts.json'}")
        return 0
    except (ActivationError, ValueError, OSError) as exc:
        code = (
            exc.code if isinstance(exc, ActivationError) else "activation_notification_local_error"
        )
        print(f"notification evidence blocked: {code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
