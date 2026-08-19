"""Tests for the bitfield decoding and derived valve state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pyflologic import (
    CRITICAL_FLAGS,
    WARNING_FLAGS,
    WATER_OFF_FLAGS,
    ControlMode,
    DeviceIdentity,
    FlowState,
    Notification,
    NotificationSetting,
    SchedulerEvent,
    ShutoffReason,
    ToggledSetting,
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

    def test_override_is_reported_as_a_status(self):
        # A real valve driven by an irrigation controller reported mode 2048
        # with no other bit set. The app calls this "Override"; before this was
        # in the priority list the library called it "unknown".
        subject = valve(mode=int(ValveMode.OVERRIDE))
        assert subject.status == "override"
        # Override is not something a user can select, so there is no control
        # mode -- and with no flow limit in force, no countdown either.
        assert subject.control_mode is None
        assert subject.current_flow_limit_minutes is None

    def test_a_leak_still_outranks_override(self):
        subject = valve(mode=ValveMode.OVERRIDE | ValveMode.SENSOR_LEAK)
        assert subject.status == "sensor_leak"

    def test_every_named_mode_bit_has_a_status(self):
        # A bit missing from STATUS_PRIORITY silently degrades to "unknown",
        # which is how the Override gap went unnoticed.
        for flag in ValveMode:
            if flag.name:
                assert valve(mode=int(flag)).status != "unknown", flag.name

    def test_an_autonomous_flow_time_shutoff(self):
        # Captured live: a valve in AWAY with a 30-second flow limit shut
        # itself off and reported mode 40 == SHUTOFF | FLOW_TIME_EXCEEDED.
        # This is the whole reason the device exists, so pin every derived
        # value it produces.
        subject = valve(mode=40, flowState=1, awayIntervalTime=0.5)
        assert subject.mode.flag_names == ["SHUTOFF", "FLOW_TIME_EXCEEDED"]
        assert subject.status == "flow_time_exceeded"
        assert subject.control_mode is ControlMode.SHUTOFF
        assert subject.active_water_off_flags == [
            ValveMode.FLOW_TIME_EXCEEDED,
            ValveMode.SHUTOFF,
        ]
        assert subject.active_warning_flags == []
        assert subject.active_critical_flags == []

    def test_recovery_from_a_flow_time_shutoff(self):
        # Commanding HOME clears both bits; there is no separate reset.
        assert valve(mode=1).status == "home"
        assert valve(mode=1).active_water_off_flags == []

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

    def test_flow_and_shutoff_are_not_mutually_exclusive(self):
        # Observed live: a valve watched closing reported flowState 4 for
        # several seconds while its mode was already SHUTOFF. Confirmed to be
        # downstream pipes draining, not a lagging mechanism. Nothing should
        # assume a shut valve cannot report flow.
        subject = valve(mode=int(ValveMode.SHUTOFF), flowState=4)
        assert subject.control_mode is ControlMode.SHUTOFF
        assert subject.is_water_flowing is True

    def test_a_closed_valve_reports_no_flow_not_valve_closed(self):
        # FlowState.VALVE_CLOSED has never been seen on real hardware; a valve
        # in SHUTOFF reports NO_FLOW like any other idle valve.
        subject = valve(mode=int(ValveMode.SHUTOFF), flowState=1)
        assert subject.flow_state is FlowState.NO_FLOW
        assert subject.is_water_flowing is False

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

    def test_the_name_is_per_account_but_the_unique_id_is_not(self):
        # One real valve, two logins, same instant: the owner sees "Riverside
        # Upper Valve" and a shared user sees "Riverside Whole House". Only the
        # three name fields differ; the other 108 are identical.
        as_owner = valve(uuid="hw-2245", valveFriendlyName="Riverside Upper Valve")
        as_shared = valve(uuid="hw-2245", valveFriendlyName="Riverside Whole House")
        assert as_owner.name != as_shared.name
        assert as_owner.unique_id == as_shared.unique_id

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

    @pytest.mark.parametrize("raw", [-18, -1, -0.5])
    def test_a_negative_setting_is_off_but_keeps_its_value(self, raw):
        # Confirmed against the app: Auto Away showing an OFF toggle beside
        # "18 hours" is stored as autoAwayTime = -18. The sign is the switch,
        # the magnitude is what the user configured.
        setting = valve(autoAwayTime=raw).auto_away
        assert setting.enabled is False
        assert setting.configured == abs(raw)
        assert setting.effective is None
        assert not setting

    def test_a_positive_setting_is_on(self):
        setting = valve(autoAwayTime=24).auto_away
        assert setting.enabled is True
        assert setting.configured == 24
        assert setting.effective == 24
        assert setting
        assert valve(autoAwayTime=24).auto_away_hours == 24

    def test_a_disabled_setting_reports_no_effective_hours(self):
        assert valve(autoAwayTime=-18).auto_away_hours is None

    @pytest.mark.parametrize("raw", [0, None, "nonsense"])
    def test_a_missing_setting_is_off_with_no_value(self, raw):
        setting = valve(autoAwayTime=raw).auto_away
        assert setting.enabled is False
        assert setting.configured is None

    def test_delay_away_and_winter_mode_use_the_same_encoding(self):
        subject = valve(delayAwayIntervalTime=-1, winterModeTime=-0.1)
        assert subject.delay_away == ToggledSetting(enabled=False, configured=1.0)
        # winterModeTime is misnamed on the wire: the app calls it "Winter Flow
        # Sensitivity" and shows it in ounces per minute, not as a duration.
        assert subject.winter_flow_sensitivity.configured == 0.1

    def test_a_low_temperature_limit_of_one_degree_is_a_real_setting(self):
        # A real valve had both temperature toggles ON at 1 F. Treating a small
        # positive value as a disabled sentinel would have been wrong.
        subject = valve(lowTemperatureAlert=1, lowTemperatureLimit=1)
        assert subject.low_temp_alert.enabled is True
        assert subject.low_temp_shutoff_f == 1

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
        assert subject.sensor_humidity_alert.effective == 75
        assert subject.sensor_humidity_shutoff.effective == 95
        assert subject.sensor_temp_alert.effective == 45
        assert subject.sensor_temp_shutoff.effective == 36

    def test_a_valve_without_sensors(self):
        assert valve().has_water_sensors is False
        assert valve().sensor_humidity_alert.configured is None

    def test_site_metadata_is_exposed(self):
        subject = valve(networkName="Riverside", valveAddress="12 Example Lane")
        assert subject.network_name == "Riverside"
        assert subject.address == "12 Example Lane"

    def test_last_seen_falls_back_to_modified(self):
        assert valve(modified="2026-01-01T12:00:00").last_seen == FLOW_START
        assert valve().last_seen is None


class TestNumericCoercion:
    """Telemetry arrives as ints, floats, strings, or nothing at all."""

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

    def test_a_real_shutoff_notification(self):
        # Verbatim from the live account. The history is the only place the
        # flow that tripped the limit is recorded at all -- flowState never
        # reported it.
        row = Notification(
            {
                "id": 52495530,
                "created": "2026-08-16T18:33:58.147",
                "title": "Mode Change",
                "text": (
                    "WATER SHUTOFF: Away flow limit of 30 seconds exceeded. "
                    "Water has been shut off for 34 Sample Road."
                ),
                "delivered": False,
                "accessId": 33232,
            }
        )
        assert row.notification_id == 52495530
        assert row.title == "Mode Change"
        assert row.is_delivered is False
        assert row.message is not None
        assert "Away flow limit of 30 seconds exceeded" in row.message
        assert row.created_at is not None
        assert row.created_at.year == 2026
        # Rows come back already scoped to the valves asked for, with no
        # valveId of their own.
        assert row.valve_id == ""


class TestDeviceIdentity:
    """Identities must be unique and reusable."""

    def test_generate_is_unique(self):
        first, second = DeviceIdentity.generate(), DeviceIdentity.generate()
        assert first.code != second.code
        assert first.token != second.token

    def test_generate_uses_the_android_prefix(self):
        assert DeviceIdentity.generate("app").code.startswith("AND-")
        assert DeviceIdentity.generate("app").name == "app"


class TestAutomaticShutoff:
    """Telling "you closed it" apart from "it closed itself"."""

    def test_a_manual_shutoff_is_not_automatic(self):
        subject = valve(mode=int(ValveMode.SHUTOFF))
        assert subject.active_water_off_flags == [ValveMode.SHUTOFF]
        assert subject.automatic_shutoff_flags == []
        assert subject.is_automatically_shut_off is False

    def test_a_flow_limit_shutoff_is_automatic(self):
        # The real capture: mode 40 is SHUTOFF | FLOW_TIME_EXCEEDED, so the
        # SHUTOFF bit alone cannot distinguish the two cases.
        subject = valve(mode=40)
        assert subject.automatic_shutoff_flags == [ValveMode.FLOW_TIME_EXCEEDED]
        assert subject.is_automatically_shut_off is True

    def test_a_leak_shutoff_is_automatic(self):
        subject = valve(mode=ValveMode.SHUTOFF | ValveMode.SENSOR_LEAK)
        assert subject.automatic_shutoff_flags == [ValveMode.SENSOR_LEAK]
        assert subject.is_automatically_shut_off is True

    def test_an_open_valve_is_not_shut_off_at_all(self):
        subject = valve(mode=int(ValveMode.HOME))
        assert subject.active_water_off_flags == []
        assert subject.is_automatically_shut_off is False


class TestShutoffReasonAndProblem:
    """State and reason kept apart, which is the point of having both."""

    def test_an_open_valve_has_no_reason(self):
        assert valve(mode=int(ValveMode.HOME)).shutoff_reason is None
        assert valve(mode=int(ValveMode.AWAY)).shutoff_reason is None

    def test_a_user_shutoff_reads_as_manual(self):
        assert valve(mode=int(ValveMode.SHUTOFF)).shutoff_reason is ShutoffReason.MANUAL

    def test_an_automatic_shutoff_names_what_it_reacted_to(self):
        # The real capture. SHUTOFF is set too, and must not win.
        assert valve(mode=40).shutoff_reason is ShutoffReason.FLOW_TIME_EXCEEDED

    def test_a_leak_outranks_a_flow_limit(self):
        subject = valve(
            mode=ValveMode.SHUTOFF
            | ValveMode.FLOW_TIME_EXCEEDED
            | ValveMode.SENSOR_LEAK
        )
        assert subject.shutoff_reason is ShutoffReason.SENSOR_LEAK

    @pytest.mark.parametrize(
        "flag",
        [
            ValveMode.SENSOR_LEAK,
            ValveMode.EXTERNAL_LEAK,
            ValveMode.FLOW_TIME_EXCEEDED,
            ValveMode.LOW_TEMP_SHUTOFF,
            ValveMode.HUMIDITY_SENSOR_SHUTOFF,
            ValveMode.LOW_TEMP_SENSOR_SHUTOFF,
            ValveMode.EXTERNAL_EMERGENCY_SHUTDOWN,
        ],
    )
    def test_every_automatic_reason_is_reportable(self, flag):
        """A cause with no mapping would silently report as manual.

        That is the one wrong answer that matters here: a leak reported as
        "you turned it off" is worse than reporting nothing at all.
        """
        reason = valve(mode=ValveMode.SHUTOFF | flag).shutoff_reason
        assert reason is not None
        assert reason is not ShutoffReason.MANUAL
        assert reason is ShutoffReason.from_flag(flag)

    def test_every_water_off_flag_maps_to_a_reason(self):
        for flag in WATER_OFF_FLAGS:
            if flag is ValveMode.SHUTOFF:
                continue
            assert ShutoffReason.from_flag(flag) is not None, flag

    def test_a_healthy_valve_has_no_problem(self):
        assert valve(mode=int(ValveMode.HOME)).problem is None

    def test_a_problem_is_reported_while_the_valve_stays_open(self):
        subject = valve(mode=ValveMode.HOME | ValveMode.CHANGE_BATTERY)
        assert subject.problem is ValveMode.CHANGE_BATTERY
        assert subject.shutoff_reason is None

    def test_a_valve_failure_outranks_a_low_battery(self):
        # A valve that cannot be trusted to close is worse news than a battery.
        subject = valve(
            mode=ValveMode.HOME | ValveMode.CHANGE_BATTERY | ValveMode.VALVE_FAILURE
        )
        assert subject.problem is ValveMode.VALVE_FAILURE

    def test_a_shutoff_and_a_problem_coexist(self):
        subject = valve(
            mode=ValveMode.SHUTOFF | ValveMode.SENSOR_LEAK | ValveMode.AC_LOST
        )
        assert subject.shutoff_reason is ShutoffReason.SENSOR_LEAK
        assert subject.problem is ValveMode.AC_LOST

    def test_every_warning_and_critical_flag_can_be_reported(self):
        for flag in (*WARNING_FLAGS, *CRITICAL_FLAGS):
            assert valve(mode=int(flag)).problem is flag, flag


class TestUnknownCauses:
    """What the library says when FloLogic says something it does not define."""

    def test_a_closed_valve_with_an_unmapped_bit_is_not_called_manual(self):
        # The dangerous failure: a cause carried on a bit this library does
        # not know would leave automatic_shutoff_flags empty, and the absence
        # of a cause is exactly what MANUAL infers from. Reporting a leak as
        # a deliberate shutoff is worse than admitting ignorance.
        subject = valve(mode=int(ValveMode.SHUTOFF) | (1 << 27))
        assert subject.mode.unknown_bits == 1 << 27
        assert subject.shutoff_reason is ShutoffReason.UNRECOGNIZED

    def test_no_reason_collides_with_a_reserved_home_assistant_state(self):
        """ "unknown" and "unavailable" mean "no data" to a consumer.

        A reason using either word would be displayed as a broken sensor
        rather than as the state it describes.
        """
        reserved = {"unknown", "unavailable", "none"}
        assert not [r for r in ShutoffReason if r.value in reserved]

    def test_a_clean_manual_shutoff_is_still_manual(self):
        subject = valve(mode=int(ValveMode.SHUTOFF))
        assert subject.mode.unknown_bits == 0
        assert subject.shutoff_reason is ShutoffReason.MANUAL

    def test_a_known_cause_wins_over_an_unmapped_bit(self):
        # We know why this one closed, so ignorance elsewhere does not matter.
        subject = valve(mode=ValveMode.SHUTOFF | ValveMode.SENSOR_LEAK | (1 << 27))
        assert subject.shutoff_reason is ShutoffReason.SENSOR_LEAK

    def test_recognised_bits_alongside_a_shutoff_do_not_trigger_unknown(self):
        # AC_LOST is mapped and is not a shutoff cause; it is not ignorance.
        subject = valve(mode=ValveMode.SHUTOFF | ValveMode.AC_LOST)
        assert subject.shutoff_reason is ShutoffReason.MANUAL
        assert subject.problem is ValveMode.AC_LOST


class TestShutoffTarget:
    """The absolute instant a flow limit will fire."""

    def flowing(self, **fields: object) -> Valve:
        """A valve flowing since FLOW_START under a 30 minute Home limit."""
        payload: dict[str, object] = {
            "flowState": 4,
            "mode": int(ValveMode.HOME),
            "homeIntervalTime": 30,
            "lastNewFlow": "2026-01-01T12:00:00Z",
        }
        payload.update(fields)
        return valve(**payload)

    def test_the_target_is_the_start_plus_the_limit(self):
        assert self.flowing().shutoff_at == FLOW_START + timedelta(minutes=30)

    def test_the_target_does_not_move_as_time_passes(self):
        # The point of a timestamp over a countdown: nothing has to be
        # rewritten while the flow continues.
        subject = self.flowing()
        first = subject.shutoff_at
        assert first == subject.shutoff_at
        # And it agrees with the countdown taken at any instant.
        now = FLOW_START + timedelta(minutes=5)
        assert subject.shutoff_countdown_seconds(now) == int(
            (first - now).total_seconds()
        )

    def test_it_uses_the_active_modes_limit(self):
        subject = self.flowing(mode=int(ValveMode.AWAY), awayIntervalTime=0.5)
        assert subject.shutoff_at == FLOW_START + timedelta(seconds=30)

    def test_no_target_without_flow_or_a_limit(self):
        assert self.flowing(flowState=1).shutoff_at is None
        assert self.flowing(homeIntervalTime=0).shutoff_at is None
        assert self.flowing(mode=int(ValveMode.BYPASS)).shutoff_at is None

    def test_no_target_in_override(self):
        # An irrigation controller suspends the limit entirely.
        assert self.flowing(mode=int(ValveMode.OVERRIDE)).shutoff_at is None


class TestGuestMode:
    """A field named for a duration that holds a flow limit."""

    def test_the_value_is_a_flow_limit_not_a_span(self):
        # Two real valves switched on together, carrying 1 and 60, expired at
        # the same instant. A duration could not do that.
        subject = valve(guestModeTime=60, guestModeDuration="2026-08-19T06:59:00")
        assert subject.guest_flow_limit.enabled is True
        assert subject.guest_flow_limit.configured == 60
        assert subject.guest_mode_expires_at is not None

    def test_the_sign_still_carries_the_switch(self):
        subject = valve(guestModeTime=-99)
        assert subject.guest_flow_limit.enabled is False
        # Off, and the configured limit survives, as with every signed setting.
        assert subject.guest_flow_limit.configured == 99

    def test_the_expiry_is_independent_of_the_limit(self):
        """Different limits, same expiry: the two are unrelated."""
        one = valve(guestModeTime=1, guestModeDuration="2026-08-19T06:59:00")
        sixty = valve(guestModeTime=60, guestModeDuration="2026-08-19T06:59:00")
        assert one.guest_flow_limit.configured != sixty.guest_flow_limit.configured
        assert one.guest_mode_expires_at == sixty.guest_mode_expires_at

    def test_a_valve_that_never_used_guest_mode(self):
        assert valve().guest_mode_expires_at is None
        assert valve().guest_flow_limit.configured is None


class TestValveClosedNeverOccurs:
    """flowState 8 is not produced by this hardware."""

    def test_a_closed_valve_reports_no_flow(self):
        # Closing against actively running water produced NO_FLOW in 1.8
        # seconds, with no intermediate state across ninety seconds of samples.
        subject = valve(mode=int(ValveMode.SHUTOFF), flowState=1)
        assert subject.flow_state is FlowState.NO_FLOW
        assert subject.is_water_flowing is False
        # Indistinguishable from an idle open valve on flowState alone.
        idle = valve(mode=int(ValveMode.HOME), flowState=1)
        assert idle.flow_state is subject.flow_state
        # Only the mode tells them apart.
        assert subject.shutoff_reason is not None
        assert idle.shutoff_reason is None
