from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Bound the multipart body before Starlette spools it to disk."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = self.limit + 1
        if declared > self.limit:
            return await self.reject(scope, receive, send)
        size = 0

        class BodyExceeded(Exception):
            pass

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > self.limit:
                    raise BodyExceeded
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyExceeded:
            await self.reject(scope, receive, send)

    async def reject(self, scope, receive, send):
        response = JSONResponse(
            {"error": {"code": "too_large", "message": "Превышен лимит тела запроса."}},
            status_code=413,
        )
        await response(scope, receive, send)
