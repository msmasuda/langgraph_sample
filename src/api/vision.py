"""Multipart image helpers for the generic vision endpoint."""

from fastapi import UploadFile

from src.errors import ImageTooLargeError, InvalidImageError


async def read_image_upload(upload: UploadFile, *, max_bytes: int) -> bytes:
    """Read one bounded upload and always close its temporary backing file."""
    try:
        if upload.size is not None and upload.size > max_bytes:
            raise ImageTooLargeError()
        content = await upload.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ImageTooLargeError()
        if not content:
            raise InvalidImageError()
        return content
    finally:
        await upload.close()
