"""Multipart lifecycle tests for the vision API helper."""

from tempfile import SpooledTemporaryFile

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from src.api.vision import read_image_upload
from src.errors import ImageTooLargeError, InvalidImageError


def upload(content: bytes) -> tuple[UploadFile, SpooledTemporaryFile]:
    temporary = SpooledTemporaryFile(max_size=1)
    temporary.write(content)
    temporary.seek(0)
    return (
        UploadFile(
            file=temporary,
            size=len(content),
            filename="image.png",
            headers=Headers({"content-type": "image/png"}),
        ),
        temporary,
    )


@pytest.mark.asyncio
async def test_upload_is_closed_after_success():
    incoming, temporary = upload(b"image")

    assert await read_image_upload(incoming, max_bytes=10) == b"image"
    assert temporary.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "error_type"),
    [(b"", InvalidImageError), (b"too-large", ImageTooLargeError)],
)
async def test_upload_is_closed_after_validation_error(content, error_type):
    incoming, temporary = upload(content)

    with pytest.raises(error_type):
        await read_image_upload(incoming, max_bytes=3)
    assert temporary.closed is True
