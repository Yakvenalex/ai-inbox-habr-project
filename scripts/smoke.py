"""Opt-in paid smoke checks. No mail is ever sent; model inputs are synthetic.

uv run python scripts/smoke.py --mail --ai --speech
"""

import argparse
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config

from ai_inbox.application.outgoing_dto import DeliveryResult
from ai_inbox.bootstrap.container import Container
from ai_inbox.bootstrap.settings import ROOT, Settings
from ai_inbox.domain.entities import new_id
from ai_inbox.domain.errors import AppError, ProviderUnavailable
from ai_inbox.infrastructure.mail.imap import decode_id


def flags(mail, id):
    validity, uid = decode_id(id)
    with mail._connect() as conn:
        assert mail._validity(conn) == validity
        status, data = conn.uid("fetch", uid, "(FLAGS)")
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise RuntimeError("IMAP FLAGS unavailable")
        import imaplib

        return set(imaplib.ParseFlags(data[0]))


async def check_mail(c):
    messages = await c.mail.list_emails(True, 3)
    if not messages:
        messages = await c.mail.list_emails(False, 1)
    if not messages:
        return {"status": "passed", "count": 0, "read_flags": "not_tested_empty_mailbox"}
    id = messages[0]["id"]
    before = await c.mail_gateway._run(flags, c.mail_gateway, id)
    message = await c.mail.read(id)
    after = await c.mail_gateway._run(flags, c.mail_gateway, id)
    assert before == after, "IMAP flags changed"
    assert message["id"] == id
    return {"status": "passed", "sample_count": len(messages), "flags_unchanged": True}


async def check_ai(c):
    image = (ROOT / "tests/fixtures/invoice.png").read_bytes()
    saved = await c.media.save_image(image, "image/png")
    conversation = await c.chat.create_conversation("Synthetic vision smoke")
    result = await c.chat(
        conversation["id"],
        "Прочитай учебный документ и создай задачу проверить счёт с указанным сроком. "
        "Сохрани номер и сумму с копейками в заметках. Не отправляй письма. "
        "Источник image:" + saved["file_id"],
        new_id(),
        saved["file_id"],
    )
    if result["status"] != "completed":
        return {
            "status": "provider_unavailable"
            if result["error"] == "provider_unavailable"
            else "failed",
            "error": result["error"],
            "saved_actions": len(result["actions"]),
        }
    tasks = await c.tasks.list()
    assert len(tasks) == 1, "Expected one persisted task"
    task = tasks[0]
    assert task["due_at"] == "2026-11-19T06:20:00+00:00", "Wrong deadline"
    digits = "".join(ch for ch in task["notes"] if ch.isdigit())
    assert "8940675" in digits, "Wrong amount"
    assert "ЖЮ-3082" in task["notes"], "Wrong document identifier"
    assert task["id"] in result["text"], "Final answer missing persisted ID"
    return {
        "status": "passed",
        "vision": True,
        "persisted_tasks": 1,
        "deadline": True,
        "amount": True,
        "document_id": True,
        "grounded_reply": True,
        "tool_results": len(result["actions"]),
    }


async def check_speech(c, out):
    media = await c.media.synthesize("Создай задачу проверить учебный счёт.")
    info = await c.media.get(media["file_id"])
    mp3 = await c.storage.read(info["filename"])
    await asyncio.to_thread((out / "synthetic-speech.mp3").write_bytes, mp3)
    webm = out / "synthetic-speech.webm"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(out / "synthetic-speech.mp3"),
            "-c:a",
            "libopus",
            str(webm),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result = await c.media.transcribe(await asyncio.to_thread(webm.read_bytes), "audio/webm")
    await asyncio.to_thread((out / "transcription.txt").write_text, result["text"])
    assert result["confirmation_required"] is True
    assert "сч" in result["text"].lower() and "задач" in result["text"].lower(), (
        "Unexpected ASR text"
    )
    assert not await c.tasks.list(), "ASR must not execute a command"
    return {
        "status": "passed",
        "mp3_bytes": len(mp3),
        "webm_asr": True,
        "requires_confirmation": True,
    }


async def check_smtp(c):
    await c.sender.check_connection()
    return {"status": "passed", "tls_login_noop": True, "messages_sent": 0}


async def check_compose(c):
    result = await c.outbox.compose(
        "Кратко и вежливо попроси уточнить реквизиты учебного счёта ЖЮ-3082. "
        "Сроков и обещаний нет, не добавляй их. Это синтетический пример."
    )
    assert result["subject"] and result["body"], "Empty email composition"
    assert "3082" in result["subject"] + result["body"], "Document identifier lost"
    assert not await c.outbox.list(), "Composition must not create or send a letter"
    return {
        "status": "passed",
        "subject_and_body": True,
        "document_preserved": True,
        "messages_sent": 0,
    }


async def check_outgoing_agent(c):
    conversation = await c.chat.create_conversation("Synthetic outgoing draft")
    result = await c.chat(
        conversation["id"],
        "Отправь на почту reader@example.test просьбу уточнить реквизиты учебного счёта "
        "ЖЮ-3082, но перепиши вежливо. Это синтетическая проверка. Не придумывай сроки.",
        new_id(),
    )
    if result["status"] != "completed":
        return {
            "status": "provider_unavailable"
            if result["error"] == "provider_unavailable"
            else "failed",
            "error": result["error"],
        }
    letters = await c.outbox.list()
    assert len(letters) == 1, "Expected one outgoing draft"
    assert letters[0]["status"] == "draft" and letters[0]["recipient"] == "reader@example.test"
    assert letters[0]["id"] in result["text"], "Missing persisted outgoing ID in final answer"
    return {"status": "passed", "prepared_drafts": 1, "messages_sent": 0, "grounded_reply": True}


async def check_chat_send(c):
    # The model is real; SMTP is deliberately replaced for this paid workflow check.
    class NoNetworkSender:
        def __init__(self):
            self.messages = []

        async def send(self, message):
            self.messages.append(message)
            return DeliveryResult("sent")

    simulated = NoNetworkSender()
    original_sender = c.outbox.sender
    c.outbox.sender = simulated
    try:
        conversation = await c.chat.create_conversation("Synthetic confirmed chat send")
        result = await c.chat(
            conversation["id"],
            "Отправь на почту reader@example.test приветствие и спроси, как дела. "
            "Перепиши вежливо. Это синтетическая проверка.",
            new_id(),
        )
        if result["status"] != "completed":
            return {
                "status": "provider_unavailable"
                if result["error"] == "provider_unavailable"
                else "failed",
                "error": result["error"],
                "real_messages_sent": 0,
            }
        previews = [
            a["result"]
            for a in result["actions"]
            if a["name"] == "prepare_outgoing_email" and not a["result"].get("error")
        ]
        assert len(previews) == 1 and previews[0]["confirmation_available"]
        assert not simulated.messages, "Must not send while preparing"
        request_id = new_id()
        sent = await c.chat(conversation["id"], "Всё ок, отправляй", request_id)
        assert sent["status"] == "completed"
        assert sent["actions"][0]["result"]["status"] == "sent"
        assert len(simulated.messages) == 1
        assert simulated.messages[0].recipient == "reader@example.test"
        assert simulated.messages[0].body == previews[0]["body"]
        assert await c.chat(conversation["id"], "Всё ок, отправляй", request_id) == sent
        assert len(simulated.messages) == 1
        return {
            "status": "passed",
            "model": "real",
            "smtp": "simulated",
            "review_then_confirmation": True,
            "idempotent": True,
            "real_messages_sent": 0,
        }
    finally:
        c.outbox.sender = original_sender


async def check_stream(c):
    events = []

    async def collect(event):
        events.append(event)

    conversation = await c.chat.create_conversation("Synthetic agent streaming")
    result = await c.chat(
        conversation["id"],
        "Создай задачу «Проверить учебный стриминг» без срока. Затем объясни в трёх "
        "кратких пунктах, как читать потоковый ответ. В ответ включи реальный ID созданной "
        "задачи из результата инструмента. Это синтетический пример.",
        new_id(),
        emit=collect,
    )
    if result["status"] != "completed":
        return {
            "status": "provider_unavailable"
            if result["error"] == "provider_unavailable"
            else "failed",
            "error": result["error"],
            "events": len(events),
        }
    deltas = [e for e in events if e["type"] == "delta"]
    tasks = await c.tasks.list()
    assert deltas and len(tasks) == 1, "Expected streamed text and a persisted task"
    assert tasks[0]["id"] in result["text"], "Final answer must reference the real task"
    assert any(e["type"] == "tool_start" for e in events)
    assert any(e["type"] == "tool_end" and e["ok"] for e in events)
    saved = await c.chat.messages(conversation["id"])
    assert saved[-1]["text"] == result["text"]
    return {
        "status": "passed",
        "text_chunks": len(deltas),
        "tool_progress": True,
        "persisted_tasks": 1,
        "grounded_reply": True,
        "messages_sent": 0,
        "provider_buffered_single_chunk": len(deltas) == 1,
    }


async def check_mail_read_state(c):
    messages = await c.mail.list_emails(False, 1)
    if not messages:
        return {"status": "not_tested", "reason": "empty_mailbox"}
    id = messages[0]["id"]
    before = await c.mail_gateway._run(flags, c.mail_gateway, id)
    original_unread = not any(flag.lower() == b"\\seen" for flag in before)
    try:
        state = await c.mail.set_unread(id, not original_unread)
        after = await c.mail_gateway._run(flags, c.mail_gateway, id)
        assert state["unread"] == (not original_unread)
        assert (not any(flag.lower() == b"\\seen" for flag in after)) == state["unread"]

        def other(values):
            return {f.lower() for f in values if f.lower() not in {b"\\seen", b"\\recent"}}

        assert other(before) == other(after), "Other IMAP flags changed"
        assert (await c.mail.counts())["total"] >= 1
    finally:
        try:
            await c.mail.set_unread(id, original_unread)
            restored = await c.mail_gateway._run(flags, c.mail_gateway, id)
            assert {f.lower() for f in before if f.lower() != b"\\recent"} == {
                f.lower() for f in restored if f.lower() != b"\\recent"
            }, "Original flags were not restored"
        except Exception as exc:
            raise ProviderUnavailable(
                "Не удалось подтвердить восстановление статуса письма. Проверь его вручную."
            ) from exc
    return {
        "status": "passed",
        "changed_messages": 1,
        "server_confirmed": True,
        "original_flags_restored": True,
        "other_flags_preserved": True,
    }


async def main(args):
    out = ROOT / "data/smoke"
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix="ai-inbox-smoke-") as temp:
        settings = Settings().model_copy(
            update={
                "database_url": f"sqlite+aiosqlite:///{temp}/smoke.sqlite3",
                "media_dir": Path(temp) / "media",
                "digest_enabled": False,
            }
        )
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.attributes["database_url"] = settings.database_url
        await asyncio.to_thread(command.upgrade, cfg, "head")
        c = Container(settings)
        await c.start()
        try:
            for name, enabled, check in [
                ("mail", args.mail, lambda: check_mail(c)),
                ("mail_read_state", args.mail_read_state, lambda: check_mail_read_state(c)),
                ("smtp", args.smtp, lambda: check_smtp(c)),
                ("compose", args.compose, lambda: check_compose(c)),
                ("outgoing_agent", args.outgoing_agent, lambda: check_outgoing_agent(c)),
                ("chat_send", args.chat_send, lambda: check_chat_send(c)),
                ("stream", args.stream, lambda: check_stream(c)),
                ("speech", args.speech, lambda: check_speech(c, out)),
                ("ai", args.ai, lambda: check_ai(c)),
            ]:
                if not enabled:
                    continue
                try:
                    results[name] = await check()
                except AppError as exc:
                    results[name] = {
                        "status": "provider_unavailable"
                        if isinstance(exc, ProviderUnavailable)
                        else "failed",
                        "code": exc.code,
                        "message": str(exc),
                    }
                except AssertionError as exc:
                    results[name] = {"status": "failed", "message": str(exc)}
                except Exception as exc:
                    results[name] = {"status": "failed", "error_type": type(exc).__name__}
                print(json.dumps({name: results[name]}, ensure_ascii=False), flush=True)
        finally:
            await c.close()
    (out / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results and all(v["status"] == "passed" for v in results.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mail", action="store_true", help="Read up to three messages, compare flags"
    )
    parser.add_argument(
        "--ai", action="store_true", help="PAID vision + real tool cycle on synthetic invoice"
    )
    parser.add_argument(
        "--speech", action="store_true", help="PAID TTS + WebM ASR on synthetic command"
    )
    parser.add_argument(
        "--smtp", action="store_true", help="TLS login and NOOP only; never sends mail"
    )
    parser.add_argument(
        "--compose",
        action="store_true",
        help="PAID subject/body composition from synthetic outline; no sending",
    )
    parser.add_argument(
        "--outgoing-agent",
        action="store_true",
        help="PAID agent prepares a synthetic outgoing draft; never sends",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="PAID real streaming model and tool cycle on synthetic input; no mail sent",
    )
    parser.add_argument(
        "--chat-send",
        action="store_true",
        help="PAID real model prepares a draft; chat confirmation uses simulated SMTP only",
    )
    parser.add_argument(
        "--mail-read-state",
        action="store_true",
        help="Toggle Seen on one message, verify, then restore original flags; no mail sent",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
