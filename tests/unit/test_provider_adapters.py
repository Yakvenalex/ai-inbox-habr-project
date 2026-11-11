import asyncio
import subprocess

import httpx
import pytest

from ai_inbox.domain.errors import InvalidInput, ProviderUnavailable
from ai_inbox.infrastructure.ai.retry import provider_request
from ai_inbox.infrastructure.speech.anymodel import AnyModelSpeech


@pytest.mark.parametrize("status", [429, 503, 524])
async def test_transient_retries_are_bounded_and_html_safe(monkeypatch, status):
    count = 0

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(
            status, text="<html>private upstream diagnostic</html>", headers={"Retry-After": "0"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def operation():
            response = await client.get("https://provider.test/")
            response.raise_for_status()
            return response

        with pytest.raises(ProviderUnavailable) as exc:
            await provider_request(operation, 2)
        assert str(status) in str(exc.value) and "private" not in str(exc.value)
    assert count == 2


async def test_nontransient_not_retried():
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        httpx.Response(
            400, request=httpx.Request("POST", "https://provider.test")
        ).raise_for_status()

    with pytest.raises(ProviderUnavailable):
        await provider_request(operation, 2)
    assert calls == 1


async def test_webm_is_converted_to_ogg_before_asr_and_no_voice_field():
    # Generate one second of synthetic silence; never use the microphone in tests.
    generated = (
        await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=16000:cl=mono",
                "-t",
                "1",
                "-c:a",
                "libopus",
                "-f",
                "webm",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        )
    ).stdout
    requests = []

    async def handler(request):
        body = await request.aread()
        requests.append((request.url.path, body))
        if request.url.path.endswith("transcriptions"):
            assert b"OggS" in body and b"ru" in body
            return httpx.Response(200, json={"text": "Учебная команда"})
        assert b'"voice"' not in body
        return httpx.Response(200, content=b"ID3-synthetic")

    async with httpx.AsyncClient(
        base_url="https://provider.test/v1/", transport=httpx.MockTransport(handler)
    ) as http:
        speech = AnyModelSpeech(http, "dg/nova-3", "ru", "anyvoice/builtin-7", 2)
        assert await speech.transcribe(generated, "audio/webm") == "Учебная команда"
        assert await speech.synthesize("Тест") == b"ID3-synthetic"
        with pytest.raises(InvalidInput):
            await speech.transcribe(b"<html>", "audio/ogg")
    assert len(requests) == 2
