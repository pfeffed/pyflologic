"""Tests for the SignalR transport itself, against the fake hub."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp
import pytest

from pyflologic.exceptions import (
    FloLogicAuthError,
    FloLogicCommandError,
    FloLogicConnectionError,
    FloLogicProtocolError,
    FloLogicTimeoutError,
)
from pyflologic.signalr import SignalRConnection

from .fake_hub import FakeHub


@pytest.fixture
def connect(
    hub: FakeHub, session: aiohttp.ClientSession
) -> Callable[..., AsyncIterator[SignalRConnection]]:
    """Return a factory that builds connections against the fake hub."""

    def _build(**overrides: Any) -> SignalRConnection:
        options: dict[str, Any] = {
            "session": session,
            "url": hub.url,
            "headers": {"userDeviceCode": "AND-test"},
        }
        options.update(overrides)
        return SignalRConnection(**options)

    return _build


class TestHandshake:
    """The handshake reply has to be read and checked, not assumed."""

    async def test_connects(self, connect):
        connection = connect()
        await connection.async_connect()
        assert connection.connected
        await connection.async_close()
        assert not connection.connected

    async def test_rejected_handshake_raises_immediately(self, connect, hub):
        hub.handshake_error = "unsupported protocol"
        connection = connect()
        with pytest.raises(FloLogicProtocolError, match="unsupported protocol"):
            await connection.async_connect()
        await connection.async_close()

    async def test_negotiate_auth_failure(self, connect, hub):
        hub.negotiate_status = 401
        with pytest.raises(FloLogicAuthError):
            await connect().async_connect()

    async def test_negotiate_server_error(self, connect, hub):
        hub.negotiate_status = 503
        with pytest.raises(FloLogicConnectionError):
            await connect().async_connect()


class TestRequests:
    """Request/response correlation, such as the protocol allows."""

    async def test_request_returns_the_answering_event(self, connect, hub):
        connection = connect()
        await connection.async_connect()
        try:
            result = await connection.async_request("Login", "LoggedIn", "a", "b")
            assert result == [hub.user]
        finally:
            await connection.async_close()

    async def test_timeout_when_the_hub_stays_silent(self, connect, hub):
        hub.silent_targets = {"Login"}
        connection = connect()
        await connection.async_connect()
        try:
            with pytest.raises(FloLogicTimeoutError, match="LoggedIn"):
                await connection.async_request("Login", "LoggedIn", "a", timeout=0.15)
        finally:
            await connection.async_close()

    async def test_error_event_fails_the_request_without_waiting(self, connect, hub):
        hub.reject_login = True
        connection = connect()
        await connection.async_connect()
        try:
            # Would otherwise burn the full timeout waiting for LoggedIn.
            with pytest.raises(FloLogicCommandError):
                await connection.async_request(
                    "Login", "LoggedIn", "a", timeout=30, error_event="ErrorOccured"
                )
        finally:
            await connection.async_close()

    async def test_match_predicate_rejects_the_wrong_shape(self, connect, hub):
        connection = connect()
        await connection.async_connect()
        try:
            with pytest.raises(FloLogicTimeoutError):
                await connection.async_request(
                    "Login",
                    "LoggedIn",
                    "a",
                    timeout=0.15,
                    match=lambda args: isinstance(args[0], list),
                )
            assert hub.invocations("Login")
        finally:
            await connection.async_close()

    async def test_send_without_a_connection_raises(self, connect):
        with pytest.raises(FloLogicConnectionError):
            await connect().async_send("Login")

    async def test_an_answering_frame_does_not_reach_the_listener(self, connect, hub):
        # Otherwise the caller both returns the data and handles it as a push,
        # acting on the same update twice.
        received: list[str] = []
        connection = connect(on_event=lambda t, _a: received.append(t))
        await connection.async_connect()
        try:
            await connection.async_request("Login", "LoggedIn", "a")
            await asyncio.sleep(0.05)
            assert received == []

            await hub.push("LoggedIn", hub.user)
            await _until(lambda: received == ["LoggedIn"])
        finally:
            await connection.async_close()


class TestFraming:
    """Frames and websocket messages do not map one to one."""

    async def test_several_frames_in_one_message(self, connect, hub):
        received: list[tuple[str, list[Any]]] = []
        connection = connect(on_event=lambda t, a: received.append((t, a)))
        await connection.async_connect()
        try:
            await hub.push_batch(
                [("ValveSent", [{"id": "v1"}]), ("ValveSent", [{"id": "v2"}])]
            )
            await _until(lambda: len(received) == 2)
            assert [args[0]["id"] for _, args in received] == ["v1", "v2"]
        finally:
            await connection.async_close()

    async def test_one_frame_split_across_messages(self, connect, hub):
        received: list[tuple[str, list[Any]]] = []
        connection = connect(on_event=lambda t, a: received.append((t, a)))
        await connection.async_connect()
        try:
            await hub.push_split("ValveSent", {"id": "v9"})
            await _until(lambda: len(received) == 1)
            assert received[0] == ("ValveSent", [{"id": "v9"}])
        finally:
            await connection.async_close()

    async def test_a_raising_listener_does_not_kill_the_socket(self, connect, hub):
        def explode(_target: str, _args: list[Any]) -> None:
            raise RuntimeError("listener bug")

        connection = connect(on_event=explode)
        await connection.async_connect()
        try:
            await hub.push("ValveSent", {"id": "v1"})
            await asyncio.sleep(0.05)
            assert connection.connected
            # The connection still answers requests afterwards.
            assert await connection.async_request("Login", "LoggedIn", "a")
        finally:
            await connection.async_close()


class TestKeepalive:
    """Without pings the real hub drops idle sockets after ~30 seconds."""

    async def test_sends_pings(self, connect, hub):
        connection = connect(ping_interval=0.05)
        await connection.async_connect()
        try:
            await _until(lambda: hub.pings_received >= 2, timeout=2.0)
        finally:
            await connection.async_close()

    async def test_drops_a_silent_connection(self, connect, hub):
        dropped = asyncio.Event()
        connection = connect(
            ping_interval=0.05,
            server_timeout=0.1,
            on_disconnect=dropped.set,
        )
        await connection.async_connect()
        try:
            # The fake hub never sends anything unprompted, so the client sees
            # total silence -- exactly the hung-peer case server_timeout exists
            # for, and one a plain TCP-level check would miss.
            assert hub.sockets
            await asyncio.wait_for(dropped.wait(), timeout=2.0)
            assert not connection.connected
        finally:
            await connection.async_close()


class TestDisconnect:
    """Losing the socket has to be reported, once."""

    async def test_reports_a_dropped_connection(self, connect, hub):
        calls = 0

        def on_disconnect() -> None:
            nonlocal calls
            calls += 1

        connection = connect(on_disconnect=on_disconnect)
        await connection.async_connect()
        try:
            await hub.drop_connections()
            await _until(lambda: calls >= 1)
            await asyncio.sleep(0.05)
            assert calls == 1
        finally:
            await connection.async_close()

    async def test_a_pending_request_fails_when_the_socket_drops(self, connect, hub):
        hub.silent_targets = {"Login"}
        connection = connect()
        await connection.async_connect()
        try:
            request = asyncio.create_task(
                connection.async_request("Login", "LoggedIn", "a", timeout=5)
            )
            await asyncio.sleep(0.05)
            await hub.drop_connections()
            with pytest.raises(FloLogicConnectionError):
                await request
        finally:
            await connection.async_close()

    async def test_deliberate_close_is_not_reported_as_a_drop(self, connect):
        calls = 0

        def on_disconnect() -> None:
            nonlocal calls
            calls += 1

        connection = connect(on_disconnect=on_disconnect)
        await connection.async_connect()
        await connection.async_close()
        await asyncio.sleep(0.05)
        assert calls == 0


async def _until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    """Wait until ``predicate`` holds, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition was never met")
