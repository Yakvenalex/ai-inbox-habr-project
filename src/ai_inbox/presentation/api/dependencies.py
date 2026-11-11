import hmac
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from ai_inbox.domain.errors import Forbidden, Unauthorized

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def container(request: Request):
    return request.app.state.container


async def current_owner(
    request: Request, bearer: Annotated[str | None, Depends(oauth2)]
) -> dict[str, Any]:
    c = container(request)
    header_present = "authorization" in request.headers
    token = bearer if header_present else request.cookies.get(c.settings.auth_cookie_name)
    if not token:
        raise Unauthorized("Войди в приложение.")
    claims = c.validate_token(token)
    if not header_present and request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf and request.url.path == "/auth/logout":
            csrf = str((await request.form()).get("csrf_token", ""))
        if not csrf or not hmac.compare_digest(csrf.encode(), claims["csrf"].encode()):
            raise Forbidden("Не прошла проверка CSRF. Обнови страницу.")
    return claims


def check_origin(request: Request) -> None:
    if request.headers.get("origin", "").rstrip("/") != container(request).settings.app_base_url:
        raise Forbidden("Запрос должен исходить со страницы приложения.")


Owner = Annotated[dict[str, Any], Depends(current_owner)]
