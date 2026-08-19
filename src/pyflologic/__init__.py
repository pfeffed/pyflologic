"""Async client for the FloLogic Connect leak-detection valve cloud service.

FloLogic has no public API. This library speaks the same SignalR protocol the
mobile app uses, reverse-engineered from its traffic, and it is not affiliated
with or endorsed by FloLogic.

Every account may hold several valves plus a G-Connect gateway, so the client
is account-scoped and takes a valve ID on each command::

    from pyflologic import ControlMode, DeviceIdentity, FloLogicClient

    async with FloLogicClient(
        email="you@example.com",
        password="...",
        device=DeviceIdentity.generate("Home Assistant"),
    ) as client:
        for valve_id, valve in client.account.controllable_valves.items():
            print(valve.name, valve.status, valve.is_water_flowing)
        await client.async_set_mode(valve_id, ControlMode.AWAY)
"""

from __future__ import annotations

from .client import FloLogicClient, ListenerCallback
from .const import (
    DEFAULT_HUB_URL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    MIN_POLL_INTERVAL,
)
from .enums import (
    CRITICAL_FLAGS,
    PROBLEM_PRIORITY,
    SHUTOFF_REASON_PRIORITY,
    WARNING_FLAGS,
    WATER_OFF_FLAGS,
    ControlMode,
    FlowState,
    NotificationSetting,
    ShutoffReason,
    ToggledSettingName,
    ValveMode,
)
from .exceptions import (
    FloLogicAuthError,
    FloLogicCommandError,
    FloLogicConnectionError,
    FloLogicError,
    FloLogicProtocolError,
    FloLogicTimeoutError,
    FloLogicValidationError,
    UnknownValveError,
)
from .models import (
    Account,
    DeviceIdentity,
    Notification,
    SchedulerEvent,
    ToggledSetting,
    User,
    Valve,
    ValveAccess,
)

__version__ = "0.4.1"

__all__ = [
    "CRITICAL_FLAGS",
    "DEFAULT_HUB_URL",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_REQUEST_TIMEOUT",
    "MIN_POLL_INTERVAL",
    "PROBLEM_PRIORITY",
    "SHUTOFF_REASON_PRIORITY",
    "WARNING_FLAGS",
    "WATER_OFF_FLAGS",
    "Account",
    "ControlMode",
    "DeviceIdentity",
    "FloLogicAuthError",
    "FloLogicClient",
    "FloLogicCommandError",
    "FloLogicConnectionError",
    "FloLogicError",
    "FloLogicProtocolError",
    "FloLogicTimeoutError",
    "FloLogicValidationError",
    "FlowState",
    "ListenerCallback",
    "Notification",
    "NotificationSetting",
    "SchedulerEvent",
    "ShutoffReason",
    "ToggledSetting",
    "ToggledSettingName",
    "UnknownValveError",
    "User",
    "Valve",
    "ValveAccess",
    "ValveMode",
    "__version__",
]
