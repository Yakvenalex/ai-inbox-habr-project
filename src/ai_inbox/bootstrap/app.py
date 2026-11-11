import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ai_inbox.domain.errors import (
    AppError,
    AssistantFailure,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    ProviderUnavailable,
    TooLarge,
    Unauthorized,
)
from ai_inbox.presentation.api.body_limit import BodyLimitMiddleware
from ai_inbox.presentation.api.routes import router as api
from ai_inbox.presentation.web.routes import router as web

from .container import Container
from .settings import Settings


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or Settings()
    c = container or Container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await c.start()
        try:
            yield
        finally:
            await c.close()

    app = FastAPI(
        title="AI Inbox",
        version="0.1.0",
        lifespan=lifespan,
        description="Почта, задачи и мультимодальный агент. Ручная отправка с подтверждением.",
        swagger_ui_parameters={"persistAuthorization": False},
    )
    app.state.container = c
    app.add_middleware(BodyLimitMiddleware, limit=(settings.max_upload_mb + 1) * 1024 * 1024)

    @app.middleware("http")
    async def privacy_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        if isinstance(exc, Unauthorized) and not request.url.path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        status = {
            Unauthorized: 401,
            Forbidden: 403,
            NotFound: 404,
            Conflict: 409,
            InvalidInput: 422,
            TooLarge: 413,
            ProviderUnavailable: 503,
            AssistantFailure: 502,
        }.get(type(exc), 400)
        return JSONResponse(
            {"error": {"code": exc.code, "message": str(exc)}},
            status_code=status,
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else {},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Validation inputs may contain credentials or private mail; never echo them.
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": "; ".join(
                        f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
                    ),
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logging.getLogger(__name__).error("Unhandled error: %s", type(exc).__name__)
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "Внутренняя ошибка. Данные сохранены; проверь журнал приложения.",
                }
            },
            status_code=500,
        )

    @app.get("/health", tags=["Сервис"], summary="Состояние процесса без обращения к платным API")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api)
    app.include_router(web)
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parents[1] / "presentation/web/static"),
        name="static",
    )
    return app
