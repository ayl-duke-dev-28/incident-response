from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from incident_response.auth import AuthPolicy
from incident_response.config import Settings
from incident_response.main import create_app


class FakeOIDCClient:
    def __init__(self, groups: list[str]) -> None:
        self.groups = groups
        self.redirect_uri = ""

    async def authorize_redirect(self, request, redirect_uri):
        self.redirect_uri = str(redirect_uri)
        return RedirectResponse("https://identity.example/authorize", status_code=302)

    async def authorize_access_token(self, request):
        return {
            "access_token": "provider-token-must-not-enter-session",
            "userinfo": {
                "sub": "operator-123",
                "email": "operator@example.com",
                "name": "Operator",
                "groups": self.groups,
            },
        }


def _settings(tmp_path, runbooks_dir) -> Settings:
    return Settings(
        _env_file=None,
        llm_mode="mock",
        db_path=tmp_path / "incidents.db",
        runbooks_dir=runbooks_dir,
        auth_mode="oidc",
        session_secret="s" * 32,
        session_https_only=False,
        oidc_client_id="incident-response",
        oidc_client_secret="client-secret",
        oidc_metadata_url="https://identity.example/.well-known/openid-configuration",
    )


def _policy() -> AuthPolicy:
    return AuthPolicy(
        viewer_groups={"incident-viewers"},
        responder_groups={"incident-responders"},
        admin_groups={"incident-admins"},
        token_factory=lambda: "fixed-csrf-token",
    )


def _authenticated_client(tmp_path, runbooks_dir, group: str) -> TestClient:
    app = create_app(
        _settings(tmp_path, runbooks_dir),
        oidc_client=FakeOIDCClient([group]),
        auth_policy=_policy(),
    )
    client = TestClient(app)
    response = client.get("/auth/callback", follow_redirects=False)
    assert response.status_code == 303
    return client


def test_console_redirects_to_oidc_and_callback_establishes_then_clears_session(
    tmp_path, runbooks_dir
):
    oidc = FakeOIDCClient(["incident-viewers"])
    app = create_app(
        _settings(tmp_path, runbooks_dir),
        oidc_client=oidc,
        auth_policy=_policy(),
    )

    with TestClient(app) as client:
        unauthenticated = client.get("/console", follow_redirects=False)
        login = client.get("/auth/login", follow_redirects=False)
        callback = client.get("/auth/callback", follow_redirects=False)
        authenticated = client.get("/console")
        logout = client.post(
            "/auth/logout",
            headers={"x-csrf-token": "fixed-csrf-token"},
            follow_redirects=False,
        )
        after_logout = client.get("/console", follow_redirects=False)

    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"].startswith("/auth/login")
    assert login.status_code == 302
    assert login.headers["location"] == "https://identity.example/authorize"
    assert oidc.redirect_uri.endswith("/auth/callback")
    assert callback.status_code == 303
    assert callback.headers["location"] == "/console"
    assert authenticated.status_code == 200
    assert "provider-token-must-not-enter-session" not in authenticated.text
    assert logout.status_code == 303
    assert after_logout.status_code == 303


def test_oidc_callback_rejects_user_without_mapped_group(tmp_path, runbooks_dir):
    app = create_app(
        _settings(tmp_path, runbooks_dir),
        oidc_client=FakeOIDCClient(["engineering"]),
        auth_policy=_policy(),
    )

    with TestClient(app) as client:
        response = client.get("/auth/callback")

    assert response.status_code == 403
    assert response.json() == {"detail": "OIDC user is not authorized"}


def test_viewer_can_read_but_cannot_replay_dead_letters(tmp_path, runbooks_dir):
    with _authenticated_client(tmp_path, runbooks_dir, "incident-viewers") as client:
        assert client.get("/incidents").status_code == 200
        forbidden = client.post(
            "/dead-letters/missing/replay",
            headers={"x-csrf-token": "fixed-csrf-token"},
        )

    assert forbidden.status_code == 403


def test_responder_requires_csrf_and_cannot_approve_remediation(tmp_path, runbooks_dir):
    with _authenticated_client(tmp_path, runbooks_dir, "incident-responders") as client:
        missing_csrf = client.post(
            "/alerts/missing/resolve",
            json={"resolution_note": "fixed"},
        )
        authorized = client.post(
            "/alerts/missing/resolve",
            json={"resolution_note": "fixed"},
            headers={"x-csrf-token": "fixed-csrf-token"},
        )
        approve = client.post(
            "/alerts/missing/remediation/approve",
            json={"decided_by": "ignored@example.com"},
            headers={"x-csrf-token": "fixed-csrf-token"},
        )

    assert missing_csrf.status_code == 403
    assert authorized.status_code == 404
    assert approve.status_code == 403


def test_admin_can_reach_privileged_operator_actions(tmp_path, runbooks_dir):
    with _authenticated_client(tmp_path, runbooks_dir, "incident-admins") as client:
        replay = client.post(
            "/dead-letters/missing/replay",
            headers={"x-csrf-token": "fixed-csrf-token"},
        )
        approve = client.post(
            "/alerts/missing/remediation/approve",
            json={"decided_by": "ignored@example.com"},
            headers={"x-csrf-token": "fixed-csrf-token"},
        )

    assert replay.status_code == 404
    assert approve.status_code == 404
