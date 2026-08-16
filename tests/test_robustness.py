"""Robustness tests: concurrency, connection loss, and resource hygiene.

These cover the situations a long-running consumer hits that a scripted
session never does -- two valves commanded at once, the socket dropping
mid-command, an integration reloading in a loop. None of it needs hardware.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from pyflologic import (
    Account,
    ControlMode,
    FloLogicClient,
    FloLogicCommandError,
    FloLogicError,
    ValveMode,
)
from pyflologic import client as client_module

from .fake_hub import FakeHub, make_valve


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait until ``predicate`` holds, or fail the test."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition was never met")


def _library_tasks() -> set[asyncio.Task[object]]:
    """Return the pyflologic background tasks currently alive."""
    interesting = ("_read_loop", "_ping_loop", "_async_reconnect")
    return {
        task
        for task in asyncio.all_tasks()
        if any(name in repr(task.get_coro()) for name in interesting)
    }


class TestConcurrency:
    """Several operations in flight at once must not cross-talk.

    The hub answers requests with free-standing events and no correlation ID,
    so the transport serializes them. That is exactly the design most likely
    to mismatch a reply with the wrong request under load.
    """

    async def test_commands_to_different_valves_run_concurrently(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await asyncio.gather(
            client.async_set_mode("valve-1", ControlMode.SHUTOFF),
            client.async_set_mode("valve-2", ControlMode.BYPASS),
        )
        assert hub.valve("valve-1")["mode"] == int(ValveMode.SHUTOFF)
        assert hub.valve("valve-2")["mode"] == int(ValveMode.BYPASS)

    async def test_many_concurrent_commands_all_land(
        self, client: FloLogicClient, hub: FakeHub
    ):
        hub.valves = [make_valve(f"v{i}", f"Valve {i}") for i in range(6)]
        await client.async_refresh()
        await asyncio.gather(
            *(client.async_set_mode(f"v{i}", ControlMode.AWAY) for i in range(6))
        )
        for i in range(6):
            assert hub.valve(f"v{i}")["mode"] == int(ValveMode.AWAY), i

    async def test_concurrent_reads_do_not_swap_answers(
        self, client: FloLogicClient, hub: FakeHub
    ):
        # Three different request/response pairs at once. A transport that
        # matched replies loosely would hand the scheduler rows to the
        # notification caller.
        valves, accesses, scheduler, notifications = await asyncio.gather(
            client.async_refresh(),
            client.async_refresh_accesses(),
            client.async_fetch_scheduler("valve-1"),
            client.async_fetch_notifications(["valve-1"]),
        )
        assert set(valves) == {"valve-1", "valve-2", "gw-1"}
        assert set(accesses) == {"valve-1", "valve-2"}
        assert all(event.raw.get("valveId") == "valve-1" for event in scheduler)
        assert all("Leak" in (row.message or "") for row in notifications)

    async def test_a_refresh_during_a_command(
        self, client: FloLogicClient, hub: FakeHub
    ):
        hub.state_push_delay = 0.3
        results = await asyncio.gather(
            client.async_set_mode("valve-1", ControlMode.SHUTOFF),
            client.async_refresh(),
        )
        assert results[1]
        assert client.get_valve("valve-1").control_mode is ControlMode.SHUTOFF


class TestConnectionLoss:
    """The socket can go away at any point, including mid-command."""

    async def test_a_command_survives_a_drop_when_reconnecting(
        self,
        make_client: Callable[..., FloLogicClient],
        hub: FakeHub,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(client_module, "_RECONNECT_BACKOFF", (0.05,))
        client = make_client(auto_reconnect=True)
        await client.async_connect()
        try:
            hub.suppress_state_push = True
            command = asyncio.create_task(
                client.async_set_mode("valve-1", ControlMode.AWAY, timeout=6)
            )
            await asyncio.sleep(0.1)
            await hub.drop_connections()
            # The refresh fallback reconnects and confirms from fresh state.
            await command
            assert client.get_valve("valve-1").control_mode is ControlMode.AWAY
        finally:
            await client.async_disconnect()

    async def test_a_command_fails_cleanly_when_it_cannot_reconnect(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        client = make_client(auto_reconnect=False)
        await client.async_connect()
        try:
            hub.silent_targets = {"RequestStateChange"}
            with pytest.raises(FloLogicError):
                await client.async_set_mode("valve-1", ControlMode.AWAY, timeout=1)
        finally:
            await client.async_disconnect()

    async def test_reads_recover_after_a_drop(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        client = make_client(auto_reconnect=False)
        await client.async_connect()
        try:
            for _ in range(3):
                await hub.drop_connections()
                await _until(lambda: not client.connected)
                assert await client.async_refresh()
            assert hub.connection_count == 4
        finally:
            await client.async_disconnect()

    async def test_pushes_resume_after_a_reconnect(
        self,
        make_client: Callable[..., FloLogicClient],
        hub: FakeHub,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(client_module, "_RECONNECT_BACKOFF", (0.05,))
        client = make_client(auto_reconnect=True)
        await client.async_connect()
        seen: list[Account] = []
        client.add_listener(seen.append)
        try:
            await hub.drop_connections()
            await _until(lambda: hub.connection_count >= 2, timeout=3)
            await _until(lambda: client.connected, timeout=3)
            seen.clear()
            await hub.push("ValveSent", make_valve("valve-1", "Main", mode=4))
            await _until(lambda: bool(seen))
        finally:
            await client.async_disconnect()


class TestOfflineValves:
    """A valve can be unreachable while the account is perfectly healthy."""

    async def test_an_offline_valve_is_still_listed(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await hub.push("ValveSent", {"id": "valve-1", "online": False})
        await _until(lambda: not client.get_valve("valve-1").is_online)
        offline = client.get_valve("valve-1")
        assert offline.is_water_flowing is False
        # Still a real valve with real settings; only its reachability changed.
        assert offline.is_controllable is True
        assert offline.home_limit_minutes == 30

    async def test_commanding_an_offline_valve_is_not_blocked(
        self, client: FloLogicClient, hub: FakeHub
    ):
        # The library does not second-guess reachability: the cloud queues
        # commands, and refusing locally would break a valve that is merely
        # slow to check in.
        hub.valve("valve-1")["online"] = False
        await client.async_refresh()
        await client.async_set_mode("valve-1", ControlMode.SHUTOFF)
        assert hub.valve("valve-1")["mode"] == int(ValveMode.SHUTOFF)


class TestResourceHygiene:
    """A long-lived consumer reloads; nothing may accumulate."""

    async def test_disconnect_leaves_no_background_tasks(
        self, make_client: Callable[..., FloLogicClient]
    ):
        before = _library_tasks()
        client = make_client()
        await client.async_connect()
        assert _library_tasks() - before
        await client.async_disconnect()
        await asyncio.sleep(0.05)
        assert _library_tasks() - before == set()

    async def test_repeated_reload_cycles_do_not_accumulate(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        before = _library_tasks()
        for _ in range(5):
            client = make_client()
            await client.async_connect()
            await client.async_refresh()
            await client.async_disconnect()
        await asyncio.sleep(0.05)
        assert _library_tasks() - before == set()
        assert hub.connection_count == 5

    async def test_a_failed_connect_leaves_no_tasks(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        before = _library_tasks()
        hub.reject_login = True
        for _ in range(3):
            with pytest.raises(FloLogicError):
                await make_client().async_connect()
        await asyncio.sleep(0.05)
        assert _library_tasks() - before == set()

    async def test_listeners_do_not_accumulate(self, client: FloLogicClient):
        unsubscribes = [client.add_listener(lambda _a: None) for _ in range(50)]
        for unsubscribe in unsubscribes:
            unsubscribe()
        assert client._listeners == []

    async def test_nothing_reconnects_after_disconnect(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        client = make_client()
        await client.async_connect()
        await client.async_disconnect()
        for operation in (
            client.async_refresh(),
            client.async_refresh_accesses(),
            client.async_fetch_notifications(),
        ):
            with pytest.raises(FloLogicError):
                await operation
        assert hub.connection_count == 1
        assert not client.connected

    async def test_reconnecting_deliberately_still_works(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        # The shutdown guard must not make a client single-use.
        client = make_client()
        await client.async_connect()
        await client.async_disconnect()
        await client.async_connect()
        try:
            assert await client.async_refresh()
            assert hub.connection_count == 2
        finally:
            await client.async_disconnect()

    async def test_disconnect_while_a_command_is_in_flight(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        client = make_client()
        await client.async_connect()
        hub.silent_targets = {"RequestStateChange"}
        command = asyncio.create_task(
            client.async_set_mode("valve-1", ControlMode.AWAY, timeout=5)
        )
        await asyncio.sleep(0.1)
        await client.async_disconnect()
        with pytest.raises((FloLogicError, FloLogicCommandError)):
            await command
        await asyncio.sleep(0.05)
        assert not client.connected
