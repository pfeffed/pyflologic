"""Shared fixtures: a running fake API and clients pointed at it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from pyflologic import ApiKeyAuth, FloLogicClient
from pyflologic import client as client_module

from .fake_api import API_KEY, COMPANY_ID, FakeApi

ClientFactory = Callable[..., FloLogicClient]


@pytest.fixture(autouse=True)
def fast_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll for write confirmation quickly, so tests do not wait seconds."""
    monkeypatch.setattr(client_module, "_CONFIRM_POLL_SECONDS", 0.02)


@pytest.fixture
async def api() -> AsyncIterator[FakeApi]:
    """Yield a FakeApi with its HTTP server running."""
    fake = FakeApi()
    server = TestServer(fake.app)
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
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
def make_client(api: FakeApi, session: aiohttp.ClientSession) -> ClientFactory:
    """Return a factory building clients pointed at the fake API."""

    def _make(**overrides: object) -> FloLogicClient:
        options: dict[str, object] = {
            "auth": ApiKeyAuth(API_KEY, COMPANY_ID),
            "session": session,
            "base_url": api.url,
        }
        options.update(overrides)
        return FloLogicClient(**options)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def client(make_client: ClientFactory) -> FloLogicClient:
    """Return a client pointed at the fake API."""
    return make_client()
