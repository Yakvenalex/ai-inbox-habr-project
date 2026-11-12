from tests.fakes import FakeMail


async def test_paged_api_does_not_replace_legacy_contract_or_mark_read(client, auth):
    fake = FakeMail(count=25)
    client.container.mail.gateway = fake
    first = await client.get("/api/v1/emails/page?unread=true&limit=20", headers=auth)
    assert first.status_code == 200
    page = first.json()
    assert len(page["items"]) == 20 and page["next_cursor"]
    await fake.delete(page["items"][-1]["id"])  # Cursor still identifies an ordered boundary.
    second = await client.get(
        "/api/v1/emails/page", headers=auth, params={"before": page["next_cursor"], "limit": 20}
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 5 and second.json()["next_cursor"] is None
    assert not {e["id"] for e in page["items"]} & {e["id"] for e in second.json()["items"]}
    legacy = await client.get("/api/v1/emails", headers=auth)
    assert isinstance(legacy.json(), list)
    assert fake.email.unread and fake.reads == 0


async def test_page_is_authenticated_and_cursor_validated(client, auth):
    assert (await client.get("/api/v1/emails/page")).status_code == 401
    assert (await client.get("/api/v1/emails/page?before=bad", headers=auth)).status_code == 404
    for limit in (0, 51):
        assert (
            await client.get("/api/v1/emails/page", headers=auth, params={"limit": limit})
        ).status_code == 422
    schema = (await client.get("/openapi.json")).json()
    assert "get" in schema["paths"]["/api/v1/emails/page"]
