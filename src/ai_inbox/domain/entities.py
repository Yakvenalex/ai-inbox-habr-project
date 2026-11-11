from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .errors import InvalidInput


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    if value is not None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidInput("Укажи точное время и часовой пояс срока.")
        return value.astimezone(UTC)
    return None


class TaskStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"


@dataclass(kw_only=True)
class Entity:
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(kw_only=True)
class Task(Entity):
    title: str
    notes: str = ""
    due_at: datetime | None = None
    status: str = TaskStatus.OPEN
    source: str = "manual"

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        if not self.title or len(self.title) > 300:
            raise InvalidInput("Название задачи должно содержать от 1 до 300 символов.")
        self.due_at = aware(self.due_at)
        if self.status not in TaskStatus:
            raise InvalidInput("Неизвестный статус задачи.")

    def complete(self, now: datetime) -> None:
        self.status = TaskStatus.COMPLETED
        self.updated_at = now


@dataclass(kw_only=True)
class Draft(Entity):
    email_id: str
    subject: str
    body: str
    status: str = "draft"


@dataclass(kw_only=True)
class Conversation(Entity):
    title: str = "Новый диалог"
    sdk_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(kw_only=True)
class Message(Entity):
    conversation_id: str
    role: str
    text: str
    image_id: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    request_id: str | None = None


@dataclass(kw_only=True)
class Action(Entity):
    key: str
    request_id: str
    tool_call_id: str
    name: str
    result: dict[str, Any]


@dataclass(kw_only=True)
class ChatRun(Entity):
    conversation_id: str
    request_id: str
    status: str = "running"
    response: dict[str, Any] | None = None


@dataclass(kw_only=True)
class Digest(Entity):
    key: str
    status: str = "generating"
    text: str = ""
    sources: list[str] = field(default_factory=list)
    audio_id: str | None = None
    error: str | None = None


@dataclass(kw_only=True)
class Media(Entity):
    kind: str
    content_type: str
    filename: str
    size: int


@dataclass(kw_only=True)
class Email:
    id: str
    uid: str
    mailbox: str
    sender: str
    subject: str
    date: str
    text: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    unread: bool = True
