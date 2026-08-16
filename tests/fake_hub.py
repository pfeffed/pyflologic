"""An in-process stand-in for the FloLogic SignalR hub.

Emulating the wire behavior -- negotiate, handshake, record-separated frames,
event-shaped replies -- means the tests exercise the real protocol path instead
of a mocked-out client.
"""

from __future__ import annotations

import json
from typing import Any

from aiohttp import WSMsgType, web

RECORD_SEPARATOR = "\x1e"

DEFAULT_USER: dict[str, Any] = {
    "id": "user-1",
    "email": "owner@example.com",
    "relogToken": "relog-abc",
}


def make_valve(
    valve_id: str,
    name: str,
    *,
    mode: int = 1,
    flow_state: int = 1,
    online: bool = True,
    gateway: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """Build a valve payload shaped like the ones FloLogic sends."""
    valve: dict[str, Any] = {
        "id": valve_id,
        "uuid": f"uuid-{valve_id}",
        "valveFriendlyName": name,
        "mode": mode,
        "flowState": flow_state,
        "online": online,
        "isZGateway": gateway,
        "deviceTypeName": "G-Connect Gateway" if gateway else "FloLogic Connect",
        "softwareVersion": "3.2.1",
        "currentFlow": 0,
        "temperature": 68.0,
        "batteryLevel": 92,
        "signalStrength": -55,
        "dripRate": 0.5,
        "homeIntervalTime": 30,
        "awayIntervalTime": 5,
        "bypassTime": 60,
        "autoAwayTime": 24,
        "lowTemperatureAlert": 45,
        "lowTemperatureLimit": 38,
        "preAlertNoticeInterval": 5,
        "noFlowNoticeInterval": 3600,
    }
    valve.update(extra)
    return valve


class FakeHub:
    """A configurable fake of the FloLogic cloud hub."""

    def __init__(self) -> None:
        """Start with one account holding two valves and a gateway."""
        self.user: dict[str, Any] = dict(DEFAULT_USER)
        self.valves: list[dict[str, Any]] = [
            make_valve("valve-1", "Main House"),
            make_valve("valve-2", "Guest House", mode=2),
            make_valve("gw-1", "Gateway", gateway=True),
        ]
        self.accesses: list[dict[str, Any]] = [
            {"valveId": "valve-1", "notificationsList": 0b1000100},
            {"valveId": "valve-2", "notificationsList": 0},
        ]
        self.scheduler: list[dict[str, Any]] = [
            {"valveId": "valve-1", "action": 1, "actionPayload": "{}"},
            {"valveId": "valve-1", "action": None, "actionPayload": None},
        ]
        self.notifications: list[dict[str, Any]] = [
            {
                "valveId": "valve-1",
                "message": "Leak detected",
                "created": "2026-01-01T00:00:00Z",
            }
        ]

        self.received: list[tuple[str, list[Any]]] = []
        self.negotiate_status = 200
        self.handshake_error: str | None = None
        self.reject_login = False
        self.login_sends_error_event = False
        self.silent_targets: set[str] = set()
        self.sockets: list[web.WebSocketResponse] = []
        self.connection_count = 0
        self.pings_received = 0
        self.url = ""  # filled in by the fixture once the server is up

        self.app = web.Application()
        self.app.router.add_post("/signalr/negotiate", self._handle_negotiate)
        self.app.router.add_get("/signalr", self._handle_websocket)

    # --- test helpers ---------------------------------------------------

    def valve(self, valve_id: str) -> dict[str, Any]:
        """Return the stored payload for one valve."""
        return next(valve for valve in self.valves if valve["id"] == valve_id)

    def invocations(self, target: str) -> list[list[Any]]:
        """Return the arguments of every invocation of ``target``."""
        return [args for name, args in self.received if name == target]

    async def push(self, target: str, *arguments: Any) -> None:
        """Send an unsolicited hub event to every connected client."""
        for socket in list(self.sockets):
            await self._send(socket, target, *arguments)

    async def push_batch(self, events: list[tuple[str, list[Any]]]) -> None:
        """Send several events packed into a single websocket message.

        SignalR allows this, and a client that assumes one frame per message
        silently drops everything after the first.
        """
        payload = "".join(
            json.dumps({"type": 1, "target": target, "arguments": args})
            + RECORD_SEPARATOR
            for target, args in events
        )
        for socket in list(self.sockets):
            await socket.send_str(payload)

    async def push_split(self, target: str, *arguments: Any) -> None:
        """Send one event cut in half across two websocket messages."""
        payload = (
            json.dumps({"type": 1, "target": target, "arguments": list(arguments)})
            + RECORD_SEPARATOR
        )
        middle = len(payload) // 2
        for socket in list(self.sockets):
            await socket.send_str(payload[:middle])
            await socket.send_str(payload[middle:])

    async def drop_connections(self) -> None:
        """Close every open socket, as the cloud does when it recycles."""
        for socket in list(self.sockets):
            await socket.close()

    # --- request handling -----------------------------------------------

    async def _handle_negotiate(self, request: web.Request) -> web.Response:
        """Answer the SignalR negotiate request."""
        del request
        if self.negotiate_status != 200:
            return web.Response(status=self.negotiate_status)
        return web.json_response({"connectionId": "conn-1", "availableTransports": []})

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Run one client connection."""
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.connection_count += 1
        self.sockets.append(socket)
        try:
            async for message in socket:
                if message.type is not WSMsgType.TEXT:
                    break
                for raw in message.data.split(RECORD_SEPARATOR):
                    if raw:
                        await self._handle_frame(socket, raw)
        finally:
            if socket in self.sockets:
                self.sockets.remove(socket)
        return socket

    async def _handle_frame(self, socket: web.WebSocketResponse, raw: str) -> None:
        """Route one decoded client frame."""
        frame = json.loads(raw)
        if "protocol" in frame:
            error = {"error": self.handshake_error} if self.handshake_error else {}
            await socket.send_str(json.dumps(error) + RECORD_SEPARATOR)
            return
        if frame.get("type") == 6:
            self.pings_received += 1
            return
        if frame.get("type") != 1:
            return

        target = frame.get("target", "")
        arguments = frame.get("arguments", [])
        self.received.append((target, arguments))
        if target in self.silent_targets:
            return
        await self._respond(socket, target, arguments)

    async def _respond(
        self,
        socket: web.WebSocketResponse,
        target: str,
        arguments: list[Any],
    ) -> None:
        """Send the event that answers one invocation."""
        if target == "Login":
            if self.reject_login:
                await self._send(socket, "ErrorOccured", "Invalid credentials")
            elif self.login_sends_error_event:
                await self._send(socket, "ErrorOccured", "boom")
            else:
                await self._send(socket, "LoggedIn", self.user)
        elif target == "RefreshValveArray":
            await self._send(socket, "ValveArraySent", self.valves)
        elif target == "RequestUserAccesses":
            await self._send(socket, "UserAccessesSent", self.accesses)
        elif target == "RequestSchedulerEvents":
            valve_id = arguments[1] if len(arguments) > 1 else None
            await self._send(
                socket,
                "SchedulerEventsSent",
                [row for row in self.scheduler if row["valveId"] == valve_id],
            )
        elif target == "RefreshValvesNotificationsHistory":
            wanted = arguments[1] if len(arguments) > 1 else []
            await self._send(
                socket,
                "NotificationsHistorySent",
                [row for row in self.notifications if row["valveId"] in wanted],
            )
        elif target == "RequestStateChange":
            await self._apply_state_change(socket, arguments)

    async def _apply_state_change(
        self, socket: web.WebSocketResponse, arguments: list[Any]
    ) -> None:
        """Mutate the stored valve the way the real cloud would, then confirm."""
        command = arguments[2] if len(arguments) > 2 else {}
        valve_id = command.get("valveId")
        stored = next((valve for valve in self.valves if valve["id"] == valve_id), None)
        if stored is None:
            await self._send(socket, "StateChangeResult", {"success": False})
            return
        for key, value in command.items():
            if key not in ("active", "created", "userId", "valveId"):
                stored[key] = value
        await self._send(socket, "StateChangeResult", {"success": True})

    async def _send(
        self, socket: web.WebSocketResponse, target: str, *arguments: Any
    ) -> None:
        """Write one hub event frame."""
        if socket.closed:
            return
        frame = {"type": 1, "target": target, "arguments": list(arguments)}
        await socket.send_str(json.dumps(frame) + RECORD_SEPARATOR)
