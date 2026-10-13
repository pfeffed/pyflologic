#!/usr/bin/env python3
"""Read the same valves through the Web API and through the app protocol.

The two interfaces sit on one backend and should agree. Where they do not,
one of them is being misread -- which is how the Web API's semantic claims
(thresholds that include the temperature offset, a VID that is the app's
uuid) get checked rather than taken on trust.

    uv run python tools/compare.py
    uv run python tools/compare.py --account david

Read-only. The app-protocol side runs ``tools/snapshot.py`` in the sibling
``pyflologic-unofficial`` checkout, with that repository's own credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from credentials import REPO_ROOT, CredentialError, resolve

from pyflologic import ApiKeyAuth, FloLogicClient, FloLogicError
from pyflologic.models import parse_datetime

UNOFFICIAL = REPO_ROOT.parent / "pyflologic-unofficial"


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _magnitude(value: Any) -> float | None:
    number = _num(value)
    return abs(number) if number is not None else None


def _enabled(value: Any) -> bool:
    number = _num(value)
    return number is not None and number > 0


def _with_offset(field: str) -> Callable[[dict[str, Any]], float | None]:
    """The app's threshold plus its offset: what the Web API says it reports."""

    def compute(raw: dict[str, Any]) -> float | None:
        threshold = raw.get(field)
        if not _enabled(threshold):
            return None
        magnitude = _magnitude(threshold)
        assert magnitude is not None
        return magnitude + (_num(raw.get("temperatureOffset")) or 0)

    return compute


# (Web API field, how to derive the same value from an app-protocol payload)
FIELDS: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
    ("Online", lambda raw: bool(raw.get("online"))),
    ("Mode", lambda raw: _num(raw.get("mode"))),
    ("FlowState", lambda raw: _num(raw.get("flowState"))),
    ("DripRate", lambda raw: _num(raw.get("dripRate"))),
    ("Temperature", lambda raw: _num(raw.get("temperature"))),
    ("HomeTime", lambda raw: _num(raw.get("homeIntervalTime"))),
    ("AwayTime", lambda raw: _num(raw.get("awayIntervalTime"))),
    ("BypassTime", lambda raw: _num(raw.get("bypassTime"))),
    (
        "LowTemperatureAlertEnabled",
        lambda raw: _enabled(raw.get("lowTemperatureAlert")),
    ),
    ("LowTemperatureAlertThreshold", _with_offset("lowTemperatureAlert")),
    (
        "LowTemperatureShutoffEnabled",
        lambda raw: _enabled(raw.get("lowTemperatureLimit")),
    ),
    ("LowTemperatureShutoffThreshold", _with_offset("lowTemperatureLimit")),
    ("PreAlertNoticeTime", lambda raw: _num(raw.get("preAlertNoticeInterval"))),
    ("LastFlowDateTime", lambda raw: parse_datetime(raw.get("lastFlowChange"))),
]


def _normalize(field: str, value: Any) -> Any:
    if field == "LastFlowDateTime":
        return parse_datetime(value)
    if isinstance(value, bool):
        return value
    number = _num(value)
    return number if number is not None else value


def unofficial_snapshot(account: str) -> dict[str, dict[str, Any]]:
    """Run the app-protocol snapshot tool and return payloads keyed by uuid."""
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "unofficial.json"
        subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(UNOFFICIAL),
                "python",
                str(UNOFFICIAL / "tools" / "snapshot.py"),
                "--account",
                account,
                "-o",
                str(out),
            ],
            check=True,
            capture_output=True,
            cwd=UNOFFICIAL,
        )
        payloads = json.loads(out.read_text())["valves"]
    return {str(raw.get("uuid")): raw for raw in payloads.values() if raw.get("uuid")}


async def run(account: str) -> int:
    """Compare the two views and print every disagreement."""
    try:
        creds = resolve()
    except CredentialError as err:
        print(err, file=sys.stderr)
        return 2
    async with FloLogicClient(ApiKeyAuth(creds.api_key, creds.company_id)) as client:
        try:
            official = await client.async_refresh()
        except FloLogicError as err:
            print(f"Web API read failed: {err}", file=sys.stderr)
            return 1
    try:
        app = unofficial_snapshot(account)
    except (subprocess.CalledProcessError, FileNotFoundError, KeyError) as err:
        print(f"app-protocol snapshot failed: {err}", file=sys.stderr)
        return 1

    disagreements = 0
    for vid, valve in sorted(official.items()):
        raw = app.get(vid)
        if raw is None:
            print(f"\n{vid}: not visible to the app-protocol account {account!r}")
            continue
        name = raw.get("valveFriendlyName") or raw.get("combinedName") or vid
        offset = raw.get("temperatureOffset")
        print(f"\n{vid} = app id {raw.get('id')} ({name}), temperatureOffset={offset}")
        for field, derive in FIELDS:
            web = _normalize(field, valve.raw.get(field))
            expected = derive(raw)
            same = web == expected
            if not same:
                disagreements += 1
            mark = "  " if same else "!!"
            print(f"  {mark} {field:<32} web={web!s:<28} app={expected!s}")
    print(f"\n{disagreements} disagreement(s)")
    # Values move between the two reads; one differing field is a prompt to
    # rerun, not proof of a mismatch.
    return 0


def main() -> int:
    """Parse arguments and compare."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account", default="david", help="account name in the unofficial .env"
    )
    return asyncio.run(run(parser.parse_args().account))


if __name__ == "__main__":
    sys.exit(main())
