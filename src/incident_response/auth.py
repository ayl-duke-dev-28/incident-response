"""OIDC identity, role mapping, signed-session payloads, and CSRF policy."""

from __future__ import annotations

import hmac
import hashlib
import json
import secrets
import time
from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException, Request


class Role(str, Enum):
    VIEWER = "viewer"
    RESPONDER = "responder"
    ADMIN = "admin"


_ROLE_RANK = {Role.VIEWER: 1, Role.RESPONDER: 2, Role.ADMIN: 3}


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    role: Role
    name: str = ""

    def permits(self, required: Role) -> bool:
        return _ROLE_RANK[self.role] >= _ROLE_RANK[required]


def role_for_groups(
    groups: Set[str],
    *,
    viewer_groups: Set[str],
    responder_groups: Set[str],
    admin_groups: Set[str],
) -> Role | None:
    if groups & admin_groups:
        return Role.ADMIN
    if groups & responder_groups:
        return Role.RESPONDER
    if groups & viewer_groups:
        return Role.VIEWER
    return None


class AuthPolicy:
    def __init__(
        self,
        *,
        viewer_groups: Set[str],
        responder_groups: Set[str],
        admin_groups: Set[str],
        groups_claim: str = "groups",
        session_seconds: int = 8 * 60 * 60,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if session_seconds < 1:
            raise ValueError("session_seconds must be positive")
        self._viewer_groups = viewer_groups
        self._responder_groups = responder_groups
        self._admin_groups = admin_groups
        self._groups_claim = groups_claim
        self._session_seconds = session_seconds
        self._clock = clock
        self._token_factory = token_factory

    def session_from_oidc_token(self, token: Mapping[str, object]) -> dict[str, object]:
        userinfo = token.get("userinfo")
        if not isinstance(userinfo, Mapping):
            raise ValueError("OIDC token is missing userinfo")
        subject = userinfo.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("OIDC userinfo is missing a stable subject")
        raw_groups = userinfo.get(self._groups_claim, [])
        if isinstance(raw_groups, str):
            groups = {raw_groups}
        elif isinstance(raw_groups, (list, tuple, set, frozenset)):
            groups = {str(group) for group in raw_groups}
        else:
            groups = set()
        role = role_for_groups(
            groups,
            viewer_groups=self._viewer_groups,
            responder_groups=self._responder_groups,
            admin_groups=self._admin_groups,
        )
        if role is None:
            raise PermissionError("User is not in an authorized OIDC group")
        email = userinfo.get("email")
        name = userinfo.get("name")
        return {
            "sub": subject,
            "email": email if isinstance(email, str) else "",
            "name": name if isinstance(name, str) else "",
            "role": role.value,
            "expires_at": self._clock() + self._session_seconds,
            "csrf_token": self._token_factory(),
        }

    def principal_from_session(self, session: Mapping[str, object]) -> Principal | None:
        expires_at = session.get("expires_at")
        if not isinstance(expires_at, (int, float)) or self._clock() >= expires_at:
            return None
        subject = session.get("sub")
        role_value = session.get("role")
        if not isinstance(subject, str) or not subject or not isinstance(role_value, str):
            return None
        try:
            role = Role(role_value)
        except ValueError:
            return None
        email = session.get("email")
        name = session.get("name")
        return Principal(
            subject=subject,
            email=email if isinstance(email, str) else "",
            name=name if isinstance(name, str) else "",
            role=role,
        )

    def verify_csrf(self, session: Mapping[str, object], supplied: str) -> bool:
        if self.principal_from_session(session) is None or not supplied:
            return False
        expected = session.get("csrf_token")
        return isinstance(expected, str) and hmac.compare_digest(expected, supplied)


def parse_bearer_token_roles(value: str) -> dict[str, Role]:
    """Parse a JSON mapping of SHA-256 token digests to operator roles."""
    if not value.strip():
        return {}
    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("Operator bearer tokens must be a JSON object")
    parsed: dict[str, Role] = {}
    for digest, role_value in raw.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Operator bearer token keys must be lowercase SHA-256 digests")
        try:
            parsed[digest] = Role(role_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Operator bearer token roles must be viewer, responder, or admin") from exc
    return parsed


class AuthContext:
    """Resolve signed sessions and enforce route roles without trusting form identity."""

    def __init__(
        self,
        *,
        enabled: bool,
        policy: AuthPolicy,
        bearer_token_roles: Mapping[str, Role] | None = None,
    ) -> None:
        self.enabled = enabled
        self.policy = policy
        self._bearer_token_roles = dict(bearer_token_roles or {})
        self._development_principal = Principal(
            subject="local-development",
            email="local-development",
            name="Local development",
            role=Role.ADMIN,
        )

    def principal(self, request: Request) -> Principal | None:
        if not self.enabled:
            return self._development_principal
        return self.policy.principal_from_session(request.session)

    def bearer_principal(self, request: Request) -> Principal | None:
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            return None
        supplied_digest = hashlib.sha256(token.encode()).hexdigest()
        for configured_digest, role in self._bearer_token_roles.items():
            if hmac.compare_digest(configured_digest, supplied_digest):
                return Principal(
                    subject=f"api-token:{configured_digest[:12]}",
                    email="",
                    name="Operator API token",
                    role=role,
                )
        return None

    async def require(
        self,
        request: Request,
        role: Role,
        *,
        csrf: bool = False,
        redirect_to_login: bool = False,
    ) -> Principal:
        bearer_principal = self.bearer_principal(request) if self.enabled else None
        if bearer_principal is not None:
            if not bearer_principal.permits(role):
                raise HTTPException(status_code=403, detail="Insufficient role")
            return bearer_principal
        principal = self.principal(request)
        if principal is None:
            if redirect_to_login:
                raise HTTPException(
                    status_code=303,
                    detail="Authentication required",
                    headers={"Location": "/auth/login"},
                )
            raise HTTPException(status_code=401, detail="Authentication required")
        if not principal.permits(role):
            raise HTTPException(status_code=403, detail="Insufficient role")
        if csrf and self.enabled:
            supplied = request.headers.get("x-csrf-token", "")
            if not supplied:
                content_type = request.headers.get("content-type", "")
                if content_type.startswith("application/x-www-form-urlencoded"):
                    form = await request.form()
                    supplied = str(form.get("csrf_token", ""))
            if not self.policy.verify_csrf(request.session, supplied):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")
        return principal

    def csrf_token(self, request: Request) -> str:
        if not self.enabled:
            return ""
        value = request.session.get("csrf_token")
        return value if isinstance(value, str) else ""


def csv_groups(value: str) -> set[str]:
    return {group.strip() for group in value.split(",") if group.strip()}


def build_oidc_client(settings: object) -> object:
    try:
        from authlib.integrations.starlette_client import OAuth
    except ImportError as exc:
        raise RuntimeError(
            "OIDC requires the production dependencies; "
            "install incident-response[production]"
        ) from exc
    oauth = OAuth()
    oauth.register(
        name="incident_response",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=settings.oidc_metadata_url,
        client_kwargs={"scope": "openid profile email"},
    )
    return oauth.create_client("incident_response")
