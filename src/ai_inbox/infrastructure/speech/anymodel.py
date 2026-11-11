import asyncio
import io
import tempfile
from pathlib import Path

import httpx

from ai_inbox.domain.errors import InvalidInput, ProviderUnavailable, TooLarge
from ai_inbox.infrastructure.ai.retry import provider_request


class AnyModelSpeech:
    def __init__(
        self, http: httpx.AsyncClient, asr_model: str, language: str, tts_model: str, attempts: int
    ):
        self.http, self.asr_model, self.language = http, asr_model, language
        self.tts_model, self.attempts = tts_model, attempts
        self.conversions = asyncio.Semaphore(2)

    async def _convert(self, data: bytes) -> bytes:
        # No shell, no user filename, no remote URLs or playlists. Only local demuxers.
        if not (
            data.startswith((b"OggS", b"RIFF", b"ID3", b"\x1aE\xdf\xa3"))
            or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
            or data[4:8] == b"ftyp"
        ):
            raise InvalidInput("Нужен аудиофайл OGG, WebM, WAV, MP3 или M4A.")
        async with self.conversions:
            with tempfile.TemporaryDirectory(prefix="ai-inbox-audio-") as folder:
                source, output = Path(folder) / "input", Path(folder) / "audio.ogg"
                await asyncio.to_thread(source.write_bytes, data)
                try:
                    process = await asyncio.create_subprocess_exec(
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-format_whitelist",
                        "ogg,matroska,webm,wav,mp3,mov,mp4,m4a,3gp,3g2,mj2",
                        "-i",
                        str(source),
                        "-t",
                        "301",
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "libopus",
                        "-b:a",
                        "32k",
                        str(output),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except FileNotFoundError as exc:
                    raise ProviderUnavailable("Для аудио установи ffmpeg.") from exc
                try:
                    await asyncio.wait_for(process.wait(), 40)
                except (TimeoutError, asyncio.CancelledError):
                    process.kill()
                    await process.wait()
                    raise
                if process.returncode or not output.exists():
                    raise InvalidInput("Не удалось декодировать аудио. Загрузи другой файл.")
                # Duration after conversion, independent of attacker-controlled file metadata.
                probe = await asyncio.create_subprocess_exec(
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    str(output),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                raw, _ = await asyncio.wait_for(probe.communicate(), 10)
                try:
                    duration = float(raw.strip())
                except ValueError as exc:
                    raise InvalidInput("Не удалось определить длительность аудио.") from exc
                if duration > 300:
                    raise TooLarge("Аудио должно быть не длиннее пяти минут.")
                return await asyncio.to_thread(output.read_bytes)

    async def transcribe(self, data: bytes, content_type: str) -> str:
        normalized = await self._convert(data)

        async def request():
            response = await self.http.post(
                "audio/transcriptions",
                data={"model": self.asr_model, "language": self.language},
                files={"file": ("recording.ogg", io.BytesIO(normalized), "audio/ogg")},
            )
            response.raise_for_status()
            return response

        response = await provider_request(request, self.attempts)
        try:
            text = response.json()["text"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError
            return text
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable("AnyModel не вернул расшифровку речи.") from exc

    async def synthesize(self, text: str) -> bytes:
        async def request():
            response = await self.http.post(
                "audio/speech",
                json={"model": self.tts_model, "input": text, "response_format": "mp3"},
            )
            response.raise_for_status()
            return response

        response = await provider_request(request, self.attempts)
        data = response.content
        if not data or not (
            data.startswith(b"ID3") or data[0] == 255 and len(data) > 1 and data[1] & 224 == 224
        ):
            raise ProviderUnavailable("AnyModel вернул ответ без MP3. Попробуй позже.")
        return data
