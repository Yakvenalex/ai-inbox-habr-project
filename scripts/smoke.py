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

from ai_inbox.bootstrap.container import Container
from ai_inbox.bootstrap.settings import ROOT, Settings
from ai_inbox.domain.entities import new_id
from ai_inbox.domain.errors import AppError
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
    c.assistant.max_turns = 4
    original = c.assistant.model.get_response

    async def diagnosed(*args, **kwargs):
        response = await original(*args, **kwargs)
        diagnostic = []
        for item in response.output:
            data = item.model_dump()
            if data.get("type") == "function_call":
                try:
                    keys = list(json.loads(data.get("arguments", "{}")))
                except ValueError:
                    keys = ["invalid_json"]
                diagnostic.append({"type": "function_call", "name": data.get("name"), "keys": keys})
            else:
                diagnostic.append(
                    {"type": data.get("type"), "content_length": len(str(data.get("content", "")))}
                )
        print(json.dumps({"model_output_shape": diagnostic}, ensure_ascii=False), flush=True)
        return response

    c.assistant.model.get_response = diagnosed
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
            "status": "provider_unavailable",
            "error": result["error"],
            "saved_actions": len(result["actions"]),
        }
    tasks = await c.tasks.list()
    (ROOT / "data/smoke/vision-debug.json").write_text(
        json.dumps({"response": result, "tasks": tasks}, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                "vision_diagnostic": {
                    "task_count": len(tasks),
                    "actions": [
                        {"name": a["name"], "error": a["result"].get("error")}
                        for a in result["actions"]
                    ],
                }
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
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
                ("speech", args.speech, lambda: check_speech(c, out)),
                ("ai", args.ai, lambda: check_ai(c)),
            ]:
                if not enabled:
                    continue
                try:
                    results[name] = await check()
                except AppError as exc:
                    results[name] = {
                        "status": "provider_unavailable",
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
    raise SystemExit(asyncio.run(main(parser.parse_args())))
