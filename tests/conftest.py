"""Shared fixtures: a running fake hub and clients pointed at it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from pyflologic import DeviceIdentity, FloLogicClient

from .fake_hub import FakeHub

ClientFactory = Callable[..., FloLogicClient]


@pytest.fixture
async def hub() -> AsyncIterator[FakeHub]:
    """Yield a FakeHub with its HTTP server running."""
    fake = FakeHub()
    server = TestServer(fake.app)
    await server.start_server()
    fake.url = str(server.make_url("/signalr"))
    try:
        yield fake
    finally:
        await server.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Yield an aiohttp session owned by the test, not by the client."""
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
def make_client(hub: FakeHub, session: aiohttp.ClientSession) -> ClientFactory:
    """Return a factory building clients pointed at the fake hub."""

    def _make(**overrides: object) -> FloLogicClient:
        options: dict[str, object] = {
            "email": "owner@example.com",
            "password": "secret",
            "device": DeviceIdentity("test-device", "AND-test", "token"),
            "session": session,
            "hub_url": hub.url,
            "auto_reconnect": False,
        }
        options.update(overrides)
        return FloLogicClient(**options)  # type: ignore[arg-type]

    return _make


@pytest.fixture
async def client(make_client: ClientFactory) -> AsyncIterator[FloLogicClient]:
    """Yield a connected client."""
    instance = make_client()
    await instance.async_connect()
    try:
        yield instance
    finally:
        await instance.async_disconnect()
