"""Authentication for the Web API.

FloLogic offers two ways in, and both end in the same bearer token:

- **API key** (:class:`ApiKeyAuth`): ``GET /Token`` with an ``x-api-key``
  header and a ``company_id``. The token is company-scoped -- it sees every
  device the company owns -- and lasts 24 hours, after which a new one is
  simply requested again.
- **OAuth2 authorization code** (:class:`OAuthAuth`): for an application
  FloLogic has registered, acting for one user. Access tokens last 24 hours;
  refresh tokens 90 days, and each refresh *rotates* the refresh token, so the
  new one must be stored every time.

:class:`AccessTokenAuth` covers a third case: an application that manages
OAuth tokens itself (Home Assistant does) and only needs this library to
present them.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .const import (
    DEFAULT_AUTHORIZE_URL,
    DEFAULT_BASE_URL,
    DEFAULT_REQUEST_TIMEOUT,
    PATH_OAUTH_TOKEN,
    PATH_TOKEN,
    TOKEN_LIFETIME,
    TOKEN_REFRESH_MARGIN,
)
from .exceptions import (
    FloLogicApiError,
    FloLogicAuthError,
    FloLogicConnectionError,
    FloLogicProtocolError,
)
from .models import lookup

__all__ = [
    "AccessTokenAuth",
    "ApiKeyAuth",
    "OAuthAuth",
    "OAuthTokens",
    "TokenProvider",
    "async_exchange_code",
    "build_authorize_url",
    "token_expiry",
]


def token_expiry(token: str) -> float | None:
    """Return a JWT's ``exp`` claim as a Unix timestamp, without verifying it.

    The client only needs to know when to ask for a new token; FloLogic does
    the verifying. A token that is not a readable JWT returns ``None`` and is
    treated as lasting the documented 24 hours.
    """
    try:
        payload = token.split(".")[1]
        decoded = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except (IndexError, ValueError, binascii.Error):
        return None
    if not isinstance(decoded, dict):
        return None
    exp = decoded.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return None
    return float(exp)


def _now() -> float:
    """Return the current Unix time; a seam for tests."""
    return time.time()


def _join(value: Any) -> str:
    """Render one problem-JSON ``errors`` entry, a list of messages or one."""
    return " ".join(map(str, value)) if isinstance(value, list) else str(value)


def _error_text(body: Any, fallback: str) -> str:
    """Pull FloLogic's own explanation out of an error body.

    Errors arrive in three shapes: plain text ("Invalid API key."), RFC 7807
    problem JSON with per-field ``errors``, and RFC 6749 OAuth errors.
    """
    if isinstance(body, str) and body.strip():
        return body.strip()
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, str):
            description = body.get("error_description")
            return f"{error}: {description}" if description else error
        errors = body.get("errors")
        if isinstance(errors, Mapping):
            details = "; ".join(
                f"{key}: {_join(value)}" for key, value in errors.items()
            )
            if details:
                return details
        title = body.get("title")
        if isinstance(title, str):
            return title
    return fallback


async def read_body(response: aiohttp.ClientResponse) -> Any:
    """Return a response body as JSON when it parses, otherwise as text."""
    text = await response.text()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


class TokenProvider(ABC):
    """Supplies the bearer token every API request carries."""

    @abstractmethod
    async def async_get_token(
        self, session: aiohttp.ClientSession, base_url: str, *, force: bool = False
    ) -> str:
        """Return a usable access token, obtaining a new one if needed.

        ``force`` asks for a fresh token even if the cached one looks valid;
        the client passes it after a 401, which means the server disagrees.
        """


class _CachingProvider(TokenProvider):
    """Caches a token until shortly before it expires."""

    def __init__(self) -> None:
        """Start with no token."""
        self._token: str | None = None
        self._expires_at = 0.0
        # Concurrent requests that all find the token stale must share one
        # renewal rather than each requesting their own.
        self._lock = asyncio.Lock()

    @property
    def expires_at(self) -> float | None:
        """Return when the cached token expires, as a Unix timestamp."""
        return self._expires_at if self._token else None

    def _cache(self, token: str) -> None:
        """Remember ``token`` and when it lapses."""
        self._token = token
        self._expires_at = token_expiry(token) or (_now() + TOKEN_LIFETIME)

    def _valid(self) -> bool:
        """Return whether the cached token has comfortably long left."""
        return self._token is not None and _now() < (
            self._expires_at - TOKEN_REFRESH_MARGIN
        )

    async def async_get_token(
        self, session: aiohttp.ClientSession, base_url: str, *, force: bool = False
    ) -> str:
        """Return the cached token, renewing it when stale or forced."""
        stale = self._token
        async with self._lock:
            # Another task may have renewed while this one waited: a forced
            # renewal is only needed if the token is still the one that failed.
            if (
                self._token is not None
                and self._valid()
                and (not force or self._token != stale)
            ):
                return self._token
            token = await self._async_obtain(session, base_url)
            self._cache(token)
            return token

    @abstractmethod
    async def _async_obtain(self, session: aiohttp.ClientSession, base_url: str) -> str:
        """Fetch a brand-new access token."""


class ApiKeyAuth(_CachingProvider):
    """The API-key flow: a company's key and ID, exchanged for a 24 hour token."""

    def __init__(self, api_key: str, company_id: int) -> None:
        """Configure the flow without contacting FloLogic."""
        super().__init__()
        self._api_key = api_key
        self.company_id = company_id

    async def _async_obtain(self, session: aiohttp.ClientSession, base_url: str) -> str:
        """Exchange the API key for a token."""
        try:
            async with session.get(
                f"{base_url}{PATH_TOKEN}",
                params={"company_id": self.company_id},
                headers={"x-api-key": self._api_key},
                timeout=aiohttp.ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT),
            ) as response:
                body = await read_body(response)
                status = response.status
        except (TimeoutError, aiohttp.ClientError) as err:
            raise FloLogicConnectionError(f"could not reach FloLogic: {err}") from err

        # A wrong key and a wrong company ID are both 401s, distinguished
        # only by the text: "Invalid API key." or "Invalid company ID.".
        if status in (400, 401, 403):
            raise FloLogicAuthError(_error_text(body, f"HTTP {status}"))
        if status >= 400:  # noqa: PLR2004
            raise FloLogicApiError(status, _error_text(body, "token request failed"))
        token = lookup(body, "Token") if isinstance(body, Mapping) else None
        if not isinstance(token, str) or not token:
            raise FloLogicProtocolError("token response carried no Token")
        return token


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """The tokens an OAuth2 exchange or refresh returns."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"

    @property
    def expires_at(self) -> float | None:
        """Return when the access token expires, read from its JWT."""
        return token_expiry(self.access_token)

    @classmethod
    def from_json(cls, body: Any) -> OAuthTokens:
        """Build tokens from an RFC 6749 token response."""
        if not isinstance(body, Mapping):
            raise FloLogicProtocolError("token response was not a JSON object")
        access = body.get("access_token")
        refresh = body.get("refresh_token")
        if not isinstance(access, str) or not isinstance(refresh, str):
            raise FloLogicProtocolError("token response lacked access or refresh token")
        return cls(
            access_token=access,
            refresh_token=refresh,
            token_type=str(body.get("token_type") or "Bearer"),
        )


async def _async_token_request(
    session: aiohttp.ClientSession, base_url: str, form: Mapping[str, str]
) -> OAuthTokens:
    """POST a grant to ``/oauth/token`` and return the tokens."""
    try:
        async with session.post(
            f"{base_url}{PATH_OAUTH_TOKEN}",
            data=dict(form),
            timeout=aiohttp.ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT),
        ) as response:
            body = await read_body(response)
            status = response.status
    except (TimeoutError, aiohttp.ClientError) as err:
        raise FloLogicConnectionError(f"could not reach FloLogic: {err}") from err

    if status >= 400:  # noqa: PLR2004
        message = _error_text(body, f"HTTP {status}")
        code = body.get("error") if isinstance(body, Mapping) else None
        # invalid_grant covers an expired, reused or rotated-away refresh
        # token: only the user can fix that, by authorizing again.
        if code in {"invalid_grant", "invalid_client", "unauthorized_client"}:
            raise FloLogicAuthError(message)
        raise FloLogicApiError(status, message)
    return OAuthTokens.from_json(body)


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    *,
    authorize_url: str = DEFAULT_AUTHORIZE_URL,
    extra: Mapping[str, str] | None = None,
) -> str:
    """Return the consent URL to send a user to.

    FloLogic documents ``client_id``, ``redirect_uri`` and ``state``, and not
    the ``response_type=code`` RFC 6749 requires. Standard clients send it
    anyway; ``extra`` exists for that and anything else FloLogic adds.
    """
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "state": state}
    params.update(extra or {})
    return f"{authorize_url}?{urlencode(params)}"


async def async_exchange_code(
    session: aiohttp.ClientSession,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    client_secret: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> OAuthTokens:
    """Exchange an authorization code for tokens.

    Codes are single-use and expire after five minutes, so call this as soon
    as the redirect arrives.
    """
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        form["client_secret"] = client_secret
    return await _async_token_request(session, base_url, form)


TokenCallback = Callable[[OAuthTokens], Awaitable[None] | None]


class OAuthAuth(_CachingProvider):
    """Presents OAuth2 tokens and refreshes them when they lapse.

    Refresh tokens rotate: every refresh invalidates the previous one, and
    FloLogic advises treating a replayed one as a compromise. ``on_refresh``
    is called with the new tokens *before* the access token is used, so the
    caller can persist the new refresh token while it is still the only valid
    one -- losing it means the user has to authorize again.
    """

    def __init__(
        self,
        client_id: str,
        tokens: OAuthTokens,
        *,
        client_secret: str | None = None,
        on_refresh: TokenCallback | None = None,
    ) -> None:
        """Start from tokens obtained earlier."""
        super().__init__()
        self._client_id = client_id
        self._client_secret = client_secret
        self._on_refresh = on_refresh
        self.tokens = tokens
        self._cache(tokens.access_token)

    async def _async_obtain(self, session: aiohttp.ClientSession, base_url: str) -> str:
        """Use the refresh token to get a new access token."""
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self.tokens.refresh_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            form["client_secret"] = self._client_secret
        tokens = await _async_token_request(session, base_url, form)
        self.tokens = tokens
        if self._on_refresh is not None:
            result = self._on_refresh(tokens)
            if inspect.isawaitable(result):
                await result
        return tokens.access_token


class AccessTokenAuth(TokenProvider):
    """Presents whatever token a caller-supplied coroutine returns.

    For applications that already manage the OAuth lifecycle themselves and
    want this library only to make the requests.
    """

    def __init__(self, get_token: Callable[[], Awaitable[str]]) -> None:
        """Wrap ``get_token``, which must return a currently valid token."""
        self._get_token = get_token

    async def async_get_token(
        self, session: aiohttp.ClientSession, base_url: str, *, force: bool = False
    ) -> str:
        """Return the caller's current token."""
        del session, base_url, force
        return await self._get_token()
