import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from openai import APIConnectionError, APIStatusError

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
