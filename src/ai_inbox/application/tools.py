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
        user_messages: list[str],
    ):
        self.uow, self.tasks, self.drafts = uow, tasks, drafts
        self.mail, self.digests, self.request_id = mail, digests, request_id
        self.outbox = outbox
        # Only actual owner messages, never mail/tool/model text, can supply a new address.
        self.user_messages = user_messages
        self.lock = asyncio.Lock()
        # Capability for the previous turn's snapshot; intent is decided by the agent.
        self.reviewed_email: PendingEmailReview | None = None

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
                if name in {"prepare_outgoing_email", "review_outgoing_email"}:
                    # A new/repeated preview needs a NEW owner turn before sending.
                    self.reviewed_email = None
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
            review = self.reviewed_email
            if review is None:
                raise Forbidden(
                    "Нет черновика, показанного перед текущим сообщением. Покажи актуальный "
                    "черновик и дождись отдельного подтверждения владельца."
                )
            self.reviewed_email = None
            # The model cannot choose the recipient, body, target ID or content token.
            return await self.outbox.send(
                review["outbox_id"], review["delivery_request_id"], True, review["content_token"]
            )
        if name == "prepare_outgoing_email":
            recipient = text_arg(args, "recipient", 254)
            if not re.search(
                r"(?<![\w.+-])" + re.escape(recipient.lower()) + r"(?![\w.-])",
                "\n".join(self.user_messages).lower(),
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
