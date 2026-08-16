"""Exceptions raised by :mod:`pyflologic`."""

from __future__ import annotations

__all__ = [
    "FloLogicAuthError",
    "FloLogicCommandError",
    "FloLogicConnectionError",
    "FloLogicError",
    "FloLogicProtocolError",
    "FloLogicTimeoutError",
    "UnknownValveError",
]


class FloLogicError(Exception):
    """Base class for every error this library raises."""


class FloLogicConnectionError(FloLogicError):
    """The FloLogic cloud could not be reached, or the socket dropped."""


class FloLogicAuthError(FloLogicError):
    """FloLogic rejected the credentials or the client-device identity."""


class FloLogicTimeoutError(FloLogicError):
    """FloLogic accepted a request but never sent the answering event."""


class FloLogicProtocolError(FloLogicError):
    """FloLogic sent something this client cannot make sense of."""


class FloLogicCommandError(FloLogicError):
    """FloLogic reported that a state-change command failed."""


class UnknownValveError(FloLogicError):
    """The requested valve is not present on this account."""

    def __init__(self, valve_id: str) -> None:
        """Record which valve was asked for."""
        super().__init__(f"No FloLogic valve with id {valve_id!r} on this account")
        self.valve_id = valve_id
