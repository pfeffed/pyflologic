#!/usr/bin/env python3
"""Show what the library makes of every valve, and optionally diff two moments.

    uv run python tools/snapshot.py
    uv run python tools/snapshot.py -o before.snapshot.json
    uv run python tools/snapshot.py --diff before.snapshot.json

Read-only. Raw payloads include street addresses, so saved snapshots are
gitignored -- keep them local.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from credentials import CredentialError, resolve

from pyflologic import ApiKeyAuth, FloLogicClient, FloLogicError, Valve


def summarize(valve: Valve) -> str:
    """Return a one-line summary of a valve."""
    flow = valve.flow_state.name if valve.flow_state else "?"
    return (
        f"{valve.vid:<8} {valve.device_type_raw or '?':<12} "
        f"online={valve.is_online!s:<5} mode={int(valve.mode):<6} "
        f"{','.join(valve.mode.flag_names) or '-':<20} status={valve.status:<10} "
        f"flow={flow:<9} drip={valve.flow_sensitivity_oz_per_min} "
        f"temp={valve.temperature_f}"
    )


def diff(old: dict[str, Any], new: dict[str, Any]) -> None:
    """Print every raw field that differs between two snapshots."""
    print(f"\nbaseline {old.get('captured_at')}  ->  now {new.get('captured_at')}")
    before_all = old.get("valves", {})
    after_all = new.get("valves", {})
    for vid in sorted(before_all.keys() | after_all.keys()):
        before = before_all.get(vid, {})
        after = after_all.get(vid, {})
        changes = {
            key: (before.get(key), after.get(key))
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        }
        if changes:
            print(f"\n{vid}")
            for key, (was, now) in sorted(changes.items()):
                print(f"  {key:<32} {was!r} -> {now!r}")


async def run(args: argparse.Namespace) -> int:
    """Take the snapshot."""
    try:
        creds = resolve()
    except CredentialError as err:
        print(err, file=sys.stderr)
        return 2
    async with FloLogicClient(ApiKeyAuth(creds.api_key, creds.company_id)) as client:
        try:
            valves = await client.async_refresh()
        except FloLogicError as err:
            print(f"refresh failed: {err}", file=sys.stderr)
            return 1

    captured = {
        "captured_at": datetime.now(UTC).isoformat(),
        "valves": {vid: valve.raw for vid, valve in valves.items()},
    }
    print(f"captured_at {captured['captured_at']}  ({len(valves)} valves)")
    for valve in valves.values():
        print(summarize(valve))
        print(
            f"         shutoff_reason={valve.shutoff_reason} problem={valve.problem} "
            f"last_flow_change={valve.last_flow_change} "
            f"limits home={valve.home_limit_minutes} away={valve.away_limit_minutes} "
            f"bypass={valve.bypass_minutes if valve.bypass_enabled else 'off'}"
        )
    if args.output:
        args.output.write_text(json.dumps(captured, indent=2) + "\n")
        print(f"\nsaved -> {args.output}")
    if args.diff:
        diff(json.loads(args.diff.read_text()), captured)
    return 0


def main() -> int:
    """Parse arguments and take the snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--diff", type=Path, help="compare against a saved snapshot")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
