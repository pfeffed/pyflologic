"""Decoded FloLogic bitfields and enumerations.

The cloud reports valve state as a single ``mode`` integer that packs both the
*controllable* mode (home/away/bypass/shutoff/disabled) and every active
condition (leak detected, battery low, valve failure, ...) into one bitfield.
Modeling it as an :class:`~enum.IntFlag` keeps that structure visible instead
of hiding it behind lookup dictionaries.
"""

from __future__ import annotations

from enum import KEEP, IntEnum, IntFlag, StrEnum

__all__ = [
    "CONTROL_MODES",
    "CRITICAL_FLAGS",
    "PROBLEM_PRIORITY",
    "SHUTOFF_REASON_PRIORITY",
    "STATUS_PRIORITY",
    "WARNING_FLAGS",
    "WATER_OFF_FLAGS",
    "ControlMode",
    "FlowState",
    "NotificationSetting",
    "ShutoffReason",
    "ToggledSettingName",
    "ValveMode",
]


def _set_flag_names(flag_type: type[IntFlag], value: IntFlag) -> list[str]:
    """Return the names of every recognized bit set in ``value``."""
    return [member.name for member in flag_type if member.name and member & value]


def _unrecognized_bits(flag_type: type[IntFlag], value: IntFlag) -> int:
    """Return the bits of ``value`` that ``flag_type`` does not name."""
    known = 0
    for member in flag_type:
        known |= member.value
    return int(value) & ~known


class ValveMode(IntFlag, boundary=KEEP):
    """Bits packed into a valve's ``mode`` field.

    ``boundary=KEEP`` means bits FloLogic adds in future firmware survive
    round-tripping instead of raising, so an unrecognized valve still reports
    a usable mode.
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
        return _set_flag_names(ValveMode, self)

    @property
    def unknown_bits(self) -> int:
        """Return the raw value of any bits this library does not recognize."""
        return _unrecognized_bits(ValveMode, self)


class ToggledSettingName(StrEnum):
    """The settings FloLogic switches off by negating rather than clearing.

    Each maps to a :class:`~pyflologic.models.ToggledSetting` on the valve and
    to one signed field on the wire, so writing one means choosing a sign as
    well as a magnitude.
    """

    AUTO_AWAY = "auto_away"
    DELAY_AWAY = "delay_away"
    WINTER_FLOW_SENSITIVITY = "winter_flow_sensitivity"
    GUEST_MODE = "guest_mode"
    LOW_TEMP_ALERT = "low_temp_alert"
    LOW_TEMP_SHUTOFF = "low_temp_shutoff"


class ControlMode(StrEnum):
    """The five modes a user can actually command the valve into."""

    HOME = "home"
    AWAY = "away"
    BYPASS = "bypass"
    SHUTOFF = "shutoff"
    DISABLED = "disabled"

    @property
    def flag(self) -> ValveMode:
        """Return the :class:`ValveMode` bit this control mode sets."""
        return _CONTROL_MODE_FLAGS[self]

    @classmethod
    def from_flag(cls, flag: ValveMode) -> ControlMode | None:
        """Return the control mode a valve is in, or ``None`` if unclear.

        A valve reporting a fault often has both the fault bit and its
        underlying control bit set, so this checks bits in the order the app
        treats as most specific: any water-off condition reads as ``shutoff``.
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

CONTROL_MODES: tuple[ControlMode, ...] = tuple(ControlMode)

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
    # OVERRIDE replaces the mode outright (a real valve reported mode == 2048
    # with no HOME bit) and suspends the flow limit, which is why the app shows
    # "Override" with elapsed time but no shutoff countdown.
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
    # battery is still meaningfully "home", and the warning surfaces through
    # `active_warning_flags`. They are listed only so that a warning arriving
    # on its own does not read as "unknown".
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
"unknown", which is how ``OVERRIDE`` went unnoticed until a valve reported it.
A test enforces the exhaustiveness.

The relative order of the hardware faults at the tail is inherited from earlier
reverse engineering and has not been checked against the app -- no valve has
been observed faulted. Consumers that care about faults should read
:data:`CRITICAL_FLAGS` rather than relying on this ranking.
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

Excludes the plain ``SHUTOFF`` bit, which is set both for a user's own command
and alongside every automatic trip, so it says only "closed" and never why.
"""


class ShutoffReason(StrEnum):
    """Why a valve is closed.

    Deliberately *not* a :class:`ValveMode` bit. ``MANUAL`` has no bit on the
    wire -- FloLogic expresses it as ``SHUTOFF`` with no accompanying cause --
    and inventing one would be unsafe: a valve's mode integer is echoed back
    to the cloud in every command, so a bit this library made up would
    eventually be transmitted, and could collide with one FloLogic assigns
    later. The wire format stays a faithful mirror; the interpretation lives
    here.
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
    """Values FloLogic reports in a valve's ``flowState`` field.

    ``NO_FLOW``, ``NEW_FLOW`` and ``FLOW`` are all confirmed against live
    hardware. ``VALVE_CLOSED`` is not: a valve driven into SHUTOFF, watched
    physically closing, reported ``NO_FLOW`` throughout. The value is carried
    on inherited authority alone, so do not infer a closed valve from it --
    read :attr:`~pyflologic.models.Valve.mode` for that.
    """

    NO_FLOW = 1
    NEW_FLOW = 2
    FLOW = 4
    VALVE_CLOSED = 8

    @classmethod
    def parse(cls, value: object) -> FlowState | None:
        """Return the matching member, or ``None`` for missing/unknown values."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        try:
            return cls(value)
        except ValueError:
            return None


FLOWING_STATES: frozenset[FlowState] = frozenset({FlowState.NEW_FLOW, FlowState.FLOW})
"""Flow states that mean water is actually moving through the valve."""


class NotificationSetting(IntFlag, boundary=KEEP):
    """Bits packed into a user access record's ``notificationsList`` field.

    Real records use far more of the word than these twelve bits: a valve with
    every notification enabled reports ``2147483645``, which is bits 0-30 set
    with only ``NEVER`` cleared. The named bits below are confirmed -- turning
    off two notifications on one valve moved the value by exactly
    ``MODE_CHANGE | GENERAL_ALERT`` -- but bits 12-30 are unidentified and are
    preserved rather than interpreted. Check :attr:`unknown_bits` for them.
    """

    ALWAYS = 1 << 0
    NEVER = 1 << 1
    MODE_CHANGE = 1 << 2
    AUTO_SHUTOFF = 1 << 3
    AUTO_AWAY = 1 << 4
    DELAY_AWAY = 1 << 5
    ADVANCE_SHUTOFF = 1 << 6
    GUEST_MODE = 1 << 7
    CONNECTION_CHANGE = 1 << 8
    GENERAL_ALERT = 1 << 9
    CRITICAL_ERROR = 1 << 10
    NO_FLOW = 1 << 11

    @property
    def flag_names(self) -> list[str]:
        """Return the names of every recognized notification bit that is set."""
        return _set_flag_names(NotificationSetting, self)

    @property
    def unknown_bits(self) -> int:
        """Return the raw value of any bits this library does not recognize."""
        return _unrecognized_bits(NotificationSetting, self)
