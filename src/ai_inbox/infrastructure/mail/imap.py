import asyncio
import base64
import email.policy
import imaplib
import json
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from email import message_from_bytes
from html.parser import HTMLParser
from typing import Any

from ai_inbox.domain.entities import Email
from ai_inbox.domain.errors import InvalidInput, NotFound, ProviderUnavailable, TooLarge

MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_TEXT = 16000


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def body_text(msg) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except (LookupError, UnicodeError):
        content = (body.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    if not isinstance(content, str):
        return ""
    if body.get_content_type() == "text/html":
        parser = TextExtractor()
        parser.feed(content)
        content = " ".join(parser.parts)
    return content.strip()[:MAX_TEXT]


def email_id(validity: str, uid: str) -> str:
    payload = json.dumps(["INBOX", validity, uid], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_id(id: str) -> tuple[str, str]:
    try:
        mailbox, validity, uid = json.loads(base64.urlsafe_b64decode(id + "=" * (-len(id) % 4)))
        if mailbox != "INBOX" or not str(validity).isdigit() or not str(uid).isdigit():
            raise ValueError
        return str(validity), str(uid)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise NotFound("Некорректный идентификатор письма.") from exc


class ImapMail:
    def __init__(
        self, host: str, port: int, user: str, password: str, timeout: float, max_limit: int
    ):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.timeout, self.max_limit = timeout, max_limit
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="imap")
        self.slots = asyncio.Semaphore(3)

    def _connect(self):
        conn = imaplib.IMAP4_SSL(
            self.host, self.port, ssl_context=ssl.create_default_context(), timeout=self.timeout
        )
        try:
            conn.login(self.user, self.password)
            status, _ = conn.select("INBOX", readonly=True)
            if status != "OK":
                raise ProviderUnavailable("Не удалось открыть почтовый ящик.")
            return conn
        except Exception:
            conn.logout()
            raise

    @staticmethod
    def _validity(conn) -> str:
        data = conn.response("UIDVALIDITY")[1]
        if not data or not data[0] or not data[0].isdigit():
            raise ProviderUnavailable("IMAP не вернул UIDVALIDITY.")
        return data[0].decode()

    def _parse(self, validity: str, uid: str, raw: bytes, metadata: bytes, full: bool) -> Email:
        msg = message_from_bytes(raw, policy=email.policy.default)
        attachments: list[dict[str, Any]] = []
        if full:
            for part in msg.iter_attachments():
                attachments.append(
                    {
                        "filename": str(part.get_filename() or "Без имени")[:200],
                        "content_type": part.get_content_type(),
                        "size": len(part.get_payload(decode=True) or b""),
                    }
                )
                if len(attachments) >= 20:
                    break
        return Email(
            id=email_id(validity, uid),
            uid=uid,
            mailbox="INBOX",
            sender=str(msg.get("From", ""))[:1000],
            subject=str(msg.get("Subject", "Без темы"))[:1000],
            date=str(msg.get("Date", "")),
            text=body_text(msg) if full else "",
            attachments=attachments,
            unread=b"\\Seen" not in imaplib.ParseFlags(metadata),
        )

    def _list(self, unread: bool, limit: int) -> list[Email]:
        with self._connect() as conn:
            validity = self._validity(conn)
            status, data = conn.uid("search", None, "UNSEEN" if unread else "ALL")
            if status != "OK":
                raise ProviderUnavailable("Не удалось получить список писем.")
            ids = (data[0] or b"").split()[-limit:][::-1]
            result = []
            for uid in ids:
                status, parts = conn.uid(
                    "fetch", uid, "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
                )
                if status != "OK":
                    raise ProviderUnavailable("Не удалось прочитать заголовки письма.")
                for part in parts:
                    if isinstance(part, tuple):
                        result.append(self._parse(validity, uid.decode(), part[1], part[0], False))
            return result

    def _read(self, id: str) -> Email:
        expected, uid = decode_id(id)
        with self._connect() as conn:
            validity = self._validity(conn)
            if expected != validity:
                raise NotFound("Письмо устарело: UIDVALIDITY изменился. Обнови список.")
            status, sizes = conn.uid("fetch", uid, "(RFC822.SIZE)")
            if status != "OK" or not sizes or sizes[0] is None:
                raise NotFound("Письмо не найдено.")
            size_match = re.search(
                rb"RFC822.SIZE (\d+)", b" ".join(p for p in sizes if isinstance(p, bytes))
            )
            if not size_match:
                raise NotFound("Письмо не найдено.")
            if int(size_match[1]) > MAX_MESSAGE_BYTES:
                raise TooLarge("Письмо больше 2 МБ. Открой его в почтовом клиенте.")
            status, parts = conn.uid("fetch", uid, "(UID FLAGS BODY.PEEK[])")
            if status != "OK":
                raise ProviderUnavailable("Не удалось прочитать письмо.")
            for part in parts:
                if isinstance(part, tuple):
                    if len(part[1]) > MAX_MESSAGE_BYTES:
                        raise TooLarge("Письмо больше допустимого размера.")
                    return self._parse(validity, uid, part[1], part[0], True)
            raise NotFound("Письмо не найдено.")

    async def _run(self, function, *args):
        async with self.slots:
            try:
                return await asyncio.get_running_loop().run_in_executor(self.pool, function, *args)
            except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
                raise ProviderUnavailable(
                    "Почта временно недоступна. Проверь соединение и повтори позже."
                ) from exc

    async def list_emails(self, unread: bool, limit: int) -> list[Email]:
        if not 1 <= limit <= self.max_limit:
            raise InvalidInput(f"Лимит писем: от 1 до {self.max_limit}.")
        return await self._run(self._list, unread, limit)

    async def read(self, email_id: str) -> Email:
        return await self._run(self._read, email_id)

    async def close(self):
        await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True)
