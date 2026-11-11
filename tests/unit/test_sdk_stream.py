import json

import httpx
import pytest
from openai import AsyncOpenAI

from ai_inbox.domain.errors import ProviderUnavailable
from ai_inbox.infrastructure.ai.assistant import AnyModelAssistant
from ai_inbox.infrastructure.ai.retry import provider_stream
from tests.fakes import FakeClock


def chunk(delta, finish=None):
    return {
        "id": "synthetic",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fake",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def sse(chunks):
    return (
        b"".join(("data: " + json.dumps(c) + "\n\n").encode() for c in chunks) + b"data: [DONE]\n\n"
    )


async def test_real_sdk_streams_text_and_executes_tool_without_exposing_arguments():
    requests, calls, events = [], [], []

    async def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        assert data["stream"] is True
        if len(requests) == 1:
            arguments = json.dumps(
                {
                    "task_title": "Учебная задача",
                    "notes": "PRIVATE_TOOL_ARGUMENT",
                    "due_at": None,
                    "source": "manual",
                }
            )
            output = [
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-synthetic",
                                "type": "function",
                                "function": {"name": "create_task", "arguments": arguments},
                            }
                        ],
                    }
                ),
                chunk({}, "tool_calls"),
            ]
        else:
            output = [
                chunk({"role": "assistant", "content": "Создана "}),
                chunk({"content": "задача "}),
                chunk({"content": "saved-task"}),
                chunk({}, "stop"),
            ]
        return httpx.Response(
            200, content=sse(output), headers={"Content-Type": "text/event-stream"}
        )

    async def execute(name, arguments, call_id):
        calls.append((name, arguments, call_id))
        return {"id": "saved-task"}

    async def emit(event):
        events.append(event)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AsyncOpenAI(
            api_key="fake", base_url="http://provider.test/v1", http_client=http, max_retries=0
        )
        assistant = AnyModelAssistant(client, "fake", 1, 4, "Europe/Moscow")
        text, history = await assistant.chat_stream(
            [{"role": "user", "content": "Создай задачу"}], execute, FakeClock().now(), emit
        )
    assert text == "Создана задача saved-task"
    assert len(calls) == 1 and calls[0][1]["title"] == "Учебная задача"
    assert len(requests) == 2
    assert [event["text"] for event in events if event["type"] == "delta"] == [
        "Создана ",
        "задача ",
        "saved-task",
    ]
    assert sum(e["type"] == "message_start" for e in events) == 2
    assert "PRIVATE_TOOL_ARGUMENT" not in json.dumps(events)
    assert any(item.get("type") == "function_call_output" for item in history)
    json.dumps(history)


async def test_stream_retry_only_before_first_event(monkeypatch):
    import asyncio

    async def no_delay(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    attempts = 0

    async def before():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("private body")
        yield "text"

    assert [event async for event in provider_stream(before, 2)] == ["text"]
    assert attempts == 2
    attempts = 0
    observed = []

    async def after():
        nonlocal attempts
        attempts += 1
        yield "first text"
        raise httpx.ReadError("private body")

    with pytest.raises(ProviderUnavailable):
        async for item in provider_stream(after, 2):
            observed.append(item)
    assert attempts == 1 and observed == ["first text"]


async def test_empty_stream_is_not_success():
    async def handler(request):
        return httpx.Response(
            200,
            content=sse([chunk({"role": "assistant", "content": ""}), chunk({}, "stop")]),
            headers={"Content-Type": "text/event-stream"},
        )

    async def execute(*args):
        return {}

    async def emit(event):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AsyncOpenAI(
            api_key="fake", base_url="http://provider.test/v1", http_client=http, max_retries=0
        )
        assistant = AnyModelAssistant(client, "fake", 1, 4, "Europe/Moscow")
        with pytest.raises(ProviderUnavailable):
            await assistant.chat_stream(
                [{"role": "user", "content": "Привет"}], execute, FakeClock().now(), emit
            )
