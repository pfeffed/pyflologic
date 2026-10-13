"""An in-process stand-in for FloLogic's Web API.

Shaped after what the live API returned, not only after the documentation:
the paging envelope is PascalCase, an unknown VID in a status read is an empty
list rather than a 404, and ``/Token`` explains a rejected key in plain text.
Writes can be delayed, dropped or silently discarded, which is the behavior
the client's confirmation logic exists to handle.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

from aiohttp import web

API_KEY = "test-api-key"
COMPANY_ID = 102

MODE_VALUES = {"home": 1, "away": 2, "bypass": 4, "shutoff": 8}


def make_token(
    subject: str, *, lifetime: float = 86400.0, issued: float | None = None
) -> str:
    """Return a JWT-shaped token whose payload carries an ``exp`` claim."""
    now = time.time() if issued is None else issued

    def encode(value: dict[str, Any]) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        return raw.rstrip("=")

    header = encode({"alg": "HS256", "typ": "JWT"})
    payload = encode(
        {"sub": subject, "CompanyId": str(COMPANY_ID), "exp": int(now + lifetime)}
    )
    return f"{header}.{payload}.signature"


def make_status(vid: str, **overrides: Any) -> dict[str, Any]:
    """Build a status object shaped like a live WiFi Connect's."""
    status: dict[str, Any] = {
        "VID": vid,
        "Type": "WiFiConnect",
        "Online": True,
        "Address": "1 Example Lane, Springfield, IL, 62704, USA",
        "Mode": 1,
        "FlowState": 1,
        "DripRate": 4,
        "Temperature": 70,
        "LastFlowDateTime": "2026-10-13T15:40:10",
        "HomeTime": 60,
        "AwayTime": 0.1,
        "BypassTimeEnabled": True,
        "BypassTime": 241,
        "LowTemperatureAlertEnabled": True,
        "LowTemperatureAlertThreshold": 38,
        "LowTemperatureShutoffEnabled": True,
        "LowTemperatureShutoffThreshold": 36,
        "PreAlertNoticeTime": 5,
    }
    status.update(overrides)
    # Nullable properties are omitted rather than sent as null.
    return {key: value for key, value in status.items() if value is not None}


class FakeApi:
    """A configurable fake of the Web API."""

    def __init__(self) -> None:
        """Start with three valves, one user and one foreign callback."""
        self.url = ""
        self.valves: dict[str, dict[str, Any]] = {
            vid: make_status(vid) for vid in ("414B91", "93DBB1", "412D18")
        }
        self.users: list[dict[str, Any]] = [
            {
                "Id": 3616,
                "Email": "owner@example.com",
                "FirstName": "Pat",
                "LastName": "Owner",
                "Systems": ["414B91", "93DBB1", "412D18"],
            }
        ]
        self.callbacks: list[dict[str, Any]] = [
            {"CallbackURL": "https://other.example/hook?vid={vid}", "Active": True}
        ]

        self.token_lifetime = 86400.0
        self.valid_tokens: set[str] = set()
        self.token_requests = 0
        self.max_page_size = 100

        # OAuth state.
        self.oauth_client_id = "client-1"
        self.oauth_client_secret = "client-secret"
        self.auth_codes: set[str] = {"code-1"}
        self.refresh_tokens: set[str] = {"refresh-0"}
        self.refresh_counter = 0

        # Write behavior.
        self.apply_delay = 0.0
        self.drop_writes = 0
        """Accept this many upcoming writes with 200 and never apply them."""
        self.winter_sensitivity: float | None = None
        """Silently discard drip rates below this, as the cloud does."""
        self.fail_next: list[tuple[int, str]] = []
        """Status/body pairs to answer the next requests with, in order."""

        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.writes: list[tuple[str, dict[str, str]]] = []

        self.app = web.Application(middlewares=[self._middleware])
        self.app.router.add_get("/Token", self._token)
        self.app.router.add_post("/oauth/token", self._oauth_token)
        self.app.router.add_get("/System/All", self._system_all)
        self.app.router.add_get("/System/Status", self._system_status)
        self.app.router.add_put("/System/ValveMode", self._valve_mode)
        self.app.router.add_put("/System/FlowSensitivity", self._flow_sensitivity)
        self.app.router.add_get("/User/All", self._user_all)
        self.app.router.add_get("/User/Status", self._user_status)
        self.app.router.add_get("/Callbacks", self._callbacks_get)
        self.app.router.add_post("/Callbacks", self._callbacks_post)
        self.app.router.add_delete("/Callbacks", self._callbacks_delete)

    # --- helpers --------------------------------------------------------

    def issue_token(self) -> str:
        """Issue and remember a fresh access token."""
        token = make_token(f"t{len(self.valid_tokens)}", lifetime=self.token_lifetime)
        # Distinct even when issued in the same second.
        token = f"{token}{len(self.valid_tokens)}"
        self.valid_tokens.add(token)
        return token

    def revoke_tokens(self) -> None:
        """Invalidate every token issued so far, as an early expiry would."""
        self.valid_tokens.clear()

    def paths(self, method: str | None = None) -> list[str]:
        """Return the paths requested, optionally for one method only."""
        return [path for verb, path, _ in self.requests if method in (None, verb)]

    @web.middleware
    async def _middleware(
        self, request: web.Request, handler: Any
    ) -> web.StreamResponse:
        self.requests.append((request.method, request.path, dict(request.query)))
        if self.fail_next:
            status, body = self.fail_next.pop(0)
            return web.Response(status=status, text=body)
        if request.path not in ("/Token", "/oauth/token"):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer ") or header[7:] not in self.valid_tokens:
                return web.Response(
                    status=401,
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
        result: web.StreamResponse = await handler(request)
        return result

    @staticmethod
    def _problem(field: str, message: str) -> web.Response:
        return web.json_response(
            {
                "title": "One or more validation errors occurred.",
                "status": 400,
                "errors": {field: [message]},
            },
            status=400,
            content_type="application/problem+json",
        )

    def _page(self, request: web.Request, items: list[Any]) -> web.Response:
        page = max(1, int(request.query.get("page") or request.query.get("Page") or 1))
        size = int(request.query.get("pageSize") or request.query.get("PageSize") or 20)
        size = max(1, min(size, self.max_page_size))
        total_pages = max(1, -(-len(items) // size))
        chunk = items[(page - 1) * size : page * size]
        return web.json_response(
            {
                "Items": chunk,
                "Page": page,
                "PageSize": size,
                "TotalCount": len(items),
                "TotalPages": total_pages,
                "HasNextPage": page < total_pages,
                "HasPreviousPage": page > 1,
            }
        )

    def _vids(self, request: web.Request) -> list[str] | web.Response:
        raw = request.query.get("vid_list")
        if not raw:
            return self._problem("vid_list", "The vid_list field is required.")
        vids = [vid for vid in raw.split(",") if vid]
        if len(vids) > 10:
            return self._problem("vid_list", "At most 10 VIDs are allowed.")
        return vids

    def _apply(self, vid: str, field: str, value: Any) -> None:
        if self.apply_delay:
            asyncio.get_running_loop().call_later(
                self.apply_delay, self.valves[vid].__setitem__, field, value
            )
        else:
            self.valves[vid][field] = value

    # --- auth -----------------------------------------------------------

    async def _token(self, request: web.Request) -> web.Response:
        self.token_requests += 1
        key = request.headers.get("x-api-key")
        if not key:
            return self._problem("x-api-key", "The api_key field is required.")
        if request.query.get("company_id") != str(COMPANY_ID):
            return web.Response(status=401, text="Invalid company ID.")
        if key != API_KEY:
            return web.Response(status=401, text="Invalid API key.")
        return web.json_response({"Token": self.issue_token()})

    async def _oauth_token(self, request: web.Request) -> web.Response:
        form = await request.post()

        def error(code: str, description: str) -> web.Response:
            return web.json_response(
                {"error": code, "error_description": description}, status=400
            )

        if form.get("client_id") != self.oauth_client_id:
            return error("invalid_client", "Unknown client.")
        if form.get("client_secret") not in (None, self.oauth_client_secret):
            return error("invalid_client", "Wrong secret.")
        grant = form.get("grant_type")
        if grant == "authorization_code":
            code = str(form.get("code"))
            if code not in self.auth_codes:
                return error("invalid_grant", "Code is invalid or expired.")
            self.auth_codes.discard(code)
        elif grant == "refresh_token":
            refresh = str(form.get("refresh_token"))
            if refresh not in self.refresh_tokens:
                return error("invalid_grant", "Refresh token is invalid.")
            self.refresh_tokens.discard(refresh)
        else:
            return error("unsupported_grant_type", "Unknown grant.")
        self.refresh_counter += 1
        new_refresh = f"refresh-{self.refresh_counter}"
        self.refresh_tokens.add(new_refresh)
        return web.json_response(
            {
                "access_token": self.issue_token(),
                "token_type": "Bearer",
                "refresh_token": new_refresh,
            }
        )

    # --- systems --------------------------------------------------------

    async def _system_all(self, request: web.Request) -> web.Response:
        return self._page(request, list(self.valves))

    async def _system_status(self, request: web.Request) -> web.Response:
        vids = self._vids(request)
        if isinstance(vids, web.Response):
            return vids
        return web.json_response(
            [dict(self.valves[vid]) for vid in vids if vid in self.valves]
        )

    async def _write(
        self, request: web.Request, field: str, value: Any
    ) -> web.Response:
        vids = self._vids(request)
        if isinstance(vids, web.Response):
            return vids
        missing = [vid for vid in vids if vid not in self.valves]
        if missing:
            return web.Response(status=404, text=f"Device {missing[0]} not found.")
        self.writes.append((request.path, dict(request.query)))
        if self.drop_writes > 0:
            self.drop_writes -= 1
            return web.Response(status=200)
        for vid in vids:
            self._apply(vid, field, value)
        return web.Response(status=200)

    async def _valve_mode(self, request: web.Request) -> web.Response:
        raw = request.query.get("valve_mode", "")
        value = MODE_VALUES.get(raw.lower())
        if value is None and raw in {"1", "2", "4", "8"}:
            value = int(raw)
        if value is None:
            return self._problem("valve_mode", f"Invalid valve mode '{raw}'.")
        return await self._write(request, "Mode", value)

    async def _flow_sensitivity(self, request: web.Request) -> web.Response:
        try:
            drip = float(request.query.get("drip_rate", ""))
        except ValueError:
            return self._problem("drip_rate", "The drip_rate field is required.")
        if self.winter_sensitivity is not None and drip < self.winter_sensitivity:
            # Accepted, acknowledged, and never applied.
            self.writes.append((request.path, dict(request.query)))
            return web.Response(status=200)
        return await self._write(request, "DripRate", drip)

    # --- users ----------------------------------------------------------

    async def _user_all(self, request: web.Request) -> web.Response:
        summaries = [
            {key: user[key] for key in ("Id", "Email", "FirstName", "LastName")}
            for user in self.users
        ]
        return self._page(request, summaries)

    async def _user_status(self, request: web.Request) -> web.Response:
        wanted = request.query.get("user_id")
        for user in self.users:
            if str(user["Id"]) == wanted:
                return web.json_response(
                    [{key: value for key, value in user.items() if key != "Id"}]
                )
        return web.json_response([])

    # --- callbacks ------------------------------------------------------

    async def _callbacks_get(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(self.callbacks)

    async def _callbacks_post(self, request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, list):
            return web.Response(status=400, text="Expected a list.")
        self.callbacks = body
        return web.Response(status=200)

    async def _callbacks_delete(self, request: web.Request) -> web.Response:
        del request
        self.callbacks = []
        return web.Response(status=200)
