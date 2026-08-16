"""A minimal ASP.NET Core SignalR client, scoped to what FloLogic uses.

Only the JSON hub protocol over websockets is implemented, because that is all
the FloLogic hub speaks. The pieces that matter for reliability -- handshake
validation, keepalive pings, and server-silence detection -- are all here;
without pings the hub drops idle sockets after about 30 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import suppress
from time import monotonic
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import (
    DEFAULT_PING_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SERVER_TIMEOUT,
    MSG_CLOSE,
    MSG_COMPLETION,
    MSG_INVOCATION,
    MSG_PING,
    RECORD_SEPARATOR,
)
from .exceptions import (
    FloLogicAuthError,
    FloLogicCommandError,
    FloLogicConnectionError,
    FloLogicProtocolError,
    FloLogicTimeoutError,
)

_LOGGER = logging.getLogger(__name__)

_HANDSHAKE = json.dumps({"protocol": "json", "version": 1}) + RECORD_SEPARATOR
_PING_FRAME = json.dumps({"type": MSG_PING}) + RECORD_SEPARATOR
_HANDSHAKE_TIMEOUT = 20.0
_HTTP_UNAUTHORIZED = (401, 403)
_HTTP_BAD_REQUEST = 400

EventCallback = Callable[[str, list[Any]], None]
MatchCallback = Callable[[list[Any]], bool]


class SignalRConnection:
    """One websocket connection to a SignalR hub.

    The connection is not self-healing; reconnection policy belongs to the
    caller, which knows what has to be re-established afterwards (a login, in
    FloLogic's case). See :class:`~pyflologic.client.FloLogicClient`.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str],
        on_event: EventCallback | None = None,
        on_disconnect: Callable[[], None] | None = None,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        server_timeout: float = DEFAULT_SERVER_TIMEOUT,
    ) -> None:
        """Configure a connection without opening it."""
        self._session = session
        self._url = url.rstrip("/")
        self._headers = headers
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._ping_interval = ping_interval
        self._server_timeout = server_timeout

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._buffer = ""
        self._last_message_at = monotonic()
        self._closing = False
        self._disconnect_reported = False

        # FloLogic answers requests with free-standing hub events rather than
        # SignalR completions, so a reply carries nothing tying it to its
        # request. One request at a time is the only correlation available.
        self._request_lock = asyncio.Lock()
        self._waiter: asyncio.Future[list[Any]] | None = None
        self._waiter_event: str | None = None
        self._waiter_error_event: str | None = None
        self._waiter_match: MatchCallback | None = None

    # --- lifecycle ------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Return whether the websocket is open and usable."""
        return self._ws is not None and not self._ws.closed and not self._closing

    async def async_connect(self) -> None:
        """Negotiate, open the websocket, and complete the SignalR handshake."""
        connection_id = await self._negotiate()
        ws_url = self._websocket_url(connection_id)
        try:
            self._ws = await self._session.ws_connect(ws_url, headers=self._headers)
        except aiohttp.ClientError as err:
            raise FloLogicConnectionError(f"websocket connect failed: {err}") from err

        self._buffer = ""
        self._closing = False
        self._disconnect_reported = False
        self._last_message_at = monotonic()

        await self._handshake()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def async_close(self) -> None:
        """Close the websocket and stop the background tasks."""
        self._closing = True
        for task in (self._ping_task, self._reader_task):
            if task is not None:
                task.cancel()
        for task in (self._ping_task, self._reader_task):
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        self._ping_task = None
        self._reader_task = None

        if self._ws is not None and not self._ws.closed:
            with suppress(aiohttp.ClientError, asyncio.TimeoutError):
                await self._ws.close()
        self._ws = None
        self._fail_waiter(FloLogicConnectionError("connection closed"))

    async def _negotiate(self) -> str:
        """POST to ``/negotiate`` and return the connection id to dial with."""
        try:
            async with self._session.post(
                f"{self._url}/negotiate",
                headers=self._headers,
                params={"negotiateVersion": "0"},
            ) as response:
                if response.status in _HTTP_UNAUTHORIZED:
                    raise FloLogicAuthError("FloLogic rejected the connection")
                if response.status >= _HTTP_BAD_REQUEST:
                    raise FloLogicConnectionError(
                        f"negotiate failed with HTTP {response.status}"
                    )
                # Azure has been seen replying text/plain here.
                payload = await response.json(content_type=None)
        except aiohttp.ClientError as err:
            raise FloLogicConnectionError(f"negotiate failed: {err}") from err
        except (json.JSONDecodeError, ValueError) as err:
            raise FloLogicProtocolError(f"negotiate returned non-JSON: {err}") from err

        if not isinstance(payload, dict):
            raise FloLogicProtocolError("negotiate returned an unexpected payload")
        token = payload.get("connectionToken") or payload.get("connectionId")
        if not token:
            raise FloLogicProtocolError("negotiate returned no connection id")
        return str(token)

    def _websocket_url(self, connection_id: str) -> str:
        """Return the ``wss://`` URL carrying the negotiated connection id."""
        base = self._url
        if base.startswith("https://"):
            base = f"wss://{base[len('https://') :]}"
        elif base.startswith("http://"):
            base = f"ws://{base[len('http://') :]}"
        return f"{base}?id={quote(connection_id, safe='')}"

    async def _handshake(self) -> None:
        """Send the protocol handshake and verify the hub accepted it.

        The original app ignores the handshake reply, which turns a rejected
        protocol into a mystery timeout 30 seconds later. Checking it makes the
        failure say what it is.
        """
        assert self._ws is not None
        await self._ws.send_str(_HANDSHAKE)
        try:
            raw = await asyncio.wait_for(self._read_frame(), timeout=_HANDSHAKE_TIMEOUT)
        except TimeoutError as err:
            raise FloLogicTimeoutError("hub did not answer the handshake") from err
        if raw is None:
            raise FloLogicConnectionError("hub closed during the handshake")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as err:
            raise FloLogicProtocolError(f"bad handshake response: {raw!r}") from err
        if isinstance(payload, dict) and payload.get("error"):
            raise FloLogicProtocolError(f"handshake rejected: {payload['error']}")

    # --- sending --------------------------------------------------------

    async def async_send(self, target: str, *arguments: Any) -> None:
        """Invoke a hub method without waiting for a reply."""
        if self._ws is None or not self.connected:
            raise FloLogicConnectionError("not connected to FloLogic")
        frame = {
            "type": MSG_INVOCATION,
            "target": target,
            "arguments": list(arguments),
        }
        try:
            await self._ws.send_str(
                json.dumps(frame, separators=(",", ":")) + RECORD_SEPARATOR
            )
        except (aiohttp.ClientError, ConnectionResetError) as err:
            raise FloLogicConnectionError(f"send failed: {err}") from err

    async def async_request(
        self,
        target: str,
        event: str,
        *arguments: Any,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        match: MatchCallback | None = None,
        error_event: str | None = None,
    ) -> list[Any]:
        """Invoke a hub method and return the arguments of the answering event.

        Requests are serialized: only one is in flight at a time. Pass ``match``
        to reject a frame that arrives on the right event name but is obviously
        an unrelated server push, and ``error_event`` to fail fast when the hub
        answers with an error event instead of the expected one.
        """
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[list[Any]] = loop.create_future()
            self._waiter = waiter
            self._waiter_event = event
            self._waiter_error_event = error_event
            self._waiter_match = match
            try:
                await self.async_send(target, *arguments)
                return await asyncio.wait_for(waiter, timeout)
            except TimeoutError as err:
                raise FloLogicTimeoutError(
                    f"FloLogic did not send {event} within {timeout:g}s"
                ) from err
            finally:
                self._waiter = None
                self._waiter_event = None
                self._waiter_error_event = None
                self._waiter_match = None

    # --- receiving ------------------------------------------------------

    async def _read_frame(self) -> str | None:
        """Return the next complete frame, or ``None`` if the socket closed."""
        while True:
            if (frame := self._take_buffered_frame()) is not None:
                return frame
            assert self._ws is not None
            message = await self._ws.receive()
            self._last_message_at = monotonic()
            if message.type is aiohttp.WSMsgType.TEXT:
                self._buffer += message.data
            elif message.type is aiohttp.WSMsgType.BINARY:
                self._buffer += message.data.decode("utf-8", errors="replace")
            else:
                return None

    def _take_buffered_frame(self) -> str | None:
        """Pop one complete frame out of the receive buffer, if there is one."""
        separator = self._buffer.find(RECORD_SEPARATOR)
        if separator == -1:
            return None
        frame, self._buffer = (
            self._buffer[:separator],
            self._buffer[separator + 1 :],
        )
        return frame

    def _drain_buffered_frames(self) -> Iterator[str]:
        """Yield every complete frame currently in the buffer."""
        while (frame := self._take_buffered_frame()) is not None:
            if frame:
                yield frame

    async def _read_loop(self) -> None:
        """Consume frames until the socket closes."""
        assert self._ws is not None
        try:
            async for message in self._ws:
                self._last_message_at = monotonic()
                if message.type is aiohttp.WSMsgType.TEXT:
                    self._buffer += message.data
                elif message.type is aiohttp.WSMsgType.BINARY:
                    self._buffer += message.data.decode("utf-8", errors="replace")
                else:
                    break
                for frame in self._drain_buffered_frames():
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, ConnectionResetError) as err:
            _LOGGER.debug("FloLogic read loop ended: %s", err)
        finally:
            self._report_disconnect()

    def _dispatch(self, raw_frame: str) -> None:
        """Decode one frame and route it."""
        try:
            frame = json.loads(raw_frame)
        except json.JSONDecodeError:
            _LOGGER.debug("Ignoring non-JSON FloLogic frame: %s", raw_frame)
            return
        if not isinstance(frame, dict):
            return

        message_type = frame.get("type")
        if message_type != MSG_INVOCATION:
            self._handle_control_frame(message_type, frame)
            return

        target = frame.get("target")
        if not isinstance(target, str) or not target:
            return
        arguments = frame.get("arguments")
        if not isinstance(arguments, list):
            arguments = []

        # Every hub event, named. Without this there is no way to tell "the
        # server sent nothing" from "the server sent something we ignored".
        _LOGGER.debug(
            "FloLogic event %s (%d args) while awaiting %s",
            target,
            len(arguments),
            self._waiter_event or "-",
        )

        # A frame that answered a pending request belongs to that request. Only
        # genuinely unsolicited events reach the listener, so callers can treat
        # `on_event` as "the hub told us something we did not ask for".
        if self._resolve_waiter(target, arguments):
            return
        if self._on_event is not None:
            try:
                self._on_event(target, arguments)
            except Exception:
                _LOGGER.exception("FloLogic event listener raised")

    def _handle_control_frame(self, message_type: Any, frame: dict[str, Any]) -> None:
        """Handle the non-invocation frame types the hub can send."""
        if message_type == MSG_PING:
            return  # Liveness only; _last_message_at was already refreshed.
        if message_type == MSG_CLOSE:
            _LOGGER.debug("FloLogic hub closed the connection: %s", frame.get("error"))
            self._closing = True
        elif message_type == MSG_COMPLETION:
            # FloLogic never sends these, but log rather than silently drop.
            _LOGGER.debug("Unexpected SignalR completion frame: %s", frame)

    def _resolve_waiter(self, target: str, arguments: list[Any]) -> bool:
        """Complete a pending request if this frame answers it.

        Returns whether the frame was consumed by a request.
        """
        waiter = self._waiter
        if waiter is None or waiter.done():
            return False
        if target == self._waiter_error_event:
            waiter.set_exception(
                FloLogicCommandError(f"hub reported an error: {arguments}")
            )
            return True
        if self._waiter_event != target:
            return False
        if self._waiter_match is not None and not self._waiter_match(arguments):
            return False
        waiter.set_result(arguments)
        return True

    def _fail_waiter(self, error: Exception) -> None:
        """Fail a pending request because the connection went away."""
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_exception(error)

    # --- keepalive ------------------------------------------------------

    async def _ping_loop(self) -> None:
        """Ping the hub, and give up on it if it has gone quiet."""
        while True:
            await asyncio.sleep(self._ping_interval)
            if self._ws is None or self._ws.closed:
                return
            if monotonic() - self._last_message_at > self._server_timeout:
                _LOGGER.debug(
                    "FloLogic hub silent for over %.0fs, dropping connection",
                    self._server_timeout,
                )
                # Closing here makes the read loop exit and report a disconnect,
                # which is what triggers the client's reconnect.
                await self._ws.close()
                return
            try:
                await self._ws.send_str(_PING_FRAME)
            except (aiohttp.ClientError, ConnectionResetError):
                return

    def _report_disconnect(self) -> None:
        """Notify the owner exactly once that the connection is gone."""
        self._fail_waiter(FloLogicConnectionError("connection lost"))
        if self._disconnect_reported or self._closing:
            return
        self._disconnect_reported = True
        if self._on_disconnect is not None:
            try:
                self._on_disconnect()
            except Exception:
                _LOGGER.exception("FloLogic disconnect callback raised")
