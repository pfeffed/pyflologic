"""Typed views over the JSON objects the FloLogic cloud sends.

Every model keeps the payload it was built from in ``raw``. That is not
laziness: :meth:`~pyflologic.client.FloLogicClient.async_set_mode` and friends
have to echo the *entire* valve object back to the hub, so discarding unknown
fields would break writes against firmware newer than this library.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .const import DEVICE_CODE_PREFIX
from .enums import (
    CRITICAL_FLAGS,
    FLOWING_STATES,
    PROBLEM_PRIORITY,
    SHUTOFF_REASON_PRIORITY,
    STATUS_PRIORITY,
    WARNING_FLAGS,
    WATER_OFF_FLAGS,
    ControlMode,
    FlowState,
    NotificationSetting,
    ShutoffReason,
    ValveMode,
)

__all__ = [
    "Account",
    "DeviceIdentity",
    "JsonDict",
    "Notification",
    "SchedulerEvent",
    "ToggledSetting",
    "User",
    "Valve",
    "ValveAccess",
]

JsonDict = dict[str, Any]

_PERCENT_RANGE = (0.0, 100.0)


def _as_float(value: Any) -> float | None:
    """Coerce a JSON value to ``float``, or ``None`` if it is not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """Coerce a JSON value to ``int``, or ``None`` if it is not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    """Parse a FloLogic timestamp as an aware UTC datetime.

    FloLogic is inconsistent about trailing ``Z`` and about sending an offset
    at all; naive values are treated as UTC, which matches what the app does.
    """
    if not isinstance(value, str) or not value:
        return None
    text = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _first_datetime(raw: JsonDict, keys: tuple[str, ...]) -> datetime | None:
    """Return the first key in ``keys`` that parses as a timestamp."""
    for key in keys:
        parsed = _as_datetime(raw.get(key))
        if parsed is not None:
            return parsed
    return None


def _now(now: datetime | None) -> datetime:
    """Return ``now``, defaulting to the current UTC time.

    Taking the clock as a parameter keeps the derived time properties pure and
    directly testable.
    """
    return now if now is not None else datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ToggledSetting:
    """A FloLogic setting that packs its on/off state into the sign of its value.

    FloLogic does not clear a disabled setting, it negates it: a valve with
    Auto Away switched off still reports ``autoAwayTime: -18``, and the app
    renders that as an off toggle beside "18 hours". Reading only the number
    gives "-18 hours"; discarding it entirely loses what the user configured.
    Both halves are kept here, and :attr:`effective` is the one to act on.
    """

    enabled: bool
    configured: float | None

    @property
    def effective(self) -> float | None:
        """Return the value if the setting is on, otherwise ``None``."""
        return self.configured if self.enabled else None

    def __bool__(self) -> bool:
        """Return whether the setting is switched on."""
        return self.enabled

    @classmethod
    def parse(cls, value: Any) -> ToggledSetting:
        """Decode one signed FloLogic setting."""
        parsed = _as_float(value)
        if parsed is None or parsed == 0:
            return cls(enabled=False, configured=None)
        return cls(enabled=parsed > 0, configured=abs(parsed))


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """The client-device identity FloLogic expects from a mobile app.

    FloLogic ties a session to a device code/token pair. Generate one per
    Home Assistant install (or per integration) and persist it: reusing a
    stable identity avoids piling up phantom devices on the account.
    """

    name: str
    code: str
    token: str

    @classmethod
    def generate(cls, name: str = "pyflologic") -> DeviceIdentity:
        """Create a fresh random identity to persist and reuse."""
        return cls(
            name=name,
            code=f"{DEVICE_CODE_PREFIX}{uuid4()}",
            token=secrets.token_urlsafe(32),
        )


@dataclass(frozen=True, slots=True)
class User:
    """The account holder returned by ``LoggedIn``."""

    raw: JsonDict

    @property
    def user_id(self) -> str:
        """Return the FloLogic user ID."""
        return str(self.raw.get("id", ""))

    @property
    def email(self) -> str | None:
        """Return the account email address."""
        value = self.raw.get("email")
        return str(value) if value else None

    @property
    def relog_token(self) -> str | None:
        """Return the token that lets the next connect skip a full login."""
        value = self.raw.get("relogToken")
        return str(value) if value else None


@dataclass(frozen=True, slots=True)
class Valve:
    """A single FloLogic valve.

    Instances are immutable snapshots. A refresh or a pushed update produces a
    new ``Valve``; it never mutates one you already hold.
    """

    raw: JsonDict

    # There is deliberately no ``current_flow`` here. The cloud's
    # ``currentFlow`` field is not a measurement: it reports the valve's own
    # ``dripRate`` while flow is sustained and zero otherwise, confirmed by
    # changing the sensitivity on a running valve and watching the "reading"
    # follow it. Exposing it under any name implying a rate would present a
    # setting as a measurement; ``raw["currentFlow"]`` remains for anyone who
    # wants the field itself.

    # --- identity -------------------------------------------------------

    @property
    def valve_id(self) -> str:
        """Return the FloLogic valve ID used in commands."""
        return str(self.raw.get("id", ""))

    @property
    def uuid(self) -> str | None:
        """Return the valve's hardware UUID, if the cloud reported one."""
        value = self.raw.get("uuid")
        return str(value) if value else None

    @property
    def unique_id(self) -> str:
        """Return the most stable identifier available for this valve.

        Prefers the hardware UUID so that a valve which is removed and re-added
        to the account keeps its identity downstream.
        """
        return self.uuid or self.valve_id

    @property
    def name(self) -> str:
        """Return the friendliest name FloLogic has for this valve.

        Scoped to the logged-in account, not to the hardware: the same valve
        was observed simultaneously named "Riverside Upper Valve" to its owner
        and "Riverside Whole House" to a shared user. Every other field of that
        valve's payload was identical between the two. Anything that must
        survive a change of account should key on :attr:`unique_id`.
        """
        for key in ("valveFriendlyName", "combinedName", "name"):
            value = self.raw.get(key)
            if value:
                return str(value)
        return self.uuid or f"FloLogic {self.valve_id}"

    @property
    def model(self) -> str | None:
        """Return the device type name, e.g. ``FloLogic Connect``."""
        value = self.raw.get("deviceTypeName")
        return str(value) if value else None

    @property
    def firmware_version(self) -> str | None:
        """Return the reported firmware/software version."""
        for key in ("softwareVersion", "valveAndCpFirmwareVersionString"):
            value = self.raw.get(key)
            if value:
                return str(value)
        return None

    @property
    def is_gateway(self) -> bool:
        """Return whether this entry is a G-Connect gateway rather than a valve."""
        return self.raw.get("isZGateway") is True

    @property
    def is_controllable(self) -> bool:
        """Return whether this device accepts mode changes.

        The device array mixes in everything on the account: gateways, leak
        sensors, repeaters and Z-inputs all arrive alongside real valves and
        none of them have anything to open or close. Checking each flag rather
        than only ``isZGateway`` keeps a sensor from being offered as a valve.
        """
        return not any(
            self.raw.get(flag) is True
            for flag in ("isZGateway", "isSensor", "isZRepeater", "isZInput")
        )

    @property
    def device_kind(self) -> str:
        """Return a coarse device kind, for callers that want to group devices."""
        for flag, kind in (
            ("isZGateway", "gateway"),
            ("isSensor", "sensor"),
            ("isZRepeater", "repeater"),
            ("isZInput", "input"),
        ):
            if self.raw.get(flag) is True:
                return kind
        return "valve"

    @property
    def network_name(self) -> str | None:
        """Return the FloLogic network (site) name this device belongs to.

        Useful for splitting one account's valves across houses.
        """
        value = self.raw.get("networkName")
        return str(value) if value else None

    @property
    def address(self) -> str | None:
        """Return the street address FloLogic has on file for this valve."""
        value = self.raw.get("valveAddress")
        return str(value) if value else None

    # --- live state -----------------------------------------------------

    @property
    def is_online(self) -> bool:
        """Return whether the cloud currently considers the valve reachable."""
        return bool(self.raw.get("online"))

    @property
    def mode(self) -> ValveMode:
        """Return the raw mode bitfield, decoded."""
        return ValveMode(_as_int(self.raw.get("mode")) or 0)

    @property
    def control_mode(self) -> ControlMode | None:
        """Return the settable mode the valve is in, if it can be determined."""
        return ControlMode.from_flag(self.mode)

    @property
    def status(self) -> str:
        """Return one headline status name, most newsworthy bit wins."""
        mode = self.mode
        if not mode:
            return "unknown"
        for flag in STATUS_PRIORITY:
            if mode & flag and flag.name:
                return flag.name.lower()
        return "unknown"

    @property
    def flow_state(self) -> FlowState | None:
        """Return the reported flow state, or ``None`` if unrecognized."""
        return FlowState.parse(self.raw.get("flowState"))

    @property
    def is_water_flowing(self) -> bool:
        """Return whether the valve currently reports water moving through it.

        This is the sensor's word, and it is *not* mutually exclusive with the
        valve being shut: a valve watched closing reported flow for several
        seconds afterwards. That was confirmed by direct observation to be the
        pipes draining downstream, not the mechanism lagging -- an observer at
        a lower-floor tap saw pressure fall away and then trickle out while the
        valve had already closed promptly.

        So a brief "flowing while shut" after a shutoff is drainage and is
        expected. One that persists is not, and is worth surfacing.
        """
        state = self.flow_state
        return self.is_online and state is not None and state in FLOWING_STATES

    @property
    def temperature_f(self) -> float | None:
        """Return the water temperature in degrees Fahrenheit."""
        return _as_float(self.raw.get("temperature"))

    @property
    def battery_level_raw(self) -> float | None:
        """Return ``batteryLevel`` exactly as the cloud reported it.

        Its units are not a settled question -- see :attr:`battery_percent`.
        """
        return _as_float(self.raw.get("batteryLevel"))

    @property
    def battery_percent(self) -> float | None:
        """Return the backup battery level as a percentage, if it plausibly is one.

        Real WiFi Connect valves have been observed reporting ``batteryLevel:
        8192`` alongside another valve on the same account reporting ``50``, so
        the field is clearly not a percentage on every model. Rather than
        publish "8192%", anything outside 0-100 reads as ``None`` and callers
        that want the number regardless can use :attr:`battery_level_raw`.
        """
        value = self.battery_level_raw
        if value is None or not _PERCENT_RANGE[0] <= value <= _PERCENT_RANGE[1]:
            return None
        return value

    @property
    def signal_strength_dbm(self) -> float | None:
        """Return the wireless signal strength in dBm."""
        return _as_float(self.raw.get("signalStrength"))

    @property
    def last_seen(self) -> datetime | None:
        """Return when the cloud last heard from this valve."""
        return _first_datetime(self.raw, ("lastSeen", "modified"))

    @property
    def active_water_off_flags(self) -> list[ValveMode]:
        """Return every set bit that means the valve has closed."""
        return [flag for flag in WATER_OFF_FLAGS if self.mode & flag]

    @property
    def automatic_shutoff_flags(self) -> list[ValveMode]:
        """Return the water-off conditions the valve raised by itself.

        The plain ``SHUTOFF`` bit is excluded because it means only "the valve
        is closed", and it is set for a user's own command as well as
        alongside every automatic trip -- a real flow-limit shutoff reports
        ``SHUTOFF | FLOW_TIME_EXCEEDED``. What remains after removing it is
        exactly the set of reasons the valve decided for itself, which is the
        difference between "you turned the water off" and "something went
        wrong". Only the latter deserves an alarm.
        """
        return [
            flag
            for flag in self.active_water_off_flags
            if flag is not ValveMode.SHUTOFF
        ]

    @property
    def is_automatically_shut_off(self) -> bool:
        """Return whether the valve closed itself rather than being told to."""
        return bool(self.automatic_shutoff_flags)

    @property
    def shutoff_reason(self) -> ShutoffReason | None:
        """Return why the valve is closed, or ``None`` if it is open.

        ``MANUAL`` means a person or an integration closed it; every other
        value names what the valve reacted to on its own. The distinction is
        not visible in a single bit -- an automatic trip sets ``SHUTOFF``
        alongside its cause -- so it is the *absence* of any cause that makes
        a shutoff manual.

        Which is why a closed valve carrying bits this library does not
        recognise reports ``UNRECOGNIZED`` rather than ``MANUAL``. The inference
        behind ``MANUAL`` is "no cause is present", and an unmapped bit means
        we cannot say that. Guessing there would describe a leak as a
        deliberate shutoff.
        """
        automatic = self.automatic_shutoff_flags
        if automatic:
            for flag in SHUTOFF_REASON_PRIORITY:
                if flag in automatic:
                    return ShutoffReason.from_flag(flag)
            return ShutoffReason.from_flag(automatic[0])
        if self.mode & ValveMode.SHUTOFF:
            if self.mode.unknown_bits:
                return ShutoffReason.UNRECOGNIZED
            return ShutoffReason.MANUAL
        return None

    @property
    def problem(self) -> ValveMode | None:
        """Return the most serious condition that is not closing the valve.

        Deliberately separate from :attr:`shutoff_reason`: a low battery and a
        leak are not the same kind of news, and flattening both into one
        "problem" flag loses the only part a person would act on.
        """
        for flag in PROBLEM_PRIORITY:
            if self.mode & flag:
                return flag
        return None

    @property
    def active_warning_flags(self) -> list[ValveMode]:
        """Return every set bit that means something needs attention."""
        return [flag for flag in WARNING_FLAGS if self.mode & flag]

    @property
    def active_critical_flags(self) -> list[ValveMode]:
        """Return every set bit that means the valve is faulted."""
        return [flag for flag in CRITICAL_FLAGS if self.mode & flag]

    # --- settings -------------------------------------------------------

    @property
    def flow_sensitivity_oz_per_min(self) -> float | None:
        """Return the drip-detection threshold in ounces per minute."""
        return _as_float(self.raw.get("dripRate"))

    @property
    def home_limit_minutes(self) -> float | None:
        """Return the continuous-flow limit while in Home mode."""
        return _as_float(self.raw.get("homeIntervalTime"))

    @property
    def away_limit_minutes(self) -> float | None:
        """Return the continuous-flow limit while in Away mode."""
        return _as_float(self.raw.get("awayIntervalTime"))

    @property
    def bypass_minutes(self) -> float | None:
        """Return how long Bypass mode lasts before reverting."""
        return _as_float(self.raw.get("bypassTime"))

    @property
    def auto_away(self) -> ToggledSetting:
        """Return Auto Away: idle hours before the valve switches itself to Away."""
        return ToggledSetting.parse(self.raw.get("autoAwayTime"))

    @property
    def auto_away_hours(self) -> float | None:
        """Return the Auto Away delay in hours, or ``None`` when it is off."""
        return self.auto_away.effective

    @property
    def delay_away(self) -> ToggledSetting:
        """Return Delay Away: minutes to wait before Away mode takes effect."""
        return ToggledSetting.parse(self.raw.get("delayAwayIntervalTime"))

    @property
    def winter_flow_sensitivity(self) -> ToggledSetting:
        """Return Winter Mode's alternate flow sensitivity, in ounces per minute.

        Stored in ``winterModeTime``, which is misnamed on the wire: the app
        labels it "Winter Flow Sensitivity" and shows it in ounces, not time.
        """
        return ToggledSetting.parse(self.raw.get("winterModeTime"))

    @property
    def guest_flow_limit(self) -> ToggledSetting:
        """Return Guest Mode's flow limit, in minutes, and whether it is on.

        ``guestModeTime`` is named as though it were a duration and is not.
        Two valves switched on together, carrying 1 and 60, expired at the
        same instant -- so the number is not a span. It is the flow limit that
        applies while guest mode is active, in minutes like every other flow
        limit on the device, which the app confirms by labelling it exactly
        that. How long guest mode lasts is :attr:`guest_mode_expires_at`, and
        is not something this field sets.
        """
        return ToggledSetting.parse(self.raw.get("guestModeTime"))

    @property
    def guest_mode_expires_at(self) -> datetime | None:
        """Return when guest mode ends, or ``None`` if it is not running.

        Observed to be the end of the local day it was switched on, regardless
        of the flow limit configured alongside it. The cloud decides it; it is
        not settable.

        ``None`` once guest mode is switched off, even though the cloud keeps
        the last expiry in the field. A stale value here is worse than no
        value: consumers render a timestamp as a relative time, so yesterday's
        expiry would announce that guest mode ends in an hour when it is not
        running at all.
        """
        if not self.guest_flow_limit.enabled:
            return None
        return _as_datetime(self.raw.get("guestModeDuration"))

    @property
    def low_temp_alert(self) -> ToggledSetting:
        """Return the low-temperature alert threshold, in degrees Fahrenheit."""
        return ToggledSetting.parse(self.raw.get("lowTemperatureAlert"))

    @property
    def low_temp_alert_f(self) -> float | None:
        """Return the low-temperature alert threshold, or ``None`` when off."""
        return self.low_temp_alert.effective

    @property
    def low_temp_shutoff(self) -> ToggledSetting:
        """Return the low-temperature shutoff threshold, in degrees Fahrenheit.

        A real valve was found with this on at 1 F, which is a legitimate if
        useless setting rather than a disabled sentinel -- the app displays it
        literally. Nothing here second-guesses the number.
        """
        return ToggledSetting.parse(self.raw.get("lowTemperatureLimit"))

    @property
    def low_temp_shutoff_f(self) -> float | None:
        """Return the low-temperature shutoff threshold, or ``None`` when off."""
        return self.low_temp_shutoff.effective

    @property
    def temperature_offset_f(self) -> float | None:
        """Return the calibration offset applied to the temperature reading."""
        return _as_float(self.raw.get("temperatureOffset"))

    @property
    def pre_alert_minutes(self) -> float | None:
        """Return how long before an auto-shutoff FloLogic warns the user."""
        return _as_float(self.raw.get("preAlertNoticeInterval"))

    @property
    def no_flow_notice_seconds(self) -> float | None:
        """Return the no-flow duration that triggers a notice."""
        return _as_float(self.raw.get("noFlowNoticeInterval"))

    @property
    def has_water_sensors(self) -> bool:
        """Return whether remote water sensors are paired with this valve.

        Only valves with sensors carry the ``waterSensor*`` limits, and they
        are what arm the humidity and low-temperature sensor shutoff modes.
        """
        return any(
            key in self.raw
            for key in (
                "waterSensorHumidityAlertLimit",
                "waterSensorHumidityShutoffLimit",
                "waterSensorTemperatureAlertLimit",
                "waterSensorTemperatureShutoffLimit",
            )
        )

    @property
    def sensor_humidity_alert(self) -> ToggledSetting:
        """Return the humidity percentage that triggers a water-sensor alert."""
        return ToggledSetting.parse(self.raw.get("waterSensorHumidityAlertLimit"))

    @property
    def sensor_humidity_shutoff(self) -> ToggledSetting:
        """Return the humidity percentage that triggers an automatic shutoff."""
        return ToggledSetting.parse(self.raw.get("waterSensorHumidityShutoffLimit"))

    @property
    def sensor_temp_alert(self) -> ToggledSetting:
        """Return the water-sensor temperature that triggers an alert."""
        return ToggledSetting.parse(self.raw.get("waterSensorTemperatureAlertLimit"))

    @property
    def sensor_temp_shutoff(self) -> ToggledSetting:
        """Return the water-sensor temperature that triggers a shutoff."""
        return ToggledSetting.parse(self.raw.get("waterSensorTemperatureShutoffLimit"))

    @property
    def current_flow_limit_minutes(self) -> float | None:
        """Return the flow limit that applies in the valve's current mode."""
        limits = {
            ControlMode.HOME: self.home_limit_minutes,
            ControlMode.AWAY: self.away_limit_minutes,
            ControlMode.BYPASS: self.bypass_minutes,
        }
        mode = self.control_mode
        return limits.get(mode) if mode is not None else None

    # --- derived timing -------------------------------------------------

    @property
    def flow_started_at(self) -> datetime | None:
        """Return when the current flow event began, if water is flowing.

        ``lastNewFlow`` is the right field and is confirmed against live
        hardware: it moves when a flow event begins and, unlike
        ``lastFlowChange``, does *not* move again when the water stops. The
        fallbacks cover models that omit it; both were observed carrying the
        same value at the moment flow started, so they degrade safely.
        """
        if not self.is_water_flowing:
            return None
        return _first_datetime(
            self.raw, ("lastNewFlow", "lastFlowChange", "lastFlowAnyChange")
        )

    def flow_elapsed_seconds(self, now: datetime | None = None) -> int | None:
        """Return how long water has been flowing, in seconds."""
        started_at = self.flow_started_at
        if started_at is None:
            return None
        return max(0, int((_now(now) - started_at).total_seconds()))

    @property
    def shutoff_at(self) -> datetime | None:
        """Return when FloLogic will close the valve for continuous flow.

        An absolute instant rather than a countdown, and deliberately so: a
        countdown is only true at the moment it is read, so a consumer either
        rewrites it constantly or displays a stale number. This value does not
        change while the flow continues, which lets a display count down from
        it without anything being stored again.

        ``None`` when water is not flowing or the active mode has no limit.
        Carries the same caveat as the countdown: the cloud reports short
        flows too late for this to appear during them.
        """
        started_at = self.flow_started_at
        limit_minutes = self.current_flow_limit_minutes
        if started_at is None or not limit_minutes or limit_minutes <= 0:
            return None
        return started_at + timedelta(minutes=limit_minutes)

    def shutoff_countdown_seconds(self, now: datetime | None = None) -> int | None:
        """Estimate seconds until FloLogic closes the valve for continuous flow.

        Returns ``None`` when water is not flowing or the active mode has no
        flow limit -- the estimate is derived locally from ``lastNewFlow`` plus
        the mode's limit, because the cloud does not publish a countdown.

        The arithmetic is exact where it applies: against a 99 minute limit
        with 37 seconds elapsed, this matched the app's own display to the
        second. But it can only run once the cloud reports flow, and short
        flows never get that far. Three consecutive live shutoffs on a 30
        second Away limit produced no flow report at all -- ``flowState``
        stayed ``NO_FLOW`` from the tap opening until the valve tripped -- so
        this stayed ``None`` throughout each one. Treat it as useful for long
        draws and absent for brief ones, not as a general-purpose countdown.
        """
        started_at = self.flow_started_at
        limit_minutes = self.current_flow_limit_minutes
        if started_at is None or not limit_minutes or limit_minutes <= 0:
            return None
        remaining = started_at.timestamp() + limit_minutes * 60 - _now(now).timestamp()
        return max(0, int(remaining))

    def is_in_pre_alert_window(self, now: datetime | None = None) -> bool:
        """Return whether the valve is inside its advance-shutoff warning window.

        This is purely the valve's timing. Whether the *user* would be notified
        also depends on :attr:`ValveAccess.notifications` carrying
        :attr:`~pyflologic.enums.NotificationSetting.ADVANCE_SHUTOFF`.
        """
        countdown = self.shutoff_countdown_seconds(now)
        pre_alert = self.pre_alert_minutes
        if countdown is None or not pre_alert:
            return False
        return countdown <= pre_alert * 60


@dataclass(frozen=True, slots=True)
class ValveAccess:
    """One user's access record and notification preferences for one valve."""

    raw: JsonDict

    @property
    def valve_id(self) -> str:
        """Return the valve this access record applies to."""
        return str(self.raw.get("valveId", ""))

    @property
    def user_id(self) -> str:
        """Return the user this access record belongs to."""
        return str(self.raw.get("userId", ""))

    @property
    def valve_name(self) -> str | None:
        """Return the valve's display name as recorded against this access.

        Names are *per user*: one real valve is "Riverside Upper Valve" to its
        owner and "Riverside Whole House" to a shared user, at the same instant.
        This field always agrees with the valve's own ``valveFriendlyName`` as
        returned to the same login, so either can be read -- but neither is a
        stable identifier. Use :attr:`Valve.unique_id` for that.
        """
        value = self.raw.get("valveFriendlyName")
        return str(value) if value else None

    @property
    def privilege(self) -> int | None:
        """Return the raw ``devicePrivilege`` level for this user and valve.

        Both an owner and a fully-shared user report ``2``, so the encoding of
        anything more restricted is unknown. Exposed raw rather than mapped to
        invented names: guessing which value means read-only would be exactly
        the wrong thing to be wrong about.
        """
        return _as_int(self.raw.get("devicePrivilege"))

    @property
    def notifications(self) -> NotificationSetting:
        """Return the decoded notification preference bitfield."""
        return NotificationSetting(_as_int(self.raw.get("notificationsList")) or 0)

    def wants(self, setting: NotificationSetting) -> bool:
        """Return whether the user has opted into a given notification."""
        return bool(self.notifications & setting)


@dataclass(frozen=True, slots=True)
class SchedulerEvent:
    """A scheduled mode change stored in the FloLogic cloud."""

    raw: JsonDict

    @property
    def valve_id(self) -> str:
        """Return the valve this event targets."""
        return str(self.raw.get("valveId", ""))

    @property
    def is_active(self) -> bool:
        """Return whether the entry actually does something.

        FloLogic keeps empty placeholder rows in the scheduler table; only
        entries carrying both an action and a payload will ever fire.
        """
        return (
            self.raw.get("action") is not None
            and self.raw.get("actionPayload") is not None
        )


@dataclass(frozen=True, slots=True)
class Notification:
    """One row of a valve's notification history.

    This is the closest thing FloLogic has to an event log, and it is more
    trustworthy than the live telemetry for flow events: a valve that shut
    itself off after 30 seconds of flow never showed that flow in
    :attr:`Valve.flow_state`, but did record

        "WATER SHUTOFF: Away flow limit of 30 seconds exceeded."

    Rows carry a ``title`` category and a human-readable ``text`` that names
    the actor and the threshold. Note that the payload has no ``valveId``;
    rows come back already scoped to the valves that were asked for.
    """

    raw: JsonDict

    @property
    def valve_id(self) -> str:
        """Return the valve the notification came from, if the row names one."""
        return str(self.raw.get("valveId", ""))

    @property
    def notification_id(self) -> int | None:
        """Return FloLogic's ID for this row, for de-duplicating a feed."""
        return _as_int(self.raw.get("id"))

    @property
    def title(self) -> str | None:
        """Return the notification's category, e.g. ``Mode Change``."""
        value = self.raw.get("title")
        return str(value) if value else None

    @property
    def is_delivered(self) -> bool:
        """Return whether FloLogic considers the notification delivered."""
        return bool(self.raw.get("delivered"))

    @property
    def created_at(self) -> datetime | None:
        """Return when FloLogic recorded the notification."""
        for key in ("created", "createdDate", "dateCreated"):
            parsed = _as_datetime(self.raw.get(key))
            if parsed is not None:
                return parsed
        return None

    @property
    def message(self) -> str | None:
        """Return the notification text, if present."""
        for key in ("message", "text", "body"):
            value = self.raw.get(key)
            if value:
                return str(value)
        return None


@dataclass(slots=True)
class Account:
    """Everything the client knows about one FloLogic account."""

    user: User
    valves: dict[str, Valve] = field(default_factory=dict)
    accesses: dict[str, ValveAccess] = field(default_factory=dict)

    @property
    def controllable_valves(self) -> dict[str, Valve]:
        """Return only the valves that can be commanded, excluding gateways."""
        return {
            valve_id: valve
            for valve_id, valve in self.valves.items()
            if valve.is_controllable
        }
