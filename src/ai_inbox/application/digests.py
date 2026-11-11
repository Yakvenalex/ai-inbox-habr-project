import asyncio
import json
from typing import Any
from zoneinfo import ZoneInfo

from ai_inbox.domain.entities import Digest, new_id
from ai_inbox.domain.errors import AppError

from .mail import Mail
from .ports import AssistantGateway, Clock, UowFactory
from .tasks import Tasks, public


class GenerateDigest:
    def __init__(
        self,
        uow: UowFactory,
        mail: Mail,
        tasks: Tasks,
        assistant: AssistantGateway,
        clock: Clock,
        timezone: str,
    ):
        self.uow, self.mail, self.tasks = uow, mail, tasks
        self.assistant, self.clock, self.timezone = assistant, clock, ZoneInfo(timezone)
        self.lock = asyncio.Lock()

    async def __call__(self, scheduled: bool = False, key: str | None = None) -> dict[str, Any]:
        key = key or (
            "scheduled:" + self.clock.now().astimezone(self.timezone).date().isoformat()
            if scheduled
            else "manual:" + new_id()
        )
        async with self.lock:
            async with self.uow() as uow:
                existing = await uow.digests.list(key=key)
                if existing:
                    return public(existing[0])
                digest = Digest(key=key, created_at=self.clock.now(), updated_at=self.clock.now())
                await uow.digests.add(digest)
                await uow.commit()
            try:
                headers = await self.mail.list_emails(True, 10)
                emails = [await self.mail.read(e["id"]) for e in headers]
                tasks = await self.tasks.list("open")
                digest.sources = ["email:" + e["id"] for e in emails] + [
                    "task:" + t["id"] for t in tasks
                ]
                context = {
                    "now": self.clock.now().astimezone(self.timezone).isoformat(),
                    "emails": emails,
                    "tasks": tasks,
                }
                digest.text = (
                    await self.assistant.summarize(
                        "Ежедневный дайджест.\n" + json.dumps(context, ensure_ascii=False)
                    )
                    if emails or tasks
                    else "Непрочитанных писем и открытых задач нет."
                )
                digest.status = "ready"
            except AppError as exc:
                digest.status, digest.error = "failed", str(exc)
            except Exception:
                digest.status, digest.error = "failed", "Не удалось завершить генерацию дайджеста."
            digest.updated_at = self.clock.now()
            async with self.uow() as uow:
                await uow.digests.save(digest)
                await uow.commit()
            return public(digest)

    async def list(self) -> list[dict[str, Any]]:
        async with self.uow() as uow:
            return [public(d) for d in await uow.digests.list()]

    async def get(self, id: str) -> dict[str, Any]:
        async with self.uow() as uow:
            return public(await uow.digests.get(id))
