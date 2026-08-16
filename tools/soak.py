#!/usr/bin/env python3
"""Hold a live connection open for a long time and report what it does.

The keepalive has been proven across five minutes with a 210-second silent
gap. That is not the same as proving it holds for the hours a Home Assistant
integration will run unattended, where a nightly cloud recycle or an idle NAT
timeout is what actually breaks things.

    uv run python tools/soak.py --account david --minutes 60

Read-only: it polls and listens, and never sends a command. Prints one line
per minute so a stall is visible in the output rather than only at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from accounts import CredentialError, resolve

from pyflologic import Account, FloLogicClient, FloLogicError


class Soak:
    """Tracks connection health over a long run."""

    def __init__(self) -> None:
        """Start the counters."""
        self.started = datetime.now(UTC)
        self.pushes = 0
        self.polls_ok = 0
        self.polls_failed = 0
        self.reconnects = 0
        self.last_push: datetime | None = None
        self.longest_silence = 0.0
        self.failures: list[str] = []

    def on_push(self, _account: Account) -> None:
        """Record a pushed update."""
        now = datetime.now(UTC)
        if self.last_push is not None:
            gap = (now - self.last_push).total_seconds()
            self.longest_silence = max(self.longest_silence, gap)
        self.last_push = now
        self.pushes += 1

    def line(self) -> str:
        """Return a one-line status summary."""
        elapsed = (datetime.now(UTC) - self.started).total_seconds()
        return (
            f"[{elapsed / 60:6.1f}m] pushes={self.pushes:<4} "
            f"polls ok={self.polls_ok:<4} failed={self.polls_failed:<3} "
            f"reconnects={self.reconnects:<3} "
            f"longest push gap={self.longest_silence / 60:.1f}m"
        )


async def run(args: argparse.Namespace) -> int:
    """Hold the connection open and report health once a minute."""
    try:
        credentials = resolve(args.account)
    except CredentialError as err:
        print(err, file=sys.stderr)
        return 2

    soak = Soak()
    client = FloLogicClient(
        email=credentials.email,
        password=credentials.password,
        device=credentials.device,
    )
    await client.async_connect()
    client.add_listener(soak.on_push)
    print(f"connected as {credentials.account}, {len(client.valves)} valves")
    print(f"soaking for {args.minutes:g} minutes, polling every {args.poll:g}s")

    connected = True
    deadline = asyncio.get_running_loop().time() + args.minutes * 60
    next_report = asyncio.get_running_loop().time()
    try:
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(args.poll)

            # A drop the client recovers from on its own still counts.
            if client.connected != connected:
                if client.connected:
                    soak.reconnects += 1
                    print(f"    reconnected at {soak.line()}")
                else:
                    print("    connection lost, waiting for auto-reconnect")
                connected = client.connected

            try:
                await client.async_refresh()
                soak.polls_ok += 1
            except FloLogicError as err:
                soak.polls_failed += 1
                soak.failures.append(f"{datetime.now(UTC).isoformat()} {err}")
                print(f"    poll failed: {err}")

            if asyncio.get_running_loop().time() >= next_report:
                print(soak.line())
                next_report = asyncio.get_running_loop().time() + 60
    finally:
        await client.async_disconnect()

    print("\n--- soak finished ---")
    print(soak.line())
    if soak.failures:
        print(f"\n{len(soak.failures)} failures:")
        for failure in soak.failures[:20]:
            print(f"  {failure}")
        return 1
    print("\nNo failures. Connection held for the whole run.")
    return 0


def main() -> int:
    """Parse arguments and run the soak."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="account name from .env")
    parser.add_argument("--minutes", type=float, default=60.0, help="how long to soak")
    parser.add_argument(
        "--poll",
        type=float,
        default=120.0,
        help="seconds between polls (default 120; do not go below 30)",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except FloLogicError as err:
        print(f"FloLogic error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
