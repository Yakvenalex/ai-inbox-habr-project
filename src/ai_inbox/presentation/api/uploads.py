import asyncio
import io
import warnings

from fastapi import Request, UploadFile
from PIL import Image, UnidentifiedImageError

from ai_inbox.domain.errors import InvalidInput, TooLarge


async def limited_upload(file: UploadFile, request: Request) -> bytes:
    limit = request.app.state.container.settings.max_upload_mb * 1024 * 1024
    chunks, total = [], 0
    try:
        while data := await file.read(65536):
            total += len(data)
            if total > limit:
                raise TooLarge("Файл превышает лимит загрузки.")
            chunks.append(data)
    finally:
        await file.close()
    if not total:
        raise InvalidInput("Загружен пустой файл.")
    return b"".join(chunks)


def normalize_image(data: bytes, content_type: str) -> bytes:
    if content_type not in {"image/png", "image/jpeg"}:
        raise InvalidInput("Поддерживаются только JPEG и PNG.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG"} or image.width * image.height > 20_000_000:
                    raise InvalidInput(
                        "Изображение слишком большое или имеет неподдерживаемый формат."
                    )
                expected = "PNG" if content_type == "image/png" else "JPEG"
                if image.format != expected:
                    raise InvalidInput("Тип файла не совпадает с содержимым.")
                from PIL import ImageOps

                clean = ImageOps.exif_transpose(image).convert("RGB")
                output = io.BytesIO()
                clean.save(output, format=expected)
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise InvalidInput("Не удалось прочитать изображение JPEG/PNG.") from exc


async def image_upload(file: UploadFile, request: Request) -> tuple[bytes, str]:
    content_type = file.content_type or ""
    data = await limited_upload(file, request)
    return await asyncio.to_thread(normalize_image, data, content_type), content_type
