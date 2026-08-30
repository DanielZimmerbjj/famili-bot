from io import BytesIO

import pytest
from PIL import Image

from family_bot.services.receipt_images import (
    ReceiptImageError,
    infer_document_image_mime,
    normalize_receipt_image,
)


def encoded_image(image_format: str, *, animated: bool = False) -> bytes:
    output = BytesIO()
    first = Image.new("RGB", (48, 64), "white")
    if animated:
        second = Image.new("RGB", (48, 64), "black")
        first.save(output, format=image_format, save_all=True, append_images=[second])
    else:
        options = {"progressive": True} if image_format == "JPEG" else {}
        first.save(output, format=image_format, **options)
    return output.getvalue()


@pytest.mark.parametrize(
    ("image_format", "expected_mime", "expected_extension"),
    (
        ("JPEG", "image/jpeg", ".jpg"),
        ("PNG", "image/png", ".png"),
        ("WEBP", "image/webp", ".webp"),
        ("GIF", "image/gif", ".gif"),
    ),
)
def test_openai_supported_formats_are_validated_and_preserved(
    image_format: str,
    expected_mime: str,
    expected_extension: str,
) -> None:
    source = encoded_image(image_format)

    normalized, mime_type, extension = normalize_receipt_image(source)

    assert normalized == source
    assert mime_type == expected_mime
    assert extension == expected_extension


@pytest.mark.parametrize("image_format", ("BMP", "TIFF", "HEIF", "AVIF"))
def test_other_common_photo_formats_are_converted_to_jpeg(image_format: str) -> None:
    source = encoded_image(image_format)

    normalized, mime_type, extension = normalize_receipt_image(source)

    assert mime_type == "image/jpeg"
    assert extension == ".jpg"
    with Image.open(BytesIO(normalized)) as result:
        assert result.format == "JPEG"
        assert result.size == (48, 64)


def test_animated_gif_is_flattened_to_supported_static_jpeg() -> None:
    normalized, mime_type, extension = normalize_receipt_image(encoded_image("GIF", animated=True))

    assert mime_type == "image/jpeg"
    assert extension == ".jpg"
    with Image.open(BytesIO(normalized)) as result:
        assert result.format == "JPEG"


@pytest.mark.parametrize(
    ("mime_type", "file_name", "expected"),
    (
        ("image/jpeg", "receipt.bin", "image/jpeg"),
        ("application/octet-stream", "receipt.JPG", "image/jpeg"),
        (None, "receipt.heic", "image/heic"),
        ("application/octet-stream", "receipt.avif", "image/avif"),
        ("application/pdf", "receipt.pdf", None),
    ),
)
def test_document_format_is_inferred_from_mime_or_extension(
    mime_type: str | None,
    file_name: str,
    expected: str | None,
) -> None:
    assert infer_document_image_mime(mime_type, file_name) == expected


def test_corrupt_image_is_rejected_with_clear_error() -> None:
    with pytest.raises(ReceiptImageError, match="читаемым изображением"):
        normalize_receipt_image(b"not-an-image")
