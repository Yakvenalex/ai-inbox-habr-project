import asyncio
from dataclasses import asdict
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert

from ai_inbox.domain.entities import Action, ChatRun, Conversation, Message
from ai_inbox.infrastructure.persistence.models import ActionRow, MessageRow, RunRow
from tests.integration.test_workflows import TASK, conversation


async def test_delete_history_preserves_created_task_and_other_conversation(client, auth):
    c = client.container
    c.assistant.steps = [("create_task", TASK)]
    target = await conversation(client, auth)
    other = await conversation(client, auth)
    response = await client.post(
        f"/api/v1/conversations/{target}/messages",
        headers=auth,
        json={"text": "Создай задачу проверить счёт", "request_id": str(uuid4())},
    )
    task_id = response.json()["actions"][0]["result"]["id"]
    async with c.uow() as uow:
        await uow.actions.add(
            Action(key="other", request_id=other + ":r", tool_call_id="t", name="test", result={})
        )
        await uow.commit()
    deleted = await client.delete(f"/api/v1/conversations/{target}", headers=auth)
    assert deleted.status_code == 200 and deleted.json() == {"id": target, "deleted": True}
    assert {
        item["id"] for item in (await client.get("/api/v1/conversations", headers=auth)).json()
    } == {other}
    assert (
        await client.get(f"/api/v1/conversations/{target}/messages", headers=auth)
    ).status_code == 404
    assert (await client.get("/api/v1/tasks/" + task_id, headers=auth)).status_code == 200
    assert (await client.delete(f"/api/v1/conversations/{target}", headers=auth)).status_code == 404
    retry = await client.post(
        f"/api/v1/conversations/{target}/messages",
        headers=auth,
        json={"text": "Повтор", "request_id": str(uuid4())},
    )
    assert retry.status_code == 404 and c.assistant.calls == 1
    async with c.uow() as uow:
        assert not await uow.messages.list(conversation_id=target)
        assert not await uow.runs.list(conversation_id=target)
        assert [action.key for action in await uow.actions.list()] == ["other"]


async def test_delete_all_rows_beyond_list_limit_and_rollback(client, auth):
    target = await conversation(client, auth)

    def values(entity):
        return {
            k: v.isoformat() if isinstance(v, datetime) else v for k, v in asdict(entity).items()
        }

    async with client.container.uow() as uow:
        # Bulk seed >500 rows in each child table; Repository.list is intentionally limited.
        for row, entities in [
            (
                MessageRow,
                [
                    Message(conversation_id=target, role="user", text="Учебный текст")
                    for _ in range(501)
                ],
            ),
            (
                RunRow,
                [
                    ChatRun(conversation_id=target, request_id=str(i), status="completed")
                    for i in range(501)
                ],
            ),
            (
                ActionRow,
                [
                    Action(
                        key=str(i),
                        request_id=target + ":" + str(i),
                        tool_call_id="t",
                        name="test",
                        result={},
                    )
                    for i in range(501)
                ],
            ),
        ]:
            await uow.session.execute(insert(row), [values(entity) for entity in entities])
        await uow.commit()
    with pytest.raises(RuntimeError):
        async with client.container.uow() as uow:
            await uow.conversations.delete(target)
            raise RuntimeError("Rollback deletion")
    async with client.container.uow() as uow:
        assert await uow.conversations.get(target)
        assert len(await uow.messages.list(conversation_id=target)) == 500
        assert len(await uow.runs.list(conversation_id=target)) == 500
        assert len(await uow.actions.list()) == 500
    assert (await client.delete(f"/api/v1/conversations/{target}", headers=auth)).status_code == 200
    async with client.container.uow() as uow:
        assert not await uow.messages.list()
        assert not await uow.runs.list()
        assert not await uow.actions.list()


async def test_delete_refuses_active_response_then_succeeds(client, auth):
    target = await conversation(client, auth)
    started, release = asyncio.Event(), asyncio.Event()
    original = client.container.assistant.chat

    async def slow_chat(*args):
        started.set()
        await release.wait()
        return await original(*args)

    client.container.assistant.chat = slow_chat
    pending = asyncio.create_task(
        client.post(
            f"/api/v1/conversations/{target}/messages",
            headers=auth,
            json={"text": "Учебный вопрос", "request_id": str(uuid4())},
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert (
            await client.delete(f"/api/v1/conversations/{target}", headers=auth)
        ).status_code == 409
    finally:
        release.set()
        assert (await pending).status_code == 200
    assert (await client.delete(f"/api/v1/conversations/{target}", headers=auth)).status_code == 200


async def test_delete_refuses_persisted_unfinished_run(client, auth):
    entity = Conversation()
    async with client.container.uow() as uow:
        await uow.conversations.add(entity)
        await uow.runs.add(ChatRun(conversation_id=entity.id, request_id="unfinished"))
        await uow.commit()
    assert (
        await client.delete(f"/api/v1/conversations/{entity.id}", headers=auth)
    ).status_code == 409
    await client.container.chat.recover_interrupted()
    assert (
        await client.delete(f"/api/v1/conversations/{entity.id}", headers=auth)
    ).status_code == 200


async def test_delete_auth_cookie_csrf_and_documentation(client, auth):
    target = await conversation(client, auth)
    path = f"/api/v1/conversations/{target}"
    assert (await client.delete(path)).status_code == 401
    origin = str(client.base_url).rstrip("/")
    await client.post(
        "/auth/login",
        headers={"Origin": origin},
        data={"username": "demo", "password": "test-owner-password"},
    )
    assert (await client.delete(path)).status_code == 403
    csrf = client.cookies["ai_inbox_csrf"]
    assert (
        await client.delete(path, headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf})
    ).status_code == 403
    assert (
        await client.delete(path, headers={"Origin": origin, "X-CSRF-Token": csrf})
    ).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert schema["paths"]["/api/v1/conversations/{id}"]["delete"]["security"]
