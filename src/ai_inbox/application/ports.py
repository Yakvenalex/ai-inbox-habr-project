from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, TypeVar

from ai_inbox.domain.entities import (
    Action,
    ChatRun,
    Conversation,
    Digest,
    Draft,
    Email,
    Entity,
    Media,
    Message,
    Task,
)

T = TypeVar("T", bound=Entity)


class Repository(Protocol[T]):
    async def get(self, id: str) -> T: ...
    async def list(self, **filters: Any) -> list[T]: ...
    async def add(self, entity: T) -> None: ...
    async def save(self, entity: T) -> None: ...
    async def delete(self, id: str) -> None: ...


class UnitOfWork(Protocol):
    tasks: Repository[Task]
    drafts: Repository[Draft]
    conversations: Repository[Conversation]
    messages: Repository[Message]
    actions: Repository[Action]
    runs: Repository[ChatRun]
    digests: Repository[Digest]
    media: Repository[Media]

    async def __aenter__(self) -> "UnitOfWork": ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...


UowFactory = Callable[[], UnitOfWork]


class Clock(Protocol):
    def now(self) -> datetime: ...


class TokenService(Protocol):
    def authenticate(self, username: str, password: str) -> dict[str, Any]: ...
    def validate(self, token: str) -> dict[str, Any]: ...


class MailGateway(Protocol):
    async def list_emails(self, unread: bool, limit: int) -> list[Email]: ...
    async def read(self, email_id: str) -> Email: ...


class ToolExecutor(Protocol):
    async def __call__(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> dict[str, Any]: ...


class AssistantGateway(Protocol):
    async def chat(
        self, history: list[dict[str, Any]], execute: ToolExecutor, now: datetime
    ) -> tuple[str, list[dict[str, Any]]]: ...
    async def summarize(self, context: str) -> str: ...


class SpeechGateway(Protocol):
    async def transcribe(self, data: bytes, content_type: str) -> str: ...
    async def synthesize(self, text: str) -> bytes: ...


class FileStorage(Protocol):
    async def write(self, id: str, data: bytes, extension: str) -> str: ...
    def path(self, filename: str) -> Path: ...
    async def read(self, filename: str) -> bytes: ...
    async def delete(self, filename: str) -> None: ...
