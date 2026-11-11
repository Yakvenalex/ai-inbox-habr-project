from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class EmailComposition:
    subject: str
    body: str


@dataclass(frozen=True)
class EmailAttachment:
    name: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class OutgoingMessage:
    recipient: str
    subject: str
    body: str
    message_id: str
    date: datetime
    attachments: list[EmailAttachment]


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["sent", "failed", "unknown"]
    error: str | None = None
