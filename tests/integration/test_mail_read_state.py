from uuid import uuid4

import pytest


async def test_read_state_is_explicit_and_synchronized_with_lists_and_counts(client, auth):
    assert (await client.get("/api/v1/emails/status", headers=auth)).json() == {
        "total": 1,
        "unread": 1,
    }
    assert (await client.get("/api/v1/emails/synthetic-email", headers=auth)).json()[
        "unread"
    ] is True
    assert (
        await client.post(
            "/api/v1/emails/summarize", headers=auth, json={"ids": ["synthetic-email"]}
        )
    ).status_code == 200
    assert client.container.mail_gateway.email.unread is True
    changed = await client.post(
        "/api/v1/emails/synthetic-email/read-state", headers=auth, json={"unread": False}
    )
    assert changed.status_code == 200 and changed.json() == {
        "id": "synthetic-email",
        "unread": False,
    }
    assert (await client.get("/api/v1/emails/status", headers=auth)).json()["unread"] == 0
    assert (await client.get("/api/v1/emails?unread=true", headers=auth)).json() == []
    assert (await client.get("/api/v1/emails?unread=false", headers=auth)).json()[0][
        "unread"
    ] is False
    for _ in range(2):
        assert (
            await client.post(
                "/api/v1/emails/synthetic-email/read-state", headers=auth, json={"unread": True}
            )
        ).json()["unread"] is True
    assert (await client.get("/api/v1/emails/status", headers=auth)).json()["unread"] == 1


async def test_read_state_auth_and_csrf(client, auth):
    path = "/api/v1/emails/synthetic-email/read-state"
    assert (await client.post(path, json={"unread": False})).status_code == 401
    origin = str(client.base_url).rstrip("/")
    await client.post(
        "/auth/login",
        headers={"Origin": origin},
        data={"username": "demo", "password": "test-owner-password"},
    )
    assert (await client.post(path, json={"unread": False})).status_code == 403
    assert client.container.mail_gateway.email.unread is True
    assert (
        await client.post(
            path,
            headers={
                "Origin": "https://other.test",
                "X-CSRF-Token": client.cookies["ai_inbox_csrf"],
            },
            json={"unread": False},
        )
    ).status_code == 403
    assert (
        await client.post(
            path,
            headers={"Origin": origin, "X-CSRF-Token": client.cookies["ai_inbox_csrf"]},
            json={"unread": False},
        )
    ).status_code == 200


async def test_bulk_reports_partial_success_and_deduplicates(client, auth):
    response = await client.post(
        "/api/v1/emails/read-state",
        headers=auth,
        json={"ids": ["synthetic-email", "synthetic-email", "missing"], "unread": False},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2 and results[0]["unread"] is False and results[0]["error"] is None
    assert results[1]["unread"] is None and results[1]["error"]["code"] == "not_found"
    assert client.container.mail_gateway.email.unread is False


@pytest.mark.parametrize("unread", ["false", 0, None])
async def test_read_state_requires_boolean(client, auth, unread):
    assert (
        await client.post(
            "/api/v1/emails/synthetic-email/read-state", headers=auth, json={"unread": unread}
        )
    ).status_code == 422
    assert client.container.mail_gateway.email.unread is True


async def test_read_state_missing_id_and_batch_limits(client, auth):
    assert (
        await client.post("/api/v1/emails/missing/read-state", headers=auth, json={"unread": False})
    ).status_code == 404
    for ids in [[], ["synthetic-email"] * 21]:
        assert (
            await client.post(
                "/api/v1/emails/read-state", headers=auth, json={"ids": ids, "unread": False}
            )
        ).status_code == 422
    assert client.container.mail_gateway.email.unread is True


async def test_agent_and_digest_do_not_mark_mail_read(client):
    c = client.container
    c.assistant.steps = [("read_email", {"email_id": "synthetic-email"})]
    conversation = await c.chat.create_conversation("Чтение без изменения флагов")
    result = await c.chat(conversation["id"], "Прочитай письмо synthetic-email", str(uuid4()))
    assert result["status"] == "completed"
    assert c.mail_gateway.email.unread is True
    assert (await c.digests())["status"] == "ready"
    assert c.mail_gateway.email.unread is True
