from dataclasses import replace
from datetime import UTC, datetime

from ai_inbox.domain.entities import Email, EmailDeletion, EmailPage, EmailReadState, MailboxCounts
from ai_inbox.domain.errors import NotFound, ProviderUnavailable


class FakeClock:
    def __init__(self, now=None):
        self.value = now or datetime(2026, 11, 11, 6, tzinfo=UTC)

    def now(self):
        return self.value


class FakeMail:
    def __init__(self, count=1):
        self.email = Email(
            id="synthetic-email",
            uid="42",
            mailbox="INBOX",
            sender="Учебный поставщик <demo@example.test>",
            subject="Учебный счёт ЖЮ-3082",
            date="Wed, 11 Nov 2026 09:00:00 +0300",
            text=(
                "Проверьте счёт ЖЮ-3082 на 89 406,75 руб. до 19 ноября 2026 года в 09:20 по Москве."
            ),
            attachments=[{"filename": "invoice.png", "content_type": "image/png", "size": 1024}],
        )
        self.reads = 0
        self.deleted = False
        self.extra = [
            replace(
                self.email,
                id=f"synthetic-email-{i}",
                uid=str(42 - i),
                subject=f"Учебное письмо {i:02d}",
                text=f"Синтетическое письмо номер {i}.",
                attachments=[],
            )
            for i in range(1, count)
        ]
        self.deleted_ids = set()
        self.deletions = 0

    async def list_emails(self, unread, limit):
        emails = ([] if self.deleted else [self.email]) + self.extra
        return [e for e in emails if e.id not in self.deleted_ids and (not unread or e.unread)][
            :limit
        ]

    async def page(self, unread, limit, before=None):
        emails = await self.list_emails(unread, 1000)
        if before:
            all_ids = [self.email.id] + [e.id for e in self.extra]
            if before not in all_ids:
                raise NotFound("Курсор не найден.")
            emails = [e for e in emails if all_ids.index(e.id) > all_ids.index(before)]
        more = len(emails) > limit
        return EmailPage(items=emails[:limit], next_cursor=emails[limit - 1].id if more else None)

    async def read(self, email_id):
        email = next((e for e in [self.email] + self.extra if e.id == email_id), None)
        if not email or email_id in self.deleted_ids or email_id == self.email.id and self.deleted:
            raise NotFound("Письмо не найдено.")
        self.reads += 1
        return email

    async def set_unread(self, email_id, unread):
        email = await self.read(email_id)
        email.unread = unread
        return EmailReadState(id=email_id, unread=unread)

    async def counts(self):
        emails = await self.list_emails(False, 1000)
        return MailboxCounts(total=len(emails), unread=sum(e.unread for e in emails))

    async def delete(self, email_id):
        if email_id not in [self.email.id] + [e.id for e in self.extra]:
            raise ProviderUnavailable("Не удалось подтвердить удаление.")
        absent = email_id in self.deleted_ids or email_id == self.email.id and self.deleted
        if not absent:
            self.deletions += 1
        self.deleted_ids.add(email_id)
        if email_id == self.email.id:
            self.deleted = True
        return EmailDeletion(id=email_id, already_absent=absent)


class FakeSpeech:
    def __init__(self):
        self.inputs = []

    async def transcribe(self, data, content_type):
        self.inputs.append(data)
        return "Создай задачу проверить учебный счёт"

    async def synthesize(self, text):
        self.inputs.append(text)
        return b"ID3" + b"synthetic-mp3-bytes" * 30


class FakeAssistant:
    def __init__(self):
        self.calls = 0
        self.steps = []
        self.fail = False
        self.answer = "Учебный ответ."

    async def chat(self, history, execute, now):
        self.calls += 1
        for i, (name, arguments) in enumerate(self.steps):
            await execute(name, arguments, "call-" + str(i))
        if self.fail:
            raise ProviderUnavailable("AnyModel временно недоступен (HTTP 503).")
        return self.answer, history + [{"role": "assistant", "content": self.answer}]

    async def chat_stream(self, history, execute, now, emit):
        answer, updated = await self.chat(history, execute, now)
        await emit({"type": "message_start"})
        for word in answer.splitlines(keepends=True):
            await emit({"type": "delta", "text": word})
        return answer, updated

    async def summarize(self, context):
        self.calls += 1
        if self.fail:
            raise ProviderUnavailable("AnyModel временно недоступен (HTTP 524).")
        return "Учебная сводка: проверить счёт, срок 19 ноября в 09:20."

    async def compose_email(self, outline):
        from ai_inbox.application.outgoing_dto import EmailComposition

        self.calls += 1
        if self.fail:
            raise ProviderUnavailable("AnyModel временно недоступен (HTTP 503).")
        return EmailComposition(
            "Уточнение по учебному счёту",
            "Здравствуйте! Просьба уточнить реквизиты учебного счёта.",
        )


class FakeSender:
    def __init__(self):
        from ai_inbox.application.outgoing_dto import DeliveryResult

        self.result = DeliveryResult("sent")
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        return self.result
