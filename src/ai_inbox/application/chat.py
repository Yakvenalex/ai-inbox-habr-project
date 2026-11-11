import asyncio
import base64
import json
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from ai_inbox.domain.entities import ChatRun, Conversation, Message
from ai_inbox.domain.errors import AppError, Conflict, InvalidInput

from .ports import AssistantGateway, Clock, FileStorage, UowFactory
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
        return result

    async def list_conversations(self) -> list[dict[str, Any]]:
        async with self.uow() as uow:
            return [
                {k: v for k, v in public(c).items() if k != "sdk_history"}
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
        self, conversation_id: str, text: str, request_id: str, image_id: str | None = None
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
                await uow.runs.add(run)
                await uow.messages.add(user)
                await uow.commit()
            tools = self.tools_factory(conversation_id + ":" + request_id, text)
            error = None
            try:
                answer, sdk_history = await self.assistant.chat(inputs, tools, self.clock.now())
                conversation.sdk_history = self._deflate(sdk_history, image_urls)
                run.status = "completed"
            except AppError as exc:
                error, answer, run.status = exc.code, str(exc), "failed"
            except Exception as exc:
                logging.getLogger(__name__).error("Chat processing failed: %s", type(exc).__name__)
                error, answer, run.status = (
                    "internal_error",
                    "Ошибка обработки ответа. Выполненные действия сохранены ниже.",
                    "failed",
                )
            actions = await tools.actions()
            if error:
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
            run.response = {
                "request_id": request_id,
                "status": run.status,
                "text": answer,
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
