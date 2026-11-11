from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))


class TaskRow(Record, Base):
    __tablename__ = "tasks"
    title: Mapped[str] = mapped_column(String(300))
    notes: Mapped[str] = mapped_column(Text)
    due_at: Mapped[str | None] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    source: Mapped[str] = mapped_column(Text)


class DraftRow(Record, Base):
    __tablename__ = "drafts"
    email_id: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))


class ConversationRow(Record, Base):
    __tablename__ = "conversations"
    title: Mapped[str] = mapped_column(String(100))
    sdk_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON)


class MessageRow(Record, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    image_id: Mapped[str | None] = mapped_column(String(36))
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(String(100))


class ActionRow(Record, Base):
    __tablename__ = "actions"
    key: Mapped[str] = mapped_column(String(250), unique=True)
    request_id: Mapped[str] = mapped_column(String(100), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(150))
    name: Mapped[str] = mapped_column(String(50))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)


class RunRow(Record, Base):
    __tablename__ = "chat_runs"
    __table_args__ = (
        Index("uq_run_conversation_request", "conversation_id", "request_id", unique=True),
    )
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    request_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class DigestRow(Record, Base):
    __tablename__ = "digests"
    key: Mapped[str] = mapped_column(String(150), unique=True)
    status: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    sources: Mapped[list[str]] = mapped_column(JSON)
    audio_id: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str | None] = mapped_column(Text)


class MediaRow(Record, Base):
    __tablename__ = "media"
    kind: Mapped[str] = mapped_column(String(20))
    content_type: Mapped[str] = mapped_column(String(80))
    filename: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
