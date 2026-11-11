from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_inbox.domain import entities as e
from ai_inbox.domain.errors import Conflict, NotFound

from . import models as m

T = TypeVar("T", bound=e.Entity)


class SqlRepository[T: e.Entity]:
    def __init__(self, session: AsyncSession, row: type[m.Base], entity: type[T]):
        self.session, self.row, self.entity = session, row, entity

    def _entity(self, row: Any) -> T:
        data = {f.name: getattr(row, f.name) for f in fields(self.entity)}
        for key in ("created_at", "updated_at", "due_at"):
            if data.get(key):
                data[key] = datetime.fromisoformat(data[key])
        return self.entity(**data)

    def _values(self, entity: T) -> dict[str, Any]:
        return {
            k: v.isoformat() if isinstance(v, datetime) else v for k, v in asdict(entity).items()
        }

    async def get(self, id: str) -> T:
        row = await self.session.get(self.row, id)
        if row is None:
            raise NotFound("Объект не найден.")
        return self._entity(row)

    async def list(self, **filters: Any) -> list[T]:
        statement = select(self.row)
        for key, value in filters.items():
            if key == "created_before":
                statement = statement.where(self.row.created_at < value.isoformat())
            else:
                statement = statement.where(getattr(self.row, key) == value)
        statement = statement.order_by(self.row.created_at.desc(), self.row.id).limit(500)
        return [self._entity(row) for row in (await self.session.scalars(statement)).all()]

    async def add(self, entity: T) -> None:
        self.session.add(self.row(**self._values(entity)))
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise Conflict("Нарушена уникальность операции или связь объектов.") from exc

    async def save(self, entity: T) -> None:
        row = await self.session.get(self.row, entity.id)
        if row is None:
            raise NotFound("Объект не найден.")
        for key, value in self._values(entity).items():
            setattr(row, key, value)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise Conflict("Нарушена уникальность операции или связь объектов.") from exc

    async def delete(self, id: str) -> None:
        row = await self.session.get(self.row, id)
        if row:
            await self.session.delete(row)


class SqlUnitOfWork:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.session = sessions()
        self.tasks = SqlRepository(self.session, m.TaskRow, e.Task)
        self.drafts = SqlRepository(self.session, m.DraftRow, e.Draft)
        self.conversations = SqlRepository(self.session, m.ConversationRow, e.Conversation)
        self.messages = SqlRepository(self.session, m.MessageRow, e.Message)
        self.actions = SqlRepository(self.session, m.ActionRow, e.Action)
        self.runs = SqlRepository(self.session, m.RunRow, e.ChatRun)
        self.digests = SqlRepository(self.session, m.DigestRow, e.Digest)
        self.media = SqlRepository(self.session, m.MediaRow, e.Media)

    async def __aenter__(self) -> "SqlUnitOfWork":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.session.rollback()
        await self.session.close()

    async def commit(self) -> None:
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise Conflict("Операция уже выполнена или конфликтует с другой записью.") from exc


def database(url: str):
    path = url.removeprefix("sqlite+aiosqlite:///")
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    engine = create_async_engine(url, connect_args={"timeout": 15})

    @event.listens_for(engine.sync_engine, "connect")
    def sqlite_options(conn, _):
        if path != ":memory:":
            Path(path).chmod(0o600)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")

    return engine, async_sessionmaker(engine, expire_on_commit=False)
