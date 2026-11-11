import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from typing import Any
from uuid import UUID

from ai_inbox.domain.entities import DeliveryAttempt, OutgoingEmail
from ai_inbox.domain.errors import Conflict, InvalidInput, TooLarge
from ai_inbox.domain.outgoing import MAX_ATTACHMENT_BYTES, validate_content, validate_letter

from .outgoing_dto import DeliveryResult, EmailAttachment, EmailComposition, OutgoingMessage
from .ports import Clock, EmailComposerGateway, FileStorage, MailSender, UowFactory
from .tasks import existing_action, public, record_action

UNCERTAIN = (
    "Неизвестно, принял ли сервер письмо. Проверь получение; повторная отправка заблокирована."
)


class Outbox:
    def __init__(
        self,
        uow: UowFactory,
        sender: MailSender,
        composer: EmailComposerGateway,
        storage: FileStorage,
        clock: Clock,
        message_domain: str,
    ):
        self.uow, self.sender, self.composer = uow, sender, composer
        self.storage, self.clock, self.message_domain = storage, clock, message_domain
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def content_token(letter: OutgoingEmail) -> str:
        content = [letter.recipient, letter.subject, letter.body, letter.attachment_ids]
        return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()

    async def compose(self, outline: str) -> dict[str, str]:
        if not outline.strip() or len(outline) > 10000:
            raise InvalidInput("Опиши набросок письма: от 1 до 10000 символов.")
        composition: EmailComposition = await self.composer.compose_email(outline)
        validate_content(composition.subject, composition.body)
        return {"subject": composition.subject.strip(), "body": composition.body.strip()}

    async def attachments(self, ids: list[str]) -> list[dict[str, Any]]:
        total, files = 0, []
        async with self.uow() as uow:
            for id in ids:
                media = await uow.media.get(id)
                if media.kind != "attachment":
                    raise InvalidInput("Прикрепляй файлы через загрузку вложений.")
                total += media.size
                files.append(
                    {
                        "file_id": media.id,
                        "name": media.original_name,
                        "size": media.size,
                        "content_type": media.content_type,
                        "url": f"/api/v1/media/{media.id}",
                    }
                )
        if total > MAX_ATTACHMENT_BYTES:
            raise TooLarge("Общий размер вложений не должен превышать 15 МБ.")
        return files

    async def save(
        self,
        recipient: str,
        subject: str,
        body: str,
        attachment_ids: list[str],
        id: str | None = None,
        *,
        key: str | None = None,
        request_id: str = "",
        call_id: str = "",
    ) -> dict[str, Any]:
        recipient = validate_letter(recipient, subject, body, attachment_ids)
        files = await self.attachments(attachment_ids)
        lock_key = id or "new-draft"
        async with self.locks[lock_key]:
            async with self.uow() as uow:
                if cached := await existing_action(uow, key):
                    return cached
                if id:
                    letter = await uow.outbox.get(id)
                    if letter.status not in {"draft", "failed"}:
                        raise Conflict(
                            "Письмо уже отправляется, отправлено или имеет неизвестный результат."
                        )
                    letter.recipient, letter.subject, letter.body = recipient, subject.strip(), body
                    letter.attachment_ids, letter.status, letter.error = (
                        attachment_ids,
                        "draft",
                        None,
                    )
                    letter.updated_at = self.clock.now()
                    await uow.outbox.save(letter)
                else:
                    letter = OutgoingEmail(
                        recipient=recipient,
                        subject=subject.strip(),
                        body=body,
                        attachment_ids=attachment_ids,
                        message_id="",
                        created_at=self.clock.now(),
                        updated_at=self.clock.now(),
                    )
                    letter.message_id = f"<{letter.id}@{self.message_domain}>"
                    await uow.outbox.add(letter)
                result = {
                    **public(letter),
                    "attachments": files,
                    "content_token": self.content_token(letter),
                }
                await record_action(uow, key, "prepare_outgoing_email", result, request_id, call_id)
                await uow.commit()
        return result

    async def get(self, id: str) -> dict[str, Any]:
        async with self.uow() as uow:
            letter = await uow.outbox.get(id)
        return {
            **public(letter),
            "attachments": await self.attachments(letter.attachment_ids),
            "content_token": self.content_token(letter),
        }

    async def list(self) -> list[dict[str, Any]]:
        async with self.uow() as uow:
            return [
                {k: v for k, v in public(letter).items() if k not in {"body", "attachment_ids"}}
                | {"attachment_count": len(letter.attachment_ids)}
                for letter in await uow.outbox.list()
            ]

    @staticmethod
    def receipt(attempt: DeliveryAttempt, letter: OutgoingEmail) -> dict[str, Any]:
        return {**public(attempt), "message_id": letter.message_id}

    async def send(
        self, id: str, request_id: str, confirmed: bool, content_token: str
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise InvalidInput("Отправка требует явного подтверждения.")
        try:
            UUID(request_id)
        except ValueError as exc:
            raise InvalidInput("Для отправки нужен UUID request_id.") from exc
        async with self.locks[id]:
            async with self.uow() as uow:
                letter = await uow.outbox.get(id)
                previous = await uow.deliveries.list(request_id=request_id)
                if previous:
                    if previous[0].outbox_id != id:
                        raise Conflict("Этот request_id уже относится к другому письму.")
                    return self.receipt(previous[0], letter)
                if letter.status not in {"draft", "failed"}:
                    raise Conflict(
                        "Повтор запрещён: письмо отправляется, отправлено или результат неизвестен."
                    )
            if content_token != self.content_token(letter):
                raise Conflict(
                    "Письмо изменилось после просмотра. Открой его и подтверди актуальную версию."
                )
            files = await self.attachments(letter.attachment_ids)
            payload = []
            for file in files:
                async with self.uow() as uow:
                    media = await uow.media.get(file["file_id"])
                payload.append(
                    EmailAttachment(
                        file["name"], file["content_type"], await self.storage.read(media.filename)
                    )
                )
            attempt = DeliveryAttempt(
                outbox_id=id,
                request_id=request_id,
                created_at=self.clock.now(),
                updated_at=self.clock.now(),
            )
            letter.status, letter.error, letter.updated_at = "sending", None, self.clock.now()
            async with self.uow() as uow:
                await uow.deliveries.add(attempt)
                await uow.outbox.save(letter)
                await uow.commit()
            try:
                result = await self.sender.send(
                    OutgoingMessage(
                        letter.recipient,
                        letter.subject,
                        letter.body,
                        letter.message_id,
                        self.clock.now(),
                        payload,
                    )
                )
            except Exception as exc:
                logging.getLogger(__name__).error("Delivery interrupted: %s", type(exc).__name__)
                result = DeliveryResult("unknown", UNCERTAIN)
            letter.status = attempt.status = result.status
            letter.error = attempt.error = result.error
            letter.updated_at = attempt.updated_at = self.clock.now()
            if result.status == "sent":
                letter.sent_at = attempt.accepted_at = self.clock.now()
            async with self.uow() as uow:
                await uow.outbox.save(letter)
                await uow.deliveries.save(attempt)
                await uow.commit()
            return self.receipt(attempt, letter)

    async def recover_interrupted(self) -> None:
        async with self.uow() as uow:
            for letter in await uow.outbox.list(status="sending"):
                letter.status, letter.error = "unknown", UNCERTAIN
                letter.updated_at = self.clock.now()
                await uow.outbox.save(letter)
            for attempt in await uow.deliveries.list(status="sending"):
                attempt.status, attempt.error = "unknown", UNCERTAIN
                attempt.updated_at = self.clock.now()
                await uow.deliveries.save(attempt)
            await uow.commit()
