import asyncio
import json
from uuid import uuid4

import pytest

from ai_inbox.application.chat_stream import ChatStream
from ai_inbox.domain.errors import Conflict, ProviderUnavailable
from tests.integration.test_outbox_chat import LETTER
from tests.integration.test_workflows import TASK


def frames(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def conversation(c):
    return (await c.chat.create_conversation())["id"]


async def test_disconnect_reconnect_does_not_restart_model_or_tool(client):
    c = client.container
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def model(history, execute, now, emit):
        nonlocal calls
        calls += 1
        await emit({"type": "message_start"})
        await emit({"type": "delta", "text": "Начало ответа. "})
        started.set()
        await release.wait()
        task = await execute("create_task", TASK, "once")
        await emit({"type": "delta", "text": "Готово " + task["id"]})
        answer = "Начало ответа. Готово " + task["id"]
        return answer, history + [{"role": "assistant", "content": answer}]

    c.assistant.chat_stream = model
    id, request_id = await conversation(c), str(uuid4())
    job = await c.streams.open(id, "Создай задачу проверить счёт", request_id)
    await started.wait()
    subscription = job.subscribe()
    first = await anext(subscription)
    assert first["type"] == "snapshot" and first["text"] == "Начало ответа. "
    assert job.terminal is None
    await subscription.aclose()  # Browser disconnects; generation must continue.
    assert not job.task.done()
    assert await c.streams.open(id, "Создай задачу проверить счёт", request_id) is job
    with pytest.raises(Conflict):
        await c.streams.open(id, "Другое содержимое", request_id)
    with pytest.raises(Conflict):
        await c.streams.open(id, "Другой запрос", str(uuid4()))
    active = await c.chat.active_request(id)
    assert active == {
        "text": "Создай задачу проверить счёт",
        "image_id": None,
        "request_id": request_id,
    }
    release.set()
    replay = [event async for event in job.subscribe()]
    await job.task
    assert replay[-1]["type"] == "done" and calls == 1
    assert len(await c.tasks.list()) == 1
    assert await c.chat.active_request(id) is None
    saved = await c.chat.messages(id)
    assert saved[-1]["text"] == replay[-1]["result"]["text"]
    assert any(e["type"] == "tool_start" for e in replay)
    assert any(e["type"] == "tool_end" and e["ok"] for e in replay)
    # Final response is also available through the original JSON scenario.
    assert await c.chat(id, "Создай задачу проверить счёт", request_id) == replay[-1]["result"]


async def test_partial_provider_failure_preserves_text_and_durable_task(client):
    c = client.container

    async def model(history, execute, now, emit):
        await execute("create_task", TASK, "saved")
        await emit({"type": "message_start"})
        await emit({"type": "delta", "text": "Уже полученная часть ответа"})
        raise ProviderUnavailable("AnyModel HTTP 524")

    c.assistant.chat_stream = model
    id, request_id = await conversation(c), str(uuid4())
    job = await c.streams.open(id, "Создай задачу проверить счёт", request_id)
    events = [event async for event in job.subscribe()]
    assert events[-1]["type"] == "done"
    result = events[-1]["result"]
    assert result["status"] == "failed" and result["error"] == "provider_unavailable"
    assert "Уже полученная часть ответа" in result["text"]
    assert result["actions"][0]["result"]["id"] == (await c.tasks.list())[0]["id"]
    assert (await c.chat.messages(id))[-1]["text"] == result["text"]
    assert (await c.streams.open(id, "Создай задачу проверить счёт", request_id)) is job
    assert len(await c.tasks.list()) == 1


async def test_streamed_confirmation_and_replay_send_once(client):
    c = client.container
    c.assistant.steps = [("prepare_outgoing_email", LETTER)]
    id = await conversation(c)
    await c.chat(id, "Напиши письмо для reader@example.test", str(uuid4()))
    request_id = str(uuid4())
    job = await c.streams.open(id, "Всё ок, отправляй", request_id)
    events = [event async for event in job.subscribe()]
    result = events[-1]["result"]
    assert result["actions"][0]["result"]["status"] == "sent"
    assert any(e["type"] == "tool_start" and e["name"] == "send_outgoing_email" for e in events)
    assert len(c.sender.messages) == 1 and c.assistant.calls == 1
    # Eviction/restart of the in-memory replay cache must still use SQLite idempotency.
    await job.task
    job.finished_at -= 400
    reopened = await c.streams.open(id, "Всё ок, отправляй", request_id)
    assert reopened is not job
    again = [event async for event in reopened.subscribe()]
    assert again[-1]["result"] == result and len(c.sender.messages) == 1


async def test_slow_client_receives_snapshot_instead_of_losing_deltas():
    job = ChatStream(("conversation", "text", "request", None))
    sub = job.subscribe()
    assert (await anext(sub))["sequence"] == 0
    for _ in range(300):
        await job.emit({"type": "delta", "text": "я"})
    replacement = await anext(sub)
    assert replacement["type"] == "snapshot" and replacement["text"] == "я" * 300
    assert len(job.events) == 256
    await job.emit({"type": "message_start"})
    await job.emit({"type": "delta", "text": "Финальный ответ"})
    await job.emit({"type": "done", "result": {"text": "Финальный ответ"}})
    rest = [event async for event in sub]
    assert rest[-1]["type"] == "done" and job.text == "Финальный ответ"


async def test_shutdown_recovery_does_not_restart_interrupted_request(client):
    c = client.container
    started = asyncio.Event()
    calls = 0

    async def model(history, execute, now, emit):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()

    c.assistant.chat_stream = model
    id, request_id = await conversation(c), str(uuid4())
    await c.streams.open(id, "Проверь задачи", request_id)
    await started.wait()
    await c.streams.close()
    await c.chat.recover_interrupted()
    job = await c.streams.open(id, "Проверь задачи", request_id)
    events = [event async for event in job.subscribe()]
    assert events[-1]["result"]["error"] == "interrupted" and calls == 1


async def test_http_stream_auth_csrf_and_openapi(client, auth):
    c = client.container
    id = await conversation(c)
    path = f"/api/v1/conversations/{id}/messages/stream"
    payload = {"text": "Покажи задачи", "request_id": str(uuid4())}
    assert (await client.post(path, json=payload)).status_code == 401
    origin = str(client.base_url).rstrip("/")
    await client.post(
        "/auth/login",
        headers={"Origin": origin},
        data={"username": "demo", "password": "test-owner-password"},
    )
    assert (await client.post(path, json=payload)).status_code == 403
    headers = {"Origin": origin, "X-CSRF-Token": client.cookies["ai_inbox_csrf"]}
    assert (
        await client.post(path, headers=headers | {"Origin": "https://evil.test"}, json=payload)
    ).status_code == 403
    response = await client.post(path, headers=headers, json=payload)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-store"
    result = frames(response.text)[-1]["result"]
    assert result["status"] == "completed"
    assert (
        await client.get(f"/api/v1/conversations/{id}/active-request", headers=auth)
    ).json() is None
    schema = (await client.get("/openapi.json")).json()
    assert (
        "text/event-stream"
        in schema["paths"]["/api/v1/conversations/{id}/messages/stream"]["post"]["responses"][
            "200"
        ]["content"]
    )
    assert (
        await client.post(
            "/api/v1/conversations/missing/messages/stream", headers=auth, json=payload
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/conversations/{id}/active-request", headers={"Authorization": "Bearer bad"}
        )
    ).status_code == 401


async def test_delete_conversation_forgets_cached_stream(client, auth):
    c = client.container
    id = await conversation(c)
    request_id = str(uuid4())
    response = await client.post(
        f"/api/v1/conversations/{id}/messages/stream",
        headers=auth,
        json={"text": "Привет", "request_id": request_id},
    )
    assert frames(response.text)[-1]["type"] == "done"
    assert c.streams.jobs
    assert (await client.delete(f"/api/v1/conversations/{id}", headers=auth)).status_code == 200
    assert not c.streams.jobs
    assert (
        await client.post(
            f"/api/v1/conversations/{id}/messages/stream",
            headers=auth,
            json={"text": "Привет", "request_id": request_id},
        )
    ).status_code == 404


async def test_pre_run_failure_is_safe_stream_error(client, monkeypatch):
    c = client.container

    async def broken(*args):
        raise RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_BODY")

    monkeypatch.setattr(c.chat, "_inflate", broken)
    job = await c.streams.open(await conversation(c), "Привет", str(uuid4()))
    events = [event async for event in job.subscribe()]
    assert events[-1]["type"] == "error"
    assert events[-1]["error"]["code"] == "internal_error"
    assert "SYNTHETIC_PRIVATE_PROVIDER_BODY" not in json.dumps(events)


async def test_reload_can_find_request_before_user_message_is_committed(client, monkeypatch):
    c = client.container
    started, release = asyncio.Event(), asyncio.Event()
    original = c.chat._inflate

    async def delayed(*args):
        started.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(c.chat, "_inflate", delayed)
    id, request_id = await conversation(c), str(uuid4())
    job = await c.streams.open(id, "Привет", request_id)
    await started.wait()
    assert await c.chat.active_request(id) is None
    assert await c.streams.active_request(id) == {
        "text": "Привет",
        "request_id": request_id,
        "image_id": None,
    }
    release.set()
    assert [e async for e in job.subscribe()][-1]["type"] == "done"


async def test_unbounded_provider_text_is_stopped_without_growing_replay_memory(client):
    c = client.container

    async def model(history, execute, now, emit):
        await emit({"type": "delta", "text": "x" * 100001})
        raise AssertionError("must not reach")

    c.assistant.chat_stream = model
    job = await c.streams.open(await conversation(c), "Привет", str(uuid4()))
    events = [event async for event in job.subscribe()]
    assert events[-1]["result"]["status"] == "failed"
    assert events[-1]["result"]["error"] == "assistant_error"
    assert len(job.text) == 0
