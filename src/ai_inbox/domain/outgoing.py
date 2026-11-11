import re
from email.headerregistry import Address
from pathlib import PurePosixPath

from .errors import InvalidInput, TooLarge

MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


def recipient_address(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 254 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InvalidInput("Укажи один корректный адрес получателя.")
    try:
        address = Address(addr_spec=value)
        if not address.username or not address.domain or not address.username.isascii():
            raise ValueError
        domain = address.domain.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain):
            raise ValueError
        return Address(username=address.username, domain=domain).addr_spec
    except (ValueError, IndexError, UnicodeError) as exc:
        raise InvalidInput(
            "Укажи один адрес вида name@example.com, без имени и списка адресов."
        ) from exc


def validate_content(subject: str, body: str) -> None:
    if (
        not subject.strip()
        or len(subject) > 300
        or any(ord(c) < 32 or ord(c) == 127 for c in subject)
    ):
        raise InvalidInput("Тема должна содержать от 1 до 300 символов без переводов строк.")
    if not body.strip() or len(body) > 20000 or "\x00" in body:
        raise InvalidInput("Текст письма должен содержать от 1 до 20000 символов.")


def validate_letter(recipient: str, subject: str, body: str, attachments: list[str]) -> str:
    recipient = recipient_address(recipient)
    validate_content(subject, body)
    if len(attachments) > MAX_ATTACHMENTS or len(set(attachments)) != len(attachments):
        raise InvalidInput("Можно приложить не более 10 разных файлов.")
    return recipient


def attachment_name(value: str, size: int) -> str:
    if size > MAX_ATTACHMENT_BYTES:
        raise TooLarge("Размер вложения не должен превышать 15 МБ.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InvalidInput("Имя файла содержит недопустимые символы.")
    name = PurePosixPath(value.replace("\\", "/")).name
    if not name or name in {".", ".."} or len(name.encode("utf-8")) > 240:
        raise InvalidInput("Имя вложения пустое или слишком длинное.")
    return name
