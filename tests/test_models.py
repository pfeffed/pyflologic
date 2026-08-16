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


class TestRealHardwareQuirks:
    """Behaviors confirmed against real WiFi Connect valves.

    Each of these was wrong before a live account was inspected, so they are
    pinned rather than left to the next refactor's judgement.
    """

    def test_battery_level_outside_percent_range_is_not_a_percentage(self):
        # A real valve reported batteryLevel 8192 while another on the same
        # account reported 50. Publishing "8192%" would be worse than nothing.
        subject = valve(batteryLevel=8192)
        assert subject.battery_percent is None
        assert subject.battery_level_raw == 8192

    def test_plausible_battery_level_is_kept(self):
        assert valve(batteryLevel=50).battery_percent == 50

    @pytest.mark.parametrize("raw", [-18, -1, 0, -0.5])
    def test_negative_settings_read_as_disabled(self, raw):
        # FloLogic disables a setting with a sentinel, not by omitting it.
        assert valve(autoAwayTime=raw).auto_away_hours is None
        assert valve(delayAwayIntervalTime=raw).delay_away_minutes is None

    def test_positive_settings_are_kept(self):
        assert valve(autoAwayTime=24).auto_away_hours == 24

    def test_flow_timestamp_falls_back_to_last_flow_change(self):
        # WiFi Connect valves never send lastNewFlow. Reading only that key
        # silently disabled every derived timing value on this hardware.
        subject = valve(flowState=4, lastFlowChange="2026-01-01T12:00:00")
        assert subject.flow_started_at == FLOW_START

    def test_last_new_flow_wins_when_both_are_present(self):
        subject = valve(
            flowState=4,
            lastNewFlow="2026-01-01T12:00:00Z",
            lastFlowChange="2020-01-01T00:00:00Z",
        )
        assert subject.flow_started_at == FLOW_START

    def test_countdown_works_from_the_fallback_timestamp(self):
        subject = valve(
            flowState=4,
            mode=int(ValveMode.HOME),
            homeIntervalTime=59,
            lastFlowChange="2026-01-01T12:00:00",
        )
        countdown = subject.shutoff_countdown_seconds(FLOW_START + timedelta(minutes=9))
        assert countdown == 50 * 60

    @pytest.mark.parametrize(
        ("flag", "kind"),
        [
            ("isZGateway", "gateway"),
            ("isSensor", "sensor"),
            ("isZRepeater", "repeater"),
            ("isZInput", "input"),
        ],
    )
    def test_non_valve_devices_are_not_controllable(self, flag, kind):
        subject = valve(**{flag: True})
        assert subject.is_controllable is False
        assert subject.device_kind == kind

    def test_a_wifi_connect_valve_is_controllable(self):
        # The real payload: isZConnect is False on WiFi hardware, which is
        # exactly what the old single-valve selection logic keyed on.
        subject = valve(isZConnect=False, isAnyConnect=True, isWifiConnectDevice=True)
        assert subject.is_controllable is True
        assert subject.device_kind == "valve"

    def test_battery_level_can_be_absurd(self):
        # Two of three real valves reported powers of two here: 8192 and
        # 134217728. Neither is a percentage.
        assert valve(batteryLevel=134217728).battery_percent is None
        assert valve(batteryLevel=134217728).battery_level_raw == 134217728

    def test_last_new_flow_does_not_move_when_flow_stops(self):
        # Confirmed live: lastFlowChange updates on both start and stop,
        # lastNewFlow only on start. Preferring lastNewFlow is what keeps the
        # countdown anchored to the beginning of the event.
        subject = valve(
            flowState=4,
            mode=int(ValveMode.HOME),
            homeIntervalTime=99,
            lastNewFlow="2026-01-01T12:00:00",
            lastFlowChange="2026-01-01T12:04:00",
        )
        assert subject.flow_started_at == FLOW_START

    def test_water_sensor_limits(self):
        subject = valve(
            waterSensorHumidityAlertLimit=75,
            waterSensorHumidityShutoffLimit=95,
            waterSensorTemperatureAlertLimit=45,
            waterSensorTemperatureShutoffLimit=36,
        )
        assert subject.has_water_sensors
        assert subject.sensor_humidity_alert_percent == 75
        assert subject.sensor_humidity_shutoff_percent == 95
        assert subject.sensor_temp_alert_f == 45
        assert subject.sensor_temp_shutoff_f == 36

    def test_a_valve_without_sensors(self):
        assert valve().has_water_sensors is False
        assert valve().sensor_humidity_alert_percent is None

    def test_site_metadata_is_exposed(self):
        subject = valve(networkName="Riverside", valveAddress="12 Example Lane")
        assert subject.network_name == "Riverside"
        assert subject.address == "12 Example Lane"

    def test_last_seen_falls_back_to_modified(self):
        assert valve(modified="2026-01-01T12:00:00").last_seen == FLOW_START
        assert valve().last_seen is None


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

    def test_access_notifications_use_more_bits_than_are_named(self):
        # A real "everything on" record: bits 0-30 set, only NEVER cleared.
        access = ValveAccess({"notificationsList": 2147483645})
        assert access.wants(NotificationSetting.ALWAYS)
        assert not access.wants(NotificationSetting.NEVER)
        assert access.notifications.unknown_bits != 0
        # The named bits still round-trip exactly.
        assert int(access.notifications) == 2147483645

    def test_the_named_notification_bits_are_confirmed(self):
        # Turning off two notifications on one real valve moved the value by
        # exactly MODE_CHANGE | GENERAL_ALERT, which pins those assignments.
        everything = ValveAccess({"notificationsList": 2147483645}).notifications
        reduced = ValveAccess({"notificationsList": 2147483129}).notifications
        difference = everything & ~reduced
        assert difference == (
            NotificationSetting.MODE_CHANGE | NotificationSetting.GENERAL_ALERT
        )

    def test_access_exposes_privilege_and_identity(self):
        access = ValveAccess(
            {
                "valveId": "v1",
                "userId": "3616",
                "devicePrivilege": 2,
                "valveFriendlyName": "Main House",
            }
        )
        assert access.user_id == "3616"
        assert access.privilege == 2
        assert access.valve_name == "Main House"

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
