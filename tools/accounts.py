"""Credential and device-identity handling for the local validation tools.

Deliberately not part of the library: reading ``.env`` files and caching
device identities on disk is a convenience for driving these scripts, not
something a library should impose on the application embedding it.

Accounts are named so the same valve can be inspected through more than one
login -- an owner account and a shared one see different names and different
notification preferences, and telling those apart is half the point.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from pyflologic import DeviceIdentity

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
DEVICE_STORE = REPO_ROOT / ".pyflologic-devices.json"

PREFIX = "FLOLOGIC_"
EMAIL_SUFFIX = "_EMAIL"
PASSWORD_SUFFIX = "_PASSWORD"


class CredentialError(RuntimeError):
    """No usable credentials were found for the requested account."""


@dataclass(frozen=True)
class Credentials:
    """One account's login and its persistent client-device identity."""

    account: str
    email: str
    password: str
    device: DeviceIdentity


def load_env(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` pairs from a .env file into the environment.

    Existing environment variables win, so an explicit export still overrides
    the file. Deliberately minimal -- no interpolation, no export keyword.

    ``path`` defaults to :data:`ENV_PATH` at call time rather than as a default
    argument value, which would freeze it at import and make the location
    impossible to redirect.
    """
    path = path if path is not None else ENV_PATH
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _unquote(value.strip())
        if key and value and key not in os.environ:
            os.environ[key] = value


def _unquote(value: str) -> str:
    """Strip surrounding quotes, but only a genuinely matched pair.

    Stripping quote characters individually corrupts any password that merely
    happens to start or end with one -- and passwords full of punctuation are
    exactly the ones worth getting right, because the resulting failure looks
    like "wrong password" rather than like a parsing bug.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def available_accounts() -> list[str]:
    """Return the names of every account with both an email and a password."""
    names = set()
    for key in os.environ:
        if key.startswith(PREFIX) and key.endswith(EMAIL_SUFFIX):
            name = key[len(PREFIX) : -len(EMAIL_SUFFIX)]
            if name and os.environ.get(f"{PREFIX}{name}{PASSWORD_SUFFIX}"):
                names.add(name.lower())
    return sorted(names)


def resolve(account: str | None = None) -> Credentials:
    """Return credentials for ``account``, or for the unnamed default.

    With no account named, falls back to FLOLOGIC_EMAIL/FLOLOGIC_PASSWORD, and
    then -- only if exactly one named account exists -- to that one. Being
    strict when several are configured avoids running a control test against
    whichever account happened to sort first.
    """
    load_env()

    if account:
        name = account.upper()
        email = os.environ.get(f"{PREFIX}{name}{EMAIL_SUFFIX}")
        password = os.environ.get(f"{PREFIX}{name}{PASSWORD_SUFFIX}")
        if not email or not password:
            known = available_accounts()
            raise CredentialError(
                f"No credentials for account {account!r}. "
                f"Configured accounts: {', '.join(known) if known else 'none'}"
            )
        return Credentials(account.lower(), email, password, device_for(account))

    email = os.environ.get(f"{PREFIX}EMAIL")
    password = os.environ.get(f"{PREFIX}PASSWORD")
    if email and password:
        return Credentials("default", email, password, device_for("default"))

    known = available_accounts()
    if len(known) == 1:
        return resolve(known[0])
    if not known:
        raise CredentialError(
            f"No credentials found. Copy .env.example to {ENV_PATH.name} and "
            "fill it in, or export FLOLOGIC_EMAIL and FLOLOGIC_PASSWORD."
        )
    raise CredentialError(
        f"Several accounts are configured ({', '.join(known)}); "
        "name one with --account."
    )


def device_for(account: str) -> DeviceIdentity:
    """Return a stable client-device identity for ``account``, creating one once.

    FloLogic registers a device per code/token pair, so generating a fresh
    identity on every run quietly fills the account's device list with
    single-use entries. Persisting one per account keeps that to one.
    """
    store: dict[str, dict[str, str]] = {}
    if DEVICE_STORE.is_file():
        try:
            store = json.loads(DEVICE_STORE.read_text())
        except (OSError, json.JSONDecodeError):
            store = {}

    key = account.lower()
    saved = store.get(key)
    if saved and {"name", "code", "token"} <= saved.keys():
        return DeviceIdentity(saved["name"], saved["code"], saved["token"])

    identity = DeviceIdentity.generate(f"pyflologic-{key}")
    store[key] = {
        "name": identity.name,
        "code": identity.code,
        "token": identity.token,
    }
    DEVICE_STORE.write_text(json.dumps(store, indent=2))
    DEVICE_STORE.chmod(0o600)
    return identity
