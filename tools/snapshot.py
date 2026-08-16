#!/usr/bin/env python3
"""Capture one timestamped snapshot of every valve, for comparison and diffing.

Pairs with tools/uidump.sh: run both at the same moment and you have the API's
view and the app's view of the same instant, which is how the mode bitfield
and the settings units get validated.

    uv run python tools/snapshot.py --account david
    uv run python tools/snapshot.py --account david -o before.snapshot.json
    uv run python tools/snapshot.py --account david --diff before.snapshot.json

Read-only. Raw dumps contain the account's address and policy details, so they
are gitignored -- keep them local.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from accounts import CredentialError, resolve

from pyflologic import FloLogicClient, FloLogicError


def summarize(valve: Any) -> str:
    """Return a one-line state summary for a valve."""
    return (
        f"{valve.valve_id:<8} {valve.name[:26]:<26} "
        f"mode={int(valve.mode):<10} {','.join(valve.mode.flag_names) or '-':<24} "
        f"fs={valve.flow_state.name if valve.flow_state else '?':<12} "
        f"flow={valve.current_flow_oz_per_min} "
        f"cd={valve.shutoff_countdown_seconds()}"
    )


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> None:
    """Print every raw field that differs between two snapshots."""
    old_valves = old.get("valves", {})
    new_valves = new.get("valves", {})
    print(f"\nbaseline {old.get('captured_at')}  ->  now {new.get('captured_at')}")
    for valve_id in sorted(old_valves.keys() | new_valves.keys()):
        before = old_valves.get(valve_id, {})
        after = new_valves.get(valve_id, {})
        changes = {
            key: (before.get(key), after.get(key))
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        }
        if not changes:
            continue
        name = after.get("combinedName") or before.get("combinedName") or valve_id
        print(f"\n{name} [{valve_id}]")
        for key, (was, now) in sorted(changes.items()):
            print(f"  {key:<38} {was!r} -> {now!r}")


async def run(args: argparse.Namespace) -> int:
    """Capture a snapshot and optionally diff it against an earlier one."""
    try:
        credentials = resolve(args.account)
    except CredentialError as err:
        print(err, file=sys.stderr)
        return 2

    client = FloLogicClient(
        email=credentials.email,
        password=credentials.password,
        device=credentials.device,
    )
    async with client:
        captured_at = datetime.now(UTC).isoformat()
        snapshot = {
            "captured_at": captured_at,
            "valves": {
                valve_id: valve.raw for valve_id, valve in client.valves.items()
            },
        }
        print(f"account {credentials.account}  captured_at {captured_at}")
        for valve in client.valves.values():
            print(summarize(valve))

    if args.output:
        Path(args.output).write_text(json.dumps(snapshot, indent=2, default=str))
        print(f"\nraw payloads -> {args.output}")

    if args.diff:
        baseline = json.loads(Path(args.diff).read_text())
        diff_snapshots(baseline, snapshot)

    return 0


def main() -> int:
    """Parse arguments and capture the snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="account name from .env")
    parser.add_argument("-o", "--output", help="write raw payloads to this file")
    parser.add_argument("--diff", help="diff against a previously written file")
    try:
        return asyncio.run(run(parser.parse_args()))
    except FloLogicError as err:
        print(f"FloLogic error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
