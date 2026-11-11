import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

import httpx
from openai import APIConnectionError, APIError, APIStatusError

from ai_inbox.domain.errors import ProviderUnavailable

T = TypeVar("T")
TRANSIENT = {429, 502, 503, 504, 524}


async def provider_request[T](operation: Callable[[], Awaitable[T]], attempts: int = 2) -> T:
    for attempt in range(attempts):
        try:
            return await operation()
        except (httpx.HTTPError, APIConnectionError, APIStatusError) as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            transient = status is None or status in TRANSIENT
            if not transient or attempt + 1 >= attempts:
                message = (
                    f"AnyModel временно недоступен (HTTP {status})."
                    if transient and status
                    else "AnyModel недоступен или отклонил запрос."
                )
                raise ProviderUnavailable(
                    message + " Попробуй позже; выполненные действия сохранены."
                ) from exc
            delay = 1.0
            if response is not None:
                try:
                    delay = min(5.0, max(0.0, float(response.headers.get("Retry-After", "1"))))
                except ValueError:
                    pass
            await asyncio.sleep(delay)
    raise AssertionError("attempts must be positive")


async def provider_stream[T](
    operation: Callable[[], AsyncIterator[T]], attempts: int = 2
) -> AsyncIterator[T]:
    for attempt in range(attempts):
        emitted = False
        try:
            async for event in operation():
                emitted = True
                yield event
            return
        except (httpx.HTTPError, APIError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if emitted:
                raise ProviderUnavailable(
                    "Поток AnyModel прерван. Частичный текст и выполненные действия сохранены."
                ) from exc
            if attempt + 1 >= attempts or status is not None and status not in TRANSIENT:
                raise ProviderUnavailable(
                    "AnyModel недоступен или отклонил потоковый запрос "
                    f"(HTTP {status or 'connection'})."
                ) from exc
            await asyncio.sleep(1)
    raise AssertionError("attempts must be positive")
