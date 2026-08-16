"""Wire-protocol constants for the FloLogic cloud service.

FloLogic's mobile app talks to an ASP.NET SignalR Core hub. Every request is a
hub *invocation*, and every response comes back as a separate hub *event* --
the server never uses SignalR completion messages, so there is no invocation
ID to correlate a reply with its request. :mod:`pyflologic.signalr` compensates
by serializing one request at a time.
"""

from __future__ import annotations

DEFAULT_HUB_URL = "https://hub-cloudapps-prod.azurewebsites.net"
"""Production hub used by the FloLogic mobile app."""

HUB_PATH = "/signalr"
"""Path appended to the hub URL when the caller does not supply it."""

RECORD_SEPARATOR = "\x1e"
"""SignalR JSON protocol frame terminator (ASCII record separator)."""

# --- SignalR message types (JSON hub protocol) -------------------------------

MSG_INVOCATION = 1
MSG_STREAM_ITEM = 2
MSG_COMPLETION = 3
MSG_STREAM_INVOCATION = 4
MSG_CANCEL_INVOCATION = 5
MSG_PING = 6
MSG_CLOSE = 7

# --- Hub methods the client invokes ------------------------------------------

METHOD_LOGIN = "Login"
METHOD_REFRESH_VALVE_ARRAY = "RefreshValveArray"
METHOD_REQUEST_STATE_CHANGE = "RequestStateChange"
METHOD_REQUEST_USER_ACCESSES = "RequestUserAccesses"
METHOD_REQUEST_SCHEDULER_EVENTS = "RequestSchedulerEvents"
METHOD_REFRESH_NOTIFICATIONS = "RefreshValvesNotificationsHistory"

# --- Hub events the server sends ---------------------------------------------

EVENT_LOGGED_IN = "LoggedIn"
EVENT_VALVE_SENT = "ValveSent"
EVENT_VALVE_ARRAY_SENT = "ValveArraySent"
EVENT_STATE_CHANGE_RESULT = "StateChangeResult"
"""Named by the hub's API but never actually sent.

Tracing every frame across several successful mode changes showed the hub
answering ``RequestStateChange`` with nothing at all; the acknowledgement is a
``ValveSent`` push carrying the updated valve, about a second later. Waiting on
this event means every command appears to time out while in fact succeeding.
Kept here so the next person does not rediscover it the same way.
"""
EVENT_USER_ACCESSES_SENT = "UserAccessesSent"
EVENT_SCHEDULER_EVENTS_SENT = "SchedulerEventsSent"
EVENT_NOTIFICATIONS_HISTORY_SENT = "NotificationsHistorySent"
EVENT_ERROR = "ErrorOccured"  # FloLogic's spelling, not a typo on our side

# --- Timing defaults ----------------------------------------------------------

DEFAULT_REQUEST_TIMEOUT = 30.0
"""Seconds to wait for the hub event that answers a request."""

STATE_CHANGE_TIMEOUT = 45.0
"""State changes travel cloud -> valve -> cloud, so they need longer."""

DEFAULT_PING_INTERVAL = 15.0
"""Seconds between client keepalive pings, matching the SignalR default."""

DEFAULT_SERVER_TIMEOUT = 45.0
"""Drop the connection after this many seconds of total server silence."""

MIN_POLL_INTERVAL = 30.0
"""Smallest polling interval callers should use against the FloLogic cloud.

The library never polls on its own; this is advice for the caller. Polling
faster risks rate limiting or account lockout, and gains nothing when the
push connection is healthy.
"""

DEFAULT_POLL_INTERVAL = 300.0
"""Suggested fallback poll interval when push updates are also enabled."""

# --- Client-device identity ---------------------------------------------------

DEVICE_CODE_PREFIX = "AND-"
"""The app prefixes its device code by platform; we present as Android."""

OS_PLATFORM = "Android"
