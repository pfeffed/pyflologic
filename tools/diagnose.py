#!/usr/bin/env python3
"""Connect to a real FloLogic account and report what the cloud actually sends.

Everything this library knows about FloLogic's wire format was inferred from
someone else's reverse engineering. This script checks those inferences against
real hardware: it prints the decoded view of each valve next to the raw fields,
and calls out any field the library does not map.

    uv run python tools/diagnose.py --account david

Credentials come from .env; see .env.example.

Read-only: it never sends a command. Pass --watch to keep the connection open
and print pushed updates as they arrive, which is the way to confirm that
keepalive pings hold the session up.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from accounts import CredentialError, resolve

from pyflologic import (
    Account,
    FloLogicClient,
    FloLogicError,
    NotificationSetting,
    ToggledSetting,
)

# Raw valve fields this library exposes through a typed property. Anything else
# the cloud sends is worth a look -- it may be something worth modeling.
MAPPED_FIELDS = {
    "id",
    "uuid",
    "valveFriendlyName",
    "combinedName",
    "name",
    "mode",
    "flowState",
    "online",
    "isZGateway",
    "deviceTypeName",
    "softwareVersion",
    "valveAndCpFirmwareVersionString",
    "currentFlow",
    "temperature",
    "batteryLevel",
    "signalStrength",
    "dripRate",
    "homeIntervalTime",
    "awayIntervalTime",
    "bypassTime",
    "autoAwayTime",
    "lowTemperatureAlert",
    "lowTemperatureLimit",
    "preAlertNoticeInterval",
    "noFlowNoticeInterval",
    "lastNewFlow",
    "lastFlowChange",
    "lastFlowAnyChange",
    "lastSeen",
    "modified",
    "networkName",
    "valveAddress",
    "delayAwayIntervalTime",
    "winterModeTime",
    "guestModeTime",
    "temperatureOffset",
    "waterSensorHumidityAlertLimit",
    "waterSensorHumidityShutoffLimit",
    "waterSensorTemperatureAlertLimit",
    "waterSensorTemperatureShutoffLimit",
    "isSensor",
    "isZRepeater",
    "isZInput",
}

SECRET_HINTS = ("password", "token", "secret", "apikey")

# Personal details that are not protocol signal. Site name and street address
# are deliberately *not* here -- they are how a multi-house account gets split
# into the right places, so they stay visible.
PII_HINTS = ("insurance", "policy", "firstname", "lastname", "phone", "ssn")


def redact(key: str, value: Any) -> Any:
    """Hide credentials and personal details, so output is safe to paste."""
    lowered = key.lower()
    if any(hint in lowered for hint in SECRET_HINTS):
        return "<redacted:secret>"
    if any(hint in lowered for hint in PII_HINTS):
        return "<redacted:pii>"
    return value


def describe_valve(valve: Any) -> None:
    """Print the decoded view of one valve, then its unmapped raw fields."""
    print(f"\n{'=' * 70}")
    print(f"{valve.name}  [{valve.valve_id}]")
    print("=" * 70)
    print(f"  model            : {valve.model}")
    print(f"  firmware         : {valve.firmware_version}")
    print(f"  kind             : {valve.device_kind}")
    print(f"  controllable     : {valve.is_controllable}")
    print(f"  network / address: {valve.network_name} / {valve.address}")
    print(f"  online           : {valve.is_online}  (last seen {valve.last_seen})")
    print(f"  raw mode         : {int(valve.mode)}")
    print(f"  decoded flags    : {valve.mode.flag_names}")
    if valve.mode.unknown_bits:
        print(f"  !! UNKNOWN BITS  : {valve.mode.unknown_bits:#x} <-- unmapped")
    print(f"  control mode     : {valve.control_mode}")
    print(f"  status           : {valve.status}")
    print(f"  flow state       : {valve.flow_state}")
    print(f"  water flowing    : {valve.is_water_flowing}")
    print(f"  current flow     : {valve.current_flow_oz_per_min} oz/min")
    print(f"  temperature      : {valve.temperature_f} F")
    battery = f"{valve.battery_percent} %" if valve.battery_percent else "n/a"
    print(f"  battery          : {battery} (raw {valve.battery_level_raw})")
    print(f"  signal           : {valve.signal_strength_dbm} dBm")
    print("  --- settings ---")
    print(f"  flow sensitivity : {valve.flow_sensitivity_oz_per_min} oz/min")
    print(f"  home limit       : {valve.home_limit_minutes} min")
    print(f"  away limit       : {valve.away_limit_minutes} min")
    print(f"  bypass time      : {valve.bypass_minutes} min")

    def toggled(setting: ToggledSetting, unit: str) -> str:
        """Render a signed setting the way the app does: a switch and a value."""
        state = "on " if setting.enabled else "off"
        return f"{state} @ {setting.configured} {unit}"

    print(f"  auto away        : {toggled(valve.auto_away, 'h')}")
    print(f"  delay away       : {toggled(valve.delay_away, 'min')}")
    print(f"  winter sensitivity: {toggled(valve.winter_flow_sensitivity, 'oz/min')}")
    print(f"  guest mode       : {toggled(valve.guest_mode, '')}")
    print(f"  low temp alert   : {toggled(valve.low_temp_alert, 'F')}")
    print(f"  low temp shutoff : {toggled(valve.low_temp_shutoff, 'F')}")
    print(f"  temp offset      : {valve.temperature_offset_f} F")
    print(f"  pre-alert        : {valve.pre_alert_minutes} min")
    print("  --- derived ---")
    print(f"  flow started     : {valve.flow_started_at}")
    print(f"  elapsed          : {valve.flow_elapsed_seconds()} s")
    print(f"  shutoff in       : {valve.shutoff_countdown_seconds()} s")
    print(f"  pre-alert window : {valve.is_in_pre_alert_window()}")

    extra = {
        key: redact(key, value)
        for key, value in sorted(valve.raw.items())
        if key not in MAPPED_FIELDS and value not in (None, "", [], {})
    }
    if extra:
        print(f"\n  unmapped fields ({len(extra)}):")
        for key, value in extra.items():
            rendered = str(value)
            if len(rendered) > 60:
                rendered = f"{rendered[:57]}..."
            print(f"    {key:<38} = {rendered}")


async def watch(client: FloLogicClient, account: Account, seconds: float) -> None:
    """Print every raw field that changes, so flow behavior can be observed.

    Which timestamp field marks the start of a flow event is model-dependent
    and the single most important thing left to confirm -- the shutoff
    countdown is derived from it. Diffing raw payloads answers that by
    observation rather than by guessing at field names.
    """
    print(f"\n{'=' * 70}")
    print(f"Watching for {seconds:g}s -- run some water now")
    print(f"{'=' * 70}")
    print("Every changed raw field is printed, including keys a push drops")
    print("entirely. Watch which timestamp moves when flowState leaves 1.\n")

    previous = {valve_id: dict(valve.raw) for valve_id, valve in account.valves.items()}
    started = asyncio.get_running_loop().time()

    def on_update(updated: Account) -> None:
        elapsed = asyncio.get_running_loop().time() - started
        for valve_id, valve in updated.valves.items():
            before = previous.get(valve_id, {})
            changes = {
                key: (before.get(key), valve.raw.get(key))
                for key in before.keys() | valve.raw.keys()
                if before.get(key) != valve.raw.get(key)
            }
            previous[valve_id] = dict(valve.raw)
            if not changes:
                continue
            print(f"[{elapsed:6.1f}s] {valve.name}")
            print(
                f"           flowing={valve.is_water_flowing} "
                f"flowState={valve.flow_state} "
                f"status={valve.status} "
                f"rate={valve.current_flow_oz_per_min} "
                f"countdown={valve.shutoff_countdown_seconds()}"
            )
            for key, (old, new) in sorted(changes.items()):
                marker = "  <-- FLOW TIMESTAMP?" if "flow" in key.lower() else ""
                print(f"           {key:<34} {old!r} -> {new!r}{marker}")
            print()

    client.add_listener(on_update)
    await asyncio.sleep(seconds)
    print("Watch finished.")
    print(
        "If updates arrived throughout, the keepalive is holding the session "
        "open -- that was the other thing worth confirming."
    )


async def main() -> int:
    """Connect, describe the account, and optionally watch for pushes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="stay connected this long and print pushed updates",
    )
    parser.add_argument("--account", help="account name from .env")
    parser.add_argument("--debug", action="store_true", help="log protocol frames")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        credentials = resolve(args.account)
    except CredentialError as err:
        print(err, file=sys.stderr)
        return 2
    print(f"Using account: {credentials.account} ({credentials.email})")

    client = FloLogicClient(
        email=credentials.email,
        password=credentials.password,
        device=credentials.device,
    )

    try:
        await client.async_connect()
    except FloLogicError as err:
        print(f"Could not connect: {err}", file=sys.stderr)
        return 1

    try:
        account = client.account
        print(f"\nAccount: {account.user.email} [{account.user.user_id}]")
        print(
            f"Devices: {len(account.valves)} "
            f"({len(account.controllable_valves)} controllable)"
        )

        for valve in account.valves.values():
            describe_valve(valve)

        print(f"\n{'=' * 70}\nNotification preferences\n{'=' * 70}")
        accesses = await client.async_refresh_accesses()
        for valve_id, access in accesses.items():
            name = account.valves[valve_id].name if valve_id in account.valves else "?"
            enabled = [
                setting.name
                for setting in NotificationSetting
                if setting.name and access.wants(setting)
            ]
            print(f"  {name} [{valve_id}]: {enabled or 'none'}")
            print(
                f"    advance shutoff alerts: "
                f"{access.wants(NotificationSetting.ADVANCE_SHUTOFF)}"
            )

        print(f"\n{'=' * 70}\nScheduler\n{'=' * 70}")
        for valve_id, valve in account.controllable_valves.items():
            events = await client.async_fetch_scheduler(valve_id)
            active = [event for event in events if event.is_active]
            print(f"  {valve.name}: {len(active)} active / {len(events)} rows")

        print(f"\n{'=' * 70}\nRecent notifications\n{'=' * 70}")
        for notification in (await client.async_fetch_notifications())[:10]:
            print(f"  {notification.created_at}  {notification.message}")

        if args.watch:
            await watch(client, account, args.watch)
    finally:
        await client.async_disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
