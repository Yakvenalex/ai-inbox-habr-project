from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Bound multipart bodies before Starlette parses or spools them.

    At most MAX_UPLOAD_MB + one MiB of multipart overhead is buffered. This also
    covers chunked requests with no Content-Length header.
    """

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = self.limit + 1
        if declared > self.limit:
            return await self.reject(scope, receive, send)
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > self.limit:
                return await self.reject(scope, receive, send)
            chunks.append(body)
            if not message.get("more_body", False):
                break
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, buffered_receive, send)

    async def reject(self, scope, receive, send):
        response = JSONResponse(
            {"error": {"code": "too_large", "message": "Превышен лимит тела запроса."}},
            status_code=413,
        )
        await response(scope, receive, send)
