from datetime import UTC, datetime, timedelta

import jwt
import pytest


async def test_bearer_tasks_and_validation(client, auth):
    assert (await client.get("/api/v1/tasks")).status_code == 401
    response = await client.post(
        "/api/v1/tasks",
        headers=auth,
        json={"title": "Учебный счёт", "due_at": "2026-11-19T09:20:00+03:00"},
    )
    assert response.status_code == 201
    task = response.json()
    assert task["due_at"] == "2026-11-19T06:20:00Z"
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}", headers=auth, json={"status": "completed"}
    )
    assert response.json()["status"] == "completed"
    assert (await client.get("/api/v1/tasks?status=open", headers=auth)).json() == []
    assert (
        await client.post(
            "/api/v1/tasks", headers=auth, json={"title": " ", "due_at": "2026-11-19"}
        )
    ).status_code == 422
    assert (await client.get("/api/v1/tasks/unknown", headers=auth)).status_code == 404


async def test_cookie_csrf_and_bad_header_no_fallback(client):
    response = await client.post(
        "/auth/login", data={"username": "demo", "password": "test-owner-password"}
    )
    assert response.status_code == 403
    response = await client.post(
        "/auth/login",
        headers={"Origin": str(client.base_url).rstrip("/")},
        data={"username": "demo", "password": "test-owner-password"},
    )
    assert response.status_code == 303
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert (await client.get("/api/v1/auth/me")).json() == {"username": "demo"}
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer broken"})
    ).status_code == 401
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": "Basic broken"})
    ).status_code == 401
    assert (await client.post("/api/v1/tasks", json={"title": "CSRF"})).status_code == 403
    headers = {
        "Origin": str(client.base_url).rstrip("/"),
        "X-CSRF-Token": client.cookies["ai_inbox_csrf"],
    }
    assert (
        await client.post("/api/v1/tasks", headers=headers, json={"title": "CSRF"})
    ).status_code == 201
    assert (await client.post("/auth/logout", headers=headers)).status_code == 303
    assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.parametrize("case", ["expired", "algorithm", "audience", "subject", "missing_claim"])
async def test_invalid_jwt(client, settings, case):
    claims = dict(
        sub="demo",
        iss=settings.jwt_issuer,
        aud=settings.jwt_audience,
        exp=datetime.now(UTC) + timedelta(minutes=5),
        iat=datetime.now(UTC),
        csrf="test",
    )
    algorithm = "HS256"
    if case == "expired":
        claims["exp"] = datetime.now(UTC) - timedelta(seconds=10)
    if case == "algorithm":
        algorithm = "HS512"
    if case == "audience":
        claims["aud"] = "another-app"
    if case == "subject":
        claims["sub"] = "another-owner"
    if case == "missing_claim":
        del claims["csrf"]
    token = jwt.encode(
        claims,
        (settings.jwt_secret_key.get_secret_value() + "x" * 64)
        if case == "algorithm"
        else settings.jwt_secret_key.get_secret_value(),
        algorithm=algorithm,
    )
    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_token_endpoint_never_sets_cookie(client):
    response = await client.post(
        "/api/v1/auth/token", data={"username": "demo", "password": "test-owner-password"}
    )
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert (
        await client.post("/api/v1/auth/token", data={"username": "demo", "password": "wrong"})
    ).status_code == 401


async def test_tampered_malformed_and_expired_cookie(client, settings, auth):
    good = (
        await client.post(
            "/api/v1/auth/token", data={"username": "demo", "password": "test-owner-password"}
        )
    ).json()["access_token"]
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + good + "tamper"})
    ).status_code == 401
    claims = jwt.decode(
        good,
        settings.jwt_secret_key.get_secret_value(),
        algorithms=["HS256"],
        audience=settings.jwt_audience,
    )
    claims["exp"] = None
    malformed = jwt.encode(claims, settings.jwt_secret_key.get_secret_value(), algorithm="HS256")
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + malformed})
    ).status_code == 401
    claims["exp"] = datetime.now(UTC) - timedelta(minutes=1)
    expired = jwt.encode(claims, settings.jwt_secret_key.get_secret_value(), algorithm="HS256")
    client.cookies.set(settings.auth_cookie_name, expired)
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (await client.get("/")).status_code == 303
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 200


async def test_csrf_wrong_origin_and_logout_does_not_revoke_bearer(client, auth):
    origin = str(client.base_url).rstrip("/")
    await client.post(
        "/auth/login",
        headers={"Origin": origin},
        data={"username": "demo", "password": "test-owner-password"},
    )
    csrf = client.cookies["ai_inbox_csrf"]
    assert (
        await client.post(
            "/api/v1/tasks",
            headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf},
            json={"title": "Не создавать"},
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/tasks",
            headers={"Origin": origin, "X-CSRF-Token": "incorrect"},
            json={"title": "Не создавать"},
        )
    ).status_code == 403
    response = await client.post("/auth/logout", headers={"Origin": origin, "X-CSRF-Token": csrf})
    assert response.status_code == 303 and all(
        "Max-Age=0" in h for h in response.headers.get_list("set-cookie")
    )
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 200
