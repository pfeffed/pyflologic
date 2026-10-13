"""Exceptions raised by :mod:`pyflologic`."""

from __future__ import annotations

__all__ = [
    "FloLogicApiError",
    "FloLogicAuthError",
    "FloLogicCommandError",
    "FloLogicConnectionError",
    "FloLogicError",
    "FloLogicNotFoundError",
    "FloLogicProtocolError",
    "FloLogicSignatureError",
    "FloLogicValidationError",
    "UnknownValveError",
]


class FloLogicError(Exception):
    """Base class for every error this library raises."""


class FloLogicConnectionError(FloLogicError):
    """The Web API could not be reached, or did not answer in time."""


class FloLogicAuthError(FloLogicError):
    """FloLogic rejected the API key, the company ID, or a token."""


class FloLogicApiError(FloLogicError):
    """The Web API answered with an error status this library cannot recover.

    Carries the status and FloLogic's own message, which is usually specific:
    the token endpoint says "Invalid company ID." rather than a bare 401.
    """

    def __init__(self, status: int, message: str) -> None:
        """Record the HTTP status and FloLogic's explanation."""
        super().__init__(f"FloLogic returned HTTP {status}: {message}")
        self.status = status
        self.message = message


class FloLogicNotFoundError(FloLogicApiError):
    """A resource named in the request does not exist -- typically a VID."""


class FloLogicProtocolError(FloLogicError):
    """FloLogic sent something this client cannot make sense of."""


class FloLogicCommandError(FloLogicError):
    """FloLogic accepted a command, but the valve never reported the change."""


class FloLogicValidationError(FloLogicError):
    """A request was refused here, before being sent.

    Reserved for requests the Web API does not accept at all, so the caller
    gets a precise reason instead of an HTTP 400.
    """


class FloLogicSignatureError(FloLogicError):
    """A callback's signature is missing, stale, or does not match."""


class UnknownValveError(FloLogicError):
    """The requested valve has not been seen by this client."""

    def __init__(self, vid: str) -> None:
        """Record which valve was asked for."""
        super().__init__(f"No FloLogic valve with VID {vid!r} is known")
        self.vid = vid
