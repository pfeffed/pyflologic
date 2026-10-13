"""The client against the fake API: reads, confirmed writes, users, callbacks."""

from __future__ import annotations

import json

import aiohttp
import pytest

from pyflologic import (
    ApiKeyAuth,
    CallbackDefinition,
    ControlMode,
    FloLogicApiError,
    FloLogicClient,
    FloLogicCommandError,
    FloLogicConnectionError,
    FloLogicNotFoundError,
    FloLogicValidationError,
    UnknownValveError,
    ValveMode,
)

from .conftest import ClientFactory
from .fake_api import API_KEY, COMPANY_ID, FakeApi, make_status


class TestReading:
    async def test_refresh_reads_every_valve(self, client: FloLogicClient):
        valves = await client.async_refresh()
        assert set(valves) == {"414B91", "93DBB1", "412D18"}
        assert valves["414B91"].control_mode is ControlMode.HOME

    async def test_listing_walks_every_page(self, client: FloLogicClient, api: FakeApi):
        api.max_page_size = 2
        assert await client.async_list_vids() == ["414B91", "93DBB1", "412D18"]
        pages = [
            query.get("page")
            for _, path, query in api.requests
            if path == "/System/All"
        ]
        assert pages == ["1", "2"]

    async def test_status_is_read_ten_at_a_time(
        self, client: FloLogicClient, api: FakeApi
    ):
        """The documented limit on vid_list, which the fake enforces."""
        api.valves = {f"V{n:05d}": make_status(f"V{n:05d}") for n in range(25)}
        valves = await client.async_refresh()
        assert len(valves) == 25
        reads = [q["vid_list"] for _, p, q in api.requests if p == "/System/Status"]
        assert [len(read.split(",")) for read in reads] == [10, 10, 5]

    async def test_an_unknown_vid_is_absent_rather_than_an_error(
        self, client: FloLogicClient
    ):
        valves = await client.async_get_valves(["414B91", "ZZZ999"])
        assert set(valves) == {"414B91"}

    async def test_discovery_is_cached_between_refreshes(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_refresh()
        await client.async_refresh()
        assert api.paths("GET").count("/System/All") == 1

    async def test_rediscovery_finds_a_new_valve_and_drops_a_departed_one(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_refresh()
        del api.valves["412D18"]
        api.valves["5A10DEF456"] = make_status("5A10DEF456", Type="GConnect")
        valves = await client.async_refresh(rediscover=True)
        assert set(valves) == {"414B91", "93DBB1", "5A10DEF456"}
        assert set(client.valves) == set(valves)

    async def test_discovery_runs_again_once_due(
        self, make_client: ClientFactory, api: FakeApi
    ):
        client = make_client(discovery_interval=0)
        await client.async_refresh()
        await client.async_refresh()
        assert api.paths("GET").count("/System/All") == 2

    async def test_an_empty_account_reads_as_no_valves(
        self, client: FloLogicClient, api: FakeApi
    ):
        api.valves = {}
        assert await client.async_refresh() == {}
        assert "/System/Status" not in api.paths()

    async def test_get_valve_raises_for_one_never_read(self, client: FloLogicClient):
        with pytest.raises(UnknownValveError, match="414B91"):
            client.get_valve("414B91")

    async def test_valves_property_is_a_copy(self, client: FloLogicClient):
        await client.async_refresh()
        client.valves.pop("414B91")
        assert "414B91" in client.valves


class TestSetMode:
    async def test_sends_the_documented_value_and_confirms(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_set_mode("414B91", ControlMode.SHUTOFF)
        assert api.writes == [
            ("/System/ValveMode", {"vid_list": "414B91", "valve_mode": "Shutoff"})
        ]
        assert client.get_valve("414B91").is_water_off

    async def test_waits_for_a_slow_valve(self, client: FloLogicClient, api: FakeApi):
        api.apply_delay = 0.15
        await client.async_set_mode("414B91", "away", timeout=5)
        assert client.get_valve("414B91").control_mode is ControlMode.AWAY

    async def test_resends_a_command_that_was_accepted_and_dropped(
        self, client: FloLogicClient, api: FakeApi
    ):
        api.drop_writes = 1
        await client.async_set_mode("414B91", ControlMode.AWAY, timeout=0.6)
        assert len(api.writes) == 2
        assert client.get_valve("414B91").control_mode is ControlMode.AWAY

    async def test_resends_only_to_the_valves_still_pending(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_refresh()
        api.valves["414B91"]["Mode"] = 2  # already away
        api.drop_writes = 1
        await client.async_set_mode(["414B91", "93DBB1"], "away", timeout=0.6)
        assert api.writes[1][1]["vid_list"] == "93DBB1"

    async def test_reports_a_command_that_never_lands(
        self, client: FloLogicClient, api: FakeApi
    ):
        api.drop_writes = 99
        with pytest.raises(FloLogicCommandError, match="accepted mode away"):
            await client.async_set_mode("414B91", ControlMode.AWAY, timeout=0.3)
        assert len(api.writes) == 3

    async def test_delay_away_counts_as_away(
        self, client: FloLogicClient, api: FakeApi
    ):
        """A valve with Delay Away configured is obeying, not refusing."""
        api.drop_writes = 99
        api.valves["414B91"]["Mode"] = int(ValveMode.HOME | ValveMode.DELAY_AWAY)
        await client.async_set_mode("414B91", ControlMode.AWAY, timeout=0.3)

    async def test_home_is_not_confirmed_while_the_water_is_off(
        self, client: FloLogicClient, api: FakeApi
    ):
        api.drop_writes = 99
        api.valves["414B91"]["Mode"] = int(ValveMode.HOME | ValveMode.SENSOR_LEAK)
        with pytest.raises(FloLogicCommandError):
            await client.async_set_mode("414B91", ControlMode.HOME, timeout=0.2)

    async def test_disabled_is_refused_before_sending(
        self, client: FloLogicClient, api: FakeApi
    ):
        with pytest.raises(FloLogicValidationError, match="cannot set 'disabled'"):
            await client.async_set_mode("414B91", ControlMode.DISABLED)
        assert api.writes == []

    async def test_an_unknown_mode_name_is_refused(self, client: FloLogicClient):
        with pytest.raises(ValueError, match="sideways"):
            await client.async_set_mode("414B91", "sideways")

    async def test_without_confirmation_it_returns_on_acceptance(
        self, client: FloLogicClient, api: FakeApi
    ):
        api.drop_writes = 1
        await client.async_set_mode("414B91", "away", confirm=False)
        assert len(api.writes) == 1
        assert "/System/Status" not in api.paths()

    async def test_several_valves_go_in_one_request(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_set_mode(["414B91", "93DBB1", "414B91"], "bypass")
        assert api.writes[0][1]["vid_list"] == "414B91,93DBB1"

    async def test_no_vids_is_refused(self, client: FloLogicClient):
        with pytest.raises(FloLogicValidationError):
            await client.async_set_mode([], "home")

    async def test_an_unknown_vid_is_not_found(self, client: FloLogicClient):
        with pytest.raises(FloLogicNotFoundError, match="ZZZ999"):
            await client.async_set_mode("ZZZ999", "home")


class TestFlowSensitivity:
    async def test_sets_and_confirms(self, client: FloLogicClient, api: FakeApi):
        await client.async_set_flow_sensitivity("414B91", 2.5)
        assert api.writes[0][1]["drip_rate"] == "2.5"
        assert client.get_valve("414B91").flow_sensitivity_oz_per_min == 2.5

    async def test_whole_numbers_are_sent_without_a_fraction(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_set_flow_sensitivity("414B91", 4.0)
        assert api.writes[0][1]["drip_rate"] == "4"

    async def test_a_silently_discarded_value_is_reported(
        self, client: FloLogicClient, api: FakeApi
    ):
        """Below winter sensitivity the cloud says 200 and changes nothing."""
        api.winter_sensitivity = 3.0
        with pytest.raises(FloLogicCommandError, match="accepted flow sensitivity 2"):
            await client.async_set_flow_sensitivity("414B91", 2, timeout=0.2)

    @pytest.mark.parametrize("value", [0, -1])
    async def test_non_positive_values_are_refused(
        self, client: FloLogicClient, value: float
    ):
        with pytest.raises(FloLogicValidationError):
            await client.async_set_flow_sensitivity("414B91", value)


class TestErrors:
    async def test_a_server_error_carries_status_and_text(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_refresh()
        api.fail_next = [(500, "boom")]
        with pytest.raises(FloLogicApiError, match="HTTP 500: boom") as caught:
            await client.async_refresh()
        assert caught.value.status == 500

    async def test_a_validation_problem_is_summarized(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_refresh()
        api.fail_next = [
            (
                400,
                json.dumps(
                    {
                        "title": "One or more validation errors occurred.",
                        "errors": {"vid_list": ["The vid_list field is required."]},
                    }
                ),
            )
        ]
        with pytest.raises(FloLogicApiError, match="vid_list: The vid_list field"):
            await client.async_refresh()

    async def test_an_unreachable_api_is_a_connection_error(
        self, session: aiohttp.ClientSession
    ):
        client = FloLogicClient(
            ApiKeyAuth(API_KEY, COMPANY_ID),
            session=session,
            base_url="http://127.0.0.1:9",
        )
        with pytest.raises(FloLogicConnectionError):
            await client.async_refresh()


class TestSessionOwnership:
    async def test_a_supplied_session_is_left_open(
        self, client: FloLogicClient, session: aiohttp.ClientSession
    ):
        await client.async_refresh()
        await client.async_close()
        assert not session.closed

    async def test_an_owned_session_is_closed(self, api: FakeApi):
        async with FloLogicClient(
            ApiKeyAuth(API_KEY, COMPANY_ID), base_url=api.url
        ) as client:
            await client.async_refresh()
            owned = client._session
        assert owned is not None
        assert owned.closed

    async def test_a_closed_supplied_session_is_reported(self, api: FakeApi):
        session = aiohttp.ClientSession()
        await session.close()
        client = FloLogicClient(
            ApiKeyAuth(API_KEY, COMPANY_ID), session=session, base_url=api.url
        )
        with pytest.raises(FloLogicConnectionError, match="closed"):
            await client.async_refresh()


class TestUsers:
    async def test_lists_users(self, client: FloLogicClient):
        users = await client.async_list_users()
        assert [user.user_id for user in users] == [3616]
        assert users[0].email == "owner@example.com"
        assert users[0].vids == []  # the listing carries no devices

    async def test_reads_one_user_with_their_devices(self, client: FloLogicClient):
        user = await client.async_get_user(3616)
        assert user is not None
        assert user.user_id == 3616  # filled in: the endpoint omits it
        assert user.vids == ["414B91", "93DBB1", "412D18"]

    async def test_an_unknown_user_is_none(self, client: FloLogicClient):
        assert await client.async_get_user(1) is None


class TestCallbacks:
    async def test_reads_the_company_callbacks(self, client: FloLogicClient):
        assert await client.async_get_callbacks() == [
            CallbackDefinition("https://other.example/hook?vid={vid}", active=True)
        ]

    async def test_adding_keeps_everyone_elses(
        self, client: FloLogicClient, api: FakeApi
    ):
        mine = CallbackDefinition("https://mine.example/hook?vid={vid}")
        result = await client.async_add_callback(mine)
        assert [definition.url for definition in result] == [
            "https://other.example/hook?vid={vid}",
            "https://mine.example/hook?vid={vid}",
        ]
        assert len(api.callbacks) == 2

    async def test_adding_the_same_url_replaces_it(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_add_callback(CallbackDefinition("https://m/h", active=True))
        await client.async_add_callback(CallbackDefinition("https://m/h", active=False))
        assert api.callbacks[-1] == {"CallbackURL": "https://m/h", "Active": False}
        assert len(api.callbacks) == 2

    async def test_adding_an_unchanged_callback_writes_nothing(
        self, client: FloLogicClient, api: FakeApi
    ):
        existing = CallbackDefinition("https://other.example/hook?vid={vid}")
        await client.async_add_callback(existing)
        assert "POST" not in [verb for verb, _, _ in api.requests]

    async def test_removing_keeps_everyone_elses(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_add_callback(CallbackDefinition("https://mine.example/a"))
        kept = await client.async_remove_callbacks(
            lambda definition: definition.url.startswith("https://mine.example/")
        )
        assert [definition.url for definition in kept] == [
            "https://other.example/hook?vid={vid}"
        ]
        assert len(api.callbacks) == 1

    async def test_removing_nothing_writes_nothing(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_remove_callbacks("https://absent.example/")
        assert "POST" not in [verb for verb, _, _ in api.requests]

    async def test_delete_removes_all(self, client: FloLogicClient, api: FakeApi):
        await client.async_delete_callbacks()
        assert api.callbacks == []
        assert await client.async_get_callbacks() == []
