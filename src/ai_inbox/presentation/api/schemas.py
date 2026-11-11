from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(Schema):
    code: str
    message: str


class ErrorResponse(Schema):
    error: ErrorDetail


class TokenResponse(Schema):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class OwnerResponse(Schema):
    username: str


class TaskCreate(Schema):
    title: str = Field(min_length=1, max_length=300, examples=["Проверить учебный счёт"])
    notes: str = Field("", max_length=10000)
    due_at: datetime | None = Field(None, examples=["2026-11-19T09:20:00+03:00"])
    source: str = Field("manual", max_length=500)

    @field_validator("due_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value and value.utcoffset() is None:
            raise ValueError("Нужны время и часовой пояс; дата без времени требует уточнения")
        return value


class TaskPatch(Schema):
    title: str | None = Field(None, min_length=1, max_length=300)
    notes: str | None = Field(None, max_length=10000)
    due_at: datetime | None = None
    status: Literal["open", "completed"] | None = None
    _timezone = field_validator("due_at")(TaskCreate.timezone_required.__func__)


class EntityResponse(Schema):
    id: str
    created_at: datetime
    updated_at: datetime


class TaskResponse(EntityResponse):
    title: str
    notes: str
    due_at: datetime | None
    status: str
    source: str


class EmailResponse(Schema):
    id: str
    uid: str
    mailbox: str
    sender: str
    subject: str
    date: str
    text: str
    attachments: list[dict[str, Any]]
    unread: bool


class SummarizeRequest(Schema):
    ids: list[str] | None = Field(None, max_length=20)
    limit: int = Field(10, ge=1, le=20)


class SummaryResponse(Schema):
    text: str
    sources: list[str]


class DraftCreate(Schema):
    email_id: str = Field(min_length=1, max_length=500)
    subject: str = Field("", max_length=1000)
    body: str = Field(min_length=1, max_length=20000)


class DraftPatch(Schema):
    subject: str | None = Field(None, max_length=1000)
    body: str | None = Field(None, min_length=1, max_length=20000)


class DraftResponse(EntityResponse):
    email_id: str
    subject: str
    body: str
    status: Literal["draft"]


class ConversationCreate(Schema):
    title: str = Field("Новый диалог", min_length=1, max_length=100)


class ConversationResponse(EntityResponse):
    title: str


class ChatInput(Schema):
    text: str = Field(
        min_length=1, max_length=10000, examples=["Создай задачу проверить учебный счёт"]
    )
    image_id: UUID | None = None
    request_id: UUID = Field(
        description="UUID запроса: повтор с тем же ID не выполняет действия снова"
    )


class MessageResponse(EntityResponse):
    conversation_id: str
    role: str
    text: str
    image_id: str | None
    actions: list[dict[str, Any]]
    request_id: str | None


class ChatResponse(Schema):
    request_id: str
    status: str
    text: str
    actions: list[dict[str, Any]]
    message_id: str
    error: str | None = None


class MediaResponse(Schema):
    file_id: str
    url: str
    content_type: str


class TranscriptionResponse(Schema):
    text: str
    confirmation_required: bool = True


class SpeechInput(Schema):
    text: str = Field(min_length=1, max_length=5000)


class DigestResponse(EntityResponse):
    key: str
    status: str
    text: str
    sources: list[str]
    audio_id: str | None
    error: str | None


class AttachmentResponse(MediaResponse):
    name: str
    size: int


class OutgoingInput(Schema):
    recipient: str = Field(min_length=3, max_length=254, examples=["reader@example.com"])
    subject: str = Field(min_length=1, max_length=300, examples=["Учебный счёт — уточнение"])
    body: str = Field(
        min_length=1,
        max_length=20000,
        examples=["Здравствуйте! Прошу проверить реквизиты учебного счёта."],
    )
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=10)


class ComposeEmailInput(Schema):
    outline: str = Field(
        min_length=1,
        max_length=10000,
        examples=["Вежливо попросить уточнить реквизиты учебного счёта, без выдуманных сроков."],
    )


class ComposeEmailResponse(Schema):
    subject: str
    body: str


class OutgoingSummary(EntityResponse):
    recipient: str
    subject: str
    message_id: str
    status: Literal["draft", "sending", "sent", "failed", "unknown"]
    error: str | None
    sent_at: datetime | None
    attachment_count: int


class OutgoingResponse(EntityResponse):
    content_token: str
    recipient: str
    subject: str
    body: str
    message_id: str
    attachment_ids: list[str]
    attachments: list[AttachmentResponse]
    status: Literal["draft", "sending", "sent", "failed", "unknown"]
    error: str | None
    sent_at: datetime | None


class SendEmailInput(Schema):
    content_token: str = Field(
        pattern=r"^[a-f0-9]{64}$", description="Токен содержимого из проверенного OutgoingResponse"
    )
    request_id: UUID = Field(
        description="UUID одной попытки; повтор возвращает прежний результат без SMTP-повтора"
    )
    confirmed: Literal[True] = Field(description="Явное подтверждение отправки проверенного письма")


class DeliveryResponse(EntityResponse):
    outbox_id: str
    request_id: str
    message_id: str
    status: Literal["sending", "sent", "failed", "unknown"]
    error: str | None
    accepted_at: datetime | None
