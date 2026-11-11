import asyncio
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from ai_inbox.domain.entities import Action, PendingEmailReview
from ai_inbox.domain.errors import AppError, Forbidden, InvalidInput

from .digests import GenerateDigest
from .drafts import Drafts
from .mail import Mail
from .outbox import Outbox
from .ports import UowFactory
from .tasks import Tasks, existing_action


def text_arg(args: dict[str, Any], key: str, max_length: int = 20000) -> str:
    value = args.get(key)
    if not isinstance(value, str) or len(value) > max_length:
        raise InvalidInput(f"Неверный аргумент {key}.")
    return value


class AssistantTools:
    """A request-scoped adapter. Only use cases can modify domain entities."""

    def __init__(
        self,
        uow: UowFactory,
        tasks: Tasks,
        drafts: Drafts,
        mail: Mail,
        digests: GenerateDigest,
        outbox: Outbox,
        request_id: str,
        user_text: str,
    ):
        self.uow, self.tasks, self.drafts = uow, tasks, drafts
        self.mail, self.digests, self.request_id = mail, digests, request_id
        self.outbox = outbox
        self.user_text = user_text.lower()
        self.lock = asyncio.Lock()
        self.confirmed_email: PendingEmailReview | None = None

    def _outgoing_requested(self) -> bool:
        if re.search(
            r"(напиш|состав|подготов|созда).{0,100}(письм|сообщен|ответ)",
            self.user_text,
            re.DOTALL,
        ):
            return True
        # A request to send can authorize preparing a LOCAL draft only.
        # Recipient validation still requires the address in the user's own text.
        for match in re.finditer(
            r"\b(отправь(?:те)?|отправить|отошли(?:те)?|вышли(?:те)?|"
            r"пришли(?:те)?|перешли(?:те)?)\b",
            self.user_text,
        ):
            prefix = self.user_text[: match.start()]
            if not re.search(r"\bне(?:\s+\w+){0,3}\s*$", prefix):
                return True
        return False

    def _allow_write(self, name: str) -> None:
        if name == "send_outgoing_email" and self.confirmed_email is None:
            raise Forbidden(
                "Сначала покажи черновик. Отправка разрешена только после отдельного "
                "подтверждения владельца в следующем сообщении."
            )
        if name == "review_outgoing_email" and not re.search(
            r"(покаж|проверь|открой|просмотр|посмотр|проверим).{0,100}(письм|чернов)|"
            r"(письм|чернов).{0,100}(покаж|еще раз|ещё раз|актуальн)",
            self.user_text,
            re.DOTALL,
        ):
            raise Forbidden("Просмотр черновика перед отправкой не запрошен пользователем.")
        if name == "prepare_outgoing_email" and not self._outgoing_requested():
            raise Forbidden(
                "Подготовка письма не запрошена. Попроси написать письмо и укажи адрес получателя."
            )
        patterns = {
            "create_task": r"((созда|добав|постав|запиш|сохрани|извле).{0,100}задач|напомн)",
            "complete_task": r"(заверш|закро|отмет.{0,40}(готов|выполн)|задач.{0,40}выполн)",
            "create_draft": r"(чернов|подготов.{0,30}ответ|ответь|ответ на|состав.{0,30}ответ)",
            "generate_digest": r"(дайджест|digest)",
        }
        if name in {"read_email", "list_unread_emails"} and not re.search(
            r"(почт|входящ|непрочитан|чернов|ответ|дайджест|сводк|email:|"
            r"(прочита|покаж|список|последн).{0,50}письм)",
            self.user_text,
        ):
            raise Forbidden("Чтение почты не запрошено пользователем.")
        pattern = patterns.get(name)
        if pattern and not re.search(pattern, self.user_text):
            raise Forbidden("Изменение не запрошено пользователем. Уточни намерение в чате.")

    async def __call__(self, name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        async with self.lock:
            fingerprint = hashlib.sha256(
                json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            # The semantic key also catches repeated identical calls with a new SDK call ID.
            key = f"{self.request_id}:{name}:{fingerprint}"
            async with self.uow() as uow:
                prior_calls = await uow.actions.list(
                    request_id=self.request_id, tool_call_id=call_id
                )
                if prior_calls:
                    return prior_calls[0].result
                if cached := await existing_action(uow, key):
                    return cached
            try:
                self._allow_write(name)
                metadata = {"key": key, "request_id": self.request_id, "call_id": call_id}
                result = await self._dispatch(name, arguments, metadata)
            except AppError as exc:
                result = {"error": {"code": exc.code, "message": str(exc)}}
            except (ValueError, TypeError, KeyError):
                result = {
                    "error": {
                        "code": "invalid_arguments",
                        "message": "Проверь аргументы инструмента.",
                    }
                }
            # Mutations record actions atomically; reads and errors get receipts here.
            async with self.uow() as uow:
                if not await existing_action(uow, key):
                    await uow.actions.add(
                        Action(
                            key=key,
                            request_id=self.request_id,
                            tool_call_id=call_id,
                            name=name,
                            result=result,
                        )
                    )
                    await uow.commit()
            return result

    async def _dispatch(
        self, name: str, args: dict[str, Any], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {
            "list_unread_emails": {"limit"},
            "read_email": {"email_id"},
            "create_task": {"title", "notes", "due_at", "source"},
            "list_tasks": {"status"},
            "complete_task": {"task_id"},
            "create_draft": {"email_id", "subject", "body"},
            "generate_digest": set(),
            "prepare_outgoing_email": {"recipient", "subject", "body"},
            "review_outgoing_email": {"outbox_id"},
            "send_outgoing_email": set(),
        }
        if name not in allowed:
            raise InvalidInput("Неизвестный инструмент.")
        if set(args) != allowed[name]:
            missing = ", ".join(sorted(allowed[name] - set(args))) or "нет"
            extra = ", ".join(sorted(set(args) - allowed[name])) or "нет"
            raise InvalidInput(f"Неверные поля: отсутствуют {missing}; лишние {extra}.")
        if name == "list_unread_emails":
            limit = args["limit"]
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
                raise InvalidInput("Лимит писем: 1–20.")
            return {"emails": await self.mail.list_emails(True, limit)}
        if name == "read_email":
            return await self.mail.read(text_arg(args, "email_id", 500))
        if name == "create_task":
            date_mentioned = re.search(
                r"(завтра|сегодня|послезавтра|\d{1,2}[./]\d{1,2}|\d{4}-\d{2}-\d{2})", self.user_text
            )
            time_mentioned = re.search(
                r"(\d{1,2}:\d{2}|в \d{1,2}\s*(час|утра|вечера)|полдень|полночь)", self.user_text
            )
            if date_mentioned and not time_mentioned:
                raise InvalidInput("Уточни время и часовой пояс срока: одной даты недостаточно.")
            due_at = args["due_at"]
            if due_at is not None:
                due_at = datetime.fromisoformat(text_arg(args, "due_at", 50))
            return await self.tasks.create(
                text_arg(args, "title", 300),
                text_arg(args, "notes", 10000),
                due_at,
                text_arg(args, "source", 500),
                **metadata,
            )
        if name == "list_tasks":
            if args["status"] is not None and not isinstance(args["status"], str):
                raise InvalidInput("Неверный статус.")
            return {"tasks": await self.tasks.list(args["status"])}
        if name == "complete_task":
            return await self.tasks.edit(
                text_arg(args, "task_id", 36), {"status": "completed"}, **metadata
            )
        if name == "create_draft":
            return await self.drafts.create(
                text_arg(args, "email_id", 500),
                text_arg(args, "body"),
                text_arg(args, "subject", 1000),
                **metadata,
            )
        if name == "review_outgoing_email":
            result = await self.outbox.get(text_arg(args, "outbox_id", 36))
            if result["status"] not in {"draft", "failed"}:
                raise InvalidInput(
                    "Это письмо уже отправляется, отправлено или результат неизвестен."
                )
            return {**result, "delivery_error": result.get("error"), "error": None}
        if name == "send_outgoing_email":
            review = self.confirmed_email
            if review is None:
                raise Forbidden("Отправка не подтверждена владельцем.")
            # The model cannot choose the recipient, body, target ID or content token.
            return await self.outbox.send(
                review["outbox_id"], review["delivery_request_id"], True, review["content_token"]
            )
        if name == "prepare_outgoing_email":
            recipient = text_arg(args, "recipient", 254)
            if not re.search(
                r"(?<![\w.+-])" + re.escape(recipient.lower()) + r"(?![\w.-])", self.user_text
            ):
                raise InvalidInput("Сначала попроси пользователя явно указать адрес получателя.")
            return await self.outbox.save(
                text_arg(args, "recipient", 254),
                text_arg(args, "subject", 300),
                text_arg(args, "body"),
                [],
                **metadata,
            )
        if name == "generate_digest":
            return await self.digests(key="agent:" + self.request_id)
        raise InvalidInput("Неизвестный инструмент.")

    async def actions(self) -> list[dict[str, Any]]:
        async with self.uow() as uow:
            actions = await uow.actions.list(request_id=self.request_id)
        return [
            {"name": a.name, "tool_call_id": a.tool_call_id, "result": a.result}
            for a in sorted(actions, key=lambda a: (a.created_at, a.id))
        ]
