"""The Web API client.

One :class:`FloLogicClient` holds one set of credentials and every valve they
can see. With an API key that is the whole company; with an OAuth token it is
one user's devices. Either way, VIDs are a parameter on every command rather
than something the client picks for you.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any, Self

import aiohttp

from .auth import TokenProvider, _error_text, read_body
from .const import (
    DEFAULT_BASE_URL,
    DEFAULT_REQUEST_TIMEOUT,
    MAX_PAGE_SIZE,
    MAX_VIDS_PER_REQUEST,
    PATH_CALLBACKS,
    PATH_SYSTEM_ALL,
    PATH_SYSTEM_FLOW_SENSITIVITY,
    PATH_SYSTEM_STATUS,
    PATH_SYSTEM_VALVE_MODE,
    PATH_USER_ALL,
    PATH_USER_STATUS,
)
from .enums import ControlMode, ValveMode
from .exceptions import (
    FloLogicApiError,
    FloLogicAuthError,
    FloLogicCommandError,
    FloLogicConnectionError,
    FloLogicNotFoundError,
    FloLogicProtocolError,
    FloLogicValidationError,
    UnknownValveError,
)
from .models import CallbackDefinition, Page, User, Valve

__all__ = ["DEFAULT_COMMAND_TIMEOUT", "FloLogicClient"]

_LOGGER = logging.getLogger(__name__)

DEFAULT_COMMAND_TIMEOUT = 60.0
"""Seconds a write may take, from request to the valve reporting the change."""

DEFAULT_DISCOVERY_INTERVAL = 3600.0
"""How often :meth:`FloLogicClient.async_refresh` re-lists the account's VIDs.

Valves join and leave an account rarely; listing them on every poll would
double the request count to learn nothing.
"""

_CONFIRM_POLL_SECONDS = 3.0
"""How often to re-read status while waiting for a write to land."""

_COMMAND_ATTEMPTS = 3
"""How many times to send a write before reporting it failed.

On the app protocol, FloLogic was observed accepting a command and not
applying it, with no error of any kind, and a plain resend succeeding. Both
writes here set a value, so resending one that did land changes nothing.
"""

_NUMERIC_TOLERANCE = 1e-6
"""FloLogic returns 4 where 4.0 was sent; that is not a disagreement."""


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    """Yield ``items`` in consecutive slices of at most ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _vid_list(vids: str | Iterable[str]) -> list[str]:
    """Normalize one VID or several into a de-duplicated list."""
    raw = [vids] if isinstance(vids, str) else list(vids)
    seen: dict[str, None] = {}
    for vid in raw:
        cleaned = vid.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    if not seen:
        raise FloLogicValidationError("no VIDs were supplied")
    return list(seen)


def _format_number(value: float) -> str:
    """Render a number for a query string without float noise."""
    return f"{value:g}"


def mode_reflects(valve: Valve, mode: ControlMode) -> bool:
    """Return whether ``valve`` shows that it has taken up ``mode``.

    Looser than an equality test on purpose. Shutoff counts as confirmed by
    any water-off condition, since the water is off either way. Away counts
    as confirmed by Delay Away too: a valve with Delay Away configured sits in
    that state for its delay before Away begins, which is the valve doing what
    it was told rather than refusing.
    """
    flags = valve.mode
    if mode is ControlMode.SHUTOFF:
        return valve.is_water_off
    if valve.is_water_off:
        return False
    if mode is ControlMode.AWAY:
        return bool(flags & (ValveMode.AWAY | ValveMode.DELAY_AWAY))
    return bool(flags & mode.flag)


def _drip_rate_reflects(valve: Valve, value: float) -> bool:
    """Return whether ``valve`` reports the requested flow sensitivity."""
    current = valve.flow_sensitivity_oz_per_min
    return current is not None and abs(current - value) <= _NUMERIC_TOLERANCE


class FloLogicClient:
    """A client for FloLogic's Web API.

    Typical use::

        async with FloLogicClient(ApiKeyAuth(api_key, company_id)) as client:
            valves = await client.async_refresh()
            for vid, valve in valves.items():
                print(vid, valve.status, valve.flow_state)
            await client.async_set_mode(vid, ControlMode.AWAY)

    There is no push channel in the Web API itself. Poll
    :meth:`async_refresh` -- no faster than
    :data:`~pyflologic.const.MIN_POLL_INTERVAL` -- or register a callback
    (see :mod:`pyflologic.webhook`) and refresh when one arrives.
    """

    def __init__(
        self,
        auth: TokenProvider,
        *,
        session: aiohttp.ClientSession | None = None,
        base_url: str = DEFAULT_BASE_URL,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        discovery_interval: float = DEFAULT_DISCOVERY_INTERVAL,
    ) -> None:
        """Configure a client without contacting FloLogic."""
        self._auth = auth
        self._session = session
        self._owns_session = session is None
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._discovery_interval = discovery_interval

        self._vids: list[str] | None = None
        self._discovered_at = 0.0
        self._valves: dict[str, Valve] = {}

    # --- properties -----------------------------------------------------

    @property
    def auth(self) -> TokenProvider:
        """Return the token provider in use."""
        return self._auth

    @property
    def valves(self) -> dict[str, Valve]:
        """Return the valves from the most recent read, keyed by VID."""
        return dict(self._valves)

    def get_valve(self, vid: str) -> Valve:
        """Return one valve by VID, raising if it has not been read."""
        try:
            return self._valves[vid]
        except KeyError:
            raise UnknownValveError(vid) from None

    # --- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> Self:
        """Enter an ``async with`` block; nothing is fetched until asked."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the HTTP session on exit, if this client created it."""
        await self.async_close()

    async def async_close(self) -> None:
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

    # --- transport ------------------------------------------------------

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Make one authenticated request and return the decoded body.

        A 401 is retried once with a forced token renewal: the cached token
        can be revoked or expire early, and only a second rejection means the
        credentials themselves are bad.
        """
        session = self._ensure_session()
        url = f"{self._base_url}{path}"
        for attempt in range(2):
            token = await self._auth.async_get_token(
                session, self._base_url, force=attempt > 0
            )
            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self._timeout,
                ) as response:
                    body = await read_body(response)
                    status = response.status
            except (TimeoutError, aiohttp.ClientError) as err:
                raise FloLogicConnectionError(
                    f"{method} {path} failed: {err or type(err).__name__}"
                ) from err

            if status == 401:  # noqa: PLR2004
                if attempt == 0:
                    _LOGGER.debug("FloLogic rejected the token; renewing it")
                    continue
                raise FloLogicAuthError(_error_text(body, "token rejected"))
            if status == 404:  # noqa: PLR2004
                raise FloLogicNotFoundError(status, _error_text(body, "not found"))
            if status >= 400:  # noqa: PLR2004
                raise FloLogicApiError(status, _error_text(body, "request failed"))
            return body
        raise AssertionError("unreachable")  # pragma: no cover

    # --- devices --------------------------------------------------------

    async def async_list_vids_page(
        self, page: int = 1, page_size: int = MAX_PAGE_SIZE
    ) -> Page[str]:
        """Return one page of the VIDs these credentials can see."""
        body = await self._async_request(
            "GET",
            PATH_SYSTEM_ALL,
            params={"page": page, "pageSize": min(page_size, MAX_PAGE_SIZE)},
        )
        if not isinstance(body, dict):
            raise FloLogicProtocolError("/System/All did not return a page")
        return Page.parse(body, lambda item: str(item) if item else None)

    async def async_list_vids(self) -> list[str]:
        """Return every VID these credentials can see, across all pages."""
        vids: list[str] = []
        page = 1
        while True:
            result = await self.async_list_vids_page(page)
            vids.extend(vid for vid in result.items if vid not in vids)
            if not result.has_next_page or page >= result.total_pages:
                return vids
            page += 1

    async def async_get_valves(self, vids: str | Iterable[str]) -> dict[str, Valve]:
        """Read the status of the given valves, ten per request.

        A VID FloLogic does not recognize is simply absent from the result --
        the API answers an unknown VID with an empty list, not an error.
        """
        wanted = _vid_list(vids)
        found: dict[str, Valve] = {}
        for chunk in _chunks(wanted, MAX_VIDS_PER_REQUEST):
            body = await self._async_request(
                "GET", PATH_SYSTEM_STATUS, params={"vid_list": ",".join(chunk)}
            )
            if not isinstance(body, list):
                raise FloLogicProtocolError("/System/Status did not return a list")
            for row in body:
                if isinstance(row, dict):
                    valve = Valve(row)
                    if valve.vid:
                        found[valve.vid] = valve
        self._valves.update(found)
        return found

    async def async_refresh(self, *, rediscover: bool = False) -> dict[str, Valve]:
        """Re-read every valve and return the fresh map.

        The VID list is fetched on the first call, then again whenever it is
        older than ``discovery_interval`` or ``rediscover`` is set. Valves that
        have left the account drop out of the result.
        """
        due = time.monotonic() - self._discovered_at >= self._discovery_interval
        if self._vids is None or rediscover or due:
            self._vids = await self.async_list_vids()
            self._discovered_at = time.monotonic()
        if not self._vids:
            self._valves = {}
            return {}
        found = await self.async_get_valves(self._vids)
        self._valves = found
        return dict(found)

    # --- writes ---------------------------------------------------------

    async def async_set_mode(
        self,
        vids: str | Iterable[str],
        mode: ControlMode | str,
        *,
        confirm: bool = True,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        """Put one or more valves into a mode.

        ``SHUTOFF`` closes the valve; ``HOME`` or ``AWAY`` reopens it with the
        corresponding flow limit. ``DISABLED`` is refused here because the Web
        API does not accept it.

        By default this returns only once every valve reports the new mode,
        resending to any that have not; pass ``confirm=False`` to return as
        soon as FloLogic accepts the request.
        """
        control_mode = ControlMode(mode)
        if not control_mode.settable:
            raise FloLogicValidationError(
                f"the Web API cannot set {control_mode.value!r}; it accepts "
                "home, away, bypass and shutoff"
            )
        await self._async_command(
            PATH_SYSTEM_VALVE_MODE,
            _vid_list(vids),
            {"valve_mode": control_mode.api_value},
            lambda valve: mode_reflects(valve, control_mode),
            describe=f"mode {control_mode.value}",
            confirm=confirm,
            timeout=timeout,
        )

    async def async_set_flow_sensitivity(
        self,
        vids: str | Iterable[str],
        oz_per_min: float,
        *,
        confirm: bool = True,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        """Set the drip rate -- flow sensitivity -- in ounces per minute.

        On the app protocol FloLogic silently discards a flow sensitivity
        below the valve's *winter* flow sensitivity, and the Web API does not
        expose the winter setting, so it cannot be checked here. If this
        raises :class:`FloLogicCommandError` with FloLogic having accepted the
        request, that constraint is the likeliest reason.
        """
        if oz_per_min <= 0:
            raise FloLogicValidationError("flow sensitivity must be positive")
        await self._async_command(
            PATH_SYSTEM_FLOW_SENSITIVITY,
            _vid_list(vids),
            {"drip_rate": _format_number(oz_per_min)},
            lambda valve: _drip_rate_reflects(valve, oz_per_min),
            describe=f"flow sensitivity {_format_number(oz_per_min)}",
            confirm=confirm,
            timeout=timeout,
        )

    async def _async_command(
        self,
        path: str,
        vids: list[str],
        params: dict[str, str],
        reflects: Callable[[Valve], bool],
        *,
        describe: str,
        confirm: bool,
        timeout: float,
    ) -> None:
        """Send a write and, if asked, wait for every valve to report it.

        A 200 from FloLogic means the request was accepted, not that the
        valve acted on it, so confirmation reads the valves back. Valves that
        have not reported the change are sent the command again, and the
        retries share the caller's timeout rather than multiplying it.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        pending = list(vids)
        share = timeout / _COMMAND_ATTEMPTS

        for attempt in range(_COMMAND_ATTEMPTS):
            for chunk in _chunks(pending, MAX_VIDS_PER_REQUEST):
                await self._async_request(
                    "PUT", path, params={"vid_list": ",".join(chunk), **params}
                )
            if not confirm:
                return
            attempt_deadline = min(deadline, loop.time() + share)
            pending = await self._async_await_reflected(
                pending, reflects, attempt_deadline
            )
            if not pending:
                return
            if loop.time() >= deadline:
                break
            _LOGGER.debug(
                "FloLogic accepted %s but %s did not report it (attempt %s)",
                describe,
                pending,
                attempt + 1,
            )

        raise FloLogicCommandError(
            f"FloLogic accepted {describe}, but {', '.join(pending)} did not "
            f"report it within {timeout:g}s"
        )

    async def _async_await_reflected(
        self, vids: list[str], reflects: Callable[[Valve], bool], deadline: float
    ) -> list[str]:
        """Poll until every valve reflects the change or time runs out.

        Returns the VIDs still not reporting it.
        """
        loop = asyncio.get_running_loop()
        pending = list(vids)
        while pending:
            await asyncio.sleep(
                max(0.0, min(_CONFIRM_POLL_SECONDS, deadline - loop.time()))
            )
            try:
                found = await self.async_get_valves(pending)
            except FloLogicConnectionError as err:
                # One failed read is not a failed command; keep waiting.
                _LOGGER.debug("Status read failed while confirming: %s", err)
                found = {}
            pending = [
                vid for vid in pending if vid not in found or not reflects(found[vid])
            ]
            if loop.time() >= deadline:
                break
        return pending

    # --- users ----------------------------------------------------------

    async def async_list_users_page(
        self, page: int = 1, page_size: int = MAX_PAGE_SIZE
    ) -> Page[User]:
        """Return one page of the users these credentials can see."""
        body = await self._async_request(
            "GET",
            PATH_USER_ALL,
            params={"page": page, "pageSize": min(page_size, MAX_PAGE_SIZE)},
        )
        if not isinstance(body, dict):
            raise FloLogicProtocolError("/User/All did not return a page")
        return Page.parse(
            body, lambda item: User(item) if isinstance(item, dict) else None
        )

    async def async_list_users(self) -> list[User]:
        """Return every user these credentials can see, across all pages."""
        users: list[User] = []
        page = 1
        while True:
            result = await self.async_list_users_page(page)
            users.extend(result.items)
            if not result.has_next_page or page >= result.total_pages:
                return users
            page += 1

    async def async_get_user(self, user_id: int) -> User | None:
        """Return one user's details and VIDs, or ``None`` if unknown."""
        try:
            body = await self._async_request(
                "GET", PATH_USER_STATUS, params={"user_id": user_id}
            )
        except FloLogicNotFoundError:
            return None
        rows = body if isinstance(body, list) else [body]
        for row in rows:
            if isinstance(row, dict):
                return User({"Id": user_id, **row})
        return None

    # --- callbacks ------------------------------------------------------

    async def async_get_callbacks(self) -> list[CallbackDefinition]:
        """Return the company's callback definitions."""
        body = await self._async_request("GET", PATH_CALLBACKS)
        if body is None:
            return []
        if not isinstance(body, list):
            raise FloLogicProtocolError("/Callbacks did not return a list")
        return [
            definition
            for row in body
            if (definition := CallbackDefinition.from_json(row)) is not None
        ]

    async def async_set_callbacks(
        self, callbacks: Iterable[CallbackDefinition]
    ) -> None:
        """Replace the company's entire callback list.

        Callbacks belong to the company, not to this client: whatever else
        has registered one loses it unless it is in ``callbacks``. Prefer
        :meth:`async_add_callback` and :meth:`async_remove_callbacks`, which
        preserve the rest.
        """
        await self._async_request(
            "POST", PATH_CALLBACKS, json=[callback.to_json() for callback in callbacks]
        )

    async def async_delete_callbacks(self) -> None:
        """Remove every callback the company has, including other consumers'."""
        await self._async_request("DELETE", PATH_CALLBACKS)

    async def async_add_callback(
        self, callback: CallbackDefinition
    ) -> list[CallbackDefinition]:
        """Add or update one callback, keeping every other definition.

        A definition with the same URL is replaced. The list is read and
        written in two requests, so a concurrent change by another consumer
        between them can be lost; FloLogic offers nothing atomic.
        """
        current = await self.async_get_callbacks()
        updated = [existing for existing in current if existing.url != callback.url]
        updated.append(callback)
        if updated != current:
            await self.async_set_callbacks(updated)
        return updated

    async def async_remove_callbacks(
        self, match: str | Callable[[CallbackDefinition], bool]
    ) -> list[CallbackDefinition]:
        """Remove the callbacks that match, keeping every other definition.

        ``match`` is an exact URL, or a predicate for anything looser. Nothing
        is written when nothing matches.
        """
        predicate = (
            (lambda definition: definition.url == match)
            if isinstance(match, str)
            else match
        )
        current = await self.async_get_callbacks()
        kept = [definition for definition in current if not predicate(definition)]
        if len(kept) != len(current):
            await self.async_set_callbacks(kept)
        return kept
