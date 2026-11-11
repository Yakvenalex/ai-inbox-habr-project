import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

import jwt

from ai_inbox.application.ports import Clock
from ai_inbox.domain.errors import Unauthorized


class JwtTokens:
    def __init__(
        self,
        username: str,
        password: str,
        secret: str,
        issuer: str,
        audience: str,
        ttl_minutes: int,
        clock: Clock,
    ):
        self.username, self.password, self.secret = username, password, secret
        self.issuer, self.audience, self.ttl = issuer, audience, ttl_minutes * 60
        self.clock = clock

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        def equal(a: str, b: str) -> bool:
            return hmac.compare_digest(
                hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest()
            )

        valid_user = equal(username, self.username)
        valid_password = equal(password, self.password)
        if not (valid_user and valid_password):
            raise Unauthorized("Неверный логин или пароль.")
        now = self.clock.now()
        token = jwt.encode(
            {
                "sub": self.username,
                "iat": now,
                "exp": now + timedelta(seconds=self.ttl),
                "iss": self.issuer,
                "aud": self.audience,
                "csrf": secrets.token_urlsafe(32),
            },
            self.secret,
            algorithm="HS256",
        )
        return {"access_token": token, "token_type": "bearer", "expires_in": self.ttl}

    def validate(self, token: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "sub", "iss", "aud", "iat", "csrf"]},
            )
            if claims["sub"] != self.username or not isinstance(claims["csrf"], str):
                raise Unauthorized("Войди в приложение снова.")
            return claims
        except jwt.PyJWTError as exc:
            raise Unauthorized("Войди в приложение снова.") from exc
