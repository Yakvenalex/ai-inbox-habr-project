async def test_protected_pages_and_login_markup(client, auth):
    for path in ["/", "/digests"]:
        assert (await client.get(path)).status_code == 303
    login = await client.get("/login")
    assert login.status_code == 200
    assert 'autocomplete="current-password"' in login.text
    assert "test-owner-password" not in login.text
    await client.post(
        "/auth/login",
        headers={"Origin": str(client.base_url).rstrip("/")},
        data={"username": "demo", "password": "test-owner-password"},
    )
    page = await client.get("/")
    assert page.status_code == 200
    assert "daisyui@5.7.50" in page.text and "@tailwindcss/browser@4.1.18" in page.text
    assert "access_token" not in page.text and "test-owner-password" not in page.text
    assert page.headers["referrer-policy"] == "same-origin"
    draft = (
        await client.post(
            "/api/v1/drafts",
            headers=auth,
            json={"email_id": "synthetic-email", "body": "Учебный ответ"},
        )
    ).json()
    assert (await client.get("/drafts/" + draft["id"])).status_code == 200
    digest = (await client.post("/api/v1/digests", headers=auth)).json()
    assert (await client.get("/digests/" + digest["id"])).status_code == 200
    assert (await client.get("/docs")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert (
        schema["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]["password"][
            "tokenUrl"
        ]
        == "/api/v1/auth/token"
    )
