"""Models and enums against the shapes the live API returns."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pyflologic import (
    CRITICAL_FLAGS,
    CallbackDefinition,
    CallbackEventType,
    CallbackReason,
    ControlMode,
    DeviceType,
    FlowState,
    Page,
    ShutoffReason,
    User,
    Valve,
    ValveMode,
)
from pyflologic.enums import STATUS_PRIORITY
from pyflologic.models import lookup, parse_datetime

# A live status object, verbatim apart from the address.
LIVE_STATUS = {
    "VID": "414B91",
    "Type": "WiFiConnect",
    "Online": True,
    "Address": "1 Example Lane",
    "Mode": 1,
    "FlowState": 1,
    "DripRate": 6,
    "Temperature": 70,
    "LastFlowDateTime": "2026-10-13T15:40:10",
    "HomeTime": 60,
    "AwayTime": 0.1,
    "BypassTimeEnabled": True,
    "BypassTime": 241,
    "LowTemperatureAlertEnabled": True,
    "LowTemperatureAlertThreshold": 38,
    "LowTemperatureShutoffEnabled": True,
    "LowTemperatureShutoffThreshold": 36,
    "PreAlertNoticeTime": 5,
}


def valve(**overrides: object) -> Valve:
    raw = {**LIVE_STATUS, **overrides}
    return Valve({key: value for key, value in raw.items() if value is not None})


class TestValve:
    def test_reads_a_live_payload(self):
        v = valve()
        assert v.vid == "414B91"
        assert v.device_type is DeviceType.WIFI_CONNECT
        assert v.is_online
        assert v.mode == ValveMode.HOME
        assert v.control_mode is ControlMode.HOME
        assert v.status == "home"
        assert v.flow_state is FlowState.NO_FLOW
        assert not v.is_water_flowing
        assert v.temperature_f == 70
        assert v.flow_sensitivity_oz_per_min == 6
        assert v.home_limit_minutes == 60
        assert v.away_limit_minutes == pytest.approx(0.1)
        assert v.bypass_enabled
        assert v.bypass_minutes == 241
        assert v.low_temp_alert_enabled
        assert v.low_temp_alert_f == 38
        assert v.low_temp_shutoff_enabled
        assert v.low_temp_shutoff_f == 36
        assert v.pre_alert_minutes == 5
        assert v.current_flow_limit_minutes == 60
        assert v.shutoff_reason is None
        assert v.problem is None

    def test_a_naive_timestamp_is_utc(self):
        """Read as local time, a California valve would be seven hours off."""
        assert valve().last_flow_change == datetime(
            2026, 10, 13, 15, 40, 10, tzinfo=UTC
        )

    def test_omitted_nullable_fields_read_as_none(self):
        """The API drops null properties instead of sending them."""
        v = valve(
            Temperature=None,
            LastFlowDateTime=None,
            BypassTimeEnabled=False,
            BypassTime=None,
            LowTemperatureAlertEnabled=False,
            LowTemperatureAlertThreshold=None,
        )
        assert v.temperature_f is None
        assert v.last_flow_change is None
        assert not v.bypass_enabled
        assert v.bypass_minutes is None
        assert not v.low_temp_alert_enabled
        assert v.low_temp_alert_f is None

    def test_a_zero_bypass_is_a_value_not_absence(self):
        assert valve(BypassTime=0).bypass_minutes == 0

    def test_away_zero_means_water_off(self):
        assert valve(AwayTime=0).away_turns_water_off
        assert not valve().away_turns_water_off

    def test_flowing_needs_the_valve_online(self):
        assert valve(FlowState=2).is_water_flowing
        assert valve(FlowState=4).is_water_flowing
        assert not valve(FlowState=4, Online=False).is_water_flowing

    def test_a_gconnect_is_a_valve(self):
        assert valve(Type="GConnect").device_type is DeviceType.G_CONNECT

    def test_an_unknown_type_is_kept_raw(self):
        v = valve(Type="FutureConnect")
        assert v.device_type is None
        assert v.device_type_raw == "FutureConnect"

    def test_lookup_ignores_case(self):
        assert valve(Mode=None, mode=8).mode == ValveMode.SHUTOFF
        assert lookup({"Items": [1]}, "items") == [1]

    def test_a_missing_mode_reads_as_unknown(self):
        v = valve(Mode=None)
        assert v.status == "unknown"
        assert v.control_mode is None


class TestShutoffReason:
    def test_a_plain_shutoff_is_manual(self):
        assert valve(Mode=8).shutoff_reason is ShutoffReason.MANUAL

    def test_an_automatic_trip_names_its_cause(self):
        v = valve(Mode=int(ValveMode.SHUTOFF | ValveMode.FLOW_TIME_EXCEEDED))
        assert v.shutoff_reason is ShutoffReason.FLOW_TIME_EXCEEDED
        assert v.is_automatically_shut_off
        assert v.status == "flow_time_exceeded"

    def test_a_shutoff_with_unknown_bits_is_not_called_manual(self):
        v = valve(Mode=int(ValveMode.SHUTOFF) | (1 << 30))
        assert v.shutoff_reason is ShutoffReason.UNRECOGNIZED

    def test_the_most_specific_cause_wins(self):
        mode = ValveMode.SHUTOFF | ValveMode.FLOW_TIME_EXCEEDED | ValveMode.SENSOR_LEAK
        assert valve(Mode=int(mode)).shutoff_reason is ShutoffReason.SENSOR_LEAK

    def test_a_problem_is_reported_separately(self):
        v = valve(Mode=int(ValveMode.HOME | ValveMode.CHANGE_BATTERY))
        assert v.shutoff_reason is None
        assert v.problem is ValveMode.CHANGE_BATTERY
        assert v.status == "home"


class TestEnums:
    def test_every_named_bit_has_a_status_rank(self):
        """A bit missing here silently reads as "unknown"."""
        named = {member for member in ValveMode if member.name}
        assert named == set(STATUS_PRIORITY)

    def test_critical_flags_are_named_bits(self):
        assert all(flag.name for flag in CRITICAL_FLAGS)

    def test_the_documented_mode_values(self):
        assert int(ValveMode.HOME) == 1
        assert int(ValveMode.AWAY) == 2
        assert int(ValveMode.BYPASS) == 4
        assert int(ValveMode.SHUTOFF) == 8
        assert int(ValveMode.DISABLED) == 16

    def test_unknown_bits_survive(self):
        mode = ValveMode(1 | (1 << 30))
        assert mode.unknown_bits == 1 << 30
        assert mode.flag_names == ["HOME"]

    def test_callback_mode_names_decode(self):
        mode, unknown = ValveMode.from_names("Home,DelayAway")
        assert mode == ValveMode.HOME | ValveMode.DELAY_AWAY
        assert unknown == []

    def test_callback_mode_names_ignore_case_and_spacing(self):
        mode, _ = ValveMode.from_names(" ShutOff , externalOverride ")
        assert mode == ValveMode.SHUTOFF | ValveMode.EXTERNAL_OVERRIDE

    def test_spelled_out_names_are_aliases(self):
        mode, unknown = ValveMode.from_names("Home,LowTemperatureAlert")
        assert mode == ValveMode.HOME | ValveMode.LOW_TEMP_ALERT
        assert unknown == []

    def test_unmapped_mode_names_are_kept(self):
        mode, unknown = ValveMode.from_names("Away,SomethingNew")
        assert mode == ValveMode.AWAY
        assert unknown == ["SomethingNew"]

    def test_numeric_mode_entries_are_values(self):
        assert ValveMode.from_names("8")[0] == ValveMode.SHUTOFF

    def test_api_values_and_settability(self):
        assert [mode.api_value for mode in ControlMode if mode.settable] == [
            "Home",
            "Away",
            "Bypass",
            "Shutoff",
        ]
        assert not ControlMode.DISABLED.settable

    def test_any_water_off_condition_is_shutoff(self):
        flag = ValveMode.HOME | ValveMode.LOW_TEMP_SHUTOFF
        assert ControlMode.from_flag(flag) is ControlMode.SHUTOFF

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, FlowState.VALVE_CLOSED),
            (1, FlowState.NO_FLOW),
            (4.0, FlowState.FLOW),
            (99, FlowState.PERCENT_FOLLOWING),
            (8, None),
            (True, None),
            ("1", None),
            (1.5, None),
            (None, None),
        ],
    )
    def test_flow_state_parsing(self, value: object, expected: FlowState | None):
        assert FlowState.parse(value) is expected

    def test_names_match_across_casing(self):
        """Status says WiFiConnect; callbacks say WifiConnect."""
        assert DeviceType.parse("WifiConnect") is DeviceType.WIFI_CONNECT
        assert CallbackEventType.parse("autoshutoff") is CallbackEventType.AUTO_SHUTOFF
        assert CallbackReason.parse("") is None
        assert CallbackReason.parse(None) is None

    def test_cleared_reasons_know_it(self):
        assert CallbackReason.LOW_BATTERY_CLEARED.is_cleared
        assert not CallbackReason.LOW_BATTERY.is_cleared


class TestPage:
    def test_reads_the_pascal_case_envelope_the_api_sends(self):
        page = Page.parse(
            {
                "Items": ["414B91"],
                "Page": 1,
                "PageSize": 1,
                "TotalCount": 3,
                "TotalPages": 3,
                "HasNextPage": True,
                "HasPreviousPage": False,
            },
            str,
        )
        assert page.items == ["414B91"]
        assert page.total_count == 3
        assert page.has_next_page

    def test_reads_the_camel_case_envelope_the_docs_show(self):
        page = Page.parse({"items": ["a", "b"], "page": 2, "totalPages": 2}, str)
        assert page.items == ["a", "b"]
        assert page.has_previous_page
        assert not page.has_next_page

    def test_tolerates_garbage(self):
        page = Page.parse("nonsense", str)
        assert page.items == []
        assert not page.has_next_page


class TestUserAndCallback:
    def test_a_listing_row(self):
        user = User({"Id": 3616, "Email": "a@b.c", "FirstName": "A", "LastName": "B"})
        assert user.user_id == 3616
        assert (user.first_name, user.last_name) == ("A", "B")
        assert user.vids == []

    def test_a_status_row(self):
        user = User({"Email": "a@b.c", "Systems": ["414B91", "", "93DBB1"]})
        assert user.user_id is None
        assert user.vids == ["414B91", "93DBB1"]

    def test_callback_round_trip(self):
        raw = {"CallbackURL": "https://x/h?vid={vid}", "Active": False}
        definition = CallbackDefinition.from_json(raw)
        assert definition == CallbackDefinition("https://x/h?vid={vid}", active=False)
        assert definition is not None
        assert definition.to_json() == raw

    def test_a_malformed_callback_is_skipped(self):
        assert CallbackDefinition.from_json({"Active": True}) is None
        assert CallbackDefinition.from_json("x") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-13T15:40:10", datetime(2026, 10, 13, 15, 40, 10, tzinfo=UTC)),
        ("2026-10-13T15:40:10Z", datetime(2026, 10, 13, 15, 40, 10, tzinfo=UTC)),
        ("2026-10-13T08:40:10-07:00", datetime(2026, 10, 13, 15, 40, 10, tzinfo=UTC)),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_datetime(text: object, expected: datetime | None):
    assert parse_datetime(text) == expected
