"""Offline configuration gate for the controlled Gmail/Graph activation wave.

This command deliberately does not contact a provider.  It checks that the
non-secret OAuth, scope, redirect and host-boundary configuration is complete
before an operator enables a real connector.  A green report is a preflight
result, not proof of OAuth consent, mailbox access or long-running sync.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

_PROVIDERS = ("gmail", "microsoft_graph")
_READ_ONLY_ENV = {
    "gmail": "MAILHUB_GMAIL_READ_ONLY",
    "microsoft_graph": "MAILHUB_MICROSOFT_GRAPH_READ_ONLY",
}
_PUSH_ENV = {
    "gmail": "MAILHUB_GMAIL_PUSH_ENABLED",
    "microsoft_graph": "MAILHUB_MICROSOFT_GRAPH_PUSH_ENABLED",
}
_ENABLED_ENV = {
    "gmail": "MAILHUB_GMAIL_ENABLED",
    "microsoft_graph": "MAILHUB_MICROSOFT_GRAPH_ENABLED",
}
_CONFIG_ENV = {
    "gmail": {
        "client_id": "MAILHUB_GMAIL_CLIENT_ID",
        "authorization_endpoint": "MAILHUB_GMAIL_AUTHORIZATION_ENDPOINT",
        "redirect_uri": "MAILHUB_GMAIL_REDIRECT_URI",
        "redirect_uris": "MAILHUB_GMAIL_REDIRECT_URIS",
        "scopes": "MAILHUB_GMAIL_SCOPES",
    },
    "microsoft_graph": {
        "client_id": "MAILHUB_GRAPH_CLIENT_ID",
        "authorization_endpoint": "MAILHUB_GRAPH_AUTHORIZATION_ENDPOINT",
        "redirect_uri": "MAILHUB_GRAPH_REDIRECT_URI",
        "redirect_uris": "MAILHUB_GRAPH_REDIRECT_URIS",
        "scopes": "MAILHUB_GRAPH_SCOPES",
        "authority_tenant": "MAILHUB_GRAPH_AUTHORITY_TENANT",
    },
}
_REQUIRED_SCOPES = {
    "gmail": {"https://www.googleapis.com/auth/gmail.readonly"},
    "microsoft_graph": {"mail.read", "offline_access"},
}
_WRITE_SCOPE_MARKERS = {
    "gmail": (
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.settings.",
    ),
    "microsoft_graph": (
        "mail.send",
        "mail.readwrite",
        "mail.manage",
        "mail.fullaccessasuser",
    ),
}
_AUTH_HOSTS = {
    "gmail": {"accounts.google.com"},
    "microsoft_graph": {"login.microsoftonline.com"},
}
_GRAPH_TENANT_RE = re.compile(r"(?:common|organizations|consumers|[0-9a-fA-F-]{8,64})")
_GRAPH_TENANT_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _env_bool(name: str, *, default: bool) -> tuple[bool, str | None]:
    value = os.getenv(name)
    if value is None:
        return default, None
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True, None
    if normalized in {"0", "false", "no", "off"}:
        return False, None
    return default, f"{name}_invalid_boolean"


def _split_scopes(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(dict.fromkeys(item for item in value.replace(",", " ").split() if item))


def _set_value(name: str) -> str:
    return "<set>" if os.getenv(name) else "<missing>"


def _endpoint_ok(value: str | None, *, allowed_hosts: set[str]) -> bool:
    if not value:
        return False
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and hostname in {host.casefold() for host in allowed_hosts}
        and bool(parsed.path)
        and parsed.username is None
        and parsed.password is None
        and port is None
        and not parsed.query
        and not parsed.fragment
    )


def _redirect_ok(value: str | None) -> bool:
    if not value:
        return False
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return False
    local_http = parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
    return (
        (parsed.scheme == "https" or local_http)
        and bool(hostname)
        and not parsed.username
        and not parsed.password
        # Public HTTPS registrations use the canonical host/port form; local
        # development callbacks may bind an explicit loopback port.
        and (port is None or hostname in {"localhost", "127.0.0.1", "::1"})
        and bool(parsed.path)
        and not parsed.query
        and not parsed.fragment
    )


def _host_endpoint_ok(value: str | None) -> bool:
    """Validate a Host Port base URL before any adapter can make a request."""

    if not value or any(ord(char) < 33 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        # Force parsing of a malformed numeric port instead of letting the
        # HTTP adapter discover it after the activation gate has passed.
        port = parsed.port
    except ValueError:
        return False
    local_http = parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
    return (
        bool(hostname)
        and (parsed.scheme == "https" or local_http)
        and parsed.username is None
        and parsed.password is None
        and (port is None or 1 <= port <= 65535)
        and not parsed.query
        and not parsed.fragment
    )


def _graph_authority_ok(endpoint: str | None, authority_tenant: str | None) -> bool:
    if not endpoint or not authority_tenant or _GRAPH_TENANT_RE.fullmatch(authority_tenant) is None:
        return False
    parsed = urlsplit(endpoint)
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    return bool(segments) and segments[0] == authority_tenant


def _provider_report(provider: str) -> dict[str, object]:
    enabled, enabled_error = _env_bool(_ENABLED_ENV[provider], default=False)
    read_only, read_only_error = _env_bool(_READ_ONLY_ENV[provider], default=True)
    push_enabled, push_error = _env_bool(_PUSH_ENV[provider], default=False)
    missing: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    config: dict[str, str] = {
        "enabled": "true" if enabled else "false",
        "read_only": "true" if read_only else "false",
        "push_enabled": "true" if push_enabled else "false",
        "oauth_state_signing_secret": _set_value("MAILHUB_OAUTH_STATE_SIGNING_SECRET"),
    }
    if enabled_error:
        errors.append(enabled_error)
    if read_only_error:
        errors.append(read_only_error)
    if push_error:
        errors.append(push_error)
    if not enabled:
        warnings.append("provider_disabled")
        return {
            "provider": provider,
            "phase": "preflight",
            "enabled": False,
            "ready": not errors,
            "readiness": "blocked" if errors else "disabled",
            "real_provider_evidence": False,
            "missing": missing,
            "errors": errors,
            "warnings": warnings,
            "config": config,
            "evidence": ["offline_config_contract"],
        }

    if not read_only:
        errors.append("read_only_gate_must_be_true_for_m2_m3")
    if push_enabled:
        for dependency in (
            "MAILHUB_PROVIDER_SUBSCRIPTION_ENDPOINT",
            "MAILHUB_PROVIDER_NOTIFICATION_VERIFIER_ENDPOINT",
        ):
            config_key = dependency.removeprefix("MAILHUB_PROVIDER_").lower()
            config[config_key] = _set_value(dependency)
            if not os.getenv(dependency):
                missing.append(dependency)
            elif not _host_endpoint_ok(os.getenv(dependency)):
                errors.append(f"{dependency}_must_be_https_or_loopback")
    signing_secret = os.getenv("MAILHUB_OAUTH_STATE_SIGNING_SECRET", "")
    if len(signing_secret.encode("utf-8")) < 32:
        missing.append("MAILHUB_OAUTH_STATE_SIGNING_SECRET(min_32_bytes)")
    if not os.getenv("MAILHUB_CREDENTIAL_BROKER_ENDPOINT"):
        missing.append("MAILHUB_CREDENTIAL_BROKER_ENDPOINT")
    config["host_service_token"] = _set_value("MAILHUB_HOST_SERVICE_TOKEN")
    if len(os.getenv("MAILHUB_HOST_SERVICE_TOKEN", "")) < 32:
        missing.append("MAILHUB_HOST_SERVICE_TOKEN(min_32_chars)")
    if not os.getenv("MAILHUB_HOST_IDENTITY_ENDPOINT"):
        missing.append("MAILHUB_HOST_IDENTITY_ENDPOINT")
    if not os.getenv("MAILHUB_KMS_KEY_REF"):
        missing.append("MAILHUB_KMS_KEY_REF")

    names = _CONFIG_ENV[provider]
    for key, name in names.items():
        value = os.getenv(name)
        config[key] = _set_value(name)
        # The plural allowlist is optional for backwards-compatible single
        # redirect deployments; when absent, the singular URI remains the
        # only configured entry and is still validated below.
        if not value and key != "redirect_uris":
            missing.append(name)

    endpoint = os.getenv(names["authorization_endpoint"])
    if endpoint and not _endpoint_ok(endpoint, allowed_hosts=_AUTH_HOSTS[provider]):
        errors.append(f"{names['authorization_endpoint']}_host_or_tls_invalid")
    redirect = os.getenv(names["redirect_uri"])
    if redirect and not _redirect_ok(redirect):
        errors.append(f"{names['redirect_uri']}_must_be_https_or_localhost")
    configured_redirects = _split_scopes(os.getenv(names["redirect_uris"]))
    if configured_redirects:
        config["redirect_uri_count"] = str(len(configured_redirects))
        for index, configured_redirect in enumerate(configured_redirects):
            if not _redirect_ok(configured_redirect):
                errors.append(f"{names['redirect_uris']}[{index}]_must_be_https_or_localhost")
        if redirect and redirect not in configured_redirects:
            errors.append(f"{names['redirect_uri']}_not_in_redirect_allowlist")
    else:
        config["redirect_uri_count"] = "1" if redirect else "0"
    scopes = _split_scopes(os.getenv(names["scopes"]))
    config["scope_count"] = str(len(scopes))
    normalized_scopes = tuple(scope.casefold() for scope in scopes)
    missing_scopes = _REQUIRED_SCOPES[provider].difference(normalized_scopes)
    if missing_scopes:
        missing.append(f"{names['scopes']} missing {','.join(sorted(missing_scopes))}")
    if any(
        any(
            scope == marker or scope.startswith(marker) for marker in _WRITE_SCOPE_MARKERS[provider]
        )
        for scope in normalized_scopes
    ):
        errors.append("write_scope_disallowed_for_read_only")
    if provider == "microsoft_graph":
        authority_tenant = os.getenv(names["authority_tenant"], "")
        if authority_tenant and any(char.isspace() for char in authority_tenant):
            errors.append("MAILHUB_GRAPH_AUTHORITY_TENANT_invalid")
        if not _graph_authority_ok(endpoint, authority_tenant):
            errors.append("MAILHUB_GRAPH_AUTHORITY_TENANT_endpoint_mismatch")
        if _GRAPH_TENANT_UUID_RE.fullmatch(authority_tenant) is None:
            errors.append("MAILHUB_GRAPH_AUTHORITY_TENANT_specific_tenant_required_for_beta")
    warnings.extend(
        (
            "oauth_verification_and_consent_not_evidenced",
            "isolated_mailbox_access_not_evidenced",
            "host_broker_secret_readiness_verified_out_of_process",
            "watch_or_change_notification_not_enabled_in_read_only_preflight",
        )
    )
    return {
        "provider": provider,
        "phase": "preflight",
        "enabled": True,
        "ready": not missing and not errors,
        "readiness": "config_ready" if not missing and not errors else "blocked",
        "real_provider_evidence": False,
        "missing": missing,
        "errors": errors,
        "warnings": warnings,
        "config": config,
        "evidence": ["offline_config_contract"],
    }


def build_report(providers: Iterable[str]) -> dict[str, object]:
    selected = tuple(providers)
    reports = tuple(_provider_report(provider) for provider in selected)
    readiness_values = {str(report["readiness"]) for report in reports}
    if "blocked" in readiness_values:
        readiness = "blocked"
    elif "config_ready" in readiness_values:
        readiness = "config_ready"
    else:
        readiness = "disabled"
    return {
        "schema_version": "mailhub.provider_preflight.v1",
        "network_access": False,
        "ready": all(bool(report["ready"]) for report in reports),
        # ``ready`` is retained for legacy callers where a disabled provider
        # is a non-error.  Activation and CI callers must use this explicit
        # gate so disabled is never mistaken for config readiness.
        "config_ready": all(report["readiness"] == "config_ready" for report in reports),
        "readiness": readiness,
        "real_provider_evidence": False,
        "providers": reports,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("all", *_PROVIDERS),
        default="all",
        help="provider to check; checks are offline and never call a provider",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    parser.add_argument(
        "--require-config-ready",
        action="store_true",
        help="return exit code 2 unless every selected provider reaches config_ready",
    )
    args = parser.parse_args(argv)
    providers = _PROVIDERS if args.provider == "all" else (args.provider,)
    report = build_report(providers)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"MailHub provider preflight (network_access={report['network_access']})")
        reports = report["providers"]
        assert isinstance(reports, (list, tuple))
        for item in reports:
            assert isinstance(item, Mapping)
            state = str(item["readiness"])
            missing = item.get("missing", ())
            if not isinstance(missing, (list, tuple)):
                missing = ()
            missing_text = ",".join(str(value) for value in missing) or "-"
            print(f"- {item['provider']}: {state}; missing={missing_text}")
    passed = report["config_ready"] if args.require_config_ready else report["ready"]
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
