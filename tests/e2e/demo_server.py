"""Offline demo for screenshots and browser checks. Never reads .env.

uv run python -m tests.e2e.demo_server
Login: demo / demo-inbox-only. Bind: 127.0.0.1:18764.
"""

import asyncio
import subprocess

import uvicorn
from alembic import command
from alembic.config import Config

from ai_inbox.bootstrap.app import create_app
from ai_inbox.bootstrap.container import Container
from ai_inbox.bootstrap.settings import ROOT, Settings
from tests.fakes import FakeAssistant, FakeMail, FakeSpeech


class DemoAssistant(FakeAssistant):
    async def summarize(self, context):
        return await asyncio.to_thread((ROOT / "tests/fixtures/summary.md").read_text)

    async def chat(self, history, execute, now):
        content = history[-1]["content"]
        text = (
            content
            if isinstance(content, str)
            else next(item["text"] for item in content if item["type"] == "input_text")
        )
        lower = text.lower()
        if "созда" in lower and "задач" in lower:
            result = await execute(
                "create_task",
                {
                    "title": "Проверить учебный счёт ЖЮ-3082",
                    "notes": "Синтетический документ: 89 406,75 руб.",
                    "due_at": "2026-11-19T09:20:00+03:00",
                    "source": "email:synthetic-email",
                },
                "demo-create",
            )
            answer = (
                "Сохранена задача " + result.get("id", "") + ". Срок: 19 ноября, 09:20 по Москве."
            )
        elif "чернов" in lower:
            result = await execute(
                "create_draft",
                {
                    "email_id": "synthetic-email",
                    "subject": "Re: Учебный счёт ЖЮ-3082",
                    "body": "Здравствуйте! Проверю учебный счёт до 19 ноября, 09:20. Спасибо.",
                },
                "demo-draft",
            )
            answer = (
                "Локальный черновик " + result.get("id", "") + " сохранён. Письмо не отправлено."
            )
        elif "дайджест" in lower:
            result = await execute("generate_digest", {}, "demo-digest")
            answer = result.get("text", "Дайджест сформирован.")
        elif "задач" in lower:
            result = await execute("list_tasks", {"status": "open"}, "demo-list")
            answer = "Открытые задачи:\n" + "\n".join(t["title"] for t in result["tasks"])
        else:
            result = await execute("read_email", {"email_id": "synthetic-email"}, "demo-read")
            answer = (
                "Учебное письмо просит проверить счёт ЖЮ-3082 на 89 406,75 руб. "
                "до 19 ноября в 09:20 по Москве."
            )
        return answer, history + [{"role": "assistant", "content": answer}]


class DemoSpeech(FakeSpeech):
    async def synthesize(self, text):
        return await asyncio.to_thread((ROOT / "data/demo/tone.mp3").read_bytes)


async def prepare():
    folder = ROOT / "data/demo"
    folder.mkdir(parents=True, exist_ok=True)
    # Sound is deliberately synthetic; no paid API call for browser demos.
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "libmp3lame",
            str(folder / "tone.mp3"),
        ],
        check=True,
    )
    settings = Settings(
        _env_file=None,
        app_env="demo",
        auth_username="demo",
        auth_password="demo-inbox-only",
        jwt_secret_key="public-demo-key-not-for-real-use-1234567890",
        main_key="fake-demo-provider-key",
        mail_user="demo@example.test",
        mail_pass="fake-demo-mail-password",
        digest_enabled=False,
        database_url=f"sqlite+aiosqlite:///{folder}/demo.sqlite3",
        media_dir=folder / "media",
        app_base_url="http://127.0.0.1:18764",
        app_port=18764,
    )
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = settings.database_url
    await asyncio.to_thread(command.upgrade, cfg, "head")
    return settings


def main():
    settings = asyncio.run(prepare())
    c = Container(settings, mail=FakeMail(), assistant=DemoAssistant(), speech=DemoSpeech())
    uvicorn.run(create_app(settings, c), host="127.0.0.1", port=18764, access_log=False)


if __name__ == "__main__":
    main()
