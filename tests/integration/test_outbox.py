import asyncio
from uuid import uuid4

import pytest

from ai_inbox.application.outgoing_dto import DeliveryResult
from ai_inbox.domain.entities import DeliveryAttempt, Media

LETTER = {
    "recipient": "reader@example.test",
    "subject": "Учебное письмо",
    "body": "Здравствуйте! Проверьте учебный счёт.",
    "attachment_ids": [],
}


async def create(client, auth, **changes):
    response = await client.post("/api/v1/outbox", headers=auth, json=LETTER | changes)
    assert response.status_code == 201, response.text
    return response.json()


async def test_manual_letter_ai_and_attachment(client, auth):
    ai = await client.post(
        "/api/v1/outbox/compose",
        headers=auth,
        json={"outline": "Попросить уточнить реквизиты счёта"},
    )
    assert ai.status_code == 200 and ai.json()["subject"] and ai.json()["body"]
    assert not client.container.sender.messages
    uploaded = await client.post(
        "/api/v1/files/attachments",
        headers=auth,
        files={"file": ("../../счёт.pdf", b"%PDF-synthetic-test", "application/pdf")},
    )
    assert uploaded.status_code == 201 and uploaded.json()["name"] == "счёт.pdf"
    attachment = uploaded.json()
    assert (await client.get(attachment["url"])).status_code == 401
    download = await client.get(attachment["url"], headers=auth)
    assert download.status_code == 200 and download.headers["content-disposition"].startswith(
        "attachment;"
    )
    assert download.content == b"%PDF-synthetic-test"
    letter = await create(client, auth, **ai.json(), attachment_ids=[attachment["file_id"]])
    assert letter["attachments"][0]["name"] == "счёт.pdf" and letter["status"] == "draft"
    assert not client.container.sender.messages
    assert (
        await client.post(
            f"/api/v1/outbox/{letter['id']}/send",
            headers=auth,
            json={
                "request_id": str(uuid4()),
                "confirmed": False,
                "content_token": letter["content_token"],
            },
        )
    ).status_code == 422
    request = {
        "request_id": str(uuid4()),
        "confirmed": True,
        "content_token": letter["content_token"],
    }
    result = await client.post(f"/api/v1/outbox/{letter['id']}/send", headers=auth, json=request)
    assert result.status_code == 200 and result.json()["status"] == "sent"
    sent = client.container.sender.messages[0]
    assert sent.recipient == LETTER["recipient"] and sent.attachments[0].data == download.content
    duplicate = await client.post(f"/api/v1/outbox/{letter['id']}/send", headers=auth, json=request)
    assert duplicate.json() == result.json() and len(client.container.sender.messages) == 1
    assert (
        await client.post(
            f"/api/v1/outbox/{letter['id']}/send",
            headers=auth,
            json={
                "request_id": str(uuid4()),
                "confirmed": True,
                "content_token": letter["content_token"],
            },
        )
    ).status_code == 409
    assert (
        await client.patch(f"/api/v1/outbox/{letter['id']}", headers=auth, json=LETTER)
    ).status_code == 409
    saved = (await client.get(f"/api/v1/outbox/{letter['id']}", headers=auth)).json()
    assert saved["status"] == "sent" and saved["sent_at"]


async def test_concurrent_confirmation_and_cross_letter_request_id(client, auth):
    letter = await create(client, auth)
    data = {"request_id": str(uuid4()), "confirmed": True, "content_token": letter["content_token"]}
    responses = await asyncio.gather(
        *[
            client.post(f"/api/v1/outbox/{letter['id']}/send", headers=auth, json=data)
            for _ in range(2)
        ]
    )
    assert (
        all(r.status_code == 200 for r in responses) and len(client.container.sender.messages) == 1
    )
    other = await create(client, auth, subject="Другое письмо")
    assert (
        await client.post(f"/api/v1/outbox/{other['id']}/send", headers=auth, json=data)
    ).status_code == 409


async def test_definite_failure_can_be_retried_but_old_request_keeps_receipt(client, auth):
    c = client.container
    c.sender.result = DeliveryResult("failed", "SMTP 550")
    letter = await create(client, auth)
    first = {
        "request_id": str(uuid4()),
        "confirmed": True,
        "content_token": letter["content_token"],
    }
    assert (
        await client.post(f"/api/v1/outbox/{letter['id']}/send", headers=auth, json=first)
    ).json()["status"] == "failed"
    c.sender.result = DeliveryResult("sent")
    assert (
        await client.post(
            f"/api/v1/outbox/{letter['id']}/send",
            headers=auth,
            json={
                "request_id": str(uuid4()),
                "confirmed": True,
                "content_token": letter["content_token"],
            },
        )
    ).json()["status"] == "sent"
    assert (
        await client.post(f"/api/v1/outbox/{letter['id']}/send", headers=auth, json=first)
    ).json()["status"] == "failed"
    assert len(c.sender.messages) == 2


async def test_unknown_result_blocks_new_attempt_and_edit(client, auth):
    c = client.container
    c.sender.result = DeliveryResult("unknown", "Неизвестный результат")
    letter = await create(client, auth)
    data = {"request_id": str(uuid4()), "confirmed": True, "content_token": letter["content_token"]}
    assert (
        await client.post(f"/api/v1/outbox/{letter['id']}/send", headers=auth, json=data)
    ).json()["status"] == "unknown"
    assert (
        await client.post(f"/api/v1/outbox/{letter['id']}/send", headers=auth, json=data)
    ).json()["status"] == "unknown"
    assert (
        await client.post(
            f"/api/v1/outbox/{letter['id']}/send",
            headers=auth,
            json={
                "request_id": str(uuid4()),
                "confirmed": True,
                "content_token": letter["content_token"],
            },
        )
    ).status_code == 409
    assert (
        await client.patch(f"/api/v1/outbox/{letter['id']}", headers=auth, json=LETTER)
    ).status_code == 409
    assert len(c.sender.messages) == 1


async def test_recovery_marks_interrupted_delivery_unknown(client, auth):
    c = client.container
    letter = await create(client, auth)
    request_id = str(uuid4())
    async with c.uow() as uow:
        entity = await uow.outbox.get(letter["id"])
        entity.status = "sending"
        await uow.outbox.save(entity)
        await uow.deliveries.add(DeliveryAttempt(outbox_id=entity.id, request_id=request_id))
        await uow.commit()
    await c.outbox.recover_interrupted()
    assert (await c.outbox.get(letter["id"]))["status"] == "unknown"
    response = await c.outbox.send(letter["id"], request_id, True, letter["content_token"])
    assert response["status"] == "unknown" and not c.sender.messages


@pytest.mark.parametrize(
    "changes",
    [
        {"recipient": "first@example.test,second@example.test"},
        {"recipient": "a@example.test\r\nBcc: other@example.test"},
        {"subject": "Тема\r\nBcc: other@example.test"},
        {"body": "   "},
    ],
)
async def test_invalid_letter_never_calls_smtp(client, auth, changes):
    assert (
        await client.post("/api/v1/outbox", headers=auth, json=LETTER | changes)
    ).status_code == 422
    assert not client.container.sender.messages


async def test_attachment_limits_and_type_and_csrf(client, auth):
    c = client.container
    ids = []
    async with c.uow() as uow:
        for i in range(2):
            item = Media(
                kind="attachment",
                original_name=f"file{i}.bin",
                content_type="application/octet-stream",
                filename=f"{uuid4()}.bin",
                size=8 * 1024 * 1024,
            )
            await uow.media.add(item)
            ids.append(item.id)
        image = Media(kind="image", content_type="image/png", filename=f"{uuid4()}.png", size=10)
        await uow.media.add(image)
        await uow.commit()
    assert (
        await client.post("/api/v1/outbox", headers=auth, json=LETTER | {"attachment_ids": ids})
    ).status_code == 413
    assert (
        await client.post(
            "/api/v1/outbox", headers=auth, json=LETTER | {"attachment_ids": [image.id]}
        )
    ).status_code == 422
    assert (await client.get("/api/v1/outbox/unknown", headers=auth)).status_code == 404
    await client.post(
        "/auth/login",
        headers={"Origin": str(client.base_url).rstrip("/")},
        data={"username": "demo", "password": "test-owner-password"},
    )
    assert (await client.post("/api/v1/outbox", json=LETTER)).status_code == 403


async def test_agent_can_prepare_but_not_send_and_cannot_invent_recipient(client, auth):
    c = client.container
    c.assistant.steps = [
        (
            "prepare_outgoing_email",
            {
                "recipient": LETTER["recipient"],
                "subject": LETTER["subject"],
                "body": LETTER["body"],
            },
        )
    ]
    conversation = await c.chat.create_conversation("Исходящее письмо")
    result = await c.chat(
        conversation["id"],
        "Подготовь письмо для reader@example.test: просьба проверить счёт.",
        str(uuid4()),
    )
    assert result["actions"][0]["result"]["status"] == "draft"
    assert len(await c.outbox.list()) == 1 and not c.sender.messages
    second = await c.chat(
        conversation["id"], "Подготовь письмо с просьбой проверить счёт.", str(uuid4())
    )
    assert second["actions"][0]["result"]["error"]["code"] == "invalid_input"
    assert len(await c.outbox.list()) == 1


async def test_confirmation_is_bound_to_reviewed_letter_content(client, auth):
    letter = await create(client, auth)
    edited = await client.patch(
        "/api/v1/outbox/" + letter["id"],
        headers=auth,
        json=LETTER | {"recipient": "changed@example.test"},
    )
    assert edited.status_code == 200 and edited.json()["content_token"] != letter["content_token"]
    response = await client.post(
        "/api/v1/outbox/" + letter["id"] + "/send",
        headers=auth,
        json={
            "request_id": str(uuid4()),
            "confirmed": True,
            "content_token": letter["content_token"],
        },
    )
    assert response.status_code == 409 and not client.container.sender.messages
