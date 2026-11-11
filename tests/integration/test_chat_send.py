import asyncio
from uuid import uuid4

import pytest

from ai_inbox.application.chat import ChatWithAssistant
from ai_inbox.application.outgoing_dto import DeliveryResult
from tests.integration.test_outbox_chat import LETTER


async def prepare(c, text=None):
    conversation = await c.chat.create_conversation()
    c.assistant.steps = [("prepare_outgoing_email", LETTER)]
    response = await c.chat(
        conversation["id"],
        text or "Напиши письмо для reader@example.test: приветствие.",
        str(uuid4()),
    )
    letter = response["actions"][0]["result"]
    assert letter["status"] == "draft" and letter["confirmation_available"]
    assert not c.sender.messages
    return conversation["id"], letter


async def confirm(c, conversation, text="Всё ок, отправляй", request_id=None):
    return await c.chat(conversation, text, request_id or str(uuid4()))


async def test_review_then_confirm_sends_once_without_model_and_survives_reload(client):
    c = client.container
    conversation, letter = await prepare(c)
    async with c.uow() as uow:
        review = (await uow.conversations.get(conversation)).pending_email
        assert (
            review["outbox_id"] == letter["id"]
            and review["content_token"] == letter["content_token"]
        )
    # Fresh application object, same DB: confirmation is persistent, not process-local.
    c.chat = ChatWithAssistant(c.uow, c.assistant, c.storage, c.chat.tools_factory, c.clock)
    calls = c.assistant.calls
    request_id = str(uuid4())
    result = await confirm(c, conversation, "Да, всё ок!", request_id)
    assert result["status"] == "completed" and result["error"] is None
    assert result["actions"][0]["name"] == "send_outgoing_email"
    assert result["actions"][0]["result"]["status"] == "sent"
    assert "SMTP-сервер принял" in result["text"]
    assert c.assistant.calls == calls and len(c.sender.messages) == 1
    assert c.sender.messages[0].recipient == LETTER["recipient"]
    assert c.sender.messages[0].body == LETTER["body"]
    assert (await c.outbox.get(letter["id"]))["status"] == "sent"
    assert await confirm(c, conversation, "Да, всё ок!", request_id) == result
    c.assistant.steps = [("send_outgoing_email", {})]
    again = await confirm(c, conversation)
    assert again["actions"][0]["result"]["error"]["code"] == "forbidden"
    assert len(c.sender.messages) == 1
    async with c.uow() as uow:
        assert (await uow.conversations.get(conversation)).pending_email is None
    assert "pending_email" not in str(await c.chat.list_conversations())


async def test_same_message_and_model_cannot_grant_send_permission(client):
    c = client.container
    c.assistant.steps = [("prepare_outgoing_email", LETTER), ("send_outgoing_email", {})]
    conversation = await c.chat.create_conversation()
    response = await c.chat(
        conversation["id"],
        "Напиши письмо reader@example.test и сразу отправь, всё ок.",
        str(uuid4()),
    )
    assert response["actions"][0]["result"]["status"] == "draft"
    assert response["actions"][1]["result"]["error"]["code"] == "forbidden"
    assert not c.sender.messages


async def test_other_conversation_cannot_approve_review(client):
    c = client.container
    original, _ = await prepare(c)
    other = await c.chat.create_conversation()
    c.assistant.steps = [("send_outgoing_email", {})]
    result = await confirm(c, other["id"])
    assert result["actions"][0]["result"]["error"]["code"] == "forbidden"
    assert not c.sender.messages
    assert (await confirm(c, original))["actions"][0]["result"]["status"] == "sent"


@pytest.mark.parametrize("field", ["recipient", "subject", "body", "attachment_ids"])
async def test_changed_draft_requires_new_review(client, auth, field):
    c = client.container
    conversation, letter = await prepare(c)
    changes = {
        "recipient": letter["recipient"],
        "subject": letter["subject"],
        "body": letter["body"],
        "attachment_ids": [],
    }
    if field == "attachment_ids":
        uploaded = (
            await client.post(
                "/api/v1/files/attachments",
                headers=auth,
                files={"file": ("note.txt", b"synthetic", "text/plain")},
            )
        ).json()
        changes[field] = [uploaded["file_id"]]
    else:
        changes[field] = {
            "recipient": "changed@example.test",
            "subject": "Новая тема",
            "body": "Новый текст",
        }[field]
    await c.outbox.save(**changes, id=letter["id"])
    result = await confirm(c, conversation)
    assert result["status"] == "failed" and result["error"] == "conflict"
    assert not c.sender.messages
    c.assistant.steps = [("review_outgoing_email", {"outbox_id": letter["id"]})]
    fresh = await c.chat(conversation, "Покажи актуальный черновик письма", str(uuid4()))
    assert fresh["actions"][0]["result"]["confirmation_available"]
    sent = await confirm(c, conversation)
    assert sent["actions"][0]["result"]["status"] == "sent" and len(c.sender.messages) == 1
    if field == "attachment_ids":
        assert c.sender.messages[0].attachments[0].data == b"synthetic"


@pytest.mark.parametrize(
    "text",
    ["Не отправляй", "Все ок, но поменяй тему", "Расскажи про задачи", '"Всё ок, отправляй"'],
)
async def test_other_message_revokes_pending_confirmation(client, text):
    c = client.container
    conversation, _ = await prepare(c)
    c.assistant.steps = []
    await c.chat(conversation, text, str(uuid4()))
    c.assistant.steps = [("send_outgoing_email", {})]
    result = await confirm(c, conversation)
    assert result["actions"][0]["result"]["error"]["code"] == "forbidden"
    assert not c.sender.messages


async def test_multiple_drafts_need_one_explicit_review(client):
    c = client.container
    conversation = await c.chat.create_conversation()
    c.assistant.steps = [
        ("prepare_outgoing_email", LETTER),
        ("prepare_outgoing_email", LETTER | {"subject": "Другой вариант"}),
    ]
    response = await c.chat(
        conversation["id"], "Напиши два письма для reader@example.test", str(uuid4())
    )
    assert len(response["actions"]) == 2
    async with c.uow() as uow:
        assert (await uow.conversations.get(conversation["id"])).pending_email is None
    c.assistant.steps = [("send_outgoing_email", {})]
    assert (await confirm(c, conversation["id"]))["actions"][0]["result"]["error"][
        "code"
    ] == "forbidden"
    target = response["actions"][0]["result"]["id"]
    c.assistant.steps = [("review_outgoing_email", {"outbox_id": target})]
    await c.chat(conversation["id"], "Покажи первый черновик", str(uuid4()))
    result = await confirm(c, conversation["id"])
    assert result["actions"][0]["result"]["outbox_id"] == target and len(c.sender.messages) == 1


@pytest.mark.parametrize("status", ["failed", "unknown"])
async def test_smtp_failure_is_not_success_and_never_auto_retried(client, status):
    c = client.container
    conversation, letter = await prepare(c)
    c.sender.result = DeliveryResult(status, "Синтетический сбой SMTP")
    request_id = str(uuid4())
    result = await confirm(c, conversation, request_id=request_id)
    assert result["status"] == "failed" and result["error"] == "delivery_" + status
    assert result["actions"][0]["result"]["status"] == status
    assert await confirm(c, conversation, request_id=request_id) == result
    c.assistant.steps = [("send_outgoing_email", {})]
    await confirm(c, conversation)
    assert len(c.sender.messages) == 1
    c.assistant.steps = [("review_outgoing_email", {"outbox_id": letter["id"]})]
    review = await c.chat(conversation, "Покажи актуальный черновик", str(uuid4()))
    if status == "unknown":
        assert review["actions"][0]["result"]["error"]["code"] == "invalid_input"
        assert len(c.sender.messages) == 1
    else:
        assert review["actions"][0]["result"]["confirmation_available"]
        c.sender.result = DeliveryResult("sent")
        assert (await confirm(c, conversation))["actions"][0]["result"]["status"] == "sent"
        assert len(c.sender.messages) == 2


async def test_model_failure_after_prepare_does_not_lose_review(client):
    c = client.container
    c.assistant.fail = True
    conversation, _ = await prepare(c)
    # A subsequent confirmation works even while the model is still unavailable.
    sent = await confirm(c, conversation)
    assert sent["status"] == "completed" and len(c.sender.messages) == 1
    assert c.assistant.calls == 1


async def test_concurrent_confirmations_do_not_send_twice(client):
    c = client.container
    conversation, _ = await prepare(c)
    request_id = str(uuid4())
    a, b = await asyncio.gather(
        confirm(c, conversation, request_id=request_id),
        confirm(c, conversation, request_id=request_id),
    )
    assert a == b and len(c.sender.messages) == 1


async def test_manual_send_after_chat_review_cannot_be_sent_twice(client):
    c = client.container
    conversation, letter = await prepare(c)
    await c.outbox.send(letter["id"], str(uuid4()), True, letter["content_token"])
    result = await confirm(c, conversation)
    assert result["status"] == "failed" and result["error"] == "conflict"
    assert len(c.sender.messages) == 1


async def test_interrupted_confirmation_recovers_without_send_permission(client):
    c = client.container
    conversation, letter = await prepare(c)
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked_sender(message):
        started.set()
        await release.wait()
        return DeliveryResult("sent")

    c.sender.send = blocked_sender
    pending = asyncio.create_task(confirm(c, conversation))
    await asyncio.wait_for(started.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await c.chat.recover_interrupted()
    await c.outbox.recover_interrupted()
    assert (await c.outbox.get(letter["id"]))["status"] == "unknown"
    async with c.uow() as uow:
        assert (await uow.conversations.get(conversation)).pending_email is None
    c.assistant.steps = [("send_outgoing_email", {})]
    result = await confirm(c, conversation)
    assert result["actions"][0]["result"]["error"]["code"] == "forbidden"


@pytest.mark.parametrize("mode", ["bearer", "cookie"])
async def test_chat_confirmation_api_authorization(client, auth, mode):
    c = client.container
    conversation, _ = await prepare(c)
    path = f"/api/v1/conversations/{conversation}/messages"
    payload = {"text": "Всё ок", "request_id": str(uuid4())}
    assert (await client.post(path, json=payload)).status_code == 401
    headers = auth
    if mode == "cookie":
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
    assert not c.sender.messages
    result = await client.post(path, headers=headers, json=payload)
    assert result.status_code == 200 and result.json()["actions"][0]["result"]["status"] == "sent"
    assert len(c.sender.messages) == 1
