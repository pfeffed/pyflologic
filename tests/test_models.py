"""Tests for the bitfield decoding and derived valve state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pyflologic import (
    ControlMode,
    DeviceIdentity,
    FlowState,
    Notification,
    NotificationSetting,
    SchedulerEvent,
    Valve,
    ValveAccess,
    ValveMode,
)

FLOW_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def valve(**fields: object) -> Valve:
    """Build a Valve from a minimal payload."""
    payload: dict[str, object] = {"id": "v1", "online": True}
    payload.update(fields)
    return Valve(payload)


class TestValveMode:
    """The mode bitfield packs the control mode and every active condition."""

    def test_decodes_multiple_flags(self):
        mode = ValveMode(ValveMode.AWAY | ValveMode.SENSOR_LEAK)
        assert mode.flag_names == ["AWAY", "SENSOR_LEAK"]

    def test_keeps_unrecognized_bits(self):
        # Bit 27 is unassigned; future firmware must not blow up the client.
        mode = ValveMode(ValveMode.HOME | (1 << 27))
        assert mode & ValveMode.HOME
        assert mode.unknown_bits == 1 << 27
        assert mode.flag_names == ["HOME"]

    def test_no_unknown_bits_for_known_value(self):
        assert ValveMode(ValveMode.HOME | ValveMode.AC_LOST).unknown_bits == 0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (ValveMode.HOME, ControlMode.HOME),
            (ValveMode.AWAY, ControlMode.AWAY),
            (ValveMode.BYPASS, ControlMode.BYPASS),
            (ValveMode.SHUTOFF, ControlMode.SHUTOFF),
            (ValveMode.DISABLED, ControlMode.DISABLED),
        ],
    )
    def test_plain_control_modes(self, raw, expected):
        assert ControlMode.from_flag(ValveMode(raw)) is expected

    def test_leak_while_home_reads_as_shutoff(self):
        # The valve keeps its HOME bit set after a leak closes it; the water is
        # off, so reporting "home" would be actively misleading.
        mode = ValveMode(ValveMode.HOME | ValveMode.SENSOR_LEAK)
        assert ControlMode.from_flag(mode) is ControlMode.SHUTOFF

    def test_unknown_mode_has_no_control_mode(self):
        assert ControlMode.from_flag(ValveMode(0)) is None
        assert ControlMode.from_flag(ValveMode(ValveMode.AC_LOST)) is None

    def test_control_mode_round_trips_to_its_flag(self):
        for mode in ControlMode:
            assert ControlMode.from_flag(mode.flag) is mode


class TestValveStatus:
    """Status picks the single most newsworthy bit."""

    def test_leak_outranks_the_control_mode(self):
        assert valve(mode=ValveMode.HOME | ValveMode.SENSOR_LEAK).status == (
            "sensor_leak"
        )

    def test_plain_mode_reports_itself(self):
        assert valve(mode=int(ValveMode.AWAY)).status == "away"

    def test_missing_mode_is_unknown(self):
        assert valve().status == "unknown"

    def test_grouped_flags(self):
        subject = valve(
            mode=ValveMode.AWAY | ValveMode.SENSOR_LEAK | ValveMode.CHANGE_BATTERY
        )
        assert subject.active_water_off_flags == [ValveMode.SENSOR_LEAK]
        assert subject.active_warning_flags == [ValveMode.CHANGE_BATTERY]
        assert subject.active_critical_flags == []


class TestFlowState:
    """Flow detection needs both an online valve and a flowing state."""

    @pytest.mark.parametrize(
        ("state", "flowing"),
        [(1, False), (2, True), (4, True), (8, False)],
    )
    def test_flow_states(self, state, flowing):
        assert valve(flowState=state).is_water_flowing is flowing

    def test_offline_valve_is_never_flowing(self):
        assert valve(flowState=4, online=False).is_water_flowing is False

    def test_unknown_flow_state_parses_to_none(self):
        assert FlowState.parse(99) is None
        assert FlowState.parse(None) is None
        assert FlowState.parse("4") is None
        assert valve(flowState=99).is_water_flowing is False

    def test_booleans_are_not_flow_states(self):
        # bool is an int subclass; True must not silently become NO_FLOW.
        assert FlowState.parse(True) is None


class TestDerivedTiming:
    """Countdown and elapsed time are derived locally from lastNewFlow."""

    def flowing_valve(self, **fields: object) -> Valve:
        """Build a valve that has been flowing since FLOW_START."""
        payload: dict[str, object] = {
            "flowState": 4,
            "mode": int(ValveMode.HOME),
            "homeIntervalTime": 30,
            "lastNewFlow": "2026-01-01T12:00:00Z",
        }
        payload.update(fields)
        return valve(**payload)

    def test_elapsed_seconds(self):
        now = FLOW_START + timedelta(minutes=5)
        assert self.flowing_valve().flow_elapsed_seconds(now) == 300

    def test_countdown_uses_the_active_modes_limit(self):
        now = FLOW_START + timedelta(minutes=5)
        # 30 minute Home limit, 5 minutes in.
        assert self.flowing_valve().shutoff_countdown_seconds(now) == 25 * 60

    def test_countdown_uses_away_limit_in_away_mode(self):
        now = FLOW_START + timedelta(minutes=1)
        subject = self.flowing_valve(mode=int(ValveMode.AWAY), awayIntervalTime=5)
        assert subject.shutoff_countdown_seconds(now) == 4 * 60

    def test_countdown_floors_at_zero(self):
        now = FLOW_START + timedelta(hours=2)
        assert self.flowing_valve().shutoff_countdown_seconds(now) == 0

    def test_no_countdown_when_not_flowing(self):
        subject = self.flowing_valve(flowState=1)
        assert subject.shutoff_countdown_seconds(FLOW_START) is None
        assert subject.flow_elapsed_seconds(FLOW_START) is None
        assert subject.flow_started_at is None

    def test_no_countdown_without_a_limit(self):
        subject = self.flowing_valve(mode=int(ValveMode.HOME), homeIntervalTime=0)
        assert subject.shutoff_countdown_seconds(FLOW_START) is None

    def test_no_countdown_without_a_timestamp(self):
        subject = self.flowing_valve(lastNewFlow=None)
        assert subject.shutoff_countdown_seconds(FLOW_START) is None

    def test_pre_alert_window(self):
        subject = self.flowing_valve(preAlertNoticeInterval=5)
        # 27 minutes in, 3 left, inside the 5 minute pre-alert window.
        assert subject.is_in_pre_alert_window(FLOW_START + timedelta(minutes=27))
        assert not subject.is_in_pre_alert_window(FLOW_START + timedelta(minutes=10))

    def test_pre_alert_window_needs_a_configured_interval(self):
        subject = self.flowing_valve(preAlertNoticeInterval=0)
        assert not subject.is_in_pre_alert_window(FLOW_START + timedelta(minutes=29))


class TestTimestampParsing:
    """FloLogic is inconsistent about timezone suffixes."""

    @pytest.mark.parametrize(
        "raw",
        ["2026-01-01T12:00:00Z", "2026-01-01T12:00:00", "2026-01-01T12:00:00+00:00"],
    )
    def test_naive_and_suffixed_forms_agree(self, raw):
        subject = valve(flowState=4, lastNewFlow=raw)
        assert subject.flow_started_at == FLOW_START

    def test_offset_is_normalized_to_utc(self):
        subject = valve(flowState=4, lastNewFlow="2026-01-01T07:00:00-05:00")
        assert subject.flow_started_at == FLOW_START

    @pytest.mark.parametrize("raw", ["", "not-a-date", None, 12345])
    def test_unparseable_values_are_none(self, raw):
        assert valve(flowState=4, lastNewFlow=raw).flow_started_at is None


class TestValveIdentity:
    """Naming and identity fall back sensibly."""

    def test_prefers_the_friendly_name(self):
        subject = valve(valveFriendlyName="Basement", combinedName="x", name="y")
        assert subject.name == "Basement"

    def test_falls_back_through_the_name_fields(self):
        assert valve(combinedName="Combined").name == "Combined"
        assert valve(name="Plain").name == "Plain"

    def test_falls_back_to_uuid_then_id(self):
        assert valve(uuid="abc").name == "abc"
        assert valve().name == "FloLogic v1"

    def test_unique_id_prefers_hardware_uuid(self):
        assert valve(uuid="hw-9").unique_id == "hw-9"
        assert valve().unique_id == "v1"

    def test_gateways_are_not_controllable(self):
        assert valve(isZGateway=True).is_controllable is False
        assert valve(isZGateway=True).is_gateway is True
        assert valve().is_controllable is True


class TestNumericCoercion:
    """Telemetry arrives as ints, floats, strings, or nothing at all."""

    def test_numeric_strings_are_accepted(self):
        assert valve(temperature="68.5").temperature_f == 68.5

    def test_garbage_becomes_none(self):
        assert valve(temperature="warm").temperature_f is None
        assert valve().temperature_f is None

    def test_booleans_are_not_numbers(self):
        assert valve(batteryLevel=True).battery_percent is None


class TestSupportingModels:
    """The smaller record types."""

    def test_access_decodes_notification_bits(self):
        access = ValveAccess({"valveId": "v1", "notificationsList": 0b1000100})
        assert access.wants(NotificationSetting.ADVANCE_SHUTOFF)
        assert access.wants(NotificationSetting.MODE_CHANGE)
        assert not access.wants(NotificationSetting.NO_FLOW)

    def test_access_without_notifications(self):
        assert ValveAccess({}).notifications == NotificationSetting(0)

    def test_scheduler_placeholder_rows_are_inactive(self):
        assert SchedulerEvent({"action": 1, "actionPayload": "{}"}).is_active
        assert not SchedulerEvent({"action": None, "actionPayload": None}).is_active
        assert not SchedulerEvent({"action": 1, "actionPayload": None}).is_active

    def test_notification_reads_alternate_field_names(self):
        assert Notification({"text": "hi"}).message == "hi"
        assert Notification({"createdDate": "2026-01-01T00:00:00Z"}).created_at
        assert Notification({}).message is None
        assert Notification({}).created_at is None


class TestDeviceIdentity:
    """Identities must be unique and reusable."""

    def test_generate_is_unique(self):
        first, second = DeviceIdentity.generate(), DeviceIdentity.generate()
        assert first.code != second.code
        assert first.token != second.token

    def test_generate_uses_the_android_prefix(self):
        assert DeviceIdentity.generate("app").code.startswith("AND-")
        assert DeviceIdentity.generate("app").name == "app"
