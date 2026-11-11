from typing import Any

from ai_inbox.domain.entities import Draft
from ai_inbox.domain.errors import InvalidInput

from .ports import Clock, MailGateway, UowFactory
from .tasks import existing_action, public, record_action


class Drafts:
    def __init__(self, uow: UowFactory, mail: MailGateway, clock: Clock):
        self.uow, self.mail, self.clock = uow, mail, clock

    async def create(
        self,
        email_id: str,
        body: str,
        subject: str = "",
        *,
        key: str | None = None,
        request_id: str = "",
        call_id: str = "",
    ) -> dict[str, Any]:
        if not body.strip() or len(body) > 20000 or len(subject) > 1000:
            raise InvalidInput("Нужен текст черновика до 20000 символов и тема до 1000.")
        if key:
            async with self.uow() as uow:
                if cached := await existing_action(uow, key):
                    return cached
        # Validate source before the transaction; no network while holding a DB lock.
        email = await self.mail.read(email_id)
        draft = Draft(
            email_id=email_id,
            body=body,
            subject=subject or "Re: " + email.subject,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        async with self.uow() as uow:
            if cached := await existing_action(uow, key):
                return cached
            await uow.drafts.add(draft)
            result = public(draft)
            await record_action(uow, key, "create_draft", result, request_id, call_id)
            await uow.commit()
            return result

    async def list(self) -> list[dict[str, Any]]:
        async with self.uow() as uow:
            return [public(d) for d in await uow.drafts.list()]

    async def get(self, id: str) -> dict[str, Any]:
        async with self.uow() as uow:
            return public(await uow.drafts.get(id))

    async def edit(self, id: str, changes: dict[str, Any]) -> dict[str, Any]:
        if (
            not changes
            or set(changes) - {"subject", "body"}
            or any(v is None for v in changes.values())
        ):
            raise InvalidInput("Укажи тему или текст черновика.")
        async with self.uow() as uow:
            draft = await uow.drafts.get(id)
            for name, value in changes.items():
                setattr(draft, name, value)
            if not draft.body.strip() or len(draft.body) > 20000 or len(draft.subject) > 1000:
                raise InvalidInput("Проверь длину темы и текста черновика.")
            draft.updated_at = self.clock.now()
            await uow.drafts.save(draft)
            await uow.commit()
            return public(draft)
