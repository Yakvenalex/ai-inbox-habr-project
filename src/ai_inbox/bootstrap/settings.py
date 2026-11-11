from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )
    app_env: str = "local"
    app_host: str = "127.0.0.1"
    app_port: int = Field(18763, ge=1, le=65535)
    app_base_url: str = "http://127.0.0.1:18763"
    app_timezone: str = "Europe/Moscow"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/ai-inbox.sqlite3"
    media_dir: Path = Path("data/media")
    auth_username: str
    auth_password: SecretStr
    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_issuer: str = "ai-inbox-local"
    jwt_audience: str = "ai-inbox"
    access_token_expire_minutes: int = Field(60, ge=1, le=1440)
    auth_cookie_name: str = "ai_inbox_access"
    auth_cookie_secure: bool = False
    base_url: str = "https://anymodel.org/v1"
    main_key: SecretStr
    llm_model: str = "ag/gemini-3.8-flash-low"
    llm_fallback_model: str = "cx/gpt-6.1-terra"
    llm_fallback_enabled: bool = False
    asr_model: str = "dg/nova-3"
    asr_fallback_model: str = "dg/nova-2"
    asr_language: str = "ru"
    tts_model: str = "anyvoice/builtin-7"
    ai_timeout_seconds: float = Field(120, gt=0, le=300)
    anymodel_trust_env: bool = True
    ai_max_attempts: int = Field(2, ge=1, le=2)
    agent_max_turns: int = Field(8, ge=1, le=12)
    mail_user: str
    mail_pass: SecretStr
    mail_imap_host: str = "imap.mail.ru"
    mail_imap_port: int = 993
    mail_smtp_host: str = "smtp.mail.ru"
    mail_smtp_port: int = 465
    mail_timeout_seconds: float = Field(30, gt=0, le=120)
    mail_fetch_limit: int = Field(20, ge=1, le=50)
    digest_enabled: bool = True
    digest_hour: int = Field(9, ge=0, le=23)
    digest_minute: int = Field(0, ge=0, le=59)
    digest_misfire_grace_seconds: int = Field(3600, ge=1, le=3600)
    max_upload_mb: int = Field(20, ge=1, le=50)
    audio_retention_days: int = Field(7, ge=1)

    @model_validator(mode="after")
    def validate_config(self) -> "Settings":
        for name in ("auth_password", "jwt_secret_key", "main_key", "mail_pass"):
            secret = getattr(self, name).get_secret_value()
            if not secret or secret.startswith("replace-"):
                raise ValueError(f"Задай {name.upper()} в локальном .env (значение скрыто)")
        if (
            not self.auth_username.strip()
            or len(self.jwt_secret_key.get_secret_value().encode()) < 32
        ):
            raise ValueError("Нужны непустой логин и независимый JWT_SECRET_KEY >= 32 байт")
        if self.auth_password.get_secret_value() in (
            self.mail_pass.get_secret_value(),
            self.main_key.get_secret_value(),
            self.jwt_secret_key.get_secret_value(),
        ):
            raise ValueError("Пароль приложения должен быть независим от остальных секретов")
        secrets = [
            self.jwt_secret_key.get_secret_value(),
            self.main_key.get_secret_value(),
            self.mail_pass.get_secret_value(),
        ]
        if len(set(secrets)) != len(secrets):
            raise ValueError("JWT, почтовый пароль и API-ключ должны быть независимыми")
        if self.llm_fallback_enabled:
            raise ValueError("Автоfallback требует отдельной проверки резерва; оставь false")
        ZoneInfo(self.app_timezone)
        if not self.database_url.startswith("sqlite+aiosqlite:///"):
            raise ValueError("В этой версии поддерживается SQLite через aiosqlite")
        db = self.database_url.removeprefix("sqlite+aiosqlite:///")
        if db != ":memory:":
            self.database_url = "sqlite+aiosqlite:///" + str((ROOT / db).resolve())
        self.media_dir = (ROOT / self.media_dir).resolve()
        self.app_base_url = self.app_base_url.rstrip("/")
        return self
