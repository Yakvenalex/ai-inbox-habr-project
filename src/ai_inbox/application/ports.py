from collections.abc import Awaitable, Callable
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, TypeVar

from ai_inbox.domain.entities import (
    Action,
    ChatRun,
    Conversation,
    DeliveryAttempt,
    Digest,
    Draft,
    Email,
    EmailDeletion,
    EmailPage,
    EmailReadState,
    Entity,
    MailboxCounts,
    Media,
    Message,
    OutgoingEmail,
    Task,
)

from .outgoing_dto import DeliveryResult, EmailComposition, OutgoingMessage

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
    outbox: Repository[OutgoingEmail]
    deliveries: Repository[DeliveryAttempt]

    async def __aenter__(self) -> "UnitOfWork": ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...


UowFactory = Callable[[], UnitOfWork]
ChatEventSink = Callable[[dict[str, Any]], Awaitable[None]]


class Clock(Protocol):
    def now(self) -> datetime: ...


class TokenService(Protocol):
    def authenticate(self, username: str, password: str) -> dict[str, Any]: ...
    def validate(self, token: str) -> dict[str, Any]: ...


class MailGateway(Protocol):
    async def list_emails(self, unread: bool, limit: int) -> list[Email]: ...
    async def read(self, email_id: str) -> Email: ...
    async def counts(self) -> MailboxCounts: ...
    async def page(self, unread: bool, limit: int, before: str | None = None) -> EmailPage: ...


class MailStatusGateway(Protocol):
    async def set_unread(self, email_id: str, unread: bool) -> EmailReadState: ...


class MailDeletionGateway(Protocol):
    async def delete(self, email_id: str) -> EmailDeletion: ...


class ToolExecutor(Protocol):
    async def __call__(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> dict[str, Any]: ...


class AssistantGateway(Protocol):
    async def chat(
        self, history: list[dict[str, Any]], execute: ToolExecutor, now: datetime
    ) -> tuple[str, list[dict[str, Any]]]: ...
    async def chat_stream(
        self,
        history: list[dict[str, Any]],
        execute: ToolExecutor,
        now: datetime,
        emit: ChatEventSink,
    ) -> tuple[str, list[dict[str, Any]]]: ...
    async def summarize(self, context: str) -> str: ...


class SpeechGateway(Protocol):
    async def transcribe(self, data: bytes, content_type: str) -> str: ...
    async def synthesize(self, text: str) -> bytes: ...


class FileStorage(Protocol):
    async def write(self, id: str, data: bytes, extension: str) -> str: ...
    async def exists(self, filename: str) -> bool: ...
    async def read(self, filename: str) -> bytes: ...
    async def delete(self, filename: str) -> None: ...


class MailSender(Protocol):
    async def send(self, message: OutgoingMessage) -> DeliveryResult: ...


class EmailComposerGateway(Protocol):
    async def compose_email(self, outline: str) -> EmailComposition: ...
