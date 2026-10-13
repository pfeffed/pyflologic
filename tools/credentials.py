"""Credential handling for the local validation tools.

Deliberately not part of the library: reading ``.env`` files is a convenience
for driving these scripts, not something a library should impose on the
application embedding it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"


class CredentialError(RuntimeError):
    """No usable credentials were found."""


@dataclass(frozen=True)
class ApiCredentials:
    """What the API-key flow needs, plus the webhook signing secret."""

    api_key: str
    company_id: int
    webhook_secret: str | None


def load_env(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` pairs from a .env file into the environment.

    Existing environment variables win, so an explicit export still overrides
    the file. Deliberately minimal -- no interpolation, no export keyword.
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

    Stripping quote characters individually corrupts any secret that merely
    happens to start or end with one, and the failure looks like a rejected
    key rather than a parsing bug.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def resolve() -> ApiCredentials:
    """Return the API credentials from the environment or ``.env``."""
    load_env()
    api_key = os.environ.get("FLOLOGIC_API_KEY", "")
    company = os.environ.get("FLOLOGIC_COMPANY_ID", "")
    if not api_key or not company:
        raise CredentialError(
            f"set FLOLOGIC_API_KEY and FLOLOGIC_COMPANY_ID in {ENV_PATH} "
            "(see .env.example)"
        )
    try:
        company_id = int(company)
    except ValueError:
        raise CredentialError("FLOLOGIC_COMPANY_ID must be an integer") from None
    return ApiCredentials(
        api_key=api_key,
        company_id=company_id,
        webhook_secret=os.environ.get("FLOLOGIC_WEBHOOK_SECRET") or None,
    )
