"""The FloLogic account client.

One :class:`FloLogicClient` owns one FloLogic account and *all* of the valves
on it. FloLogic accounts routinely hold several valves -- and a G-Connect
gateway alongside them -- so valve identity is a parameter on every command
rather than something the client picks for you.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import aiohttp

from .const import (
    DEFAULT_HUB_URL,
    DEFAULT_PING_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    EVENT_ERROR,
    EVENT_LOGGED_IN,
    EVENT_NOTIFICATIONS_HISTORY_SENT,
    EVENT_SCHEDULER_EVENTS_SENT,
    EVENT_USER_ACCESSES_SENT,
    EVENT_VALVE_ARRAY_SENT,
    EVENT_VALVE_SENT,
    HUB_PATH,
    METHOD_LOGIN,
    METHOD_REFRESH_NOTIFICATIONS,
    METHOD_REFRESH_VALVE_ARRAY,
    METHOD_REQUEST_SCHEDULER_EVENTS,
    METHOD_REQUEST_STATE_CHANGE,
    METHOD_REQUEST_USER_ACCESSES,
    OS_PLATFORM,
    STATE_CHANGE_TIMEOUT,
)
from .enums import ControlMode, ToggledSettingName
from .exceptions import (
    FloLogicAuthError,
    FloLogicCommandError,
    FloLogicConnectionError,
    FloLogicError,
    FloLogicProtocolError,
    FloLogicValidationError,
    UnknownValveError,
)
from .models import (
    Account,
    DeviceIdentity,
    JsonDict,
    Notification,
    SchedulerEvent,
    User,
    Valve,
    ValveAccess,
)
from .signalr import SignalRConnection

_LOGGER = logging.getLogger(__name__)

_RECONNECT_BACKOFF = (5.0, 10.0, 20.0, 45.0, 90.0, 180.0, 300.0)
"""Reconnect delays in seconds; the last value repeats forever.

Retrying indefinitely is deliberate. A valve that silently stops reporting
after a long cloud outage is worse than one that keeps trying.
"""

ListenerCallback = Callable[[Account], None]

_COMMAND_POLL_SECONDS = 0.25
"""How often to re-read the cached valve while waiting for a command."""

_COMMAND_REFRESH_SECONDS = 5.0
"""How often to ask the cloud directly while waiting for a command.

The cached valve only advances when a push arrives, so waiting on it alone
means a missed push costs the entire timeout. Asking directly on a short
cycle bounds that to a few seconds -- measured against live hardware, the
cloud reflects a setting change about seven seconds after the command.
"""

_NUMERIC_TOLERANCE = 1e-6
"""FloLogic returns 4 where 4.0 was sent; that is not a disagreement."""

_SETTING_FIELDS: dict[str, str] = {
    "flow_sensitivity_oz_per_min": "dripRate",
    "home_limit_minutes": "homeIntervalTime",
    "away_limit_minutes": "awayIntervalTime",
    "bypass_minutes": "bypassTime",
    "auto_away_hours": "autoAwayTime",
    "low_temp_alert_f": "lowTemperatureAlert",
    "low_temp_shutoff_f": "lowTemperatureLimit",
    "pre_alert_minutes": "preAlertNoticeInterval",
    "no_flow_notice_seconds": "noFlowNoticeInterval",
    "temperature_offset_f": "temperatureOffset",
}

_TOGGLED_FIELDS: dict[ToggledSettingName, str] = {
    ToggledSettingName.AUTO_AWAY: "autoAwayTime",
    ToggledSettingName.DELAY_AWAY: "delayAwayIntervalTime",
    ToggledSettingName.WINTER_FLOW_SENSITIVITY: "winterModeTime",
    ToggledSettingName.GUEST_MODE: "guestModeTime",
    ToggledSettingName.LOW_TEMP_ALERT: "lowTemperatureAlert",
    ToggledSettingName.LOW_TEMP_SHUTOFF: "lowTemperatureLimit",
}


class FloLogicClient:
    """An authenticated connection to one FloLogic account.

    Typical use::

        async with FloLogicClient(
            email="you@example.com",
            password="...",
            device=DeviceIdentity.generate("Home Assistant"),
        ) as client:
            for valve_id, valve in client.valves.items():
                print(valve.name, valve.status)
            await client.async_set_mode(valve_id, ControlMode.AWAY)
    """

    def __init__(
        self,
        *,
        email: str,
        password: str,
        device: DeviceIdentity | None = None,
        session: aiohttp.ClientSession | None = None,
        hub_url: str = DEFAULT_HUB_URL,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        auto_reconnect: bool = True,
    ) -> None:
        """Configure a client without contacting FloLogic."""
        self._email = email
        self._password = password
        self._device = device or DeviceIdentity.generate()
        self._hub_url = _hub_url_with_path(hub_url)
        self._ping_interval = ping_interval
        self._auto_reconnect = auto_reconnect

        self._session = session
        self._owns_session = session is None

        self._connection: SignalRConnection | None = None
        self._connect_lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task[None] | None = None
        # An Event, not a bool: the reconnect loop reads it while another
        # task sets it, so it has to be re-read on every check.
        self._shutdown = asyncio.Event()
        self._relog_token = ""

        self._user: User | None = None
        self._valves: dict[str, Valve] = {}
        self._accesses: dict[str, ValveAccess] = {}
        self._listeners: list[ListenerCallback] = []
        self._last_error: list[Any] | None = None

    # --- properties -----------------------------------------------------

    @property
    def connected(self) -> bool:
        """Return whether the client currently holds a live hub connection."""
        return self._connection is not None and self._connection.connected

    @property
    def device(self) -> DeviceIdentity:
        """Return the client-device identity in use, for the caller to persist."""
        return self._device

    @property
    def user(self) -> User | None:
        """Return the logged-in user, or ``None`` before the first connect."""
        return self._user

    @property
    def valves(self) -> dict[str, Valve]:
        """Return every device on the account, keyed by valve ID.

        Includes G-Connect gateways; filter with :attr:`Valve.is_controllable`
        or use :attr:`Account.controllable_valves`.
        """
        return dict(self._valves)

    @property
    def account(self) -> Account:
        """Return a snapshot of everything the client currently knows."""
        if self._user is None:
            raise FloLogicError("not logged in to FloLogic yet")
        return Account(
            user=self._user,
            valves=dict(self._valves),
            accesses=dict(self._accesses),
        )

    def get_valve(self, valve_id: str) -> Valve:
        """Return one valve by ID, raising if the account does not have it."""
        try:
            return self._valves[valve_id]
        except KeyError:
            raise UnknownValveError(valve_id) from None

    # --- listeners ------------------------------------------------------

    def add_listener(self, callback: ListenerCallback) -> Callable[[], None]:
        """Register a callback for account updates; returns an unsubscribe."""
        self._listeners.append(callback)

        def _unsubscribe() -> None:
            with suppress(ValueError):
                self._listeners.remove(callback)

        return _unsubscribe

    def _notify(self) -> None:
        """Push the current account snapshot to every listener."""
        if self._user is None or not self._listeners:
            return
        account = self.account
        for callback in list(self._listeners):
            try:
                callback(account)
            except Exception:
                _LOGGER.exception("FloLogic listener raised")

    # --- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> Self:
        """Connect on entry to an ``async with`` block."""
        await self.async_connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Disconnect on exit from an ``async with`` block."""
        await self.async_disconnect()

    async def async_connect(self) -> None:
        """Open the hub connection, log in, and load the account's valves."""
        self._shutdown.clear()
        async with self._connect_lock:
            if self.connected and self._user is not None:
                return
            await self._async_open()
        self._notify()

    async def async_disconnect(self) -> None:
        """Close the connection and release anything this client owns."""
        self._shutdown.set()
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        await self._async_close_connection()
        await self._release_owned_session()

    async def _async_open(self) -> None:
        """Establish a connection and bring account state up to date."""
        await self._async_close_connection()
        session = self._ensure_session()
        connection = SignalRConnection(
            session=session,
            url=self._hub_url,
            headers=self._build_headers(),
            on_event=self._handle_event,
            on_disconnect=self._handle_disconnect,
            ping_interval=self._ping_interval,
        )
        try:
            await connection.async_connect()
            self._connection = connection
            await self._async_login(connection)
            await self._async_load_valves(connection)
        except BaseException:
            # A failed connect must not strand the socket or the HTTP session.
            # `async with client` cannot help here: when __aenter__ is what
            # raises, __aexit__ never runs, so cleanup has to happen inline.
            await connection.async_close()
            self._connection = None
            await self._release_owned_session()
            raise

    async def _async_close_connection(self) -> None:
        """Close the hub connection if one is open."""
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.async_close()

    async def _release_owned_session(self) -> None:
        """Close the HTTP session, but only if this client created it."""
        if self._owns_session and self._session is not None:
            if not self._session.closed:
                await self._session.close()
            self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the HTTP session, creating one if the caller supplied none."""
        if self._session is None or self._session.closed:
            if not self._owns_session:
                raise FloLogicConnectionError("the supplied aiohttp session is closed")
            self._session = aiohttp.ClientSession()
        return self._session

    def _build_headers(self) -> dict[str, str]:
        """Return the headers the FloLogic hub expects from a client device."""
        return {
            "userDeviceCode": self._device.code,
            "userDeviceToken": self._device.token,
            "relogToken": self._relog_token,
            "OsPlatform": OS_PLATFORM,
            "AppVer": "pyflologic",
            "DeviceName": self._device.name,
        }

    async def _async_login(self, connection: SignalRConnection) -> None:
        """Authenticate against the hub and remember the session token."""
        try:
            arguments = await connection.async_request(
                METHOD_LOGIN,
                EVENT_LOGGED_IN,
                self._email,
                self._password,
                self._device.name,
                None,
                error_event=EVENT_ERROR,
            )
        except FloLogicCommandError as err:
            raise FloLogicAuthError(f"FloLogic rejected the login: {err}") from err
        except FloLogicError as err:
            raise FloLogicAuthError(f"FloLogic login failed: {err}") from err

        payload = arguments[0] if arguments else None
        if not isinstance(payload, dict) or not payload.get("id"):
            raise FloLogicAuthError("FloLogic login did not return a user")
        user = User(payload)
        self._user = user
        # The relog token lets a later reconnect skip a full credential check.
        self._relog_token = user.relog_token or self._relog_token

    async def _async_load_valves(self, connection: SignalRConnection) -> None:
        """Fetch every valve on the account into the local cache."""
        assert self._user is not None
        arguments = await connection.async_request(
            METHOD_REFRESH_VALVE_ARRAY,
            EVENT_VALVE_ARRAY_SENT,
            self._user.raw,
            match=lambda args: bool(args) and isinstance(args[0], list),
        )
        payload = arguments[0] if arguments else []
        if not isinstance(payload, list):
            raise FloLogicProtocolError("ValveArraySent did not carry a list")
        self._valves = _index_valves(payload)

    # --- reconnection ---------------------------------------------------

    def _handle_disconnect(self) -> None:
        """React to the hub dropping the socket."""
        if self._shutdown.is_set() or not self._auto_reconnect:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        _LOGGER.debug("FloLogic connection lost, scheduling reconnect")
        self._reconnect_task = asyncio.create_task(self._async_reconnect())

    async def _async_reconnect(self) -> None:
        """Reconnect with capped exponential backoff, retrying indefinitely."""
        attempt = 0
        while not self._shutdown.is_set():
            delay = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
            # Jitter keeps several valves at one site from retrying in lockstep.
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            if self._shutdown.is_set():
                return
            try:
                async with self._connect_lock:
                    await self._async_open()
            except FloLogicAuthError:
                # Credentials will not fix themselves; stop and surface it.
                _LOGGER.error("FloLogic reconnect failed: credentials rejected")
                return
            except (FloLogicError, aiohttp.ClientError) as err:
                attempt += 1
                _LOGGER.debug("FloLogic reconnect attempt %s failed: %s", attempt, err)
                continue
            _LOGGER.debug("FloLogic reconnected after %s attempts", attempt + 1)
            self._notify()
            return

    # --- pushed events --------------------------------------------------

    def _handle_event(self, target: str, arguments: list[Any]) -> None:
        """Fold unsolicited hub events into the local cache."""
        if target == EVENT_ERROR:
            _LOGGER.warning("FloLogic reported an error: %s", arguments)
            self._last_error = arguments
            return
        if target == EVENT_VALVE_SENT and arguments:
            if isinstance(arguments[0], dict):
                self._merge_valves([arguments[0]])
            return
        if target == EVENT_VALVE_ARRAY_SENT and arguments:
            if isinstance(arguments[0], list):
                self._merge_valves(arguments[0])
            return

    def _merge_valves(self, payloads: Iterable[Any]) -> None:
        """Fold pushed valve data into the cache and notify listeners.

        Merging happens at two levels, and both matter:

        - Across valves, because a ``ValveSent`` push carries a single valve.
          Replacing the whole map would make every other valve vanish.
        - Within a valve, because a pushed payload is not the same shape as
          the one ``RefreshValveArray`` returns. A real push was observed
          nulling ``valveFriendlyName`` and rewriting ``combinedName``, so
          overwriting the cached payload wholesale loses detail the push was
          never trying to change.

        Nulls in a push are treated as "not included" rather than "cleared".
        That is the pragmatic reading: the valve that nulled its friendly name
        mid-session plainly still had one, and letting the null through would
        rename the device to its hex ID until the next full refresh.
        """
        updated = False
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            valve_id = str(payload.get("id", ""))
            if not valve_id:
                continue
            self._valves[valve_id] = self._merged_valve(valve_id, payload)
            updated = True
        if updated:
            self._notify()

    def _merged_valve(self, valve_id: str, payload: JsonDict) -> Valve:
        """Return the cached valve updated with everything the push carried."""
        cached = self._valves.get(valve_id)
        if cached is None:
            return Valve(payload)
        present = {key: value for key, value in payload.items() if value is not None}
        return Valve({**cached.raw, **present})

    # --- reads ----------------------------------------------------------

    async def async_refresh(self) -> dict[str, Valve]:
        """Re-read every valve on the account and return the fresh map."""
        connection = await self._async_ready()
        await self._async_load_valves(connection)
        self._notify()
        return dict(self._valves)

    async def async_refresh_accesses(self) -> dict[str, ValveAccess]:
        """Re-read the account's per-valve notification preferences."""
        connection = await self._async_ready()
        assert self._user is not None
        arguments = await connection.async_request(
            METHOD_REQUEST_USER_ACCESSES,
            EVENT_USER_ACCESSES_SENT,
            self._user.raw,
        )
        payload = arguments[0] if arguments else []
        if not isinstance(payload, list):
            return {}
        accesses = {}
        for row in payload:
            if isinstance(row, dict):
                access = ValveAccess(row)
                if access.valve_id:
                    accesses[access.valve_id] = access
        self._accesses = accesses
        return dict(accesses)

    async def async_fetch_scheduler(self, valve_id: str) -> list[SchedulerEvent]:
        """Return the scheduled mode changes stored for one valve."""
        self.get_valve(valve_id)
        connection = await self._async_ready()
        assert self._user is not None
        arguments = await connection.async_request(
            METHOD_REQUEST_SCHEDULER_EVENTS,
            EVENT_SCHEDULER_EVENTS_SENT,
            self._user.user_id,
            valve_id,
        )
        payload = arguments[0] if arguments else []
        if not isinstance(payload, list):
            return []
        return [SchedulerEvent(row) for row in payload if isinstance(row, dict)]

    async def async_fetch_notifications(
        self, valve_ids: Iterable[str] | None = None
    ) -> list[Notification]:
        """Return notification history for the given valves, or for all of them."""
        connection = await self._async_ready()
        assert self._user is not None
        requested = list(valve_ids) if valve_ids is not None else list(self._valves)
        arguments = await connection.async_request(
            METHOD_REFRESH_NOTIFICATIONS,
            EVENT_NOTIFICATIONS_HISTORY_SENT,
            self._user.user_id,
            requested,
        )
        payload = arguments[0] if arguments else []
        if not isinstance(payload, list):
            return []
        return [Notification(row) for row in payload if isinstance(row, dict)]

    # --- writes ---------------------------------------------------------

    async def async_set_mode(
        self,
        valve_id: str,
        mode: ControlMode | str,
        *,
        refresh: bool = False,
        timeout: float = STATE_CHANGE_TIMEOUT,
    ) -> None:
        """Put one valve into a control mode.

        ``ControlMode.SHUTOFF`` closes the valve; ``ControlMode.HOME`` or
        ``AWAY`` reopens it with the corresponding flow limit.
        """
        control_mode = ControlMode(mode)
        await self.async_send_command(
            valve_id,
            {"mode": int(control_mode.flag)},
            refresh=refresh,
            timeout=timeout,
        )

    async def async_update_settings(
        self,
        valve_id: str,
        *,
        flow_sensitivity_oz_per_min: float | None = None,
        home_limit_minutes: float | None = None,
        away_limit_minutes: float | None = None,
        bypass_minutes: float | None = None,
        auto_away_hours: float | None = None,
        low_temp_alert_f: float | None = None,
        low_temp_shutoff_f: float | None = None,
        pre_alert_minutes: float | None = None,
        no_flow_notice_seconds: float | None = None,
        temperature_offset_f: float | None = None,
        refresh: bool = False,
    ) -> None:
        """Change one or more of a valve's settings in a single command.

        Omitted settings are left alone. Raises if nothing was passed, since a
        settings call with no settings is always a caller bug.
        """
        supplied = {
            "flow_sensitivity_oz_per_min": flow_sensitivity_oz_per_min,
            "home_limit_minutes": home_limit_minutes,
            "away_limit_minutes": away_limit_minutes,
            "bypass_minutes": bypass_minutes,
            "auto_away_hours": auto_away_hours,
            "low_temp_alert_f": low_temp_alert_f,
            "low_temp_shutoff_f": low_temp_shutoff_f,
            "pre_alert_minutes": pre_alert_minutes,
            "no_flow_notice_seconds": no_flow_notice_seconds,
            "temperature_offset_f": temperature_offset_f,
        }
        fields = {
            _SETTING_FIELDS[name]: value
            for name, value in supplied.items()
            if value is not None
        }
        if not fields:
            raise FloLogicError("no settings were supplied to update")
        self._validate_settings(valve_id, fields)
        await self.async_send_command(valve_id, fields, refresh=refresh)

    def _validate_settings(self, valve_id: str, fields: JsonDict) -> None:
        """Refuse writes FloLogic accepts and then ignores.

        Confirmed against live hardware: a flow sensitivity below the winter
        flow sensitivity is discarded without any acknowledgement. Winter mode
        is the *higher* sensitivity, so a lower normal threshold contradicts
        it -- but the cloud says nothing, and the caller sees a command that
        times out for no visible reason. The check only runs on the typed
        settings API; `async_send_command` stays a raw escape hatch.

        Note the rule binds in one direction only. Raising the winter
        sensitivity above the flow sensitivity *is* accepted, which is how a
        valve ends up unable to have its flow sensitivity written at all.
        """
        requested = fields.get("dripRate")
        if requested is None:
            return
        winter = self.get_valve(valve_id).winter_flow_sensitivity.configured
        if winter is None or requested >= winter:
            return
        raise FloLogicValidationError(
            f"flow sensitivity {requested} is below the winter flow "
            f"sensitivity {winter}; FloLogic ignores such a change without "
            f"reporting it. Lower the winter flow sensitivity first."
        )

    async def async_set_toggled_setting(
        self,
        valve_id: str,
        setting: ToggledSettingName | str,
        *,
        enabled: bool | None = None,
        value: float | None = None,
    ) -> None:
        """Switch a sign-encoded setting on or off, or change its value.

        FloLogic stores these as one signed number: the sign is the switch and
        the magnitude is the value. Writing only half of that is not possible
        on the wire, so whichever half is not supplied is read from the valve's
        current state and preserved -- turning Auto Away off keeps its 18
        hours, exactly as the app does.

        Raises if there is no value to write and none is supplied, rather than
        inventing one: a zero would read back as "not configured" and quietly
        lose whatever the user had set.
        """
        name = ToggledSettingName(setting)
        valve = self.get_valve(valve_id)
        current = getattr(valve, name.value)

        target_enabled = current.enabled if enabled is None else enabled
        magnitude = current.configured if value is None else abs(value)
        if magnitude is None:
            raise FloLogicError(
                f"{name.value} has no configured value on valve {valve_id}; "
                "supply one to set it"
            )

        signed = magnitude if target_enabled else -magnitude
        await self.async_send_command(valve_id, {_TOGGLED_FIELDS[name]: signed})

    async def async_send_command(
        self,
        valve_id: str,
        fields: JsonDict,
        *,
        refresh: bool = False,
        verify: bool = True,
        timeout: float = STATE_CHANGE_TIMEOUT,
    ) -> None:
        """Send a raw state-change command for one valve.

        The escape hatch for FloLogic fields this library has not modeled. Use
        :meth:`async_set_mode` or :meth:`async_update_settings` when they cover
        what you need.

        Returns once the valve reports the requested fields. Pass
        ``verify=False`` for a field the valve normalizes rather than echoing
        back, which would otherwise never appear to confirm.
        """
        valve = self.get_valve(valve_id)
        connection = await self._async_ready()
        assert self._user is not None

        if verify and _fields_match(valve.raw, fields):
            # Already there. FloLogic pushes nothing when a command changes
            # nothing, so waiting for confirmation would hang for the full
            # timeout on what is really a no-op.
            return

        command = {
            "active": True,
            "created": datetime.now(UTC).isoformat(),
            "userId": self._user.raw.get("id"),
            "valveId": valve.raw.get("id"),
            **fields,
        }
        self._last_error = None
        await connection.async_send(
            METHOD_REQUEST_STATE_CHANGE, self._user.raw, valve.raw, command
        )
        if verify:
            await self._async_await_command(valve_id, fields, timeout)
        if refresh:
            await self.async_refresh()

    async def _async_await_command(
        self, valve_id: str, fields: JsonDict, timeout: float
    ) -> None:
        """Wait until the valve itself reports the change, or give up.

        FloLogic never sends the ``StateChangeResult`` event its hub method
        implies -- confirmed by tracing every frame across several successful
        commands. What it does send is a ``ValveSent`` push carrying the
        updated valve, typically inside a second. So the valve's own state is
        the acknowledgement, which is the better thing to wait on anyway: it
        answers "did the valve do it" rather than "did the server accept it".

        Pushes are not guaranteed, though, so this also asks the cloud on a
        short cycle rather than trusting them for the whole timeout. Waiting
        on the cache alone was observed reporting failure for commands that
        had in fact landed: the push went missing, nothing else was consulted
        until the timeout expired, and by then the single fallback read came
        too late to matter.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # Short timeouts still get several chances to ask.
        interval = min(_COMMAND_REFRESH_SECONDS, max(timeout / 3, 0.5))
        next_refresh = loop.time() + interval
        while loop.time() < deadline:
            await asyncio.sleep(_COMMAND_POLL_SECONDS)
            cached = self._valves.get(valve_id)
            if cached is not None and _fields_match(cached.raw, fields):
                return
            if self._last_error is not None:
                raise FloLogicCommandError(
                    f"FloLogic reported an error: {self._last_error}"
                )
            if loop.time() >= next_refresh:
                next_refresh = loop.time() + interval
                with suppress(FloLogicError):
                    await self.async_refresh()

        # Out of time. One last direct read before calling it a failure.
        with suppress(FloLogicError):
            await self.async_refresh()
        cached = self._valves.get(valve_id)
        if cached is not None and _fields_match(cached.raw, fields):
            return
        raise FloLogicCommandError(
            f"valve {valve_id} did not report {fields} within {timeout:g}s"
        )

    # --- internals ------------------------------------------------------

    async def _async_ready(self) -> SignalRConnection:
        """Return a live connection, connecting first if necessary.

        Refuses to open one after :meth:`async_disconnect`. Without that, an
        operation still in flight during shutdown reconnects on its way out
        and strands a live socket that goes on pinging forever -- the shape
        this takes downstream is an integration that leaks a connection every
        time it is reloaded mid-command.
        """
        if self._shutdown.is_set():
            raise FloLogicConnectionError("client has been disconnected")
        if self.connected and self._connection is not None and self._user is not None:
            return self._connection
        async with self._connect_lock:
            if not (self.connected and self._user is not None):
                await self._async_open()
        assert self._connection is not None
        return self._connection


def _hub_url_with_path(hub_url: str) -> str:
    """Append the SignalR path unless the caller already included it."""
    url = hub_url.rstrip("/")
    if url.lower().endswith(HUB_PATH):
        return url
    return f"{url}{HUB_PATH}"


def _index_valves(payload: Iterable[Any]) -> dict[str, Valve]:
    """Build a valve-ID-keyed map from a raw device array."""
    valves: dict[str, Valve] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        valve = Valve(row)
        if valve.valve_id:
            valves[valve.valve_id] = valve
    return valves


__all__ = ["DEFAULT_REQUEST_TIMEOUT", "FloLogicClient", "ListenerCallback"]


def _fields_match(raw: JsonDict, fields: JsonDict) -> bool:
    """Return whether a valve payload already reflects every requested field.

    Numbers are compared numerically: FloLogic is inconsistent about returning
    ``4`` where ``4.0`` was sent, and a type mismatch is not a disagreement.
    """
    for key, expected in fields.items():
        actual = raw.get(key)
        if isinstance(expected, bool) or isinstance(actual, bool):
            if actual is not expected:
                return False
        elif isinstance(expected, int | float) and isinstance(actual, int | float):
            if abs(float(actual) - float(expected)) > _NUMERIC_TOLERANCE:
                return False
        elif actual != expected:
            return False
    return True
