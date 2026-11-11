import json
from uuid import uuid4

import pytest
from agents import ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from ai_inbox.infrastructure.ai.assistant import AnyModelAssistant
from tests.unit.test_sdk_agent import ToolCycleModel

LETTER = {
    "recipient": "reader@example.test",
    "subject": "Учебное письмо",
    "body": "Здравствуйте! Как ваши дела?",
}


@pytest.mark.parametrize(
    "text",
    [
        'Отправь на почту reader@example.test "Привет! Как дела?", но перепиши вежливо.',
        'Отправьте reader@example.test сообщение: "Как дела?"',
        "Можешь отправить на почту reader@example.test приветствие?",
        'Отошли reader@example.test: "Привет!"',
        'Вышли на почту reader@example.test: "Привет!"',
        'Перешли reader@example.test сообщение: "Привет!"',
        'Напиши\nписьмо для reader@example.test: "Привет!"',
    ],
)
async def test_send_request_prepares_only_local_draft(client, text):
    c = client.container
    c.assistant.steps = [("prepare_outgoing_email", LETTER)]
    conversation = await c.chat.create_conversation()
    result = await c.chat(conversation["id"], text, str(uuid4()))
    receipt = result["actions"][0]["result"]
    assert receipt["status"] == "draft" and receipt["recipient"] == LETTER["recipient"]
    assert len(await c.outbox.list()) == 1
    assert not c.sender.messages


@pytest.mark.parametrize(
    "text,code",
    [
        ("Прочитай письмо, получатель reader@example.test.", "forbidden"),
        ("Не отправь письмо на reader@example.test. Просто посмотри почту.", "forbidden"),
        ("Не нужно отправить на reader@example.test. Просто посмотри почту.", "forbidden"),
        ("Отправь на почту приветствие, но перепиши вежливо.", "invalid_input"),
    ],
)
async def test_send_intent_requires_request_and_explicit_recipient(client, text, code):
    c = client.container
    c.assistant.steps = [("prepare_outgoing_email", LETTER)]
    conversation = await c.chat.create_conversation()
    result = await c.chat(conversation["id"], text, str(uuid4()))
    assert result["actions"][0]["result"]["error"]["code"] == code
    assert not await c.outbox.list() and not c.sender.messages


class EmptyAfterOutgoingModel(ToolCycleModel):
    async def get_response(self, system_instructions, input, **kwargs):
        self.inputs.append(input)
        if len(self.inputs) == 1:
            output = [
                ResponseFunctionToolCall(
                    type="function_call",
                    id="outgoing",
                    call_id="prepare-once",
                    name="prepare_outgoing_email",
                    arguments=json.dumps(LETTER),
                )
            ]
        else:
            output = [
                ResponseOutputMessage(
                    type="message",
                    id="empty",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(type="output_text", text="", annotations=[], logprobs=[])
                    ],
                )
            ]
        return ModelResponse(output=output, usage=Usage(), response_id="empty-after-outgoing")


@pytest.mark.parametrize("requested", [True, False])
async def test_empty_reply_reports_actual_tool_outcome_and_is_idempotent(client, requested):
    c = client.container
    model = EmptyAfterOutgoingModel()
    c.chat.assistant = AnyModelAssistant(None, "fake", 1, 4, "Europe/Moscow", sdk_model=model)
    conversation = await c.chat.create_conversation()
    text = (
        "Отправь на почту reader@example.test приветствие, но перепиши вежливо."
        if requested
        else "Прочитай письмо от reader@example.test."
    )
    request_id = str(uuid4())
    result = await c.chat(conversation["id"], text, request_id)
    assert result["status"] == "failed" and result["error"] == "provider_unavailable"
    assert "пустой текстовый ответ" in result["text"]
    assert len(model.inputs) == 2 and not c.sender.messages
    if requested:
        assert "Черновик письма сохранён" in result["text"]
        assert "Письмо ещё не отправлено" in result["text"]
        assert result["actions"][0]["result"]["status"] == "draft"
        assert len(await c.outbox.list()) == 1
    else:
        assert "Ни одно действие инструмента не выполнено" in result["text"]
        assert "Черновик письма сохранён" not in result["text"]
        assert result["actions"][0]["result"]["error"]["code"] == "forbidden"
        assert not await c.outbox.list()
    assert await c.chat(conversation["id"], text, request_id) == result
    assert len(model.inputs) == 2


async def test_provider_failure_without_tools_does_not_claim_saved_actions(client):
    c = client.container
    c.assistant.fail = True
    conversation = await c.chat.create_conversation()
    result = await c.chat(conversation["id"], "Привет", str(uuid4()))
    assert result["status"] == "failed" and result["error"] == "provider_unavailable"
    assert "503" in result["text"] and "Действия не выполнялись" in result["text"]
    assert result["actions"] == [] and not await c.outbox.list()
