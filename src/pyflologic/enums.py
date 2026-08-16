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
    "STATUS_PRIORITY",
    "WARNING_FLAGS",
    "WATER_OFF_FLAGS",
    "ControlMode",
    "FlowState",
    "NotificationSetting",
    "ValveMode",
]


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
        return [member.name for member in ValveMode if member.name and member & self]

    @property
    def unknown_bits(self) -> int:
        """Return the raw value of any bits this library does not recognize."""
        known = 0
        for member in ValveMode:
            known |= member.value
        return int(self) & ~known


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
    ValveMode.DELAY_AWAY,
    ValveMode.AUTO_AWAY,
    ValveMode.EXTERNAL_AWAY,
    ValveMode.AWAY,
    ValveMode.EXTERNAL_BYPASS,
    ValveMode.BYPASS,
    ValveMode.EXTERNAL_HOME,
    ValveMode.HOME,
    ValveMode.DISABLED,
    ValveMode.UPDATING,
    ValveMode.COMMUNICATION_ERROR,
    ValveMode.VALVE_FAILURE,
    ValveMode.SYSTEM_DOWN,
    ValveMode.ERROR,
    ValveMode.UNKNOWN_FAULT,
)
"""Bits ordered most- to least-newsworthy, for picking one headline status."""


class FlowState(IntEnum):
    """Values FloLogic reports in a valve's ``flowState`` field."""

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
    """Bits packed into a user access record's ``notificationsList`` field."""

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
