"""Ollama health and model discovery service."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.errors import ModelServiceError


@dataclass(frozen=True, slots=True)
class OllamaStatus:
    """Connection status and available Ollama model names."""

    available: bool
    models: tuple[str, ...] = ()


class OllamaModelService:
    """Retrieve Ollama availability and locally installed models."""

    def __init__(self, base_url: str, timeout_seconds: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def list_models(self) -> tuple[str, ...]:
        """Return installed model names ordered alphabetically."""
        request = Request(
            f"{self.base_url}/api/tags",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload: Any = json.load(response)
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise ModelServiceError() from error

        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ModelServiceError()

        names = {
            name
            for model in payload["models"]
            if isinstance(model, dict)
            and isinstance((name := model.get("name") or model.get("model")), str)
            and name.strip()
        }
        return tuple(sorted(names))

    async def alist_models(self) -> tuple[str, ...]:
        """Asynchronously return installed model names."""
        return await asyncio.to_thread(self.list_models)

    def get_status(self) -> OllamaStatus:
        """Return a safe availability result without exposing connection details."""
        try:
            return OllamaStatus(available=True, models=self.list_models())
        except ModelServiceError:
            return OllamaStatus(available=False)

    async def aget_status(self) -> OllamaStatus:
        """Asynchronously return a safe availability result."""
        try:
            return OllamaStatus(available=True, models=await self.alist_models())
        except ModelServiceError:
            return OllamaStatus(available=False)
