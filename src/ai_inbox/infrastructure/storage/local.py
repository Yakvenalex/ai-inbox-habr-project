import asyncio
import re
from pathlib import Path

from ai_inbox.domain.errors import NotFound


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, filename: str) -> Path:
        if not re.fullmatch(r"[a-f0-9-]{36}\.(png|jpg|mp3|bin)", filename):
            raise NotFound("Файл не найден.")
        path = self.root / filename
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise NotFound("Файл не найден.")
        return path

    async def write(self, id: str, data: bytes, extension: str) -> str:
        filename = f"{id}.{extension}"
        path = self.path(filename)
        await asyncio.to_thread(path.write_bytes, data)
        await asyncio.to_thread(path.chmod, 0o600)
        return filename

    async def exists(self, filename: str) -> bool:
        return await asyncio.to_thread(self.path(filename).is_file)

    async def read(self, filename: str) -> bytes:
        try:
            return await asyncio.to_thread(self.path(filename).read_bytes)
        except FileNotFoundError as exc:
            raise NotFound("Файл удалён или срок хранения истёк.") from exc

    async def delete(self, filename: str) -> None:
        await asyncio.to_thread(self.path(filename).unlink, missing_ok=True)
