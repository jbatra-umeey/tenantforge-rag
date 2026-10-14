import re
import time
from dataclasses import dataclass

import jwt

from .errors import Problem

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")


@dataclass(frozen=True)
class Principal:
    tenant: str
    user: str
    role: str

    def write(self):
        if self.role not in {"editor", "admin"}:
            raise Problem(403, "Editor role required")


class Auth:
    def __init__(self, settings):
        self.settings = settings
        self.jwks = jwt.PyJWKClient(settings.jwks_url, timeout=5) if settings.auth_mode == "oidc" else None

    def verify(self, authorization):
        if not authorization or not authorization.startswith("Bearer "):
            raise Problem(401, "Bearer token required")
        token = authorization[7:]
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key if self.jwks else self.settings.jwt_secret
            payload = jwt.decode(token, key, algorithms=["RS256"] if self.jwks else ["HS256"],
                audience=self.settings.jwt_audience, issuer=self.settings.jwt_issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "tenant_id", "role"]})
            tenant, user, role = payload["tenant_id"], payload["sub"], payload["role"]
            if not isinstance(tenant, str) or not ID.fullmatch(tenant):
                raise ValueError("Invalid tenant")
            if not isinstance(user, str) or not 1 <= len(user) <= 128:
                raise ValueError("Invalid subject")
            if role not in {"reader", "editor", "admin"}:
                raise ValueError("Invalid role")
            return Principal(tenant, user, role)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise Problem(401, "Invalid or expired token") from None

    def issue(self, tenant, user="alice", role="editor", seconds=3600):
        if self.settings.auth_mode != "local" or self.settings.environment == "production":
            raise ValueError("Local token minting is disabled")
        if not ID.fullmatch(tenant) or not 1 <= len(user) <= 128 or role not in {"reader", "editor", "admin"}:
            raise ValueError("Invalid identity")
        now = int(time.time())
        return jwt.encode({"sub": user, "tenant_id": tenant, "role": role, "iat": now, "exp": now+seconds,
            "iss": self.settings.jwt_issuer, "aud": self.settings.jwt_audience}, self.settings.jwt_secret, algorithm="HS256")

