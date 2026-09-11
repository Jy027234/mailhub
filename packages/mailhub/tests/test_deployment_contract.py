"""Deployment contract tests.

These assert the chart's *contract* (which Host Ports reach the container, and
through which injection point) rather than the literal text of one template
file, so moving the environment block into a shared helper cannot silently drop
an endpoint.  The rendered-manifest assertions live in
`scripts/check_helm_chart.py`, which runs `helm template`.
"""

from pathlib import Path
from typing import Any, cast

import yaml

PACKAGE_ROOT = Path(__file__).parents[1]
CHART = PACKAGE_ROOT / "deployment" / "helm" / "mailhub"

#: Host Ports the production graph must receive, as (logical name, env var).
EXPECTED_HOST_PORTS: tuple[tuple[str, str], ...] = (
    ("databaseUrl", "MAILHUB_DATABASE_URL"),
    ("kmsKeyRef", "MAILHUB_KMS_KEY_REF"),
    ("objectStoreEndpoint", "MAILHUB_OBJECT_STORE_ENDPOINT"),
    ("avScannerEndpoint", "MAILHUB_AV_SCANNER_ENDPOINT"),
    ("dlpEndpoint", "MAILHUB_DLP_ENDPOINT"),
    ("credentialBrokerEndpoint", "MAILHUB_CREDENTIAL_BROKER_ENDPOINT"),
    ("hostServiceToken", "MAILHUB_HOST_SERVICE_TOKEN"),
    ("hostIdentityEndpoint", "MAILHUB_HOST_IDENTITY_ENDPOINT"),
    ("approvalEndpoint", "MAILHUB_APPROVAL_ENDPOINT"),
    ("aiExecutionEndpoint", "MAILHUB_AI_EXECUTION_ENDPOINT"),
    ("hostActionEndpoint", "MAILHUB_HOST_ACTION_ENDPOINT"),
    ("knowledgeEndpoint", "MAILHUB_KNOWLEDGE_ENDPOINT"),
    ("agentMemoryEndpoint", "MAILHUB_AGENT_MEMORY_ENDPOINT"),
    ("eventPublisherEndpoint", "MAILHUB_EVENT_PUBLISHER_ENDPOINT"),
    ("telemetryEndpoint", "MAILHUB_TELEMETRY_ENDPOINT"),
    ("quotaEndpoint", "MAILHUB_QUOTA_ENDPOINT"),
    ("providerSubscriptionEndpoint", "MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT"),
    ("providerNotificationVerifierEndpoint", "MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT"),
    ("killSwitchEndpoint", "MAILHUB_KILL_SWITCH_ENDPOINT"),
)


def _chart_values() -> dict[str, Any]:
    loaded = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    return cast(dict[str, Any], loaded)


def _declared_host_ports() -> dict[str, str]:
    values = _chart_values()
    external = cast(dict[str, Any], values["externalSecret"])
    keys = cast(dict[str, Any], external["keys"])
    return {str(logical): str(name) for logical, name in keys.items()}


def _template(name: str) -> str:
    return (CHART / "templates" / name).read_text(encoding="utf-8")


def test_helm_values_declare_every_host_port_environment_variable() -> None:
    declared = _declared_host_ports()

    for logical, env_name in EXPECTED_HOST_PORTS:
        assert declared.get(logical) == env_name, logical


def test_helm_declared_host_ports_are_valid_environment_names() -> None:
    for logical, env_name in _declared_host_ports().items():
        assert env_name.startswith("MAILHUB_"), logical
        assert env_name.isupper(), logical
        assert " " not in env_name, logical


def test_helm_secret_helper_injects_every_declared_host_port() -> None:
    helpers = _template("_helpers.tpl")

    # One generic injection point replaces hand-maintained per-endpoint copies,
    # so adding an endpoint to values.yaml cannot be forgotten in the template.
    assert "range $logical, $key := .Values.externalSecret.keys" in helpers
    assert "name: {{ $key }}" in helpers
    assert "key: {{ $key }}" in helpers
    assert "secretKeyRef:" in helpers
    assert "name: {{ $secret }}" in helpers
    assert 'include "mailhub.secretEnv" .' in helpers


def test_helm_deployment_consumes_the_shared_environment_helper() -> None:
    deployment = _template("deployment.yaml")

    assert 'include "mailhub.env" .' in deployment
    assert 'include "mailhub.validate" .' in deployment
    # No endpoint may be hand-written back into the deployment template.
    for _, env_name in EXPECTED_HOST_PORTS:
        assert f"name: {env_name}" not in deployment, env_name


def test_helm_values_schema_locks_down_unsafe_switches() -> None:
    schema = (CHART / "values.schema.json").read_text(encoding="utf-8")

    assert "additionalProperties" in schema
    assert '"const": false' in schema
    assert "^sha256:[a-f0-9]{64}$" in schema


def test_image_uses_packaged_standalone_entrypoint() -> None:
    dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert 'CMD ["mailhub-api", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile
    assert 'python -m pip install --no-cache-dir ".[postgres]"' in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert "COPY scripts/apply_migrations.py ./scripts/apply_migrations.py" in dockerfile
