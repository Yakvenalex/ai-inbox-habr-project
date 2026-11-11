from dataclasses import asdict
from datetime import datetime
from typing import Any

from ai_inbox.domain.entities import Action, Task, TaskStatus, aware
from ai_inbox.domain.errors import InvalidInput

from .ports import Clock, UnitOfWork, UowFactory


async def existing_action(uow: UnitOfWork, key: str | None) -> dict[str, Any] | None:
    if key and (actions := await uow.actions.list(key=key)):
        return actions[0].result
    return None


def public(entity: Any) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return dict(convert(asdict(entity)))


async def record_action(
    uow: UnitOfWork,
    key: str | None,
    name: str,
    result: dict[str, Any],
    request_id: str = "",
    call_id: str = "",
) -> None:
    if key:
        await uow.actions.add(
            Action(key=key, request_id=request_id, tool_call_id=call_id, name=name, result=result)
        )


class Tasks:
    def __init__(self, uow: UowFactory, clock: Clock):
        self.uow, self.clock = uow, clock

    async def create(
        self,
        title: str,
        notes: str = "",
        due_at: datetime | None = None,
        source: str = "manual",
        *,
        key: str | None = None,
        request_id: str = "",
        call_id: str = "",
    ) -> dict[str, Any]:
        task = Task(
            title=title,
            notes=notes,
            due_at=due_at,
            source=source,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        if len(notes) > 10000 or len(source) > 500:
            raise InvalidInput("Слишком длинные заметки или источник задачи.")
        async with self.uow() as uow:
            if cached := await existing_action(uow, key):
                return cached
            await uow.tasks.add(task)
            result = public(task)
            await record_action(uow, key, "create_task", result, request_id, call_id)
            await uow.commit()
            return result

    async def list(
        self,
        status: str | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in TaskStatus:
            raise InvalidInput("Неизвестный статус задачи.")
        before, after = aware(due_before), aware(due_after)
        async with self.uow() as uow:
            tasks = await uow.tasks.list(**({"status": status} if status else {}))
        return [
            public(t)
            for t in tasks
            if (before is None or t.due_at is not None and t.due_at <= before)
            and (after is None or t.due_at is not None and t.due_at >= after)
        ]

    async def get(self, id: str) -> dict[str, Any]:
        async with self.uow() as uow:
            return public(await uow.tasks.get(id))

    async def edit(
        self,
        id: str,
        changes: dict[str, Any],
        *,
        key: str | None = None,
        request_id: str = "",
        call_id: str = "",
    ) -> dict[str, Any]:
        if not changes or set(changes) - {"title", "notes", "due_at", "status"}:
            raise InvalidInput("Укажи изменяемые поля задачи.")
        if any(v is None for k, v in changes.items() if k != "due_at"):
            raise InvalidInput("Пустое значение допустимо только для срока.")
        async with self.uow() as uow:
            if cached := await existing_action(uow, key):
                return cached
            task = await uow.tasks.get(id)
            for name, value in changes.items():
                setattr(task, name, value)
            task.__post_init__()
            task.updated_at = self.clock.now()
            await uow.tasks.save(task)
            result = public(task)
            await record_action(uow, key, "complete_task", result, request_id, call_id)
            await uow.commit()
            return result
