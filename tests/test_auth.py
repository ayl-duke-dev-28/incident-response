from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from incident_response.auth import AuthPolicy, Principal, Role, role_for_groups
from incident_response.config import Settings


def _production_settings(**overrides) -> dict[str, object]:
    return {
        "_env_file": None,
        "environment": "production",
        "database_url": "postgresql+psycopg://app:secret@db/incidents",
        "redis_url": "rediss://redis:6379/0",
        "auth_mode": "oidc",
        "session_secret": "s" * 32,
        "oidc_client_id": "incident-response",
        "oidc_client_secret": "client-secret",
        "oidc_metadata_url": "https://identity.example/.well-known/openid-configuration",
        **overrides,
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"auth_mode": "disabled"}, "OIDC"),
        ({"session_secret": "short"}, "session secret"),
        ({"session_https_only": False}, "secure session cookies"),
        ({"oidc_client_id": ""}, "client credentials"),
        ({"oidc_client_secret": ""}, "client credentials"),
        ({"oidc_metadata_url": "http://identity.example/config"}, "HTTPS"),
    ],
)
def test_production_authentication_fails_closed(overrides, message):
    with pytest.raises(ValidationError, match=message):
        Settings(**_production_settings(**overrides))


def test_role_mapping_uses_highest_matching_oidc_group():
    role = role_for_groups(
        {"engineering", "incident-responders", "incident-admins"},
        viewer_groups={"incident-viewers"},
        responder_groups={"incident-responders"},
        admin_groups={"incident-admins"},
    )

    assert role == Role.ADMIN


def test_role_mapping_rejects_user_without_an_authorized_group():
    assert (
        role_for_groups(
            {"engineering"},
            viewer_groups={"incident-viewers"},
            responder_groups={"incident-responders"},
            admin_groups={"incident-admins"},
        )
        is None
    )


def test_role_permissions_are_ordered():
    viewer = Principal(subject="viewer", email="v@example.com", role=Role.VIEWER)
    responder = Principal(subject="responder", email="r@example.com", role=Role.RESPONDER)
    admin = Principal(subject="admin", email="a@example.com", role=Role.ADMIN)

    assert viewer.permits(Role.VIEWER)
    assert not viewer.permits(Role.RESPONDER)
    assert responder.permits(Role.VIEWER)
    assert responder.permits(Role.RESPONDER)
    assert not responder.permits(Role.ADMIN)
    assert admin.permits(Role.ADMIN)


def test_oidc_session_contains_identity_role_expiry_and_csrf_but_no_tokens():
    policy = AuthPolicy(
        viewer_groups={"incident-viewers"},
        responder_groups={"incident-responders"},
        admin_groups={"incident-admins"},
        session_seconds=3600,
        clock=lambda: 1_000.0,
        token_factory=lambda: "csrf-token",
    )
    token = {
        "access_token": "must-not-enter-session",
        "refresh_token": "must-not-enter-session",
        "userinfo": {
            "sub": "oidc-123",
            "email": "operator@example.com",
            "name": "Operator",
            "groups": ["incident-responders"],
        },
    }

    session = policy.session_from_oidc_token(token)

    assert session == {
        "sub": "oidc-123",
        "email": "operator@example.com",
        "name": "Operator",
        "role": "responder",
        "expires_at": 4_600.0,
        "csrf_token": "csrf-token",
    }
    assert "access_token" not in session
    assert "refresh_token" not in session


def test_session_expiry_and_csrf_are_enforced():
    now = {"value": 100.0}
    policy = AuthPolicy(
        viewer_groups={"incident-viewers"},
        responder_groups=set(),
        admin_groups=set(),
        session_seconds=60,
        clock=lambda: now["value"],
        token_factory=lambda: "csrf-token",
    )
    session = policy.session_from_oidc_token(
        {
            "userinfo": {
                "sub": "oidc-viewer",
                "email": "viewer@example.com",
                "groups": ["incident-viewers"],
            }
        }
    )

    assert policy.principal_from_session(session) is not None
    assert policy.verify_csrf(session, "csrf-token")
    assert not policy.verify_csrf(session, "wrong")
    assert not policy.verify_csrf(session, "")

    now["value"] = 161.0
    assert policy.principal_from_session(session) is None


def test_oidc_session_rejects_missing_subject_or_authorized_role():
    policy = AuthPolicy(
        viewer_groups={"incident-viewers"},
        responder_groups=set(),
        admin_groups=set(),
    )

    with pytest.raises(ValueError, match="subject"):
        policy.session_from_oidc_token(
            {"userinfo": {"email": "viewer@example.com", "groups": ["incident-viewers"]}}
        )
    with pytest.raises(PermissionError, match="authorized OIDC group"):
        policy.session_from_oidc_token(
            {"userinfo": {"sub": "outsider", "groups": ["engineering"]}}
        )
