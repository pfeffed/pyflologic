"""Typed views over the JSON objects the Web API returns.

Every model keeps the payload it was built from in ``raw``, so a field this
library has not modeled is still there for the caller.

FloLogic documents PascalCase property names, and the status objects use them,
but the paging envelope arrives as ``Items``/``Page``/``TotalCount`` where the
documentation shows ``items``/``page``/``totalCount``. Every lookup here is
therefore case-insensitive: the casing has already drifted once, and neither
spelling is worth failing over.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from .enums import (
    CRITICAL_FLAGS,
    FLOWING_STATES,
    PROBLEM_PRIORITY,
    SHUTOFF_REASON_PRIORITY,
    STATUS_PRIORITY,
    WARNING_FLAGS,
    WATER_OFF_FLAGS,
    CallbackEventType,
    CallbackReason,
    ControlMode,
    DeviceType,
    FlowState,
    ShutoffReason,
    UpdateTarget,
    ValveMode,
)

__all__ = [
    "CallbackDefinition",
    "CallbackEvent",
    "JsonDict",
    "Page",
    "User",
    "Valve",
]

JsonDict = dict[str, Any]

T = TypeVar("T")


def lookup(raw: Mapping[str, Any], key: str) -> Any:
    """Return ``raw[key]``, matching the key case-insensitively."""
    if key in raw:
        return raw[key]
    wanted = key.lower()
    for candidate, value in raw.items():
        if candidate.lower() == wanted:
            return value
    return None


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


def _as_str(value: Any) -> str | None:
    """Return a non-empty string, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value)
    return text or None


def _as_bool(value: Any) -> bool | None:
    """Return a JSON boolean, accepting the strings a query string carries."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1"}:
            return True
        if lowered in {"false", "0"}:
            return False
    return None


def parse_datetime(value: Any) -> datetime | None:
    """Parse a FloLogic timestamp as an aware UTC datetime.

    The documentation promises ISO 8601 in UTC, and the values are UTC, but
    they arrive without a ``Z`` or any offset (``2026-10-13T15:40:10``). Naive
    values are therefore read as UTC rather than as local time -- read as
    local, a valve in California would report flow from seven hours ago.
    """
    if not isinstance(value, str) or not value:
        return None
    text = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Valve:
    """One FloLogic Connect device, as ``GET /System/Status`` reports it.

    The Web API calls these "systems"; each is a valve controller, a WiFi
    Connect or a G-Connect. Instances are immutable snapshots.

    Compared with the app protocol, the Web API leaves out the valve's name,
    signal strength, firmware, battery, temperature offset, no-flow interval,
    Auto Away, Delay Away, winter sensitivity and guest mode. Those are absent
    from this model because they are absent from the API, not because they
    were overlooked.
    """

    raw: JsonDict

    # --- identity -------------------------------------------------------

    @property
    def vid(self) -> str:
        """Return the device identifier used in every request.

        The same value the app protocol calls ``uuid`` -- a stable hardware
        identity, and the right key for anything that must survive a change
        of account or credentials.
        """
        return str(lookup(self.raw, "VID") or "")

    @property
    def device_type(self) -> DeviceType | None:
        """Return the device type, or ``None`` if FloLogic sent a new one."""
        return DeviceType.parse(lookup(self.raw, "Type"))

    @property
    def device_type_raw(self) -> str | None:
        """Return ``Type`` exactly as sent, for types this library predates."""
        return _as_str(lookup(self.raw, "Type"))

    @property
    def address(self) -> str | None:
        """Return the formatted street address FloLogic has on file.

        Not a name, and not unique: two valves in one house share an address,
        which is why nothing here derives a name from it. The Web API does
        not return the name the owner gave the valve.
        """
        return _as_str(lookup(self.raw, "Address"))

    # --- live state -----------------------------------------------------

    @property
    def is_online(self) -> bool:
        """Return whether FloLogic currently considers the device reachable."""
        return lookup(self.raw, "Online") is True

    @property
    def mode(self) -> ValveMode:
        """Return the mode bitfield, decoded."""
        return ValveMode(_as_int(lookup(self.raw, "Mode")) or 0)

    @property
    def control_mode(self) -> ControlMode | None:
        """Return the mode the valve is in, if it can be determined."""
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
        return FlowState.parse(lookup(self.raw, "FlowState"))

    @property
    def is_water_flowing(self) -> bool:
        """Return whether the valve reports water moving through it.

        Expect this to miss short flows. On the app protocol -- the same
        backend -- three real auto-shutoffs on a 30 second limit never showed
        any flow at all before the valve closed. Use it for long draws, and
        :attr:`mode` for anything that has to be right.
        """
        state = self.flow_state
        return self.is_online and state is not None and state in FLOWING_STATES

    @property
    def last_flow_change(self) -> datetime | None:
        """Return when the flow state last changed.

        Matches the app protocol's ``lastFlowChange`` to the second, not its
        ``lastNewFlow``. The difference matters: this moves again when a flow
        is promoted from new to sustained, and again when it stops, so it is
        not when the current flow began, and no shutoff countdown can be
        derived from it.
        """
        return parse_datetime(lookup(self.raw, "LastFlowDateTime"))

    @property
    def temperature_f(self) -> float | None:
        """Return the device temperature in degrees Fahrenheit."""
        return _as_float(lookup(self.raw, "Temperature"))

    # --- settings -------------------------------------------------------

    @property
    def flow_sensitivity_oz_per_min(self) -> float | None:
        """Return the drip-detection threshold, ``DripRate``, in ounces per minute.

        The Web API gives no unit. Ounces per minute is the app's, and the
        values agree with the app protocol's ``dripRate`` for the same valves.
        """
        return _as_float(lookup(self.raw, "DripRate"))

    @property
    def home_limit_minutes(self) -> float | None:
        """Return the continuous-flow limit while in Home mode."""
        return _as_float(lookup(self.raw, "HomeTime"))

    @property
    def away_limit_minutes(self) -> float | None:
        """Return the continuous-flow limit while in Away mode.

        Sub-minute values are normal: ``0.1`` is six seconds. A value of
        ``0`` means Away turns the water off outright -- see
        :attr:`away_turns_water_off`.
        """
        return _as_float(lookup(self.raw, "AwayTime"))

    @property
    def away_turns_water_off(self) -> bool:
        """Return whether Away mode is configured to shut the water off.

        FloLogic documents an ``AwayTime`` of ``0`` as "water off": no flow
        allowed at all, rather than a zero-length limit.
        """
        return self.away_limit_minutes == 0

    @property
    def bypass_enabled(self) -> bool:
        """Return whether the bypass timer is enabled."""
        return lookup(self.raw, "BypassTimeEnabled") is True

    @property
    def bypass_minutes(self) -> float | None:
        """Return how long Bypass lasts, or ``None`` when its timer is disabled.

        A configured ``0`` is valid and distinct from ``None``.
        """
        return _as_float(lookup(self.raw, "BypassTime"))

    @property
    def low_temp_alert_enabled(self) -> bool:
        """Return whether the low-temperature alert is enabled."""
        return lookup(self.raw, "LowTemperatureAlertEnabled") is True

    @property
    def low_temp_alert_f(self) -> float | None:
        """Return the low-temperature alert threshold, or ``None`` when off.

        FloLogic documents this as *including* the device's temperature
        offset, where the app protocol keeps the two apart. The difference is
        invisible while the offset is zero, as it is on every valve this was
        checked against.
        """
        return _as_float(lookup(self.raw, "LowTemperatureAlertThreshold"))

    @property
    def low_temp_shutoff_enabled(self) -> bool:
        """Return whether the low-temperature shutoff is enabled."""
        return lookup(self.raw, "LowTemperatureShutoffEnabled") is True

    @property
    def low_temp_shutoff_f(self) -> float | None:
        """Return the low-temperature shutoff threshold, or ``None`` when off.

        Includes the temperature offset, like :attr:`low_temp_alert_f`.
        """
        return _as_float(lookup(self.raw, "LowTemperatureShutoffThreshold"))

    @property
    def pre_alert_minutes(self) -> float | None:
        """Return how long before an auto-shutoff FloLogic warns the user."""
        return _as_float(lookup(self.raw, "PreAlertNoticeTime"))

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

    # --- derived conditions ----------------------------------------------

    @property
    def active_water_off_flags(self) -> list[ValveMode]:
        """Return every set bit that means the valve has closed."""
        return [flag for flag in WATER_OFF_FLAGS if self.mode & flag]

    @property
    def is_water_off(self) -> bool:
        """Return whether the valve has closed, for whatever reason."""
        return bool(self.active_water_off_flags)

    @property
    def automatic_shutoff_flags(self) -> list[ValveMode]:
        """Return the water-off conditions the valve raised by itself.

        The plain ``SHUTOFF`` bit is excluded because it means only "closed",
        and is set for a person's own command as well as alongside every
        automatic trip. What remains is the set of reasons the valve decided
        for itself.
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

        ``MANUAL`` means a person or an integration closed it. It is inferred
        from the *absence* of any cause, so a closed valve carrying bits this
        library does not recognize reports ``UNRECOGNIZED`` instead: guessing
        there would describe a leak as a deliberate shutoff.
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
        """Return the most serious condition that is not closing the valve."""
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


@dataclass(frozen=True, slots=True)
class User:
    """A FloLogic user visible to the API consumer.

    ``GET /User/All`` returns the ID, email and names; ``GET /User/Status``
    returns the names, email and the user's VIDs but not the ID. Whichever was
    not in the payload reads as ``None`` or empty.
    """

    raw: JsonDict

    @property
    def user_id(self) -> int | None:
        """Return the numeric user ID, if this payload carried it."""
        return _as_int(lookup(self.raw, "Id"))

    @property
    def email(self) -> str | None:
        """Return the user's email address."""
        return _as_str(lookup(self.raw, "Email"))

    @property
    def first_name(self) -> str | None:
        """Return the user's first name."""
        return _as_str(lookup(self.raw, "FirstName"))

    @property
    def last_name(self) -> str | None:
        """Return the user's last name."""
        return _as_str(lookup(self.raw, "LastName"))

    @property
    def vids(self) -> list[str]:
        """Return the VIDs of the devices the user can access."""
        systems = lookup(self.raw, "Systems")
        if not isinstance(systems, list):
            return []
        return [str(vid) for vid in systems if vid]


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of a paginated listing."""

    items: list[T]
    page: int
    page_size: int
    total_count: int
    total_pages: int
    has_next_page: bool
    has_previous_page: bool

    @classmethod
    def parse(cls, raw: Any, item: Callable[[Any], T | None]) -> Page[T]:
        """Build a page from the envelope, converting each item with ``item``.

        Items ``item`` maps to ``None`` are dropped rather than failing the
        whole page.
        """
        envelope: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        rows = lookup(envelope, "items")
        items = [
            converted
            for row in (rows if isinstance(rows, list) else [])
            if (converted := item(row)) is not None
        ]
        page = _as_int(lookup(envelope, "page")) or 1
        total_pages = _as_int(lookup(envelope, "totalPages")) or 1
        has_next = _as_bool(lookup(envelope, "hasNextPage"))
        return cls(
            items=items,
            page=page,
            page_size=_as_int(lookup(envelope, "pageSize")) or len(items),
            total_count=_as_int(lookup(envelope, "totalCount")) or len(items),
            total_pages=total_pages,
            has_next_page=has_next if has_next is not None else page < total_pages,
            has_previous_page=_as_bool(lookup(envelope, "hasPreviousPage")) or page > 1,
        )


@dataclass(frozen=True, slots=True)
class CallbackDefinition:
    """One company-level webhook: a URL template and whether it is active.

    The URL may carry placeholders such as ``{vid}`` and ``{event_type}``,
    which FloLogic substitutes and URL-encodes before calling it with GET.
    """

    url: str
    active: bool = True

    @classmethod
    def from_json(cls, raw: Any) -> CallbackDefinition | None:
        """Build a definition from the API's JSON, or ``None`` if malformed."""
        if not isinstance(raw, Mapping):
            return None
        url = _as_str(lookup(raw, "CallbackURL"))
        if url is None:
            return None
        return cls(url=url, active=lookup(raw, "Active") is not False)

    def to_json(self) -> JsonDict:
        """Return the JSON object ``POST /Callbacks`` expects."""
        return {"CallbackURL": self.url, "Active": self.active}


@dataclass(frozen=True, slots=True)
class CallbackEvent:
    """One callback delivery, decoded from its query string.

    FloLogic calls a callback URL with GET and no body, so everything the
    event carries arrives in placeholders the URL template asked for. The keys
    here are the placeholder names themselves, which is how
    :func:`~pyflologic.webhook.build_callback_url` lays the query out.

    Typed fields are ``None`` when FloLogic sent a value this library does not
    recognize; the ``*_raw`` fields and :attr:`params` keep what was sent.
    """

    params: dict[str, str]
    vid: str | None
    event_type: CallbackEventType | None
    event_type_raw: str | None
    reason: CallbackReason | None
    reason_raw: str | None
    previous_mode: ValveMode | None
    current_mode: ValveMode | None
    unknown_mode_names: list[str] = field(default_factory=list)
    source_vid: str | None = None
    device_type: DeviceType | None = None
    temperature: float | None = None
    temperature_threshold: float | None = None
    interval_seconds: float | None = None
    update_target: UpdateTarget | None = None
    occurred_at: datetime | None = None

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> CallbackEvent:
        """Decode a callback's query parameters.

        An unsubstituted placeholder -- FloLogic leaves unknown ones as
        ``{name}`` -- and an empty value both read as absent.
        """
        clean = {
            key.lower(): value
            for key, value in params.items()
            if value and not (value.startswith("{") and value.endswith("}"))
        }
        unknown: list[str] = []

        def mode(key: str) -> ValveMode | None:
            names = clean.get(key)
            if names is None:
                return None
            value, missing = ValveMode.from_names(names)
            unknown.extend(name for name in missing if name not in unknown)
            return value

        # {reason} and {message} are aliases, as are {current_mode} and
        # {new_mode}; either spelling of each is accepted.
        reason_raw = clean.get("reason") or clean.get("message")
        current = mode("current_mode")
        if current is None:
            current = mode("new_mode")
        return cls(
            params=dict(params),
            vid=clean.get("vid"),
            event_type=CallbackEventType.parse(clean.get("event_type")),
            event_type_raw=clean.get("event_type"),
            reason=CallbackReason.parse(reason_raw),
            reason_raw=reason_raw,
            previous_mode=mode("previous_mode"),
            current_mode=current,
            unknown_mode_names=unknown,
            source_vid=clean.get("source_vid"),
            device_type=DeviceType.parse(clean.get("device_type")),
            temperature=_as_float(clean.get("temperature")),
            temperature_threshold=_as_float(clean.get("temperature_threshold")),
            interval_seconds=_as_float(clean.get("interval_seconds")),
            update_target=UpdateTarget.parse(clean.get("update_target")),
            occurred_at=parse_datetime(clean.get("occurred_at")),
        )
