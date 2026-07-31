from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1]


def test_helm_production_graph_injects_event_publisher_endpoint() -> None:
    values = (PACKAGE_ROOT / "deployment" / "helm" / "mailhub" / "values.yaml").read_text(
        encoding="utf-8"
    )
    template = (
        PACKAGE_ROOT / "deployment" / "helm" / "mailhub" / "templates" / "deployment.yaml"
    ).read_text(encoding="utf-8")

    assert "eventPublisherEndpoint: MAILHUB_EVENT_PUBLISHER_ENDPOINT" in values
    assert "name: MAILHUB_EVENT_PUBLISHER_ENDPOINT" in template
    assert ".Values.externalSecret.keys.eventPublisherEndpoint" in template


def test_helm_production_graph_injects_provider_subscription_endpoint() -> None:
    values = (PACKAGE_ROOT / "deployment" / "helm" / "mailhub" / "values.yaml").read_text(
        encoding="utf-8"
    )
    template = (
        PACKAGE_ROOT / "deployment" / "helm" / "mailhub" / "templates" / "deployment.yaml"
    ).read_text(encoding="utf-8")

    assert "providerSubscriptionEndpoint: MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT" in values
    assert "name: MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT" in template
    assert ".Values.externalSecret.keys.providerSubscriptionEndpoint" in template


def test_helm_production_graph_injects_provider_notification_verifier_endpoint() -> None:
    values = (PACKAGE_ROOT / "deployment" / "helm" / "mailhub" / "values.yaml").read_text(
        encoding="utf-8"
    )
    template = (
        PACKAGE_ROOT / "deployment" / "helm" / "mailhub" / "templates" / "deployment.yaml"
    ).read_text(encoding="utf-8")

    assert (
        "providerNotificationVerifierEndpoint: "
        "MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT" in values
    )
    assert "name: MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT" in template
    assert ".Values.externalSecret.keys.providerNotificationVerifierEndpoint" in template


def test_helm_production_graph_injects_host_service_token() -> None:
    values = (PACKAGE_ROOT / "deployment" / "helm" / "mailhub" / "values.yaml").read_text(
        encoding="utf-8"
    )
    template = (
        PACKAGE_ROOT / "deployment" / "helm" / "mailhub" / "templates" / "deployment.yaml"
    ).read_text(encoding="utf-8")

    assert "hostServiceToken: MAILHUB_HOST_SERVICE_TOKEN" in values
    assert "name: MAILHUB_HOST_SERVICE_TOKEN" in template
    assert ".Values.externalSecret.keys.hostServiceToken" in template


def test_image_uses_packaged_standalone_entrypoint() -> None:
    dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert 'CMD ["mailhub-api", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile
    assert 'python -m pip install --no-cache-dir ".[postgres]"' in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert "COPY scripts/apply_migrations.py ./scripts/apply_migrations.py" in dockerfile
