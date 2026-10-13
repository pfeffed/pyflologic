"""Decoded FloLogic bitfields and enumerations.

The Web API reports valve state as a single ``Mode`` integer that packs both
the *controllable* mode (home/away/bypass/shutoff/disabled) and every active
condition (leak detected, battery low, valve failure, ...) into one bitfield.
Modeling it as an :class:`~enum.IntFlag` keeps that structure visible instead
of hiding it behind lookup dictionaries.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import KEEP, IntEnum, IntFlag, StrEnum
from typing import Self

__all__ = [
    "CRITICAL_FLAGS",
    "FLOWING_STATES",
    "PROBLEM_PRIORITY",
    "SETTABLE_MODES",
    "SHUTOFF_REASON_PRIORITY",
    "STATUS_PRIORITY",
    "WARNING_FLAGS",
    "WATER_OFF_FLAGS",
    "CallbackEventType",
    "CallbackReason",
    "ControlMode",
    "DeviceType",
    "FlowState",
    "ShutoffReason",
    "UpdateTarget",
    "ValveMode",
]


def _normalize(name: str) -> str:
    """Fold a PascalCase or UPPER_SNAKE name to one comparable form."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


class ValveMode(IntFlag, boundary=KEEP):
    """Bits packed into a valve's ``Mode`` field.

    The Web API documents five values -- ``Home = 1``, ``Away = 2``,
    ``Bypass = 4``, ``ShutOff = 8`` and ``Disabled = 16`` -- and names six more
    flags without their values: ``DelayAway``, ``Override``,
    ``ExternalOverride``, ``ExternalHome``, ``ExternalAway`` and
    ``ExternalBypass``. The full layout below comes from the app protocol,
    where the same backend enum was mapped bit by bit; the documented five
    agree with it exactly, and the six named flags all appear in it.

    Callbacks carry modes as comma-separated *names* rather than a number;
    :meth:`from_names` decodes those against this same table.

    ``boundary=KEEP`` means bits FloLogic adds later survive round-tripping
    instead of raising, so an unrecognized valve still reports a usable mode.
    """

    HOME = 1 << 0
    AWAY = 1 << 1
    BYPASS = 1 << 2
    SHUTOFF = 1 << 3
    DISABLED = 1 << 4
    FLOW_TIME_EXCEEDED = 1 << 5
    EXTERNAL_LEAK = 1 << 6
    AUTO_AWAY = 1 << 7
    EXTERNAL_BYPASS = 1 << 8
    DELAY_AWAY = 1 << 9
    EXTERNAL_AWAY = 1 << 10
    OVERRIDE = 1 << 11
    AC_LOST = 1 << 12
    CHANGE_BATTERY = 1 << 13
    ERROR = 1 << 14
    SENSOR_LEAK = 1 << 15
    SYSTEM_DOWN = 1 << 16
    VALVE_FAILURE = 1 << 17
    COMMUNICATION_ERROR = 1 << 18
    EXTERNAL_HOME = 1 << 19
    EXTERNAL_EMERGENCY_SHUTDOWN = 1 << 20
    UPDATING = 1 << 21
    EXTERNAL_OVERRIDE = 1 << 22
    LOW_TEMP_ALERT = 1 << 23
    LOW_TEMP_SHUTOFF = 1 << 24
    HUMIDITY_SENSOR_SHUTOFF = 1 << 25
    LOW_TEMP_SENSOR_SHUTOFF = 1 << 26
    UNKNOWN_FAULT = 1 << 28

    @property
    def flag_names(self) -> list[str]:
        """Return the names of every *recognized* bit that is set.

        Unrecognized bits are deliberately omitted rather than guessed at; use
        :attr:`unknown_bits` to see whether any were present.
        """
        return [member.name for member in ValveMode if member.name and member & self]

    @property
    def unknown_bits(self) -> int:
        """Return the raw value of any bits this library does not recognize."""
        known = 0
        for member in ValveMode:
            known |= member.value
        return int(self) & ~known

    @classmethod
    def from_names(cls, names: str | Iterable[str]) -> tuple[ValveMode, list[str]]:
        """Decode mode names as callbacks send them, e.g. ``"Home,DelayAway"``.

        Matching ignores case and separators, so ``ShutOff`` finds
        :attr:`SHUTOFF`. Names this table does not know are returned alongside
        rather than dropped: a callback naming a flag nobody has mapped yet is
        exactly the evidence worth keeping. A purely numeric entry is read as
        a raw value, since FloLogic's examples accept ``"1"`` for ``Home``.
        """
        parts = names.split(",") if isinstance(names, str) else list(names)
        value = cls(0)
        unknown: list[str] = []
        for part in parts:
            token = part.strip()
            if not token:
                continue
            if token.isdigit():
                value |= cls(int(token))
                continue
            member = _MODE_NAMES.get(_normalize(token))
            if member is None:
                unknown.append(token)
            else:
                value |= member
        return value, unknown


_MODE_NAMES: dict[str, ValveMode] = {
    _normalize(member.name): member for member in ValveMode if member.name
}
# The member names above abbreviate where FloLogic's own enum likely spells
# out; callbacks carry FloLogic's spelling. Unconfirmed until a callback shows
# one, but harmless if FloLogic never sends them.
_MODE_NAMES.update(
    {
        _normalize("LowTemperatureAlert"): ValveMode.LOW_TEMP_ALERT,
        _normalize("LowTemperatureShutoff"): ValveMode.LOW_TEMP_SHUTOFF,
        _normalize("LowTemperatureSensorShutoff"): ValveMode.LOW_TEMP_SENSOR_SHUTOFF,
        _normalize("AcPowerLost"): ValveMode.AC_LOST,
    }
)


class ControlMode(StrEnum):
    """The modes a valve can be in that a person chose.

    ``DISABLED`` is readable but cannot be set through the Web API, which
    accepts only Home, Away, Bypass and Shutoff -- see :data:`SETTABLE_MODES`.
    """

    HOME = "home"
    AWAY = "away"
    BYPASS = "bypass"
    SHUTOFF = "shutoff"
    DISABLED = "disabled"

    @property
    def flag(self) -> ValveMode:
        """Return the :class:`ValveMode` bit this control mode sets."""
        return _CONTROL_MODE_FLAGS[self]

    @property
    def settable(self) -> bool:
        """Return whether the Web API accepts this mode as a command."""
        return self in SETTABLE_MODES

    @property
    def api_value(self) -> str:
        """Return the ``valve_mode`` value ``PUT /System/ValveMode`` expects."""
        return _CONTROL_MODE_API_VALUES[self]

    @classmethod
    def from_flag(cls, flag: ValveMode) -> ControlMode | None:
        """Return the control mode a valve is in, or ``None`` if unclear.

        A valve reporting a fault often has both the fault bit and its
        underlying control bit set, so any water-off condition reads as
        ``shutoff`` -- the water is off, whatever else is true.
        """
        for water_off in WATER_OFF_FLAGS:
            if flag & water_off:
                return cls.SHUTOFF
        for mode in (cls.BYPASS, cls.AWAY, cls.HOME, cls.DISABLED):
            if flag & mode.flag:
                return mode
        return None


_CONTROL_MODE_FLAGS: dict[ControlMode, ValveMode] = {
    ControlMode.HOME: ValveMode.HOME,
    ControlMode.AWAY: ValveMode.AWAY,
    ControlMode.BYPASS: ValveMode.BYPASS,
    ControlMode.SHUTOFF: ValveMode.SHUTOFF,
    ControlMode.DISABLED: ValveMode.DISABLED,
}

_CONTROL_MODE_API_VALUES: dict[ControlMode, str] = {
    ControlMode.HOME: "Home",
    ControlMode.AWAY: "Away",
    ControlMode.BYPASS: "Bypass",
    ControlMode.SHUTOFF: "Shutoff",
    ControlMode.DISABLED: "Disabled",
}

SETTABLE_MODES: tuple[ControlMode, ...] = (
    ControlMode.HOME,
    ControlMode.AWAY,
    ControlMode.BYPASS,
    ControlMode.SHUTOFF,
)
"""The modes ``PUT /System/ValveMode`` documents accepting."""

WATER_OFF_FLAGS: tuple[ValveMode, ...] = (
    ValveMode.FLOW_TIME_EXCEEDED,
    ValveMode.SENSOR_LEAK,
    ValveMode.EXTERNAL_LEAK,
    ValveMode.EXTERNAL_EMERGENCY_SHUTDOWN,
    ValveMode.LOW_TEMP_SHUTOFF,
    ValveMode.HUMIDITY_SENSOR_SHUTOFF,
    ValveMode.LOW_TEMP_SENSOR_SHUTOFF,
    ValveMode.SHUTOFF,
)
"""Conditions in which the valve has closed and water is off."""

WARNING_FLAGS: tuple[ValveMode, ...] = (
    ValveMode.LOW_TEMP_ALERT,
    ValveMode.CHANGE_BATTERY,
    ValveMode.AC_LOST,
    ValveMode.COMMUNICATION_ERROR,
    ValveMode.UPDATING,
)
"""Conditions that need attention but have not closed the valve."""

CRITICAL_FLAGS: tuple[ValveMode, ...] = (
    ValveMode.ERROR,
    ValveMode.SYSTEM_DOWN,
    ValveMode.VALVE_FAILURE,
    ValveMode.UNKNOWN_FAULT,
)
"""Hardware or service faults that make the valve untrustworthy."""

STATUS_PRIORITY: tuple[ValveMode, ...] = (
    ValveMode.FLOW_TIME_EXCEEDED,
    ValveMode.SENSOR_LEAK,
    ValveMode.EXTERNAL_LEAK,
    ValveMode.EXTERNAL_EMERGENCY_SHUTDOWN,
    ValveMode.LOW_TEMP_SHUTOFF,
    ValveMode.HUMIDITY_SENSOR_SHUTOFF,
    ValveMode.LOW_TEMP_SENSOR_SHUTOFF,
    ValveMode.SHUTOFF,
    # Override outranks the ordinary modes: an irrigation controller signalling
    # OVERRIDE replaces the mode outright and suspends the flow limit.
    ValveMode.EXTERNAL_OVERRIDE,
    ValveMode.OVERRIDE,
    ValveMode.DELAY_AWAY,
    ValveMode.AUTO_AWAY,
    ValveMode.EXTERNAL_AWAY,
    ValveMode.AWAY,
    ValveMode.EXTERNAL_BYPASS,
    ValveMode.BYPASS,
    ValveMode.EXTERNAL_HOME,
    ValveMode.HOME,
    ValveMode.DISABLED,
    # Warnings rank below the operating modes: a valve that is HOME with a low
    # battery is still meaningfully "home". They are listed only so that a
    # warning arriving on its own does not read as "unknown".
    ValveMode.LOW_TEMP_ALERT,
    ValveMode.AC_LOST,
    ValveMode.CHANGE_BATTERY,
    ValveMode.UPDATING,
    ValveMode.COMMUNICATION_ERROR,
    ValveMode.VALVE_FAILURE,
    ValveMode.SYSTEM_DOWN,
    ValveMode.ERROR,
    ValveMode.UNKNOWN_FAULT,
)
"""Bits ordered most- to least-newsworthy, for picking one headline status.

Every named bit must appear here; one that does not silently degrades to
"unknown". A test enforces the exhaustiveness.
"""

PROBLEM_PRIORITY: tuple[ValveMode, ...] = (
    ValveMode.VALVE_FAILURE,
    ValveMode.ERROR,
    ValveMode.SYSTEM_DOWN,
    ValveMode.UNKNOWN_FAULT,
    ValveMode.COMMUNICATION_ERROR,
    ValveMode.LOW_TEMP_ALERT,
    ValveMode.AC_LOST,
    ValveMode.CHANGE_BATTERY,
    ValveMode.UPDATING,
)
"""Conditions that need attention without closing the valve, worst first.

A valve failure outranks everything: a valve that cannot be trusted to close
is worse than any condition it might need to close for. Low temperature sits
above the power and battery warnings because it is a countdown to burst pipes
rather than a maintenance note.
"""

SHUTOFF_REASON_PRIORITY: tuple[ValveMode, ...] = (
    ValveMode.SENSOR_LEAK,
    ValveMode.EXTERNAL_LEAK,
    ValveMode.FLOW_TIME_EXCEEDED,
    ValveMode.EXTERNAL_EMERGENCY_SHUTDOWN,
    ValveMode.LOW_TEMP_SHUTOFF,
    ValveMode.LOW_TEMP_SENSOR_SHUTOFF,
    ValveMode.HUMIDITY_SENSOR_SHUTOFF,
)
"""Why a closed valve closed itself, most specific first.

Excludes the plain ``SHUTOFF`` bit, which is set both for a person's own
command and alongside every automatic trip, so it says only "closed" and
never why.
"""


class ShutoffReason(StrEnum):
    """Why a valve is closed, as read from its mode bits.

    Deliberately *not* a :class:`ValveMode` bit. ``MANUAL`` has no bit on the
    wire -- FloLogic expresses it as ``SHUTOFF`` with no accompanying cause --
    so it is an interpretation, and lives here rather than in the bitfield.

    Callbacks carry a reason of their own for automatic shutoffs; see
    :class:`CallbackReason`.
    """

    MANUAL = "manual"
    UNRECOGNIZED = "unrecognized"
    SENSOR_LEAK = "sensor_leak"
    EXTERNAL_LEAK = "external_leak"
    FLOW_TIME_EXCEEDED = "flow_time_exceeded"
    EMERGENCY_SHUTDOWN = "emergency_shutdown"
    LOW_TEMPERATURE = "low_temperature"
    LOW_TEMPERATURE_SENSOR = "low_temperature_sensor"
    HUMIDITY_SENSOR = "humidity_sensor"

    @classmethod
    def from_flag(cls, flag: ValveMode) -> ShutoffReason | None:
        """Return the reason a mode bit represents, if it is a shutoff cause."""
        return _SHUTOFF_REASON_FLAGS.get(flag)


_SHUTOFF_REASON_FLAGS: dict[ValveMode, ShutoffReason] = {
    ValveMode.SENSOR_LEAK: ShutoffReason.SENSOR_LEAK,
    ValveMode.EXTERNAL_LEAK: ShutoffReason.EXTERNAL_LEAK,
    ValveMode.FLOW_TIME_EXCEEDED: ShutoffReason.FLOW_TIME_EXCEEDED,
    ValveMode.EXTERNAL_EMERGENCY_SHUTDOWN: ShutoffReason.EMERGENCY_SHUTDOWN,
    ValveMode.LOW_TEMP_SHUTOFF: ShutoffReason.LOW_TEMPERATURE,
    ValveMode.LOW_TEMP_SENSOR_SHUTOFF: ShutoffReason.LOW_TEMPERATURE_SENSOR,
    ValveMode.HUMIDITY_SENSOR_SHUTOFF: ShutoffReason.HUMIDITY_SENSOR,
}


class FlowState(IntEnum):
    """Values the Web API documents for a valve's ``FlowState``.

    Not a bitfield, despite the 1/2/4 run: ``VALVE_CLOSED`` is 0 and
    ``PERCENT_FOLLOWING`` is 99.

    On the app protocol, closing a valve against moving water went straight
    to ``NO_FLOW`` both times it was tried, with no intermediate state -- so
    whether ``VALVE_CLOSED`` is ever reported is an open question. Read
    :attr:`~pyflologic.models.Valve.mode` to learn whether a valve is closed.
    ``PERCENT_FOLLOWING`` is undocumented beyond its name.
    """

    VALVE_CLOSED = 0
    NO_FLOW = 1
    NEW_FLOW = 2
    FLOW = 4
    PERCENT_FOLLOWING = 99

    @classmethod
    def parse(cls, value: object) -> FlowState | None:
        """Return the matching member, or ``None`` for missing/unknown values."""
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        try:
            return cls(int(value))
        except ValueError:
            return None


FLOWING_STATES: frozenset[FlowState] = frozenset({FlowState.NEW_FLOW, FlowState.FLOW})
"""Flow states that mean water is actually moving through the valve."""


class _TolerantStrEnum(StrEnum):
    """A string enum that matches FloLogic's names case-insensitively.

    FloLogic is not consistent about casing across endpoints -- a status
    response says ``WiFiConnect`` where a callback says ``WifiConnect`` -- and
    neither spelling is worth failing over.
    """

    @classmethod
    def parse(cls, value: object) -> Self | None:
        """Return the member named by ``value``, or ``None`` if unrecognized."""
        if not isinstance(value, str) or not value:
            return None
        wanted = _normalize(value)
        for member in cls:
            if _normalize(member.value) == wanted:
                return member
        return None


class DeviceType(_TolerantStrEnum):
    """Device types the Web API reports.

    Both are valve controllers. A G-Connect here is a Connect *device*, not
    the gateway the app protocol lists alongside valves -- the Web API omits
    gateways and sensors entirely.
    """

    WIFI_CONNECT = "WiFiConnect"
    G_CONNECT = "GConnect"


class CallbackEventType(_TolerantStrEnum):
    """Typed event names FloLogic delivers to callbacks.

    State-transition events fire when a state is entered or cleared, never for
    a repeated unchanged state. Timer events fire when their interval elapses.
    """

    AUTO_SHUTOFF = "AutoShutoff"
    MODE_CHANGED = "ModeChanged"
    VALVE_ISSUE = "ValveIssue"
    POWER_CHANGED = "PowerChanged"
    LOW_TEMPERATURE_ALERT = "LowTemperatureAlert"
    LOW_TEMPERATURE_ALERT_REMINDER = "LowTemperatureAlertReminder"
    OFFLINE = "Offline"
    ONLINE = "Online"
    PRE_ALERT = "PreAlert"
    NO_FLOW = "NoFlow"
    SOFTWARE_UPDATE = "SoftwareUpdate"


class CallbackReason(_TolerantStrEnum):
    """Reason codes FloLogic attaches to callback events.

    These are enum names, not free text. ``AutoShutoff`` carries the first
    five; a manual shutoff is reported as ``ModeChanged``, never as
    ``AutoShutoff``, which is the distinction that matters most.
    """

    FLOW_TIME_EXCEEDED = "FlowTimeExceeded"
    PINPOINT_LEAK = "PinpointLeak"
    LOW_TEMPERATURE = "LowTemperature"
    HUMIDITY = "Humidity"
    EXTERNAL_SIGNAL = "ExternalSignal"
    DELAY_AWAY = "DelayAway"
    LOW_BATTERY = "LowBattery"
    VALVE_FAILURE = "ValveFailure"
    SYSTEM_DOWN = "SystemDown"
    COMMUNICATION_ERROR = "CommunicationError"
    UNKNOWN_ERROR = "UnknownError"
    AC_POWER_LOST = "AcPowerLost"
    AC_POWER_RESTORED = "AcPowerRestored"
    UPDATE_STARTED = "UpdateStarted"
    DEVICE_OFFLINE = "DeviceOffline"
    SHUTOFF_PENDING = "ShutoffPending"
    CONNECT = "Connect"
    NO_FLOW = "NoFlow"
    NO_FLOW_ANY = "NoFlowAny"
    LOW_BATTERY_CLEARED = "LowBatteryCleared"
    VALVE_FAILURE_CLEARED = "ValveFailureCleared"
    SYSTEM_DOWN_CLEARED = "SystemDownCleared"
    COMMUNICATION_ERROR_CLEARED = "CommunicationErrorCleared"
    UNKNOWN_ERROR_CLEARED = "UnknownErrorCleared"

    @property
    def is_cleared(self) -> bool:
        """Return whether this reason announces a condition ending."""
        return self.value.endswith("Cleared")


class UpdateTarget(_TolerantStrEnum):
    """What a ``SoftwareUpdate`` event is updating."""

    CONNECT = "Connect"
    VALVE = "Valve"
    CONTROL_PANEL = "ControlPanel"
