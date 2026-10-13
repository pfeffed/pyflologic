#!/usr/bin/env python3
"""Change one valve through the Web API, time it, and put it back.

This WRITES to a real valve. A mode change can shut off a building's water,
and Away can shut it off within seconds of any flow, so nothing is sent
without ``--yes-change-my-valve`` and an explicit VID.

    uv run python tools/control_test.py --vid 414B91 mode bypass --yes-change-my-valve
    uv run python tools/control_test.py --vid 414B91 drip 6.5 --yes-change-my-valve

For each change it records how long FloLogic took to accept the request and
how long until the valve reported it, polling once a second -- the numbers
the client's confirmation timeout is based on -- then restores the original
value the same way. The original is restored even if the change fails, and
a restore that FloLogic accepts without applying is resent until it lands.

Measured on 2026-10-13 against a WiFi Connect: a mode change was reported
1.6-4.4 s after the request, a flow-sensitivity change 5 s after -- and one
flow-sensitivity restore was accepted and never applied until resent.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable

from credentials import CredentialError, resolve

from pyflologic import (
    ApiKeyAuth,
    ControlMode,
    FloLogicClient,
    FloLogicError,
    Valve,
)
from pyflologic.client import mode_reflects

POLL_SECONDS = 1.0
TIMEOUT = 90.0


async def timed(
    client: FloLogicClient,
    vid: str,
    label: str,
    send: Callable[[], Awaitable[None]],
    reflects: Callable[[Valve], bool],
) -> bool:
    """Send one change, then poll until the valve reports it. Return success."""
    started = time.monotonic()
    await send()
    accepted = time.monotonic() - started
    print(f"  {label}: accepted in {accepted:.2f}s", flush=True)
    polls = 0
    while time.monotonic() - started < TIMEOUT:
        await asyncio.sleep(POLL_SECONDS)
        polls += 1
        valve = (await client.async_get_valves(vid)).get(vid)
        if valve is not None and reflects(valve):
            elapsed = time.monotonic() - started
            print(
                f"  {label}: reported after {elapsed:.1f}s ({polls} polls) "
                f"mode={int(valve.mode)} {valve.mode.flag_names}",
                flush=True,
            )
            return True
    print(f"  {label}: NOT reported within {TIMEOUT:g}s", flush=True)
    return False


async def run(args: argparse.Namespace) -> int:
    """Make the change and restore it."""
    try:
        creds = resolve()
    except CredentialError as err:
        print(err, file=sys.stderr)
        return 2
    async with FloLogicClient(ApiKeyAuth(creds.api_key, creds.company_id)) as client:
        original = (await client.async_get_valves(args.vid)).get(args.vid)
        if original is None:
            print(f"{args.vid} is not visible to these credentials", file=sys.stderr)
            return 1
        print(
            f"{args.vid}: mode={int(original.mode)} {original.mode.flag_names} "
            f"drip={original.flow_sensitivity_oz_per_min} "
            f"flow={original.flow_state.name if original.flow_state else '?'}"
        )

        ok = True
        try:
            if args.what == "mode":
                target = ControlMode(args.value)
                back = original.control_mode
                if back is None or not back.settable:
                    print(f"cannot restore from {original.control_mode}; refusing")
                    return 1
                ok = await timed(
                    client,
                    args.vid,
                    f"-> {target.value}",
                    lambda: client.async_set_mode(args.vid, target, confirm=False),
                    lambda valve: mode_reflects(valve, target),
                )
            else:
                target_drip = float(args.value)
                ok = await timed(
                    client,
                    args.vid,
                    f"-> drip {target_drip:g}",
                    lambda: client.async_set_flow_sensitivity(
                        args.vid, target_drip, confirm=False
                    ),
                    lambda valve: valve.flow_sensitivity_oz_per_min == target_drip,
                )
        except FloLogicError as err:
            print(f"  change failed: {err}")
            ok = False
        finally:
            print("restoring:")
            if args.what == "mode":
                assert original.control_mode is not None
                restore_mode = original.control_mode
                restored = await timed(
                    client,
                    args.vid,
                    f"-> {restore_mode.value}",
                    lambda: client.async_set_mode(
                        args.vid, restore_mode, confirm=False
                    ),
                    lambda valve: mode_reflects(valve, restore_mode),
                )
                if not restored:
                    await insist(
                        client.async_set_mode(args.vid, restore_mode, timeout=TIMEOUT)
                    )
            else:
                restore_drip = original.flow_sensitivity_oz_per_min
                assert restore_drip is not None
                restored = await timed(
                    client,
                    args.vid,
                    f"-> drip {restore_drip:g}",
                    lambda: client.async_set_flow_sensitivity(
                        args.vid, restore_drip, confirm=False
                    ),
                    lambda valve: valve.flow_sensitivity_oz_per_min == restore_drip,
                )
                if not restored:
                    await insist(
                        client.async_set_flow_sensitivity(
                            args.vid, restore_drip, timeout=TIMEOUT
                        )
                    )
    return 0 if ok else 1


async def insist(command: Awaitable[None]) -> None:
    """Restore with the library's own resending, after a timed attempt failed.

    The timed attempts send once, to measure; a restore must not stop there.
    FloLogic was observed accepting a restore and never applying it, which
    left a valve at the test value until a resend went through.
    """
    print("  restore not applied; resending until it is", flush=True)
    started = time.monotonic()
    try:
        await command
    except FloLogicError as err:
        print(f"  RESTORE FAILED: {err}. Set it back by hand.", flush=True)
        return
    print(
        f"  restored after {time.monotonic() - started:.1f}s of resending", flush=True
    )


def main() -> int:
    """Parse arguments and run the test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vid", required=True)
    parser.add_argument("what", choices=["mode", "drip"])
    parser.add_argument("value", help="a mode name, or a flow sensitivity")
    parser.add_argument("--yes-change-my-valve", action="store_true")
    args = parser.parse_args()
    if not args.yes_change_my_valve:
        print("refusing to write without --yes-change-my-valve", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
