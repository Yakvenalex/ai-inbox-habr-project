import asyncio
import base64
import json
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from ai_inbox.domain.entities import ChatRun, Conversation, Message
from ai_inbox.domain.errors import AppError, AssistantFailure, Conflict, InvalidInput

from .chat_outgoing import confirms_sending, delivery_answer, pending_review
from .ports import AssistantGateway, ChatEventSink, Clock, FileStorage, UowFactory
from .tasks import public
from .tools import AssistantTools


class ChatWithAssistant:
    def __init__(
        self,
        uow: UowFactory,
        assistant: AssistantGateway,
        storage: FileStorage,
        tools_factory: Callable[[str, str], AssistantTools],
        clock: Clock,
    ):
        self.uow, self.assistant, self.storage = uow, assistant, storage
        self.tools_factory, self.clock = tools_factory, clock
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def create_conversation(self, title: str = "Новый диалог") -> dict[str, Any]:
        conversation = Conversation(
            title=title, created_at=self.clock.now(), updated_at=self.clock.now()
        )
        async with self.uow() as uow:
            await uow.conversations.add(conversation)
            await uow.commit()
        result = public(conversation)
        result.pop("sdk_history")
        result.pop("pending_email")
        return result

    async def list_conversations(self) -> list[dict[str, Any]]:
        async with self.uow() as uow:
            return [
                {k: v for k, v in public(c).items() if k not in {"sdk_history", "pending_email"}}
                for c in await uow.conversations.list()
            ]

    async def delete_conversation(self, id: str) -> dict[str, Any]:
        lock = self.locks[id]
        if lock.locked():
            raise Conflict("Ассистент ещё отвечает в этом диалоге. Дождись завершения и повтори.")
        async with lock:
            async with self.uow() as uow:
                await uow.conversations.get(id)
                if await uow.runs.list(conversation_id=id, status="running"):
                    raise Conflict("В диалоге есть незавершённый запрос. Удаление пока недоступно.")
                await uow.conversations.delete(id)
                await uow.commit()
        return {"id": id, "deleted": True}

    async def messages(self, id: str) -> list[dict[str, Any]]:
        async with self.uow() as uow:
            await uow.conversations.get(id)
            messages = await uow.messages.list(conversation_id=id)
        return [public(m) for m in sorted(messages, key=lambda m: (m.created_at, m.id))]

    async def active_request(self, id: str) -> dict[str, Any] | None:
        async with self.uow() as uow:
            await uow.conversations.get(id)
            runs = await uow.runs.list(conversation_id=id, status="running")
            if not runs:
                return None
            users = await uow.messages.list(
                conversation_id=id, request_id=runs[0].request_id, role="user"
            )
            if not users:
                return None
            return {
                "text": users[0].text,
                "image_id": users[0].image_id,
                "request_id": runs[0].request_id,
            }

    async def _inflate(self, value: Any, image_urls: dict[str, str]) -> Any:
        if isinstance(value, str) and value.startswith("local-image:"):
            id = value.removeprefix("local-image:")
            async with self.uow() as uow:
                media = await uow.media.get(id)
            if media.kind != "image":
                raise InvalidInput("Нужен JPEG или PNG.")
            url = (
                f"data:{media.content_type};base64,"
                + base64.b64encode(await self.storage.read(media.filename)).decode()
            )
            image_urls[url] = value
            return url
        if isinstance(value, list):
            return [await self._inflate(v, image_urls) for v in value]
        if isinstance(value, dict):
            return {k: await self._inflate(v, image_urls) for k, v in value.items()}
        return value

    @staticmethod
    def _deflate(value: Any, image_urls: dict[str, str]) -> Any:
        if isinstance(value, str):
            return image_urls.get(value, value)
        if isinstance(value, list):
            return [ChatWithAssistant._deflate(v, image_urls) for v in value]
        if isinstance(value, dict):
            return {k: ChatWithAssistant._deflate(v, image_urls) for k, v in value.items()}
        return value

    async def __call__(
        self,
        conversation_id: str,
        text: str,
        request_id: str,
        image_id: str | None = None,
        *,
        emit: ChatEventSink | None = None,
    ) -> dict[str, Any]:
        if not text.strip() or len(text) > 10000:
            raise InvalidInput("Сообщение должно содержать от 1 до 10000 символов.")
        async with self.locks[conversation_id]:
            async with self.uow() as uow:
                conversation = await uow.conversations.get(conversation_id)
                runs = await uow.runs.list(conversation_id=conversation_id, request_id=request_id)
                if runs:
                    if runs[0].response:
                        return runs[0].response
                    raise Conflict(
                        "Этот запрос уже выполняется или был прерван. Проверь историю действий."
                    )
            content: Any = text
            if image_id:
                async with self.uow() as uow:
                    media = await uow.media.get(image_id)
                if media.kind != "image":
                    raise InvalidInput("В сообщении можно использовать только изображение.")
                content = [
                    {"type": "input_text", "text": text},
                    {
                        "type": "input_image",
                        "image_url": "local-image:" + image_id,
                        "detail": "auto",
                    },
                ]
            history = conversation.sdk_history + [{"role": "user", "content": content}]
            # Cut only at user boundaries, preserving complete tool call/result pairs.
            starts = [i for i, item in enumerate(history) if item.get("role") == "user"]
            if len(starts) > 12:
                history = history[starts[-12] :]
            image_urls: dict[str, str] = {}
            inputs = await self._inflate(history, image_urls)
            run = ChatRun(conversation_id=conversation_id, request_id=request_id)
            user = Message(
                conversation_id=conversation_id,
                role="user",
                text=text,
                image_id=image_id,
                request_id=request_id,
                created_at=self.clock.now(),
            )
            async with self.uow() as uow:
                # A review applies only to the next user turn in this conversation.
                # Revoke it durably BEFORE any model/tool/SMTP call, including crashes.
                review = conversation.pending_email
                confirmed_send = bool(review and not image_id and confirms_sending(text))
                conversation.pending_email = None
                await uow.conversations.save(conversation)
                await uow.runs.add(run)
                await uow.messages.add(user)
                await uow.commit()
            tools = self.tools_factory(conversation_id + ":" + request_id, text)
            if confirmed_send:
                tools.confirmed_email = review
            partial_text = ""

            async def notify(event: dict[str, Any]) -> None:
                nonlocal partial_text
                if event["type"] == "message_start":
                    partial_text = ""
                elif event["type"] == "delta":
                    if len(partial_text) + len(event["text"]) > 100000:
                        raise AssistantFailure("Поток ответа превысил допустимый размер.")
                    partial_text += event["text"]
                if emit is not None:
                    await emit(event)

            async def execute(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
                await notify({"type": "tool_start", "name": name})
                result = await tools(name, arguments, call_id)
                await notify(
                    {
                        "type": "tool_end",
                        "name": name,
                        "ok": not result.get("error")
                        and result.get("status") not in {"failed", "unknown"},
                    }
                )
                return result

            error = None
            try:
                await notify({"type": "status", "phase": "thinking"})
                if confirmed_send:
                    # Confirmation does not need a model to choose whether/what to send.
                    result = await execute("send_outgoing_email", {}, "confirmed-send")
                    answer, error = delivery_answer(result)
                    conversation.sdk_history = history + [
                        {
                            "role": "assistant",
                            "content": answer + "\n" + json.dumps(result, ensure_ascii=False),
                        }
                    ]
                    run.status = "failed" if error else "completed"
                else:
                    if emit is not None:
                        answer, sdk_history = await self.assistant.chat_stream(
                            inputs, execute, self.clock.now(), notify
                        )
                    else:
                        answer, sdk_history = await self.assistant.chat(
                            inputs, tools, self.clock.now()
                        )
                    conversation.sdk_history = self._deflate(sdk_history, image_urls)
                    run.status = "completed"
            except AppError as exc:
                error, answer, run.status = exc.code, str(exc), "failed"
            except Exception as exc:
                logging.getLogger(__name__).error("Chat processing failed: %s", type(exc).__name__)
                error, answer, run.status = (
                    "internal_error",
                    "Ошибка обработки ответа.",
                    "failed",
                )
            actions = await tools.actions()
            if error:
                if partial_text.strip():
                    answer += "\n\nНезавершённый текст модели (ответ прерван):\n\n" + partial_text
                successful = [a for a in actions if not a["result"].get("error")]
                prepared = [a for a in successful if a["name"] == "prepare_outgoing_email"]
                if confirmed_send:
                    if error == "internal_error":
                        answer += (
                            "\n\nПроверь статус в исходящих перед любыми дальнейшими действиями."
                        )
                elif prepared:
                    answer += (
                        "\n\nЧерновик письма сохранён. Открой его по ссылке ниже, проверь текст "
                        "и подтверди отправку в редакторе. Письмо ещё не отправлено."
                    )
                elif successful:
                    answer += "\n\nУспешные результаты инструментов сохранены и показаны ниже."
                elif actions:
                    answer += (
                        "\n\nНи одно действие инструмента не выполнено. Причины показаны ниже."
                    )
                else:
                    answer += "\n\nДействия не выполнялись."
                # Preserve successful receipts even if the provider failed after a mutation.
                history.append(
                    {
                        "role": "assistant",
                        "content": answer
                        + "\nРезультаты действий: "
                        + json.dumps(actions, ensure_ascii=False),
                    }
                )
                conversation.sdk_history = history
            assistant_message = Message(
                conversation_id=conversation_id,
                role="assistant",
                text=answer,
                actions=actions,
                request_id=request_id,
                created_at=self.clock.now(),
                updated_at=self.clock.now(),
            )
            conversation.pending_email = (
                pending_review(actions, assistant_message.id) if not confirmed_send else None
            )
            if conversation.pending_email:
                confirmation_hint = (
                    "Проверь получателя, тему, текст и вложения в карточке черновика ниже. "
                    "Если всё верно, ответь «Всё ок, отправляй»."
                )
                assistant_message.text += "\n\n" + confirmation_hint
                for action in actions:
                    if action["name"] in {"prepare_outgoing_email", "review_outgoing_email"}:
                        action["result"]["confirmation_available"] = (
                            action["result"].get("id") == conversation.pending_email["outbox_id"]
                            and action["result"].get("content_token")
                            == conversation.pending_email["content_token"]
                        )
                # Also ground the next model turn in the application's confirmation step.
                conversation.sdk_history.append({"role": "assistant", "content": confirmation_hint})
            run.response = {
                "request_id": request_id,
                "status": run.status,
                "text": assistant_message.text,
                "actions": actions,
                "message_id": assistant_message.id,
                "error": error,
            }
            conversation.updated_at = self.clock.now()
            async with self.uow() as uow:
                await uow.messages.add(assistant_message)
                await uow.conversations.save(conversation)
                await uow.runs.save(run)
                await uow.commit()
            return run.response

    async def recover_interrupted(self) -> None:
        async with self.uow() as uow:
            for run in await uow.runs.list(status="running"):
                actions = await uow.actions.list(
                    request_id=run.conversation_id + ":" + run.request_id
                )
                receipts = [
                    {"name": a.name, "tool_call_id": a.tool_call_id, "result": a.result}
                    for a in actions
                ]
                text = "Запрос прерван остановкой приложения. Выполненные действия сохранены ниже."
                message = Message(
                    conversation_id=run.conversation_id,
                    role="assistant",
                    text=text,
                    actions=receipts,
                    request_id=run.request_id,
                )
                await uow.messages.add(message)
                conversation = await uow.conversations.get(run.conversation_id)
                user_messages = await uow.messages.list(
                    conversation_id=run.conversation_id, request_id=run.request_id, role="user"
                )
                if user_messages:
                    conversation.sdk_history.append(
                        {"role": "user", "content": user_messages[0].text}
                    )
                conversation.sdk_history.append(
                    {
                        "role": "assistant",
                        "content": text + "\n" + json.dumps(receipts, ensure_ascii=False),
                    }
                )
                await uow.conversations.save(conversation)
                run.status = "failed"
                run.response = {
                    "request_id": run.request_id,
                    "status": "failed",
                    "text": text,
                    "actions": receipts,
                    "message_id": message.id,
                    "error": "interrupted",
                }
                await uow.runs.save(run)
            for digest in await uow.digests.list(status="generating"):
                digest.status, digest.error = "failed", "Генерация прервана остановкой приложения."
                await uow.digests.save(digest)
            await uow.commit()
