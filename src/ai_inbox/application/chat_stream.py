"""One-process, bounded live streams. SQLite remains the source of final results."""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from ai_inbox.domain.errors import AppError, Conflict, InvalidInput

from .chat import ChatWithAssistant


@dataclass(eq=False)
class ChatStream:
    payload: tuple[str, str, str, str | None]
    text: str = ""
    progress: dict[str, Any] = field(
        default_factory=lambda: {"type": "status", "phase": "thinking"}
    )
    sequence: int = 0
    terminal: dict[str, Any] | None = None
    finished_at: float | None = None
    task: asyncio.Task[None] | None = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=256))
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def emit(self, event: dict[str, Any]) -> None:
        async with self.changed:
            self.sequence += 1
            event = {**event, "sequence": self.sequence}
            if event["type"] == "message_start":
                self.text = ""
                self.progress = {"type": "status", "phase": "thinking"}
            elif event["type"] == "delta":
                self.text += event["text"]
            elif event["type"] in {"status", "tool_start", "tool_end"}:
                self.progress = event
            elif event["type"] in {"done", "error"}:
                self.terminal = event
                self.finished_at = monotonic()
            self.events.append(event)
            self.changed.notify_all()

    def snapshot(self) -> dict[str, Any]:
        return self.terminal or {
            "type": "snapshot",
            "sequence": self.sequence,
            "text": self.text,
            "progress": self.progress,
        }

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        async with self.changed:
            initial = self.snapshot()
        yield initial
        cursor = int(initial["sequence"])
        if initial["type"] in {"done", "error"}:
            return

        def advanced() -> bool:
            return self.sequence > cursor

        while True:
            async with self.changed:
                batch: list[dict[str, Any]]
                try:
                    async with asyncio.timeout(10):
                        await self.changed.wait_for(advanced)
                except TimeoutError:
                    batch = [{"type": "heartbeat", "sequence": cursor}]
                else:
                    # A slow/reconnected client gets a replacement snapshot, not lost deltas.
                    batch = (
                        [self.snapshot()]
                        if self.events[0]["sequence"] > cursor + 1
                        else [event for event in self.events if event["sequence"] > cursor]
                    )
            for event in batch:
                yield event
                cursor = int(event["sequence"])
                if event["type"] in {"done", "error"}:
                    return


class StreamChat:
    def __init__(self, chat: ChatWithAssistant):
        self.chat = chat
        self.jobs: dict[tuple[str, str], ChatStream] = {}
        self.admission = asyncio.Lock()

    def _prune(self) -> None:
        finished = sorted(
            ((key, job) for key, job in self.jobs.items() if job.finished_at is not None),
            key=lambda item: item[1].finished_at or 0,
        )
        for index, (key, job) in enumerate(finished):
            if monotonic() - (job.finished_at or 0) > 300 or index < len(finished) - 16:
                del self.jobs[key]

    async def open(
        self, conversation_id: str, text: str, request_id: str, image_id: str | None = None
    ) -> ChatStream:
        payload = (conversation_id, text, request_id, image_id)
        async with self.admission:
            self._prune()
            async with self.chat.uow() as uow:
                await uow.conversations.get(conversation_id)
                if image_id:
                    media = await uow.media.get(image_id)
                    if media.kind != "image":
                        raise InvalidInput("В сообщении можно использовать только изображение.")
            key = (conversation_id, request_id)
            if job := self.jobs.get(key):
                if job.payload != payload:
                    raise Conflict("Этот request_id уже относится к другому содержимому сообщения.")
                return job
            if self.chat.locks[conversation_id].locked() or any(
                j.payload[0] == conversation_id and j.terminal is None for j in self.jobs.values()
            ):
                raise Conflict("В этом диалоге уже выполняется другой запрос. Дождись ответа.")
            if sum(job.terminal is None for job in self.jobs.values()) >= 4:
                raise Conflict("Уже выполняются четыре запроса. Дождись завершения одного из них.")
            job = self.jobs[key] = ChatStream(payload)
            job.task = asyncio.create_task(self._run(job))
            return job

    async def active_request(self, conversation_id: str) -> dict[str, Any] | None:
        async with self.admission:
            persisted = await self.chat.active_request(conversation_id)
            if persisted:
                return persisted
            for job in self.jobs.values():
                if job.payload[0] == conversation_id and job.terminal is None:
                    return {
                        "text": job.payload[1],
                        "request_id": job.payload[2],
                        "image_id": job.payload[3],
                    }
            return None

    async def _run(self, job: ChatStream) -> None:
        try:
            result = await self.chat(*job.payload, emit=job.emit)
            # done is emitted only AFTER the final reply and receipts are committed.
            await job.emit({"type": "done", "result": result})
        except asyncio.CancelledError:
            await job.emit(
                {
                    "type": "error",
                    "error": {
                        "code": "interrupted",
                        "message": "Приложение остановлено. Проверь историю после запуска.",
                    },
                }
            )
            raise
        except AppError as exc:
            await job.emit({"type": "error", "error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:
            logging.getLogger(__name__).error("Chat stream failed: %s", type(exc).__name__)
            await job.emit(
                {
                    "type": "error",
                    "error": {
                        "code": "internal_error",
                        "message": "Не удалось завершить запрос. Проверь историю действий.",
                    },
                }
            )

    async def forget(self, conversation_id: str) -> None:
        async with self.admission:
            self.jobs = {key: job for key, job in self.jobs.items() if key[0] != conversation_id}

    async def close(self) -> None:
        tasks = [job.task for job in self.jobs.values() if job.task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
