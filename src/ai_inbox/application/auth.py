from typing import Any

from .ports import TokenService


class Authenticate:
    def __init__(self, tokens: TokenService):
        self.tokens = tokens

    def __call__(self, username: str, password: str) -> dict[str, Any]:
        return self.tokens.authenticate(username, password)


class ValidateAccessToken:
    def __init__(self, tokens: TokenService):
        self.tokens = tokens

    def __call__(self, token: str) -> dict[str, Any]:
        return self.tokens.validate(token)
