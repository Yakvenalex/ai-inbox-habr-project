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

from ai_inbox.domain.entities import Email, EmailDeletion, EmailPage, EmailReadState, MailboxCounts
from ai_inbox.domain.errors import Conflict, InvalidInput, NotFound, ProviderUnavailable, TooLarge

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

    def _connect(self, readonly: bool = True, select_mailbox: bool = True):
        conn = imaplib.IMAP4_SSL(
            self.host, self.port, ssl_context=ssl.create_default_context(), timeout=self.timeout
        )
        try:
            conn.login(self.user, self.password)
            if select_mailbox:
                status, _ = conn.select("INBOX", readonly=readonly)
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
            unread=not any(flag.lower() == b"\\seen" for flag in imaplib.ParseFlags(metadata)),
        )

    def _list(self, unread: bool, limit: int) -> list[Email]:
        return self._page(unread, limit).items

    def _page(self, unread: bool, limit: int, before: str | None = None) -> EmailPage:
        cursor = decode_id(before) if before else None
        with self._connect() as conn:
            validity = self._validity(conn)
            if cursor and cursor[0] != validity:
                raise Conflict("Почтовый ящик изменился. Обнови список и начни с последних писем.")
            criteria = ["UNSEEN" if unread else "ALL"]
            if cursor:
                upper = int(cursor[1]) - 1
                if upper <= 0:
                    return EmailPage(items=[], next_cursor=None)
                criteria += ["UID", f"1:{upper}"]
            status, data = conn.uid("search", None, *criteria)
            if status != "OK":
                raise ProviderUnavailable("Не удалось получить список писем.")
            raw_ids = (data[0] or b"").split()
            if any(not uid.isdigit() or int(uid) <= 0 for uid in raw_ids):
                raise ProviderUnavailable("IMAP вернул некорректные UID писем.")
            ids = sorted(set(raw_ids), key=int, reverse=True)
            if cursor:
                ids = [uid for uid in ids if int(uid) < int(cursor[1])]
            more = len(ids) > limit
            ids = ids[:limit]
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
            return EmailPage(
                items=result,
                next_cursor=email_id(validity, ids[-1].decode()) if more else None,
            )

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

    @staticmethod
    def _fetch_unread(conn, uid: str) -> bool:
        status, data = conn.uid("fetch", uid, "(UID FLAGS)")
        if status != "OK":
            raise ProviderUnavailable("Не удалось проверить статус письма.")
        for item in data or []:
            if isinstance(item, bytes) and re.search(rb"\bUID " + uid.encode() + rb"\b", item):
                if not re.search(rb"\bFLAGS \(", item):
                    raise ProviderUnavailable("IMAP не вернул флаги письма.")
                return not any(flag.lower() == b"\\seen" for flag in imaplib.ParseFlags(item))
        raise NotFound("Письмо не найдено. Обнови список.")

    def _set_unread(self, id: str, unread: bool) -> EmailReadState:
        expected, uid = decode_id(id)
        with self._connect(readonly=False) as conn:
            if expected != self._validity(conn):
                raise NotFound("Письмо устарело: UIDVALIDITY изменился. Обнови список.")
            before = self._fetch_unread(conn, uid)
            if before != unread:
                operation = "-FLAGS.SILENT" if unread else "+FLAGS.SILENT"
                status, _ = conn.uid("store", uid, operation, r"(\Seen)")
                if status != "OK":
                    raise ProviderUnavailable("Почтовый сервер не подтвердил изменение статуса.")
            actual = self._fetch_unread(conn, uid)
            if actual != unread:
                raise Conflict(
                    "Статус письма изменился в другом клиенте или не сохранился. Обнови список."
                )
            return EmailReadState(id=id, unread=actual)

    def _counts(self) -> MailboxCounts:
        with self._connect(select_mailbox=False) as conn:
            status, data = conn.status("INBOX", "(MESSAGES UNSEEN)")
            if status != "OK":
                raise ProviderUnavailable("Не удалось получить счётчики почтового ящика.")
            raw = b" ".join(item for item in data or [] if isinstance(item, bytes))
            total = re.search(rb"\bMESSAGES (\d+)", raw, re.I)
            unseen = re.search(rb"\bUNSEEN (\d+)", raw, re.I)
            if not total or not unseen or int(unseen[1]) > int(total[1]):
                raise ProviderUnavailable("IMAP не вернул корректные счётчики.")
            return MailboxCounts(total=int(total[1]), unread=int(unseen[1]))

    @staticmethod
    def _uid_exists(conn, uid: str) -> bool:
        # SEARCH gives the command's current set, unlike unsolicited old FETCH flags.
        status, data = conn.uid("search", None, "UID", uid)
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise ProviderUnavailable("Не удалось проверить наличие письма на сервере.")
        ids = data[0].split()
        if any(item != uid.encode() for item in ids):
            raise ProviderUnavailable("IMAP вернул некорректный результат проверки письма.")
        return uid.encode() in ids

    @staticmethod
    def _trash_mailbox(conn) -> bytes:
        status, folders = conn.list()
        if status != "OK":
            raise ProviderUnavailable("Не удалось найти корзину на почтовом сервере.")
        candidates = set()
        for folder in folders or []:
            # imaplib represents a literal mailbox name as (prefix, literal).
            prefix = folder[0] if isinstance(folder, tuple) else folder
            if not isinstance(prefix, bytes):
                continue
            match = re.fullmatch(rb'\(([^)]*)\) (?:NIL|"(?:[^"\\]|\\.)*") (.+)', prefix)
            if not match:
                continue
            flags = {f.upper() for f in match[1].split()}
            if b"\\TRASH" not in flags or b"\\NOSELECT" in flags:
                continue
            name = match[2]
            if isinstance(folder, tuple):
                name = folder[1]
                if not re.fullmatch(rb"\{\d+\}", match[2]) or not isinstance(name, bytes):
                    continue
            elif name.startswith(b'"'):
                if not re.fullmatch(rb'"(?:[^"\\]|\\.)*"', name):
                    continue
                name = re.sub(rb"\\(.)", rb"\1", name[1:-1])
            elif re.search(rb'[\s(){}"\\]', name):
                continue
            if (
                not name
                or name.upper() == b"INBOX"
                or any(c in name for c in (b"\r", b"\n", b"\0"))
            ):
                continue
            candidates.add(name)
        if len(candidates) != 1:
            raise Conflict(
                "Сервер не обозначил единственную корзину (\\Trash). Удаление недоступно."
            )
        name = candidates.pop()
        # Preserve the server's mailbox encoding and quote it as one IMAP argument.
        return b'"' + name.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'

    def _delete(self, id: str) -> EmailDeletion:
        expected, uid = decode_id(id)
        with self._connect(readonly=False) as conn:
            if expected != self._validity(conn):
                raise NotFound("Письмо устарело: UIDVALIDITY изменился. Обнови список.")
            if not self._uid_exists(conn, uid):
                # A retry after lost acknowledgement must not move another message.
                return EmailDeletion(id=id, already_absent=True)
            status, data = conn.capability()
            capabilities = b" ".join(data or []).upper().split()
            if status != "OK" or b"MOVE" not in capabilities:
                raise Conflict(
                    "Почтовый сервер не поддерживает безопасное удаление через IMAP MOVE."
                )
            trash = self._trash_mailbox(conn)
            try:
                status, _ = conn.uid("move", uid, trash)
            except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
                raise ProviderUnavailable(
                    "Не удалось подтвердить удаление. Обнови входящие и проверь корзину на почте."
                ) from exc
            if status != "OK":
                raise ProviderUnavailable(
                    "Сервер не подтвердил перенос в корзину. Обнови входящие и проверь корзину."
                )
            if not self._uid_exists(conn, uid):
                return EmailDeletion(id=id)
            raise Conflict("Письмо осталось во входящих. Удаление не подтверждено сервером.")

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

    async def page(self, unread: bool, limit: int, before: str | None = None) -> EmailPage:
        if not 1 <= limit <= self.max_limit:
            raise InvalidInput(f"Лимит писем: от 1 до {self.max_limit}.")
        return await self._run(self._page, unread, limit, before)

    async def read(self, email_id: str) -> Email:
        return await self._run(self._read, email_id)

    async def set_unread(self, email_id: str, unread: bool) -> EmailReadState:
        return await self._run(self._set_unread, email_id, unread)

    async def counts(self) -> MailboxCounts:
        return await self._run(self._counts)

    async def delete(self, email_id: str) -> EmailDeletion:
        return await self._run(self._delete, email_id)

    async def close(self):
        await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True)
