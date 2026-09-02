"""Application lifecycle tests for optional vision model preloading."""

import asyncio
import logging

import pytest
from fastapi import FastAPI

from src.api.runtime import build_lifespan
from src.config import Settings
from src.errors import VisionModelUnavailableError


class StubVisionService:
    def __init__(self, *, fail: bool = False, delay: float = 0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def preload(self) -> None:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise VisionModelUnavailableError()


@pytest.mark.asyncio
async def test_lifespan_preloads_vision_model():
    app = FastAPI()
    vision = StubVisionService()
    app.state.vision_service = vision
    lifespan = build_lifespan(Settings(vision_preload=True), enabled=False)

    async with lifespan(app):
        assert vision.calls == 1


@pytest.mark.asyncio
async def test_lifespan_keeps_api_running_when_preload_fails(caplog):
    app = FastAPI()
    vision = StubVisionService(fail=True)
    app.state.vision_service = vision
    lifespan = build_lifespan(Settings(vision_preload=True), enabled=False)

    with caplog.at_level(logging.WARNING, logger="langgraph.api"):
        async with lifespan(app):
            assert vision.calls == 1

    assert "vision.preload_failed" in caplog.messages


@pytest.mark.asyncio
async def test_lifespan_can_disable_vision_preload():
    app = FastAPI()
    vision = StubVisionService()
    app.state.vision_service = vision
    lifespan = build_lifespan(Settings(vision_preload=False), enabled=False)

    async with lifespan(app):
        assert vision.calls == 0


@pytest.mark.asyncio
async def test_lifespan_limits_preload_wait_time(caplog):
    app = FastAPI()
    vision = StubVisionService(delay=1)
    app.state.vision_service = vision
    lifespan = build_lifespan(
        Settings(
            vision_preload=True,
            vision_preload_timeout_seconds=0.01,
        ),
        enabled=False,
    )

    with caplog.at_level(logging.WARNING, logger="langgraph.api"):
        async with lifespan(app):
            assert vision.calls == 1

    assert "vision.preload_failed" in caplog.messages
