import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from ai_inbox.bootstrap.settings import Settings
from ai_inbox.infrastructure.persistence.models import Base


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    # Tests may provide a temporary URL without reading the real configuration.
    url = context.config.attributes.get("database_url") or Settings().database_url
    if url != "sqlite+aiosqlite:///:memory:":
        from pathlib import Path

        Path(url.removeprefix("sqlite+aiosqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(run)
    await engine.dispose()


asyncio.run(online())
