from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()

OPENAI_IMAGE_MIMES = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}

DOCUMENT_IMAGE_MIMES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}


class ReceiptImageError(ValueError):
    pass


def infer_document_image_mime(mime_type: str | None, file_name: str | None) -> str | None:
    """Accept Telegram image documents even when their MIME is generic or missing."""
    normalized = (mime_type or "").casefold().strip()
    if normalized.startswith("image/"):
        return "image/jpeg" if normalized in {"image/jpg", "image/pjpeg"} else normalized
    suffix = Path(file_name or "").suffix.casefold()
    return DOCUMENT_IMAGE_MIMES.get(suffix)


def normalize_receipt_image(data: bytes) -> tuple[bytes, str, str]:
    """Validate an image and convert formats unsupported by OpenAI Vision to JPEG."""
    if not data:
        raise ReceiptImageError("Telegram вернул пустое изображение")

    try:
        with Image.open(BytesIO(data)) as probe:
            image_format = (probe.format or "").upper()
            probe.verify()
        with Image.open(BytesIO(data)) as metadata:
            orientation = metadata.getexif().get(274, 1)
            frame_count = int(getattr(metadata, "n_frames", 1))
    except (OSError, RuntimeError, UnidentifiedImageError, ValueError) as exc:
        raise ReceiptImageError("Файл не является читаемым изображением") from exc

    direct = OPENAI_IMAGE_MIMES.get(image_format)
    if direct is not None and frame_count == 1 and orientation in (None, 1):
        return data, direct[0], direct[1]

    try:
        with Image.open(BytesIO(data)) as source:
            source.seek(0)
            normalized = ImageOps.exif_transpose(source)
            if normalized.mode in {"RGBA", "LA"} or (
                normalized.mode == "P" and "transparency" in normalized.info
            ):
                rgba = normalized.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                normalized = background
            elif normalized.mode != "RGB":
                normalized = normalized.convert("RGB")

            output = BytesIO()
            normalized.save(output, format="JPEG", quality=95, optimize=True)
            return output.getvalue(), "image/jpeg", ".jpg"
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ReceiptImageError("Не удалось подготовить изображение чека") from exc
