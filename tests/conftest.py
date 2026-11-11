from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config

from ai_inbox.bootstrap.app import create_app
from ai_inbox.bootstrap.container import Container
from ai_inbox.bootstrap.settings import Settings
from tests.fakes import FakeAssistant, FakeMail, FakeSpeech

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def settings(tmp_path):
    return Settings(
        _env_file=None,
        auth_username="demo",
        auth_password="test-owner-password",
        jwt_secret_key="test-independent-jwt-secret-over-32-bytes",
        main_key="fake-api-key",
        mail_user="demo@example.test",
        mail_pass="test-mail-password",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.sqlite3",
        media_dir=tmp_path / "media",
        digest_enabled=False,
    )


@pytest.fixture
async def client(settings):
    import asyncio

    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    await asyncio.to_thread(command.upgrade, config, "head")
    c = Container(settings, mail=FakeMail(), assistant=FakeAssistant(), speech=FakeSpeech())
    app = create_app(settings, c)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.app_base_url
        ) as client:
            client.container = c
            yield client


@pytest.fixture
async def auth(client):
    response = await client.post(
        "/api/v1/auth/token", data={"username": "demo", "password": "test-owner-password"}
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Обычные тесты не должны обращаться в сеть")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
