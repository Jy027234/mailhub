from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from mailhub.oauth import (
    InMemoryOAuthStateStore,
    OAuthAuthorizationService,
    OAuthProvider,
    validate_granted_scopes,
)


def _service() -> OAuthAuthorizationService:
    return OAuthAuthorizationService(
        signing_secret=b"s" * 32,
        state_store=InMemoryOAuthStateStore(),
        allowed_redirect_uris={
            OAuthProvider.GMAIL: ("https://app.example.test/mail/callback",),
        },
    )


@pytest.mark.asyncio
async def test_oauth_state_is_pkce_bound_and_one_time() -> None:
    current = datetime(2026, 7, 28, tzinfo=UTC)
    service = _service()
    request = await service.begin(
        provider=OAuthProvider.GMAIL,
        tenant_id="tenant-1",
        subject_id="user-1",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        client_id="client-1",
        redirect_uri="https://app.example.test/mail/callback",
        scopes=("openid", "https://www.googleapis.com/auth/gmail.readonly"),
        now=current,
    )
    assert "code_challenge_method=S256" in request.authorization_url
    context = await service.consume(
        provider=OAuthProvider.GMAIL,
        state=request.state,
        redirect_uri="https://app.example.test/mail/callback",
        now=current + timedelta(minutes=1),
    )
    assert context.tenant_id == "tenant-1"
    assert context.code_verifier == request.code_verifier
    assert context.requested_scopes == (
        "openid",
        "https://www.googleapis.com/auth/gmail.readonly",
    )
    with pytest.raises(ValueError, match="replayed_or_missing"):
        await service.consume(
            provider=OAuthProvider.GMAIL,
            state=request.state,
            redirect_uri="https://app.example.test/mail/callback",
            now=current + timedelta(minutes=1),
        )


@pytest.mark.asyncio
async def test_oauth_state_observation_ref_is_opaque_and_stable() -> None:
    service = _service()
    request = await service.begin(
        provider=OAuthProvider.GMAIL,
        tenant_id="tenant-1",
        subject_id="user-1",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        client_id="client-1",
        redirect_uri="https://app.example.test/mail/callback",
        scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )

    first = service.state_observation_ref(request.state)
    second = service.state_observation_ref(request.state)

    assert first == second
    assert len(first) == 64
    assert request.state not in first


@pytest.mark.asyncio
async def test_oauth_authorize_requires_real_provider_read_only_scopes() -> None:
    service = _service()
    with pytest.raises(ValueError, match="required_scope_missing"):
        await service.begin(
            provider=OAuthProvider.GMAIL,
            tenant_id="tenant-1",
            subject_id="user-1",
            authorization_endpoint="https://accounts.google.com/auth",
            client_id="client-1",
            redirect_uri="https://app.example.test/mail/callback",
            scopes=("openid",),
        )
    with pytest.raises(ValueError, match="write_scope_disallowed"):
        await service.begin(
            provider=OAuthProvider.GMAIL,
            tenant_id="tenant-1",
            subject_id="user-1",
            authorization_endpoint="https://accounts.google.com/auth",
            client_id="client-1",
            redirect_uri="https://app.example.test/mail/callback",
            scopes=(
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            ),
        )


def test_oauth_granted_scope_validation_rejects_scope_drift_and_write_access() -> None:
    requested = ("https://www.googleapis.com/auth/gmail.readonly",)
    assert (
        validate_granted_scopes(
            OAuthProvider.GMAIL,
            ["https://www.googleapis.com/auth/gmail.readonly"],
            requested,
        )
        == requested
    )
    with pytest.raises(ValueError, match="scope_escalation"):
        validate_granted_scopes(
            OAuthProvider.GMAIL,
            ["https://www.googleapis.com/auth/gmail.readonly", "openid"],
            requested,
        )
    with pytest.raises(ValueError, match="write_scope_disallowed"):
        validate_granted_scopes(
            OAuthProvider.GMAIL,
            [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            ],
            requested,
        )


@pytest.mark.asyncio
async def test_oauth_rejects_redirect_and_provider_binding() -> None:
    service = _service()
    with pytest.raises(ValueError, match="not_allowlisted"):
        await service.begin(
            provider=OAuthProvider.GMAIL,
            tenant_id="tenant-1",
            subject_id="user-1",
            authorization_endpoint="https://accounts.google.com/auth",
            client_id="client-1",
            redirect_uri="https://evil.example.test/callback",
            scopes=("openid",),
        )
    request = await service.begin(
        provider=OAuthProvider.GMAIL,
        tenant_id="tenant-1",
        subject_id="user-1",
        authorization_endpoint="https://accounts.google.com/auth",
        client_id="client-1",
        redirect_uri="https://app.example.test/mail/callback",
        scopes=("openid", "https://www.googleapis.com/auth/gmail.readonly"),
    )
    with pytest.raises(ValueError, match="provider_mismatch"):
        await service.consume(
            provider=OAuthProvider.MICROSOFT_GRAPH,
            state=request.state,
            redirect_uri="https://app.example.test/mail/callback",
        )


@pytest.mark.asyncio
async def test_oauth_registration_can_pin_endpoint_client_and_scopes() -> None:
    service = OAuthAuthorizationService(
        signing_secret=b"s" * 32,
        state_store=InMemoryOAuthStateStore(),
        allowed_redirect_uris={
            OAuthProvider.GMAIL: ("https://app.example.test/mail/callback",),
        },
        allowed_authorization_endpoints={
            OAuthProvider.GMAIL: ("https://accounts.google.com/o/oauth2/v2/auth",),
        },
        allowed_client_ids={OAuthProvider.GMAIL: ("client-1",)},
        allowed_scopes={
            OAuthProvider.GMAIL: (
                "openid",
                "https://www.googleapis.com/auth/gmail.readonly",
            )
        },
    )
    request = await service.begin(
        provider=OAuthProvider.GMAIL,
        tenant_id="tenant-1",
        subject_id="user-1",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        client_id="client-1",
        redirect_uri="https://app.example.test/mail/callback",
        scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    assert request.provider is OAuthProvider.GMAIL
    with pytest.raises(ValueError, match="endpoint_not_allowlisted"):
        await service.begin(
            provider=OAuthProvider.GMAIL,
            tenant_id="tenant-1",
            subject_id="user-1",
            authorization_endpoint="https://accounts.google.com/other",
            client_id="client-1",
            redirect_uri="https://app.example.test/mail/callback",
            scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        )
    with pytest.raises(ValueError, match="scope_not_allowlisted"):
        await service.begin(
            provider=OAuthProvider.GMAIL,
            tenant_id="tenant-1",
            subject_id="user-1",
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            client_id="client-1",
            redirect_uri="https://app.example.test/mail/callback",
            scopes=("mail.google.com",),
        )


@pytest.mark.asyncio
async def test_oauth_rejects_ambiguous_redirect_uri_shapes_before_state_creation() -> None:
    service = _service()
    invalid_redirects = (
        "https://app.example.test/mail/callback?next=mail",
        "https://app.example.test:443/mail/callback",
        "https://user:password@app.example.test/mail/callback",
        "https://app.example.test",
    )
    for redirect_uri in invalid_redirects:
        with pytest.raises(ValueError):
            await service.begin(
                provider=OAuthProvider.GMAIL,
                tenant_id="tenant-1",
                subject_id="subject-1",
                authorization_endpoint="https://accounts.google.com/auth",
                client_id="client-1",
                redirect_uri=redirect_uri,
                scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            )


@pytest.mark.asyncio
async def test_oauth_allows_explicit_loopback_port_for_local_development() -> None:
    service = OAuthAuthorizationService(
        signing_secret=b"s" * 32,
        state_store=InMemoryOAuthStateStore(),
        allowed_redirect_uris={
            OAuthProvider.GMAIL: ("http://127.0.0.1:5180/mail/oauth/gmail/callback",),
        },
    )
    request = await service.begin(
        provider=OAuthProvider.GMAIL,
        tenant_id="tenant-1",
        subject_id="subject-1",
        authorization_endpoint="https://accounts.google.com/auth",
        client_id="client-1",
        redirect_uri="http://127.0.0.1:5180/mail/oauth/gmail/callback",
        scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    )
    assert "127.0.0.1%3A5180" in request.authorization_url


@pytest.mark.asyncio
async def test_oauth_reauthorization_state_binds_connection_and_revision() -> None:
    service = _service()
    connection_id = uuid4()
    request = await service.begin(
        provider=OAuthProvider.GMAIL,
        tenant_id="tenant-1",
        subject_id="user-1",
        authorization_endpoint="https://accounts.google.com/auth",
        client_id="client-1",
        redirect_uri="https://app.example.test/mail/callback",
        scopes=("openid", "https://www.googleapis.com/auth/gmail.readonly"),
        connection_id=connection_id,
        expected_revision=4,
    )
    assert request.connection_id == connection_id
    assert request.expected_revision == 4
    context = await service.consume(
        provider=OAuthProvider.GMAIL,
        state=request.state,
        redirect_uri="https://app.example.test/mail/callback",
    )
    assert context.connection_id == connection_id
    assert context.expected_revision == 4

    with pytest.raises(ValueError, match="connection_id_required"):
        await service.begin(
            provider=OAuthProvider.GMAIL,
            tenant_id="tenant-1",
            subject_id="user-1",
            authorization_endpoint="https://accounts.google.com/auth",
            client_id="client-1",
            redirect_uri="https://app.example.test/mail/callback",
            scopes=("openid",),
            expected_revision=4,
        )
