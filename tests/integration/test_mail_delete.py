import pytest

from ai_inbox.domain.errors import ProviderUnavailable
from ai_inbox.infrastructure.ai.assistant import TOOL_DEFINITIONS


async def test_delete_updates_real_gateway_view_and_repeat_is_safe(client, auth):
    c = client.container
    task = await c.tasks.create("Учебная задача", source="email:synthetic-email")
    draft = await c.drafts.create("synthetic-email", subject="Re: Учебный счёт", body="Ответ")
    response = await client.request(
        "DELETE", "/api/v1/emails/synthetic-email", headers=auth, json={"confirmed": True}
    )
    assert response.status_code == 200
    assert response.json() == {"id": "synthetic-email", "deleted": True, "already_absent": False}
    assert (await client.get("/api/v1/emails?unread=false", headers=auth)).json() == []
    assert (await client.get("/api/v1/emails/status", headers=auth)).json() == {
        "total": 0,
        "unread": 0,
    }
    assert (await client.get("/api/v1/emails/synthetic-email", headers=auth)).status_code == 404
    again = await client.request(
        "DELETE", "/api/v1/emails/synthetic-email", headers=auth, json={"confirmed": True}
    )
    assert again.json()["already_absent"] and c.mail_gateway.deletions == 1
    assert (await c.tasks.get(task["id"]))["title"] == "Учебная задача"
    assert (await c.drafts.get(draft["id"]))["body"] == "Ответ"
    assert not any("delete" in name or "trash" in name for name in TOOL_DEFINITIONS)


@pytest.mark.parametrize(
    "body", [{}, {"confirmed": False}, {"confirmed": "true"}, {"confirmed": 1}, {"confirmed": None}]
)
async def test_explicit_strict_confirmation_required(client, auth, body):
    response = await client.request(
        "DELETE", "/api/v1/emails/synthetic-email", headers=auth, json=body
    )
    assert response.status_code == 422 and not client.container.mail_gateway.deleted


async def test_auth_csrf_and_wrong_origin_cannot_delete(client):
    path = "/api/v1/emails/synthetic-email"
    assert (await client.request("DELETE", path, json={"confirmed": True})).status_code == 401
    origin = str(client.base_url).rstrip("/")
    await client.post(
        "/auth/login",
        headers={"Origin": origin},
        data={"username": "demo", "password": "test-owner-password"},
    )
    assert (await client.request("DELETE", path, json={"confirmed": True})).status_code == 403
    assert (
        await client.request(
            "DELETE",
            path,
            json={"confirmed": True},
            headers={
                "Origin": "https://other.test",
                "X-CSRF-Token": client.cookies["ai_inbox_csrf"],
            },
        )
    ).status_code == 403
    assert not client.container.mail_gateway.deleted
    assert (
        await client.request(
            "DELETE",
            path,
            json={"confirmed": True},
            headers={"Origin": origin, "X-CSRF-Token": client.cookies["ai_inbox_csrf"]},
        )
    ).status_code == 200


async def test_bulk_partial_results_deduplicates_and_can_retry_only_failed(client, auth):
    response = await client.post(
        "/api/v1/emails/delete",
        headers=auth,
        json={"ids": ["synthetic-email", "synthetic-email", "missing"], "confirmed": True},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2 and results[0]["deleted"] and results[0]["error"] is None
    assert not results[1]["deleted"] and results[1]["error"]["code"] == "provider_unavailable"
    assert client.container.mail_gateway.deletions == 1
    for ids in [[], ["synthetic-email"] * 21]:
        assert (
            await client.post(
                "/api/v1/emails/delete", headers=auth, json={"ids": ids, "confirmed": True}
            )
        ).status_code == 422


async def test_provider_failure_is_not_success_and_does_not_leak_or_retry(client, auth):
    calls = 0

    async def fail(id):
        nonlocal calls
        calls += 1
        raise ProviderUnavailable("Почта недоступна. Проверь корзину.")

    client.container.mail.deletions.delete = fail
    response = await client.request(
        "DELETE", "/api/v1/emails/synthetic-email", headers=auth, json={"confirmed": True}
    )
    assert response.status_code == 503 and calls == 1
    assert not client.container.mail_gateway.deleted
