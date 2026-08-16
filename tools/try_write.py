#!/usr/bin/env python3
"""Verify the write path against one real valve, then put it back.

The read path has been checked against live hardware; RequestStateChange has
not. This exercises it on a single valve you name, and restores the mode it
found afterwards.

    uv run python tools/try_write.py --account david --valve-id 4613

Credentials come from .env; see .env.example.

Defaults to `bypass` because it is the reversible mode: it self-reverts after
the valve's bypass timer even if this script dies partway through. Setting
`shutoff` closes the water and needs --i-understand-this-closes-the-water.

Only ever touches the one valve given on the command line.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from accounts import CredentialError, resolve

from pyflologic import ControlMode, FloLogicClient, FloLogicError

SETTLE_SECONDS = 20.0
"""How long to wait for the cloud to report the valve's new mode."""


async def run(args: argparse.Namespace) -> int:
    """Change one valve's mode, observe the result, and restore it."""
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

    print(f"Account : {credentials.account} ({credentials.email})")
    client = FloLogicClient(
        email=credentials.email,
        password=credentials.password,
        device=credentials.device,
    )
    await client.async_connect()
    try:
        try:
            valve = client.get_valve(args.valve_id)
        except FloLogicError as err:
            print(f"{err}\n\nValves on this account:", file=sys.stderr)
            for valve_id, known in client.valves.items():
                print(f"  {valve_id:<10} {known.name}", file=sys.stderr)
            return 2

        original = valve.control_mode
        print(f"Valve   : {valve.name} [{valve.valve_id}]")
        print(f"Site    : {valve.network_name}")
        print(f"Current : {original} (raw mode {int(valve.mode)})")
        print(f"Will set: {target}, then restore {original}")
        if original is None:
            print(
                "\nCannot determine the current mode, so it could not be "
                "restored afterwards. Stopping.",
                file=sys.stderr,
            )
            return 1

        if not args.yes:
            # In a thread: blocking the event loop while waiting on a human
            # would stall the keepalive and get the session dropped.
            answer = await asyncio.to_thread(input, "\nType the valve name: ")
            if answer.strip() != valve.name:
                print("Name did not match. Nothing sent.")
                return 1

        print(f"\nSetting {target}...")
        await client.async_set_mode(args.valve_id, target)
        print(f"  reported: {client.get_valve(args.valve_id).control_mode}")

        print(f"Waiting {SETTLE_SECONDS:g}s for the valve to settle...")
        await asyncio.sleep(SETTLE_SECONDS)
        await client.async_refresh()
        settled = client.get_valve(args.valve_id)
        print(f"  reported: {settled.control_mode} (raw mode {int(settled.mode)})")
        print(f"  flags   : {settled.mode.flag_names}")
        if settled.mode.unknown_bits:
            print(f"  UNKNOWN BITS: {settled.mode.unknown_bits:#x}")

        print(f"\nRestoring {original}...")
        await client.async_set_mode(args.valve_id, original)
        restored = client.get_valve(args.valve_id)
        print(f"  reported: {restored.control_mode} (raw mode {int(restored.mode)})")

        if restored.control_mode is original:
            print("\nWrite path works, and the valve is back where it started.")
            return 0
        print(
            f"\nWARNING: valve reports {restored.control_mode}, expected "
            f"{original}. Check the FloLogic app.",
            file=sys.stderr,
        )
        return 1
    finally:
        await client.async_disconnect()


def main() -> int:
    """Parse arguments and run the write test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="account name from .env")
    parser.add_argument("--valve-id", required=True, help="valve to test")
    parser.add_argument(
        "--mode",
        default=ControlMode.BYPASS.value,
        choices=[mode.value for mode in ControlMode],
        help="mode to set before restoring (default: bypass)",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    parser.add_argument(
        "--i-understand-this-closes-the-water",
        action="store_true",
        help="required to send shutoff",
    )
    try:
        return asyncio.run(run(parser.parse_args()))
    except FloLogicError as err:
        print(f"FloLogic error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
