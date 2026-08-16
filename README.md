# pyflologic

Async Python client for [FloLogic](https://flologic.com/) Connect leak-detection
shutoff valves.

FloLogic publishes no API. This library speaks the same SignalR protocol the
mobile app uses, reverse-engineered from its traffic. It is not affiliated with
or endorsed by FloLogic, and a server-side change could break it at any time.

## Why this exists

Every FloLogic integration I could find assumes one valve per account. Real
accounts routinely have several — plus a G-Connect gateway sitting in the same
device list — so `pyflologic` is account-scoped: it loads *all* your valves and
takes a valve ID on every command.

## Install

```bash
pip install pyflologic
```

Requires Python 3.11+. The only runtime dependency is `aiohttp`.

## Usage

```python
import asyncio

from pyflologic import ControlMode, DeviceIdentity, FloLogicClient


async def main() -> None:
    async with FloLogicClient(
        email="you@example.com",
        password="...",
        device=DeviceIdentity.generate("my-app"),
    ) as client:
        for valve_id, valve in client.account.controllable_valves.items():
            print(f"{valve.name}: {valve.status}, flowing={valve.is_water_flowing}")

        # Close the water at one specific valve.
        await client.async_set_mode(valve_id, ControlMode.SHUTOFF)


asyncio.run(main())
```

### Live updates

The client holds one websocket open and folds pushed valve updates into its
cache. Register a listener instead of polling:

```python
def on_update(account):
    for valve in account.controllable_valves.values():
        print(valve.name, valve.status)


unsubscribe = client.add_listener(on_update)
```

Pushes are best-effort, so a slow fallback poll is still worthwhile — call
`async_refresh()` on a timer. Do not poll faster than `MIN_POLL_INTERVAL`
(30 s); each poll is a real request against FloLogic's cloud.

### Device identity

FloLogic ties a session to a client-device code/token pair, the way the app
registers your phone. Generate one with `DeviceIdentity.generate()` and
**persist it** — regenerating on every start piles up phantom devices on the
account:

```python
device = DeviceIdentity.generate("Home Assistant")
save({"name": device.name, "code": device.code, "token": device.token})
```

## Reading valve state

`Valve` wraps the cloud's JSON with typed accessors, keeping the raw payload in
`valve.raw` (writes have to echo the full object back, so nothing is discarded).

| Property | Meaning |
| --- | --- |
| `name`, `model`, `firmware_version` | Identity |
| `is_online`, `is_controllable`, `is_gateway` | Availability and kind |
| `mode` | Full `ValveMode` bitfield |
| `control_mode` | The settable mode (`home`/`away`/`bypass`/`shutoff`/`disabled`) |
| `status` | One headline status, most newsworthy bit wins |
| `flow_state`, `is_water_flowing`, `current_flow_oz_per_min` | Flow |
| `temperature_f`, `battery_percent`, `signal_strength_dbm` | Telemetry |
| `active_water_off_flags` / `active_warning_flags` / `active_critical_flags` | Grouped conditions |
| `flow_started_at`, `flow_elapsed_seconds()`, `shutoff_countdown_seconds()` | Derived timing |

FloLogic packs both the current mode and every active condition into one
integer, so `mode` is an `IntFlag`:

```python
from pyflologic import ValveMode

if valve.mode & ValveMode.SENSOR_LEAK:
    print("leak sensor tripped")
print(valve.mode.flag_names)  # ['AWAY', 'SENSOR_LEAK']
```

Unrecognized bits from future firmware are preserved rather than rejected; check
`valve.mode.unknown_bits` if you want to know they were there.

### Shutoff countdown

FloLogic closes the valve after water runs continuously past the current mode's
limit. The cloud does not publish a countdown, so `shutoff_countdown_seconds()`
derives one locally from `lastNewFlow` plus the active mode's limit. It takes an
optional `now` so it stays testable and pure:

```python
remaining = valve.shutoff_countdown_seconds()
if valve.is_in_pre_alert_window():
    print(f"auto-shutoff in {remaining}s")
```

Whether the *user* would actually be warned also depends on their notification
preferences — fetch those with `async_refresh_accesses()` and check
`access.wants(NotificationSetting.ADVANCE_SHUTOFF)`.

## Writing

```python
await client.async_set_mode(valve_id, ControlMode.AWAY)

await client.async_update_settings(
    valve_id,
    home_limit_minutes=45,
    away_limit_minutes=5,
    low_temp_shutoff_f=40,
)

# Escape hatch for fields this library has not modeled:
await client.async_send_command(valve_id, {"someNewField": 1})
```

## Errors

All exceptions derive from `FloLogicError`:

| Exception | Meaning |
| --- | --- |
| `FloLogicConnectionError` | Cloud unreachable, or the socket dropped |
| `FloLogicAuthError` | Credentials or device identity rejected |
| `FloLogicTimeoutError` | Request accepted, answering event never arrived |
| `FloLogicProtocolError` | Response could not be understood |
| `FloLogicCommandError` | The hub explicitly rejected a command |
| `UnknownValveError` | No such valve on this account |

## Protocol notes

Documented for whoever maintains this next.

- The hub is ASP.NET Core SignalR over websockets, JSON protocol, frames
  terminated by `\x1e`.
- Auth is a `Login` invocation carrying email and password in the clear (over
  TLS), plus `userDeviceCode` / `userDeviceToken` headers identifying the
  client device.
- **Requests and responses are not correlated.** The hub answers with
  free-standing events (`RefreshValveArray` → `ValveArraySent`) and never uses
  SignalR completion messages, so there is no invocation ID to match on. The
  client serializes one request at a time; that is the only correlation the
  protocol permits.
- **Keepalive pings are mandatory.** The hub drops idle sockets after roughly
  30 seconds. This client sends `{"type":6}` every 15 s and treats 45 s of
  server silence as a dead connection.
- The device array mixes valves and G-Connect gateways. Gateways have
  `isZGateway: true` and cannot be commanded.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

Tests run against an in-process SignalR hub that emulates FloLogic's wire
behavior — no account or network access needed.

## License

MIT
