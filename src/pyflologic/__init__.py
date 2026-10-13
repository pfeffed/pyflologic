"""Async client for FloLogic's official Web API.

FloLogic Connect valves stop leaks by shutting off a building's water. This
library reads their status and changes their mode and flow sensitivity
through the Web API FloLogic publishes for integrators, and helps verify and
decode the callbacks it sends::

    from pyflologic import ApiKeyAuth, ControlMode, FloLogicClient

    async with FloLogicClient(ApiKeyAuth(api_key, company_id)) as client:
        for vid, valve in (await client.async_refresh()).items():
            print(vid, valve.status, valve.flow_state)
        await client.async_set_mode(vid, ControlMode.AWAY)

The Web API covers less of the device than the mobile app's own protocol;
``pyflologic-unofficial`` speaks that one.
"""

from __future__ import annotations

from .auth import (
    AccessTokenAuth,
    ApiKeyAuth,
    OAuthAuth,
    OAuthTokens,
    TokenProvider,
    async_exchange_code,
    build_authorize_url,
)
from .client import DEFAULT_COMMAND_TIMEOUT, FloLogicClient
from .const import (
    DEFAULT_BASE_URL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    MAX_VIDS_PER_REQUEST,
    MIN_POLL_INTERVAL,
)
from .enums import (
    CRITICAL_FLAGS,
    PROBLEM_PRIORITY,
    SETTABLE_MODES,
    SHUTOFF_REASON_PRIORITY,
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
from .exceptions import (
    FloLogicApiError,
    FloLogicAuthError,
    FloLogicCommandError,
    FloLogicConnectionError,
    FloLogicError,
    FloLogicNotFoundError,
    FloLogicProtocolError,
    FloLogicSignatureError,
    FloLogicValidationError,
    UnknownValveError,
)
from .models import CallbackDefinition, CallbackEvent, Page, User, Valve
from .webhook import (
    CALLBACK_VARIABLES,
    build_callback_url,
    callback_url_candidates,
    callback_url_from_request,
    compute_signature,
    parse_callback,
    verify_callback,
    verify_signature,
)

__version__ = "0.5.0"

__all__ = [
    "CALLBACK_VARIABLES",
    "CRITICAL_FLAGS",
    "DEFAULT_BASE_URL",
    "DEFAULT_COMMAND_TIMEOUT",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_REQUEST_TIMEOUT",
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "MAX_VIDS_PER_REQUEST",
    "MIN_POLL_INTERVAL",
    "PROBLEM_PRIORITY",
    "SETTABLE_MODES",
    "SHUTOFF_REASON_PRIORITY",
    "WARNING_FLAGS",
    "WATER_OFF_FLAGS",
    "AccessTokenAuth",
    "ApiKeyAuth",
    "CallbackDefinition",
    "CallbackEvent",
    "CallbackEventType",
    "CallbackReason",
    "ControlMode",
    "DeviceType",
    "FloLogicApiError",
    "FloLogicAuthError",
    "FloLogicClient",
    "FloLogicCommandError",
    "FloLogicConnectionError",
    "FloLogicError",
    "FloLogicNotFoundError",
    "FloLogicProtocolError",
    "FloLogicSignatureError",
    "FloLogicValidationError",
    "FlowState",
    "OAuthAuth",
    "OAuthTokens",
    "Page",
    "ShutoffReason",
    "TokenProvider",
    "UnknownValveError",
    "UpdateTarget",
    "User",
    "Valve",
    "ValveMode",
    "__version__",
    "async_exchange_code",
    "build_authorize_url",
    "build_callback_url",
    "callback_url_candidates",
    "callback_url_from_request",
    "compute_signature",
    "parse_callback",
    "verify_callback",
    "verify_signature",
]
