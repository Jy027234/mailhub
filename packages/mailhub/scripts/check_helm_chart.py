"""Fail-closed verification of the MailHub Helm chart.

The chart is a release contract, so this gate renders it and asserts the
properties a production release depends on, instead of trusting review:

* an empty/mutable image reference is refused (schema + rendered digest);
* the pod is hardened (non-root, read-only root, no capabilities, seccomp);
* every Host Port value comes from the operator Secret, never inline;
* probes, resources, PDB and NetworkPolicy exist by default;
* unsafe switches (sandbox, outbound without a kill switch) fail rendering.

`helm` must be available (`HELM_BIN` or PATH).  A missing helm is a failure,
not a skip: "the chart was not verified" must never look like a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = PACKAGE_ROOT / "deployment" / "helm" / "mailhub"
VALID_DIGEST = "sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class Outcome:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


def _helm() -> str:
    candidate = os.environ.get("HELM_BIN") or shutil.which("helm")
    if not candidate:
        raise SystemExit(
            "helm_not_available: install helm or set HELM_BIN; "
            "the chart gate refuses to report an unverified chart as passed"
        )
    return candidate


def _run(helm: str, *args: str) -> Outcome:
    completed = subprocess.run([helm, *args], capture_output=True, text=True, cwd=PACKAGE_ROOT)
    return Outcome(completed.returncode, completed.stdout, completed.stderr)


def _render(helm: str, *set_args: str) -> Outcome:
    return _run(
        helm,
        "template",
        "release",
        str(CHART_DIR),
        "--set",
        f"image.digest={VALID_DIGEST}",
        *set_args,
    )


def _docs(rendered: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for document in yaml.safe_load_all(rendered):
        if isinstance(document, dict):
            documents.append(document)
    return documents


def _by_kind(documents: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [document for document in documents if document.get("kind") == kind]


def _container(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    for container in containers:
        if container["name"] == name:
            return container
    raise AssertionError(f"container {name} not found")


def _env(container: dict[str, Any]) -> dict[str, Any]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    helm = _helm()

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append((name, passed, detail))

    # 1. lint and default rendering must fail closed on a missing digest.
    default_render = _run(helm, "template", "release", str(CHART_DIR))
    check(
        "refuses_mutable_or_empty_image",
        not default_render.ok and "image.digest" in default_render.output,
        "default values must be rejected by the digest schema",
    )
    stray_tag = _run(
        helm,
        "template",
        "release",
        str(CHART_DIR),
        "--set",
        "image.digest=latest",
    )
    check(
        "refuses_tag_instead_of_digest",
        not stray_tag.ok,
        "a tag must never satisfy the digest pattern",
    )

    # 2. a valid release renders.
    rendered = _render(helm)
    check("renders_with_immutable_digest", rendered.ok, rendered.output[-400:])
    if not rendered.ok:
        return _report(checks, args.json)

    documents = _docs(rendered.stdout)
    deployments = _by_kind(documents, "Deployment")
    check("single_api_deployment", len(deployments) == 1, f"{len(deployments)} Deployment(s)")

    for kind, expected in (
        ("Service", True),
        ("ServiceAccount", True),
        ("PodDisruptionBudget", True),
        ("NetworkPolicy", True),
        ("Ingress", False),
        ("HorizontalPodAutoscaler", False),
    ):
        present = bool(_by_kind(documents, kind))
        check(f"{kind.lower()}_default_{'present' if expected else 'absent'}", present is expected)

    if deployments:
        deployment = deployments[0]
        spec = deployment["spec"]["template"]["spec"]
        container = _container(deployment, "api")
        environment = _env(container)

        check(
            "image_pinned_by_digest",
            container["image"].endswith("@" + VALID_DIGEST),
            container["image"],
        )
        check(
            "automount_service_account_token_disabled",
            spec.get("automountServiceAccountToken") is False,
        )
        pod_security = spec.get("securityContext", {})
        for key, value in (
            ("runAsNonRoot", True),
            ("seccompProfile", {"type": "RuntimeDefault"}),
        ):
            check(f"pod_security_{key}", pod_security.get(key) == value, str(pod_security.get(key)))
        container_security = container.get("securityContext", {})
        check(
            "container_allow_privilege_escalation_false",
            container_security.get("allowPrivilegeEscalation") is False,
        )
        check("container_privileged_false", container_security.get("privileged") is False)
        check(
            "container_read_only_root_filesystem",
            container_security.get("readOnlyRootFilesystem") is True,
        )
        check(
            "container_drops_all_capabilities",
            container_security.get("capabilities", {}).get("drop") == ["ALL"],
        )
        resources = container.get("resources", {})
        check(
            "resources_requests_and_limits",
            bool(resources.get("requests")) and bool(resources.get("limits")),
        )
        check("readiness_probe_configured", "httpGet" in container.get("readinessProbe", {}))
        check("liveness_probe_configured", "httpGet" in container.get("livenessProbe", {}))
        check(
            "rolling_update_keeps_capacity",
            deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0,
        )

        check(
            "env_environment_is_production",
            environment.get("MAILHUB_ENV", {}).get("value") == "production",
        )
        check(
            "env_sandbox_disabled",
            environment.get("MAILHUB_ALLOW_SANDBOX", {}).get("value") == "false",
        )
        check(
            "env_outbound_disabled_by_default",
            environment.get("MAILHUB_OUTBOUND_ENABLED", {}).get("value") == "false",
        )
        check(
            "env_rule_automation_disabled_by_default",
            environment.get("MAILHUB_RULE_AUTOMATION_ENABLED", {}).get("value") == "false",
        )
        check(
            "env_imap_disabled_by_default",
            environment.get("MAILHUB_IMAP_ENABLED", {}).get("value") == "false",
        )
        check(
            "env_smtp_size_bound_rendered",
            environment.get("MAILHUB_SMTP_MAX_SEND_BYTES", {}).get("value") == "10485760",
        )

        secret_backed = {
            name: entry
            for name, entry in environment.items()
            if "secretKeyRef" in entry.get("valueFrom", {})
        }
        inline_secret_like = [
            name
            for name, entry in environment.items()
            if "value" in entry
            and any(marker in name for marker in ("TOKEN", "SECRET", "URL", "ENDPOINT", "KEY"))
        ]
        check(
            "host_ports_come_from_secret",
            len(secret_backed) >= 15,
            f"{len(secret_backed)} secret-backed entries",
        )
        check("no_inline_secret_like_values", not inline_secret_like, ", ".join(inline_secret_like))

    # 3. the IMAP/SMTP domestic path renders the correct environment.
    imap_render = _render(
        helm,
        "--set",
        "providers.imap.enabled=true",
        "--set",
        "providers.imap.host=imap.qiye.163.com",
        "--set",
        "providers.imap.smtpHost=smtp.qiye.163.com",
    )
    check("imap_path_renders", imap_render.ok, imap_render.output[-300:])
    if imap_render.ok:
        imap_containers = _docs(imap_render.stdout)
        imap_deployments = _by_kind(imap_containers, "Deployment")
        imap_env = _env(_container(imap_deployments[0], "api")) if imap_deployments else {}
        check(
            "imap_host_rendered",
            imap_env.get("MAILHUB_IMAP_HOST", {}).get("value") == "imap.qiye.163.com",
        )
        check(
            "imap_smtp_host_rendered",
            imap_env.get("MAILHUB_SMTP_HOST", {}).get("value") == "smtp.qiye.163.com",
        )
        check(
            "imap_send_still_disabled",
            imap_env.get("MAILHUB_SMTP_SEND_ENABLED", {}).get("value") == "false",
        )

    # 4. unsafe combinations must fail rendering.
    negatives = (
        (
            "sandbox_cannot_be_enabled",
            ("--set", "runtime.allowSandbox=true"),
        ),
        (
            "outbound_requires_kill_switch_key",
            (
                "--set",
                "runtime.outboundEnabled=true",
                "--set",
                "externalSecret.keys.killSwitchEndpoint=null",
            ),
        ),
        (
            "unknown_values_key_rejected",
            ("--set", "runtime.typoSwitch=true"),
        ),
        (
            "imap_requires_host",
            ("--set", "providers.imap.enabled=true"),
        ),
        (
            "imap_send_requires_outbound",
            (
                "--set",
                "providers.imap.enabled=true",
                "--set",
                "providers.imap.host=imap.example",
                "--set",
                "providers.imap.smtpHost=smtp.example",
                "--set",
                "providers.imap.smtpSendEnabled=true",
            ),
        ),
    )
    for name, extra in negatives:
        outcome = _render(helm, *extra)
        check(name, not outcome.ok, outcome.output.splitlines()[-1] if outcome.output else "")

    # 5. ingress and autoscaling render when enabled.
    optional = _render(helm, "--set", "ingress.enabled=true", "--set", "autoscaling.enabled=true")
    check("optional_resources_render", optional.ok, optional.output[-300:])
    if optional.ok:
        kinds = {document.get("kind") for document in _docs(optional.stdout)}
        check("ingress_renders_when_enabled", "Ingress" in kinds)
        check("hpa_renders_when_enabled", "HorizontalPodAutoscaler" in kinds)

    return _report(checks, args.json)


def _report(checks: list[tuple[str, bool, str]], as_json: bool) -> int:
    failures = [(name, detail) for name, passed, detail in checks if not passed]
    if as_json:
        print(
            json.dumps(
                {
                    "passed": not failures,
                    "checks": [
                        {"name": name, "passed": passed, "detail": detail}
                        for name, passed, detail in checks
                    ],
                },
                indent=2,
            )
        )
    else:
        for name, passed, detail in checks:
            marker = "PASS" if passed else "FAIL"
            print(f"  [{marker}] {name}" + (f" - {detail}" if detail and not passed else ""))
        print(f"helm chart gate: {'ok' if not failures else str(len(failures)) + ' failure(s)'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
