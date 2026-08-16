#!/usr/bin/env python3
"""Exercise the write path against one real valve and record what happens.

Sends one mode change, watches every raw field that moves for a while, then
puts the valve back the way it was found. The timeline is the point: it shows
which bits the cloud sets, how long it takes, and whether the change arrives
by push or only on the next poll.

    uv run python tools/control_test.py --account sample \
        --valve-id 106193 --mode bypass --observe 45

Safety, in the order it matters:

- Restoration runs from a `finally` and survives Ctrl-C, a failed command, and
  a dropped connection; it retries and then verifies. If it cannot restore,
  it says so loudly rather than exiting quietly.
- Confirmation is the valve's typed name, not y/n, so a mistyped --valve-id
  cannot close the water at a house nobody is standing in.
- `shutoff` additionally needs --i-understand-this-closes-the-water.
- Only ever touches the single valve named on the command line.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from datetime import UTC, datetime
from typing import Any

from accounts import CredentialError, resolve

from pyflologic import Account, ControlMode, FloLogicClient, FloLogicError

RESTORE_ATTEMPTS = 3
POLL_SECONDS = 5.0

# Fields that move constantly and would bury the interesting ones.
NOISE = {"lastSeen", "modified", "details", "lastActive"}


class Timeline:
    """Records raw-field changes against the moment the command was sent."""

    def __init__(self, valve_id: str, baseline: dict[str, Any]) -> None:
        """Start from the valve's pre-command payload."""
        self._valve_id = valve_id
        self._previous = dict(baseline)
        self._started = datetime.now(UTC)
        self.entries: list[tuple[float, str, Any, Any]] = []

    def observe(self, account: Account, source: str) -> None:
        """Diff the valve's current payload against the last one seen."""
        valve = account.valves.get(self._valve_id)
        if valve is None:
            return
        current = valve.raw
        elapsed = (datetime.now(UTC) - self._started).total_seconds()
        changed = [
            (key, self._previous.get(key), current.get(key))
            for key in sorted(self._previous.keys() | current.keys())
            if self._previous.get(key) != current.get(key) and key not in NOISE
        ]
        if not changed:
            return
        print(
            f"\n[{elapsed:6.1f}s via {source}] "
            f"mode={int(valve.mode)} {valve.mode.flag_names} "
            f"status={valve.status} flowState={valve.flow_state} "
            f"flowing={valve.is_water_flowing} "
            f"countdown={valve.shutoff_countdown_seconds()}"
        )
        for key, was, now in changed:
            print(f"    {key:<34} {was!r} -> {now!r}")
            self.entries.append((elapsed, key, was, now))
        self._previous = dict(current)


async def restore(client: FloLogicClient, valve_id: str, mode: ControlMode) -> bool:
    """Put the valve back, retrying, and report whether it took."""
    for attempt in range(1, RESTORE_ATTEMPTS + 1):
        try:
            await client.async_set_mode(valve_id, mode)
            settled = client.get_valve(valve_id)
            if settled.control_mode is mode:
                return True
            print(
                f"  restore attempt {attempt}: valve reports "
                f"{settled.control_mode}, wanted {mode}"
            )
        except FloLogicError as err:
            print(f"  restore attempt {attempt} failed: {err}", file=sys.stderr)
        await asyncio.sleep(3)
    return False


async def run(args: argparse.Namespace) -> int:
    """Send one command, observe, and restore."""
    try:
        credentials = resolve(args.account)
    except CredentialError as err:
        print(err, file=sys.stderr)
        return 2

    target = ControlMode(args.mode)
    if target is ControlMode.SHUTOFF and not args.i_understand_this_closes_the_water:
        print(
            "Refusing to send shutoff without --i-understand-this-closes-the-water.",
            file=sys.stderr,
        )
        return 2

    client = FloLogicClient(
        email=credentials.email,
        password=credentials.password,
        device=credentials.device,
    )
    await client.async_connect()

    original: ControlMode | None = None
    restored = False
    try:
        try:
            valve = client.get_valve(args.valve_id)
        except FloLogicError as err:
            print(f"{err}\n\nValves on this account:", file=sys.stderr)
            for valve_id, known in client.valves.items():
                print(f"  {valve_id:<10} {known.name}", file=sys.stderr)
            return 2

        original = valve.control_mode
        print(f"Account : {credentials.account} ({credentials.email})")
        print(f"Valve   : {valve.name} [{valve.valve_id}] at {valve.network_name}")
        print(f"Current : {original}  (raw mode {int(valve.mode)})")
        print(f"Plan    : set {target}, observe {args.observe:g}s, restore {original}")
        if original is None:
            print(
                "\nThe valve's current mode cannot be determined, so it could "
                "not be put back afterwards. Stopping.",
                file=sys.stderr,
            )
            return 1

        if not args.yes:
            answer = await asyncio.to_thread(input, "\nType the valve name: ")
            if answer.strip() != valve.name:
                print("Name did not match. Nothing sent.")
                return 1

        timeline = Timeline(args.valve_id, valve.raw)
        unsubscribe = client.add_listener(lambda a: timeline.observe(a, "push"))

        print(f"\n--- sending {target} ---")
        await client.async_set_mode(args.valve_id, target, refresh=False)
        print("command accepted")
        if args.prompt:
            print(f"\n>>> {args.prompt}\n")

        deadline = asyncio.get_running_loop().time() + args.observe
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            with contextlib.suppress(FloLogicError):
                await client.async_refresh()
                timeline.observe(client.account, "poll")

        unsubscribe()
        print(f"\n--- observation finished ({len(timeline.entries)} changes) ---")
    finally:
        if original is not None:
            print(f"\n--- restoring {original} ---")
            restored = await restore(client, args.valve_id, original)
            final = client.get_valve(args.valve_id)
            print(f"valve now: {final.control_mode} (raw mode {int(final.mode)})")
        await client.async_disconnect()

    if original is not None and not restored:
        print(
            f"\n*** COULD NOT RESTORE {original}. Check the FloLogic app now. ***",
            file=sys.stderr,
        )
        return 1
    print("\nValve is back where it started.")
    return 0


def main() -> int:
    """Parse arguments and run the control test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="account name from .env")
    parser.add_argument("--valve-id", required=True, help="valve to test")
    parser.add_argument(
        "--mode",
        default=ControlMode.BYPASS.value,
        choices=[mode.value for mode in ControlMode],
        help="mode to set before restoring (default: bypass)",
    )
    parser.add_argument(
        "--observe",
        type=float,
        default=45.0,
        help="seconds to watch before restoring (default: 45)",
    )
    parser.add_argument(
        "--prompt",
        help="message to print after the command lands, e.g. 'run a tap now'",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    parser.add_argument(
        "--i-understand-this-closes-the-water",
        action="store_true",
        help="required to send shutoff",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        # run() restores from its finally block before this propagates.
        print("\ninterrupted", file=sys.stderr)
        return 130
    except FloLogicError as err:
        print(f"FloLogic error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
