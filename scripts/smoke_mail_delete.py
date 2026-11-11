"""Opt-in real IMAP deletion smoke: creates and removes ONLY its own synthetic message.

uv run python scripts/smoke_mail_delete.py
No SMTP, AI, existing mail deletion or mailbox-wide EXPUNGE.
"""

import asyncio
import json
import re
from email import message_from_bytes
from email.message import EmailMessage
from uuid import uuid4

from ai_inbox.bootstrap.settings import Settings
from ai_inbox.domain.errors import ProviderUnavailable
from ai_inbox.infrastructure.mail.imap import ImapMail, email_id


def check(gateway):
    marker = str(uuid4())
    message_id = f"<{marker}@example.test>"
    message = EmailMessage()
    message["From"] = "AI Inbox demo <demo@example.test>"
    message["To"] = "AI Inbox demo <demo@example.test>"
    message["Subject"] = "AI Inbox synthetic deletion smoke (disposable)"
    message["Message-ID"] = message_id
    message["X-AI-Inbox-Smoke"] = marker
    message.set_content("Disposable synthetic IMAP test. Not sent through SMTP.")

    def find_owned(conn):
        status, data = conn.uid("search", None, "HEADER", "Message-ID", f'"{message_id}"')
        if status != "OK":
            raise ProviderUnavailable("Не удалось найти синтетическое письмо для проверки.")
        ids = (data[0] or b"").split()
        for uid in ids:
            status, parts = conn.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID X-AI-INBOX-SMOKE)])"
            )
            raw = next((part[1] for part in parts or [] if isinstance(part, tuple)), b"")
            headers = message_from_bytes(raw)
            if (
                status != "OK"
                or headers.get("Message-ID") != message_id
                or headers.get("X-AI-Inbox-Smoke") != marker
            ):
                raise ProviderUnavailable(
                    "Не удалось подтвердить принадлежность синтетического письма; "
                    "очистка остановлена."
                )
        return ids

    with gateway._connect(readonly=False) as conn:
        status, caps = conn.capability()
        if status != "OK" or not {b"MOVE", b"UIDPLUS"} <= set(b" ".join(caps).upper().split()):
            raise ProviderUnavailable("Для smoke нужны MOVE и UIDPLUS. Письмо не создавалось.")
        trash = gateway._trash_mailbox(conn)
        validity = gateway._validity(conn)
        try:
            status, _ = conn.append("INBOX", r"(\Seen)", None, message.as_bytes())
            if status != "OK":
                raise ProviderUnavailable("Сервер не подтвердил создание синтетического письма.")
            ids = find_owned(conn)
            assert len(ids) == 1, "Expected one owned synthetic message"
            id = email_id(validity, ids[0].decode())
            moved = gateway._delete(id)
            assert moved.deleted and not moved.already_absent, "First deletion did not confirm MOVE"
            assert gateway._delete(id).already_absent, "Retry did not observe absent source UID"
            # A separate authenticated connection verifies the server-side destination.
            with gateway._connect(select_mailbox=False) as verify:
                status, _ = verify.select(trash, readonly=True)
                assert status == "OK", "Could not select server trash"
                assert len(find_owned(verify)) == 1, "Moved synthetic message not found in trash"
            assert not gateway._uid_exists(conn, ids[0].decode()), (
                "Synthetic message remained in INBOX"
            )
        finally:
            # Cleanup never touches existing mail: exact generated headers, exact UIDs.
            with gateway._connect(select_mailbox=False) as cleanup:
                for mailbox in ("INBOX", trash):
                    status, _ = cleanup.select(mailbox, readonly=False)
                    if status != "OK":
                        raise ProviderUnavailable(
                            "Не удалось очистить синтетическое письмо. Проверь корзину."
                        )
                    for uid in find_owned(cleanup):
                        assert re.fullmatch(rb"[0-9]+", uid), "Invalid synthetic UID"
                        status, _ = cleanup.uid("store", uid, "+FLAGS.SILENT", r"(\Deleted)")
                        if status != "OK":
                            raise ProviderUnavailable(
                                "Не удалось пометить синтетическое письмо для очистки."
                            )
                        status, _ = cleanup.uid("expunge", uid)
                        if status != "OK":
                            raise ProviderUnavailable(
                                "Не удалось очистить синтетическое письмо по UID."
                            )
                    assert not find_owned(cleanup), "Synthetic message cleanup was not confirmed"
    return {
        "status": "passed",
        "synthetic_messages": 1,
        "server_trash_verified": True,
        "idempotent_retry": True,
        "synthetic_cleanup": True,
        "existing_mail_deleted": 0,
        "smtp_messages_sent": 0,
        "ai_calls": 0,
    }


async def main():
    s = Settings()
    gateway = ImapMail(
        s.mail_imap_host,
        s.mail_imap_port,
        s.mail_user,
        s.mail_pass.get_secret_value(),
        s.mail_timeout_seconds,
        s.mail_fetch_limit,
    )
    try:
        try:
            result = await gateway._run(check, gateway)
        except ProviderUnavailable as exc:
            result = {"status": "provider_unavailable", "message": str(exc)}
        except AssertionError as exc:
            result = {"status": "failed", "message": str(exc)}
        except Exception as exc:
            result = {"status": "failed", "error_type": type(exc).__name__}
        print(json.dumps(result, ensure_ascii=False))
        return int(result["status"] != "passed")
    finally:
        await gateway.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
