from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ai_inbox.domain.errors import Unauthorized
from ai_inbox.presentation.api.dependencies import Owner, check_origin, container

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/auth/login")
async def login(request: Request):
    check_origin(request)
    form = await request.form()
    c = container(request)
    try:
        result = c.authenticate(str(form.get("username", "")), str(form.get("password", "")))
    except Unauthorized:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный логин или пароль"}, status_code=401
        )
    response = RedirectResponse("/", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    options = {
        "secure": c.settings.auth_cookie_secure,
        "samesite": "lax",
        "path": "/",
        "max_age": result["expires_in"],
    }
    response.set_cookie(
        c.settings.auth_cookie_name, result["access_token"], httponly=True, **options
    )
    claims = c.validate_token(result["access_token"])
    response.set_cookie("ai_inbox_csrf", claims["csrf"], httponly=False, **options)
    return response


@router.post("/auth/logout")
async def logout(request: Request, owner: Owner):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(container(request).settings.auth_cookie_name, path="/")
    response.delete_cookie("ai_inbox_csrf", path="/")
    return response


@router.get("/")
async def index(request: Request, owner: Owner):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "username": owner["sub"],
            "demo": container(request).settings.app_env == "demo",
            "digest_schedule": (
                f"{container(request).settings.digest_hour:02d}:{container(request).settings.digest_minute:02d}"
                if container(request).settings.digest_enabled
                else "выключено"
            ),
            "timezone": container(request).settings.app_timezone,
        },
    )


@router.get("/drafts/{id}")
async def draft_editor(id: str, request: Request, owner: Owner):
    await container(request).drafts.get(id)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "kind": "draft",
            "id": id,
            "timezone": container(request).settings.app_timezone,
        },
    )


@router.get("/digests")
async def digest_list_page(request: Request, owner: Owner):
    return RedirectResponse("/?view=digests", status_code=303)


@router.get("/digests/{id}")
async def digest_page(id: str, request: Request, owner: Owner):
    await container(request).digests.get(id)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "kind": "digest",
            "id": id,
            "timezone": container(request).settings.app_timezone,
        },
    )
