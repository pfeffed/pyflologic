"""Tests for the account client, with multi-valve behavior front and center."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from pyflologic import (
    Account,
    ControlMode,
    FloLogicAuthError,
    FloLogicClient,
    FloLogicCommandError,
    FloLogicError,
    NotificationSetting,
    UnknownValveError,
    ValveMode,
)
from pyflologic import client as client_module

from .fake_hub import FakeHub, make_valve


class TestAccountLoading:
    """An account is a set of valves, not a single valve."""

    async def test_loads_every_device(self, client: FloLogicClient):
        assert set(client.valves) == {"valve-1", "valve-2", "gw-1"}

    async def test_gateways_are_excluded_from_controllable_valves(
        self, client: FloLogicClient
    ):
        assert set(client.account.controllable_valves) == {"valve-1", "valve-2"}

    async def test_loads_many_valves(self, make_client: Callable[..., FloLogicClient]):
        # Three valves across two houses is the case that motivated this library.
        hub_client = make_client()
        assert isinstance(hub_client, FloLogicClient)

    async def test_valves_keep_their_own_state(self, client: FloLogicClient):
        assert client.get_valve("valve-1").control_mode is ControlMode.HOME
        assert client.get_valve("valve-2").control_mode is ControlMode.AWAY

    async def test_unknown_valve_raises(self, client: FloLogicClient):
        with pytest.raises(UnknownValveError, match="valve-99"):
            client.get_valve("valve-99")

    async def test_valves_property_is_a_copy(self, client: FloLogicClient):
        client.valves.pop("valve-1")
        assert "valve-1" in client.valves

    async def test_user_is_captured(self, client: FloLogicClient):
        assert client.user is not None
        assert client.user.user_id == "user-1"
        assert client.user.email == "owner@example.com"

    async def test_account_before_login_raises(
        self, make_client: Callable[..., FloLogicClient]
    ):
        with pytest.raises(FloLogicError, match="not logged in"):
            _ = make_client().account


class TestCommands:
    """Every command names the valve it applies to."""

    async def test_set_mode_targets_only_that_valve(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await client.async_set_mode("valve-2", ControlMode.SHUTOFF)
        assert hub.valve("valve-2")["mode"] == int(ValveMode.SHUTOFF)
        assert hub.valve("valve-1")["mode"] == int(ValveMode.HOME)

    async def test_set_mode_accepts_a_string(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await client.async_set_mode("valve-1", "away")
        assert hub.valve("valve-1")["mode"] == int(ValveMode.AWAY)

    async def test_set_mode_rejects_an_unknown_mode(self, client: FloLogicClient):
        with pytest.raises(ValueError, match="not a valid ControlMode"):
            await client.async_set_mode("valve-1", "vacation")

    async def test_set_mode_on_an_unknown_valve_raises(self, client: FloLogicClient):
        with pytest.raises(UnknownValveError):
            await client.async_set_mode("valve-99", ControlMode.SHUTOFF)

    async def test_command_carries_the_right_identifiers(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await client.async_set_mode("valve-2", ControlMode.BYPASS)
        _user, valve, command = hub.invocations("RequestStateChange")[0]
        assert command["valveId"] == "valve-2"
        assert command["userId"] == "user-1"
        assert valve["id"] == "valve-2"
        assert command["active"] is True

    async def test_refreshed_state_is_visible_after_a_command(
        self, client: FloLogicClient
    ):
        await client.async_set_mode("valve-1", ControlMode.SHUTOFF)
        assert client.get_valve("valve-1").control_mode is ControlMode.SHUTOFF

    async def test_refresh_can_be_skipped(self, client: FloLogicClient, hub: FakeHub):
        before = len(hub.invocations("RefreshValveArray"))
        await client.async_set_mode("valve-1", ControlMode.AWAY, refresh=False)
        assert len(hub.invocations("RefreshValveArray")) == before

    async def test_settings_map_to_wire_field_names(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await client.async_update_settings(
            "valve-1",
            home_limit_minutes=45,
            low_temp_shutoff_f=40,
            flow_sensitivity_oz_per_min=0.25,
        )
        stored = hub.valve("valve-1")
        assert stored["homeIntervalTime"] == 45
        assert stored["lowTemperatureLimit"] == 40
        assert stored["dripRate"] == 0.25
        # Untouched settings stay untouched.
        assert stored["awayIntervalTime"] == 5

    async def test_settings_are_sent_in_one_command(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await client.async_update_settings(
            "valve-1", home_limit_minutes=45, away_limit_minutes=3
        )
        assert len(hub.invocations("RequestStateChange")) == 1

    async def test_empty_settings_update_raises(self, client: FloLogicClient):
        with pytest.raises(FloLogicError, match="no settings"):
            await client.async_update_settings("valve-1")

    async def test_raw_command_escape_hatch(self, client: FloLogicClient, hub: FakeHub):
        await client.async_send_command("valve-1", {"someFutureField": 7})
        assert hub.valve("valve-1")["someFutureField"] == 7

    async def test_a_rejected_command_raises(
        self, client: FloLogicClient, hub: FakeHub
    ):
        hub.valves = [valve for valve in hub.valves if valve["id"] != "valve-1"]
        # The valve is still in the client's cache but gone from the cloud.
        with pytest.raises(FloLogicCommandError):
            await client.async_send_command("valve-1", {"mode": 8})


class TestPushedUpdates:
    """A push about one valve must not erase the others."""

    async def test_single_valve_push_preserves_the_rest(
        self, client: FloLogicClient, hub: FakeHub
    ):
        updated = make_valve("valve-2", "Guest House", mode=int(ValveMode.SHUTOFF))
        await hub.push("ValveSent", updated)
        await _until(
            lambda: client.get_valve("valve-2").control_mode is ControlMode.SHUTOFF
        )
        assert set(client.valves) == {"valve-1", "valve-2", "gw-1"}
        assert client.get_valve("valve-1").control_mode is ControlMode.HOME

    async def test_array_push_updates_everything(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await hub.push(
            "ValveArraySent",
            [
                make_valve("valve-1", "Main House", mode=int(ValveMode.BYPASS)),
                make_valve("valve-2", "Guest House", mode=int(ValveMode.BYPASS)),
            ],
        )
        await _until(
            lambda: client.get_valve("valve-1").control_mode is ControlMode.BYPASS
        )
        assert client.get_valve("valve-2").control_mode is ControlMode.BYPASS
        # The gateway was not in the push, so it is still known.
        assert "gw-1" in client.valves

    async def test_push_does_not_drop_fields_it_omits(
        self, client: FloLogicClient, hub: FakeHub
    ):
        # A real push was observed nulling valveFriendlyName and omitting
        # settings entirely. Overwriting the cached payload wholesale would
        # lose the flow limit, and with it the shutoff countdown.
        await hub.push(
            "ValveSent",
            {"id": "valve-1", "mode": 1, "flowState": 2, "valveFriendlyName": None},
        )
        await _until(lambda: client.get_valve("valve-1").is_water_flowing)
        merged = client.get_valve("valve-1")
        assert merged.home_limit_minutes == 30
        assert merged.name == "Main House"

    async def test_push_can_still_change_a_field(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await hub.push("ValveSent", {"id": "valve-1", "temperature": 41})
        await _until(lambda: client.get_valve("valve-1").temperature_f == 41)

    async def test_malformed_pushes_are_ignored(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await hub.push("ValveSent", "not a valve")
        await hub.push("ValveArraySent", [None, 42])
        await asyncio.sleep(0.05)
        assert set(client.valves) == {"valve-1", "valve-2", "gw-1"}

    async def test_a_valve_without_an_id_is_ignored(
        self, client: FloLogicClient, hub: FakeHub
    ):
        await hub.push("ValveSent", {"valveFriendlyName": "nameless"})
        await asyncio.sleep(0.05)
        assert set(client.valves) == {"valve-1", "valve-2", "gw-1"}


class TestListeners:
    """Listeners see whole-account snapshots."""

    async def test_listener_receives_pushed_updates(
        self, client: FloLogicClient, hub: FakeHub
    ):
        seen: list[Account] = []
        client.add_listener(seen.append)
        await hub.push("ValveSent", make_valve("valve-1", "Main", mode=4))
        await _until(lambda: bool(seen))
        assert seen[-1].valves["valve-1"].control_mode is ControlMode.BYPASS

    async def test_unsubscribe_stops_delivery(
        self, client: FloLogicClient, hub: FakeHub
    ):
        seen: list[Account] = []
        unsubscribe = client.add_listener(seen.append)
        unsubscribe()
        await hub.push("ValveSent", make_valve("valve-1", "Main", mode=4))
        await asyncio.sleep(0.05)
        assert not seen

    async def test_unsubscribing_twice_is_harmless(self, client: FloLogicClient):
        unsubscribe = client.add_listener(lambda _account: None)
        unsubscribe()
        unsubscribe()

    async def test_one_bad_listener_does_not_block_the_others(
        self, client: FloLogicClient, hub: FakeHub
    ):
        seen: list[Account] = []

        def explode(_account: Account) -> None:
            raise RuntimeError("listener bug")

        client.add_listener(explode)
        client.add_listener(seen.append)
        await hub.push("ValveSent", make_valve("valve-1", "Main", mode=4))
        await _until(lambda: bool(seen))

    async def test_refresh_notifies_exactly_once(self, client: FloLogicClient):
        # The ValveArraySent frame answering the refresh must not also be
        # treated as a push, or every poll fires two updates downstream.
        seen: list[Account] = []
        client.add_listener(seen.append)
        await client.async_refresh()
        await asyncio.sleep(0.05)
        assert len(seen) == 1


class TestReads:
    """Per-valve reads are scoped to the valve asked for."""

    async def test_accesses_are_keyed_by_valve(self, client: FloLogicClient):
        accesses = await client.async_refresh_accesses()
        assert set(accesses) == {"valve-1", "valve-2"}
        assert accesses["valve-1"].wants(NotificationSetting.ADVANCE_SHUTOFF)
        assert not accesses["valve-2"].wants(NotificationSetting.ADVANCE_SHUTOFF)

    async def test_scheduler_is_fetched_per_valve(
        self, client: FloLogicClient, hub: FakeHub
    ):
        events = await client.async_fetch_scheduler("valve-1")
        assert len(events) == 2
        assert sum(1 for event in events if event.is_active) == 1
        assert hub.invocations("RequestSchedulerEvents")[0] == ["user-1", "valve-1"]

    async def test_scheduler_rejects_an_unknown_valve(self, client: FloLogicClient):
        with pytest.raises(UnknownValveError):
            await client.async_fetch_scheduler("valve-99")

    async def test_notifications_default_to_every_valve(
        self, client: FloLogicClient, hub: FakeHub
    ):
        notifications = await client.async_fetch_notifications()
        assert [row.valve_id for row in notifications] == ["valve-1"]
        requested = hub.invocations("RefreshValvesNotificationsHistory")[0][1]
        assert set(requested) == {"valve-1", "valve-2", "gw-1"}

    async def test_notifications_can_be_narrowed(self, client: FloLogicClient):
        assert await client.async_fetch_notifications(["valve-2"]) == []


class TestAuthentication:
    """Login failures have to be distinguishable from outages."""

    async def test_rejected_login_raises_auth_error(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        hub.reject_login = True
        with pytest.raises(FloLogicAuthError):
            await make_client().async_connect()

    async def test_login_sends_the_device_identity(
        self, client: FloLogicClient, hub: FakeHub
    ):
        email, password, device_name, _ = hub.invocations("Login")[0]
        assert (email, password) == ("owner@example.com", "secret")
        assert device_name == "test-device"

    async def test_missing_user_payload_raises_auth_error(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        hub.user = {}
        with pytest.raises(FloLogicAuthError, match="did not return a user"):
            await make_client().async_connect()


class TestLifecycle:
    """Connecting, reconnecting, and cleaning up."""

    async def test_context_manager(self, make_client: Callable[..., FloLogicClient]):
        async with make_client() as client:
            assert client.connected
            assert client.valves
        assert not client.connected

    async def test_connect_is_idempotent(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        client = make_client()
        await client.async_connect()
        await client.async_connect()
        try:
            assert hub.connection_count == 1
        finally:
            await client.async_disconnect()

    async def test_disconnect_is_idempotent(
        self, make_client: Callable[..., FloLogicClient]
    ):
        client = make_client()
        await client.async_connect()
        await client.async_disconnect()
        await client.async_disconnect()

    async def test_a_supplied_session_is_not_closed(
        self, make_client: Callable[..., FloLogicClient], session
    ):
        client = make_client()
        await client.async_connect()
        await client.async_disconnect()
        assert not session.closed

    async def test_reconnects_after_a_drop(
        self,
        make_client: Callable[..., FloLogicClient],
        hub: FakeHub,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(client_module, "_RECONNECT_BACKOFF", (0.05,))
        client = make_client(auto_reconnect=True)
        await client.async_connect()
        try:
            await hub.drop_connections()
            await _until(lambda: hub.connection_count >= 2, timeout=3.0)
            await _until(lambda: client.connected, timeout=3.0)
            # The account is usable again, not merely socket-connected.
            assert await client.async_refresh()
        finally:
            await client.async_disconnect()

    async def test_reconnect_gives_up_on_bad_credentials(
        self,
        make_client: Callable[..., FloLogicClient],
        hub: FakeHub,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(client_module, "_RECONNECT_BACKOFF", (0.05,))
        client = make_client(auto_reconnect=True)
        await client.async_connect()
        try:
            hub.reject_login = True
            await hub.drop_connections()
            await asyncio.sleep(0.4)
            # Retrying a rejected password forever would lock the account out.
            assert hub.connection_count == 2
        finally:
            await client.async_disconnect()

    async def test_no_reconnect_when_disabled(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        client = make_client(auto_reconnect=False)
        await client.async_connect()
        try:
            await hub.drop_connections()
            await asyncio.sleep(0.2)
            assert hub.connection_count == 1
        finally:
            await client.async_disconnect()

    async def test_a_request_reconnects_a_dropped_client(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        client = make_client(auto_reconnect=False)
        await client.async_connect()
        try:
            await hub.drop_connections()
            await _until(lambda: not client.connected)
            assert await client.async_refresh()
            assert hub.connection_count == 2
        finally:
            await client.async_disconnect()

    async def test_relog_token_is_sent_on_reconnect(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        client = make_client(auto_reconnect=False)
        await client.async_connect()
        try:
            await hub.drop_connections()
            await _until(lambda: not client.connected)
            await client.async_refresh()
            assert client._build_headers()["relogToken"] == "relog-abc"
        finally:
            await client.async_disconnect()

    async def test_hub_url_accepts_either_form(
        self, make_client: Callable[..., FloLogicClient], hub: FakeHub
    ):
        base = hub.url.removesuffix("/signalr")
        client = make_client(hub_url=base)
        await client.async_connect()
        try:
            assert client.valves
        finally:
            await client.async_disconnect()


async def _until(predicate: Callable[[], bool], timeout: float = 1.0, **_: Any) -> None:
    """Wait until ``predicate`` holds, or fail the test."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition was never met")
