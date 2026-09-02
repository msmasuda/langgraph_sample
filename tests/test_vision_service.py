"""Validation and Ollama integration tests for generic image analysis."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
import pytest
from PIL import Image

from src.errors import (
    ImageTooLargeError,
    InvalidImageError,
    InvalidPromptError,
    InvalidResponseSchemaError,
    SchemaValidationFailedError,
    UnsupportedImageTypeError,
    VisionModelUnavailableError,
    VisionTimeoutError,
)
from src.services.vision_service import VisionService


def image_bytes(image_format: str = "PNG", *, size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "orange").save(output, format=image_format)
    return output.getvalue()


def jpeg_with_exif() -> bytes:
    output = io.BytesIO()
    metadata = Image.Exif()
    metadata[0x010E] = "private description"
    Image.new("RGB", (4, 3), "orange").save(output, format="JPEG", exif=metadata)
    return output.getvalue()


def service(
    handler: Any,
    **overrides: Any,
) -> VisionService:
    values = {
        "base_url": "http://ollama.test",
        "default_model": "vision-model",
        "allowed_models": frozenset({"vision-model", "other-vision"}),
        "timeout_seconds": 3,
        "think": False,
        "keep_alive": "30m",
        "max_image_bytes": 1_000_000,
        "allowed_mime_types": frozenset(
            {"image/jpeg", "image/png", "image/webp"}
        ),
        "max_prompt_chars": 100,
        "max_schema_bytes": 4_096,
        "max_response_bytes": 4_096,
        "max_image_width": 100,
        "max_image_height": 100,
        "max_image_pixels": 10_000,
        "max_model_image_edge": 64,
        "max_schema_depth": 6,
        "max_schema_properties": 20,
        "max_array_items": 10,
        "max_output_string_chars": 100,
        "transport": httpx.MockTransport(handler),
    }
    values.update(overrides)
    return VisionService(**values)


def ollama_handler(
    content: str,
    *,
    capabilities: list[str] | None = None,
    captured: list[dict[str, Any]] | None = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(
                200,
                json={"capabilities": capabilities or ["completion", "vision"]},
            )
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        if captured is not None:
            captured.append(payload)
        return httpx.Response(
            200,
            json={"model": payload["model"], "message": {"content": content}},
        )

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_format", "mime_type"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")],
)
async def test_supported_images_return_text_and_are_sanitized(
    image_format: str,
    mime_type: str,
):
    captured: list[dict[str, Any]] = []
    analyzer = service(ollama_handler("画像の説明です。", captured=captured))

    result = await analyzer.analyze(
        image=image_bytes(image_format),
        declared_mime_type=mime_type,
        prompt="画像を説明してください。",
    )

    assert result.content == "画像の説明です。"
    assert result.model == "vision-model"
    request_image = base64.b64decode(captured[0]["messages"][0]["images"][0])
    with Image.open(io.BytesIO(request_image)) as decoded:
        assert decoded.format == image_format
        assert "exif" not in decoded.info
    assert "format" not in captured[0]
    assert captured[0]["think"] is False
    assert captured[0]["keep_alive"] == "30m"


@pytest.mark.asyncio
async def test_structured_output_uses_format_and_is_validated():
    captured: list[dict[str, Any]] = []
    schema = {
        "type": "object",
        "properties": {"dishName": {"type": "string"}},
        "required": ["dishName"],
        "additionalProperties": False,
    }
    analyzer = service(
        ollama_handler('{"dishName":"カレー"}', captured=captured)
    )

    result = await analyzer.analyze(
        image=image_bytes(),
        declared_mime_type="image/png",
        prompt="料理名を返してください。",
        response_schema=json.dumps(schema),
        model="other-vision",
    )

    assert result.content == {"dishName": "カレー"}
    assert result.model == "other-vision"
    assert captured[0]["format"] == schema
    assert captured[0]["options"]["temperature"] == 0
    assert "JSONだけを返してください" in captured[0]["messages"][0]["content"]
    assert '"dishName"' in captured[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_whole_response_json_fence_is_safely_unwrapped():
    schema = {
        "type": "object",
        "properties": {"color": {"type": "string"}},
        "required": ["color"],
        "additionalProperties": False,
    }
    analyzer = service(ollama_handler('```json\n{"color":"赤"}\n```'))

    result = await analyzer.analyze(
        image=image_bytes(),
        declared_mime_type="image/png",
        prompt="色を返してください。",
        response_schema=json.dumps(schema),
    )

    assert result.content == {"color": "赤"}


@pytest.mark.asyncio
async def test_exif_metadata_is_removed_before_model_request():
    captured: list[dict[str, Any]] = []
    analyzer = service(ollama_handler("説明", captured=captured))

    await analyzer.analyze(
        image=jpeg_with_exif(),
        declared_mime_type="image/jpeg",
        prompt="説明してください。",
    )

    sanitized = base64.b64decode(captured[0]["messages"][0]["images"][0])
    with Image.open(io.BytesIO(sanitized)) as decoded:
        assert not decoded.getexif()


@pytest.mark.asyncio
async def test_large_image_is_downscaled_before_model_request():
    captured: list[dict[str, Any]] = []
    analyzer = service(
        ollama_handler("説明", captured=captured),
        max_image_width=300,
        max_image_height=300,
        max_image_pixels=30_000,
        max_model_image_edge=64,
    )

    await analyzer.analyze(
        image=image_bytes(size=(200, 100)),
        declared_mime_type="image/png",
        prompt="説明してください。",
    )

    resized = base64.b64decode(captured[0]["messages"][0]["images"][0])
    with Image.open(io.BytesIO(resized)) as decoded:
        assert decoded.size == (64, 32)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "declared", "error_type"),
    [
        (b"", "image/png", InvalidImageError),
        (b"not-an-image", "image/png", InvalidImageError),
        (image_bytes("PNG"), "image/jpeg", UnsupportedImageTypeError),
        (image_bytes("PNG"), "application/octet-stream", UnsupportedImageTypeError),
    ],
)
async def test_invalid_or_spoofed_images_are_rejected(
    content: bytes,
    declared: str,
    error_type: type[Exception],
):
    analyzer = service(ollama_handler("unused"))

    with pytest.raises(error_type):
        await analyzer.analyze(
            image=content,
            declared_mime_type=declared,
            prompt="説明してください。",
        )


@pytest.mark.asyncio
async def test_image_byte_and_pixel_limits_are_enforced():
    content = image_bytes(size=(11, 10))
    byte_limited = service(ollama_handler("unused"), max_image_bytes=len(content) - 1)
    pixel_limited = service(ollama_handler("unused"), max_image_pixels=100)

    with pytest.raises(ImageTooLargeError):
        await byte_limited.analyze(
            image=content,
            declared_mime_type="image/png",
            prompt="説明してください。",
        )
    with pytest.raises(ImageTooLargeError):
        await pixel_limited.analyze(
            image=content,
            declared_mime_type="image/png",
            prompt="説明してください。",
        )

    width_limited = service(ollama_handler("unused"), max_image_width=10)
    with pytest.raises(ImageTooLargeError):
        await width_limited.analyze(
            image=content,
            declared_mime_type="image/png",
            prompt="説明してください。",
        )


@pytest.mark.asyncio
async def test_blank_and_too_long_prompts_are_rejected():
    analyzer = service(ollama_handler("unused"), max_prompt_chars=5)

    for prompt in ("   ", "123456"):
        with pytest.raises(InvalidPromptError):
            await analyzer.analyze(
                image=image_bytes(),
                declared_mime_type="image/png",
                prompt=prompt,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        "not-json",
        "[]",
        json.dumps({"$ref": "https://example.com/schema.json"}),
        json.dumps({"type": "array", "maxItems": 11}),
        json.dumps({"type": "string", "maxLength": 101}),
    ],
)
async def test_invalid_or_unsafe_schemas_are_rejected(schema: str):
    analyzer = service(ollama_handler("unused"))

    with pytest.raises(InvalidResponseSchemaError):
        await analyzer.analyze(
            image=image_bytes(),
            declared_mime_type="image/png",
            prompt="解析してください。",
            response_schema=schema,
        )


@pytest.mark.asyncio
async def test_deep_schema_is_rejected():
    nested: dict[str, Any] = {"type": "string"}
    for _ in range(7):
        nested = {"type": "array", "items": nested}
    analyzer = service(ollama_handler("unused"), max_schema_depth=6)

    with pytest.raises(InvalidResponseSchemaError):
        await analyzer.analyze(
            image=image_bytes(),
            declared_mime_type="image/png",
            prompt="解析してください。",
            response_schema=json.dumps(nested),
        )


@pytest.mark.asyncio
async def test_schema_mismatch_is_not_returned_as_success():
    analyzer = service(ollama_handler('{"count":"invalid"}'))
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }

    with pytest.raises(SchemaValidationFailedError):
        await analyzer.analyze(
            image=image_bytes(),
            declared_mime_type="image/png",
            prompt="数えてください。",
            response_schema=json.dumps(schema),
        )


@pytest.mark.asyncio
async def test_missing_non_vision_and_unlisted_models_are_rejected():
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    for analyzer, requested_model in (
        (service(missing), None),
        (service(ollama_handler("unused", capabilities=["completion"])), None),
        (service(ollama_handler("unused")), "not-allowed"),
    ):
        with pytest.raises(VisionModelUnavailableError):
            await analyzer.analyze(
                image=image_bytes(),
                declared_mime_type="image/png",
                prompt="説明してください。",
                model=requested_model,
            )


@pytest.mark.asyncio
async def test_ollama_connection_and_timeout_are_safe_errors():
    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private host detail", request=request)

    def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    for handler, error_type in (
        (disconnected, VisionModelUnavailableError),
        (timed_out, VisionTimeoutError),
    ):
        analyzer = service(handler)
        with pytest.raises(error_type) as captured:
            await analyzer.analyze(
                image=image_bytes(),
                declared_mime_type="image/png",
                prompt="説明してください。",
            )
        assert "private" not in str(captured.value)


@pytest.mark.asyncio
async def test_oversized_model_response_is_rejected():
    analyzer = service(
        ollama_handler("x" * 100),
        max_response_bytes=50,
    )

    with pytest.raises(VisionModelUnavailableError):
        await analyzer.analyze(
            image=image_bytes(),
            declared_mime_type="image/png",
            prompt="説明してください。",
        )
