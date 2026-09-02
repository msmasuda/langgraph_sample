"""Generic, conversation-independent image analysis through Ollama."""

from __future__ import annotations

import base64
import io
import json
import warnings
from dataclasses import dataclass
from typing import Any

import httpx
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from PIL import Image, ImageOps, UnidentifiedImageError

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

_FORMAT_MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "description",
        "title",
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
    }
)
_COMBINER_KEYWORDS = ("allOf", "anyOf", "oneOf")


@dataclass(frozen=True, slots=True)
class VisionAnalysisResult:
    """Normalized result from a vision-capable model."""

    content: Any
    model: str


class VisionService:
    """Validate untrusted image inputs and call Ollama's native chat API."""

    def __init__(
        self,
        *,
        base_url: str,
        default_model: str,
        allowed_models: frozenset[str],
        timeout_seconds: float,
        max_image_bytes: int,
        allowed_mime_types: frozenset[str],
        max_prompt_chars: int,
        max_schema_bytes: int,
        max_response_bytes: int,
        max_image_width: int,
        max_image_height: int,
        max_image_pixels: int,
        max_schema_depth: int,
        max_schema_properties: int,
        max_array_items: int,
        max_output_string_chars: int,
        temperature: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.allowed_models = allowed_models
        self.timeout_seconds = timeout_seconds
        self.max_image_bytes = max_image_bytes
        self.allowed_mime_types = allowed_mime_types
        self.max_prompt_chars = max_prompt_chars
        self.max_schema_bytes = max_schema_bytes
        self.max_response_bytes = max_response_bytes
        self.max_image_width = max_image_width
        self.max_image_height = max_image_height
        self.max_image_pixels = max_image_pixels
        self.max_schema_depth = max_schema_depth
        self.max_schema_properties = max_schema_properties
        self.max_array_items = max_array_items
        self.max_output_string_chars = max_output_string_chars
        self.temperature = temperature
        self.transport = transport

    async def analyze(
        self,
        *,
        image: bytes,
        declared_mime_type: str | None,
        prompt: str,
        response_schema: str | None = None,
        model: str | None = None,
    ) -> VisionAnalysisResult:
        """Return text or schema-validated JSON without persisting input data."""
        normalized_prompt = self._validate_prompt(prompt)
        selected_model = self._select_model(model)
        schema = self._parse_schema(response_schema)
        model_prompt = normalized_prompt
        if schema is not None:
            model_prompt = (
                f"{normalized_prompt}\n\n"
                "次のJSON Schemaに適合するJSONだけを返してください。"
                "Markdownや説明文は含めないでください。\n"
                f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
            )
        encoded_image = base64.b64encode(
            self._validate_and_sanitize_image(image, declared_mime_type)
        ).decode("ascii")

        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": [
                {
                    "role": "user",
                    "content": model_prompt,
                    "images": [encoded_image],
                }
            ],
            "stream": False,
            "options": {"temperature": 0 if schema is not None else self.temperature},
        }
        if schema is not None:
            payload["format"] = schema

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                await self._ensure_vision_model(client, selected_model)
                async with client.stream("POST", "/api/chat", json=payload) as response:
                    if response.status_code >= 400:
                        raise VisionModelUnavailableError()
                    response_payload = await self._read_json_response(response)
        except VisionModelUnavailableError:
            raise
        except httpx.TimeoutException as error:
            raise VisionTimeoutError() from error
        except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise VisionModelUnavailableError() from error

        content = self._extract_content(response_payload)
        if schema is None:
            return VisionAnalysisResult(content=content, model=selected_model)
        return VisionAnalysisResult(
            content=self._validate_structured_output(content, schema),
            model=selected_model,
        )

    def _validate_prompt(self, prompt: str) -> str:
        normalized = prompt.strip()
        if not normalized or len(prompt) > self.max_prompt_chars:
            raise InvalidPromptError()
        return prompt

    def _select_model(self, model: str | None) -> str:
        selected = (model or self.default_model).strip()
        if not selected or selected not in self.allowed_models:
            raise VisionModelUnavailableError()
        return selected

    async def _ensure_vision_model(
        self,
        client: httpx.AsyncClient,
        model: str,
    ) -> None:
        async with client.stream("POST", "/api/show", json={"model": model}) as response:
            if response.status_code >= 400:
                raise VisionModelUnavailableError()
            payload = await self._read_json_response(response)
        try:
            capabilities = payload.get("capabilities", [])
        except (AttributeError, TypeError) as error:
            raise VisionModelUnavailableError() from error
        if not isinstance(capabilities, list) or "vision" not in capabilities:
            raise VisionModelUnavailableError()

    async def _read_json_response(self, response: httpx.Response) -> Any:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > self.max_response_bytes:
                raise VisionModelUnavailableError()
            content.extend(chunk)
        try:
            return json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
            raise VisionModelUnavailableError() from error

    def _validate_and_sanitize_image(
        self,
        content: bytes,
        declared_mime_type: str | None,
    ) -> bytes:
        if not content:
            raise InvalidImageError()
        if len(content) > self.max_image_bytes:
            raise ImageTooLargeError()
        declared = (declared_mime_type or "").split(";", 1)[0].strip().lower()
        if declared not in self.allowed_mime_types:
            raise UnsupportedImageTypeError()

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as probe:
                    actual_format = str(probe.format or "").upper()
                    actual_mime = _FORMAT_MIME_TYPES.get(actual_format)
                    width, height = probe.size
                    frames = int(getattr(probe, "n_frames", 1))
                    if actual_mime is None or actual_mime not in self.allowed_mime_types:
                        raise UnsupportedImageTypeError()
                    if actual_mime != declared:
                        raise UnsupportedImageTypeError()
                    if (
                        width <= 0
                        or height <= 0
                        or width > self.max_image_width
                        or height > self.max_image_height
                        or width * height > self.max_image_pixels
                    ):
                        raise ImageTooLargeError()
                    if frames != 1:
                        raise InvalidImageError()
                    probe.verify()

                with Image.open(io.BytesIO(content)) as source:
                    source.load()
                    clean = ImageOps.exif_transpose(source).copy()
                    clean.info.clear()
        except (UnsupportedImageTypeError, ImageTooLargeError, InvalidImageError):
            raise
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            ValueError,
        ) as error:
            raise InvalidImageError() from error

        output = io.BytesIO()
        save_format = actual_format
        if save_format == "JPEG" and clean.mode not in ("RGB", "L"):
            if "A" in clean.getbands():
                background = Image.new("RGB", clean.size, "white")
                background.paste(clean, mask=clean.getchannel("A"))
                clean = background
            else:
                clean = clean.convert("RGB")
        save_options = {"quality": 90} if save_format in {"JPEG", "WEBP"} else {}
        clean.save(output, format=save_format, **save_options)
        sanitized = output.getvalue()
        if len(sanitized) > self.max_image_bytes:
            raise ImageTooLargeError()
        return sanitized

    def _parse_schema(self, raw_schema: str | None) -> dict[str, Any] | None:
        if raw_schema is None:
            return None
        if not raw_schema.strip() or len(raw_schema.encode("utf-8")) > self.max_schema_bytes:
            raise InvalidResponseSchemaError()
        try:
            schema = json.loads(raw_schema)
            if not isinstance(schema, dict):
                raise InvalidResponseSchemaError()
            self._validate_schema_complexity(schema)
            Draft202012Validator.check_schema(schema)
        except InvalidResponseSchemaError:
            raise
        except (json.JSONDecodeError, UnicodeError, SchemaError, TypeError, ValueError) as error:
            raise InvalidResponseSchemaError() from error
        return schema

    def _validate_schema_complexity(self, schema: dict[str, Any]) -> None:
        property_count = 0
        node_count = 0

        def walk(node: dict[str, Any], depth: int) -> None:
            nonlocal node_count, property_count
            if depth > self.max_schema_depth:
                raise InvalidResponseSchemaError()
            node_count += 1
            if node_count > self.max_schema_properties:
                raise InvalidResponseSchemaError()
            if any(key not in _SCHEMA_KEYWORDS for key in node):
                raise InvalidResponseSchemaError()

            properties = node.get("properties", {})
            if not isinstance(properties, dict):
                raise InvalidResponseSchemaError()
            property_count += len(properties)
            if property_count > self.max_schema_properties:
                raise InvalidResponseSchemaError()
            for child in properties.values():
                if not isinstance(child, dict):
                    raise InvalidResponseSchemaError()
                walk(child, depth + 1)

            items = node.get("items")
            if items is not None:
                if not isinstance(items, dict):
                    raise InvalidResponseSchemaError()
                walk(items, depth + 1)

            additional = node.get("additionalProperties")
            if isinstance(additional, dict):
                walk(additional, depth + 1)
            elif additional is not None and not isinstance(additional, bool):
                raise InvalidResponseSchemaError()

            negated = node.get("not")
            if negated is not None:
                if not isinstance(negated, dict):
                    raise InvalidResponseSchemaError()
                walk(negated, depth + 1)

            for keyword in _COMBINER_KEYWORDS:
                alternatives = node.get(keyword, [])
                if not isinstance(alternatives, list) or len(alternatives) > 10:
                    raise InvalidResponseSchemaError()
                for child in alternatives:
                    if not isinstance(child, dict):
                        raise InvalidResponseSchemaError()
                    walk(child, depth + 1)

            max_items = node.get("maxItems")
            if max_items is not None and (
                not isinstance(max_items, int) or max_items > self.max_array_items
            ):
                raise InvalidResponseSchemaError()
            max_length = node.get("maxLength")
            if max_length is not None and (
                not isinstance(max_length, int)
                or max_length > self.max_output_string_chars
            ):
                raise InvalidResponseSchemaError()
            enum_values = node.get("enum")
            if isinstance(enum_values, list) and len(enum_values) > self.max_array_items:
                raise InvalidResponseSchemaError()
            required = node.get("required")
            if required is not None and (
                not isinstance(required, list)
                or len(required) > self.max_schema_properties
                or not all(isinstance(item, str) for item in required)
            ):
                raise InvalidResponseSchemaError()

        walk(schema, 1)

    @staticmethod
    def _extract_content(payload: Any) -> str:
        try:
            content = payload["message"]["content"]
        except (KeyError, TypeError) as error:
            raise VisionModelUnavailableError() from error
        if not isinstance(content, str) or not content.strip():
            raise VisionModelUnavailableError()
        return content

    def _validate_structured_output(
        self,
        content: str,
        schema: dict[str, Any],
    ) -> Any:
        try:
            parsed = json.loads(self._unwrap_json_fence(content))
            self._validate_output_complexity(parsed)
            Draft202012Validator(schema).validate(parsed)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
            raise SchemaValidationFailedError() from error
        return parsed

    @staticmethod
    def _unwrap_json_fence(content: str) -> str:
        """Accept one whole-response JSON fence emitted by some local models."""
        stripped = content.strip()
        lines = stripped.splitlines()
        if (
            len(lines) >= 3
            and lines[0].strip().lower() in {"```", "```json"}
            and lines[-1].strip() == "```"
        ):
            return "\n".join(lines[1:-1]).strip()
        return stripped

    def _validate_output_complexity(self, value: Any, depth: int = 1) -> None:
        if depth > self.max_schema_depth:
            raise SchemaValidationFailedError()
        if isinstance(value, str):
            if len(value) > self.max_output_string_chars:
                raise SchemaValidationFailedError()
        elif isinstance(value, list):
            if len(value) > self.max_array_items:
                raise SchemaValidationFailedError()
            for item in value:
                self._validate_output_complexity(item, depth + 1)
        elif isinstance(value, dict):
            if len(value) > self.max_schema_properties:
                raise SchemaValidationFailedError()
            for item in value.values():
                self._validate_output_complexity(item, depth + 1)
