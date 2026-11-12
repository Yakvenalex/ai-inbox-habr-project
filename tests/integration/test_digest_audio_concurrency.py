import asyncio

import pytest

from ai_inbox.domain.errors import ProviderUnavailable


async def test_concurrent_digest_audio_requests_share_one_paid_generation(client):
    c = client.container
    digest = await c.digests()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def synthesize(text):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return b"ID3-synthetic-audio"

    c.media.speech.synthesize = synthesize
    first = asyncio.create_task(c.media.digest_audio(digest["id"]))
    await entered.wait()
    second = asyncio.create_task(c.media.digest_audio(digest["id"]))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    a, b = await asyncio.gather(first, second)
    assert a == b and calls == 1
    assert (await c.digests.get(digest["id"]))["audio_id"] == a["file_id"]
    async with c.uow() as uow:
        assert len(await uow.media.list(kind="audio")) == 1


async def test_failed_generation_unlocks_for_explicit_retry_and_expired_cache_regenerates(client):
    c = client.container
    digest = await c.digests()
    calls = 0

    async def synthesize(text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderUnavailable("Synthetic outage")
        return b"ID3-synthetic-audio"

    c.media.speech.synthesize = synthesize
    with pytest.raises(ProviderUnavailable):
        await c.media.digest_audio(digest["id"])
    audio = await c.media.digest_audio(digest["id"])
    info = await c.media.get(audio["file_id"])
    await c.storage.delete(info["filename"])
    replacement = await c.media.digest_audio(digest["id"])
    assert replacement["file_id"] != audio["file_id"] and calls == 3
