from datetime import UTC, datetime

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from openai import AsyncOpenAI
from sqlalchemy import text

from ai_inbox.application.auth import Authenticate, ValidateAccessToken
from ai_inbox.application.chat import ChatWithAssistant
from ai_inbox.application.digests import GenerateDigest
from ai_inbox.application.drafts import Drafts
from ai_inbox.application.mail import Mail
from ai_inbox.application.media import MediaService
from ai_inbox.application.tasks import Tasks
from ai_inbox.application.tools import AssistantTools
from ai_inbox.infrastructure.ai.assistant import AnyModelAssistant
from ai_inbox.infrastructure.auth.jwt import JwtTokens
from ai_inbox.infrastructure.mail.imap import ImapMail
from ai_inbox.infrastructure.persistence.uow import SqlUnitOfWork, database
from ai_inbox.infrastructure.scheduling.scheduler import make_scheduler
from ai_inbox.infrastructure.speech.anymodel import AnyModelSpeech
from ai_inbox.infrastructure.storage.local import LocalStorage

from .settings import ROOT, Settings


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class Container:
    def __init__(self, settings: Settings, *, mail=None, assistant=None, speech=None, clock=None):
        self.settings = settings
        self.clock = clock or SystemClock()
        self.engine, sessions = database(settings.database_url)
        self.uow = lambda: SqlUnitOfWork(sessions)
        tokens = JwtTokens(
            settings.auth_username,
            settings.auth_password.get_secret_value(),
            settings.jwt_secret_key.get_secret_value(),
            settings.jwt_issuer,
            settings.jwt_audience,
            settings.access_token_expire_minutes,
            self.clock,
        )
        self.authenticate = Authenticate(tokens)
        self.validate_token = ValidateAccessToken(tokens)
        self.http = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            headers={
                "Authorization": "Bearer " + settings.main_key.get_secret_value(),
                "User-Agent": "AI-Inbox/0.1",
            },
            timeout=settings.ai_timeout_seconds,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
        )
        self.openai = AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.main_key.get_secret_value(),
            max_retries=0,
            timeout=settings.ai_timeout_seconds,
        )
        self.assistant = assistant or AnyModelAssistant(
            self.openai,
            settings.llm_model,
            settings.ai_max_attempts,
            settings.agent_max_turns,
            settings.app_timezone,
        )
        self.mail_gateway = mail or ImapMail(
            settings.mail_imap_host,
            settings.mail_imap_port,
            settings.mail_user,
            settings.mail_pass.get_secret_value(),
            settings.mail_timeout_seconds,
            settings.mail_fetch_limit,
        )
        self.speech_gateway = speech or AnyModelSpeech(
            self.http,
            settings.asr_model,
            settings.asr_language,
            settings.tts_model,
            settings.ai_max_attempts,
        )
        self.storage = LocalStorage(settings.media_dir)
        self.tasks = Tasks(self.uow, self.clock)
        self.mail = Mail(self.mail_gateway, self.assistant)
        self.drafts = Drafts(self.uow, self.mail_gateway, self.clock)
        self.media = MediaService(
            self.uow, self.storage, self.speech_gateway, self.clock, settings.audio_retention_days
        )
        self.digests = GenerateDigest(
            self.uow, self.mail, self.tasks, self.assistant, self.clock, settings.app_timezone
        )
        self.chat = ChatWithAssistant(
            self.uow,
            self.assistant,
            self.storage,
            lambda request_id, user_text: AssistantTools(
                self.uow, self.tasks, self.drafts, self.mail, self.digests, request_id, user_text
            ),
            self.clock,
        )
        self.scheduler = make_scheduler(settings, self.digests, self.media)

    async def start(self):
        try:
            async with self.engine.connect() as connection:
                actual = (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
                expected = ScriptDirectory.from_config(
                    Config(str(ROOT / "alembic.ini"))
                ).get_current_head()
                if actual != expected:
                    raise RuntimeError("Database migration is out of date")
        except Exception:
            await self.close()
            raise RuntimeError("Сначала выполни uv run alembic upgrade head") from None
        await self.chat.recover_interrupted()
        await self.media.cleanup()
        self.scheduler.start()

    async def close(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if hasattr(self.mail_gateway, "close"):
            await self.mail_gateway.close()
        await self.openai.close()
        await self.http.aclose()
        await self.engine.dispose()
