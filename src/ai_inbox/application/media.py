from datetime import timedelta
from typing import Any

from ai_inbox.domain.entities import Media, new_id
from ai_inbox.domain.errors import InvalidInput

from .ports import Clock, FileStorage, SpeechGateway, UowFactory
from .speech_text import prepare_speech
from .tasks import public


class MediaService:
    def __init__(
        self,
        uow: UowFactory,
        storage: FileStorage,
        speech: SpeechGateway,
        clock: Clock,
        retention_days: int,
    ):
        self.uow, self.storage, self.speech = uow, storage, speech
        self.clock, self.retention_days = clock, retention_days

    async def save_image(self, data: bytes, content_type: str) -> dict[str, Any]:
        # Decoding/normalization belongs to the presentation-side upload adapter.
        return await self._save(
            data, "image", content_type, "png" if content_type == "image/png" else "jpg"
        )

    async def _save(
        self, data: bytes, kind: str, content_type: str, extension: str
    ) -> dict[str, Any]:
        id = new_id()
        filename = await self.storage.write(id, data, extension)
        entity = Media(
            id=id,
            kind=kind,
            content_type=content_type,
            filename=filename,
            size=len(data),
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
        )
        try:
            async with self.uow() as uow:
                await uow.media.add(entity)
                await uow.commit()
        except BaseException:
            await self.storage.delete(filename)
            raise
        return {"file_id": id, "url": f"/api/v1/media/{id}", "content_type": content_type}

    async def transcribe(self, data: bytes, content_type: str) -> dict[str, Any]:
        text = await self.speech.transcribe(data, content_type)
        return {"text": text, "confirmation_required": True}

    async def synthesize(self, text: str) -> dict[str, Any]:
        if not text.strip() or len(text) > 5000:
            raise InvalidInput("Для озвучки нужен текст от 1 до 5000 символов.")
        audio = await self.speech.synthesize(prepare_speech(text))
        return await self._save(audio, "audio", "audio/mpeg", "mp3")

    async def get(self, id: str) -> dict[str, Any]:
        async with self.uow() as uow:
            media = await uow.media.get(id)
        # Verify existence without loading an entire MP3 into memory.
        if not await self.storage.exists(media.filename):
            from ai_inbox.domain.errors import NotFound

            raise NotFound("Файл удалён или срок хранения истёк.")
        return public(media)

    async def digest_audio(self, id: str) -> dict[str, Any]:
        async with self.uow() as uow:
            digest = await uow.digests.get(id)
        if digest.status != "ready":
            raise InvalidInput("Сначала сформируй дайджест.")
        if digest.audio_id:
            from ai_inbox.domain.errors import NotFound

            try:
                await self.get(digest.audio_id)
                return {
                    "file_id": digest.audio_id,
                    "url": f"/api/v1/media/{digest.audio_id}",
                    "content_type": "audio/mpeg",
                }
            except NotFound:
                pass
        media = await self.synthesize(digest.text)
        digest.audio_id = media["file_id"]
        digest.updated_at = self.clock.now()
        async with self.uow() as uow:
            await uow.digests.save(digest)
            await uow.commit()
        return media

    async def cleanup(self) -> None:
        threshold = self.clock.now() - timedelta(days=self.retention_days)
        while True:
            async with self.uow() as uow:
                expired = await uow.media.list(kind="audio", created_before=threshold)
            if not expired:
                break
            for media in expired:
                await self.storage.delete(media.filename)
                async with self.uow() as uow:
                    await uow.media.delete(media.id)
                    await uow.commit()
