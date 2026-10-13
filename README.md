# pyflologic

Async Python client for [FloLogic](https://flologic.com/)'s official Web API,
for Connect leak-detection shutoff valves.

It covers the whole API: both token flows, device and user listing, device
status, the mode and flow-sensitivity writes, and the company callback list,
plus helpers to build, verify and decode callbacks. It is not affiliated with
FloLogic.

The Web API reaches less of the device than the mobile app's protocol does.
[`pyflologic-unofficial`](https://github.com/pfeffed/pyflologic-unofficial)
speaks that one. Until 0.5.0 it was itself called `pyflologic`.

## Install

Not published to PyPI. Install from the repository:

```bash
pip install git+https://github.com/pfeffed/pyflologic
```

Requires Python 3.11+. The only runtime dependency is `aiohttp`.

## Usage

```python
import asyncio

from pyflologic import ApiKeyAuth, ControlMode, FloLogicClient


async def main() -> None:
    async with FloLogicClient(ApiKeyAuth("your-api-key", 123)) as client:
        valves = await client.async_refresh()
        for vid, valve in valves.items():
            print(vid, valve.status, valve.flow_state, valve.shutoff_reason)

        # Returns once the valve itself reports the new mode.
        await client.async_set_mode(vid, ControlMode.SHUTOFF)


asyncio.run(main())
```

### Authentication

| Flow | Class | Notes |
| --- | --- | --- |
| API key | `ApiKeyAuth(api_key, company_id)` | Company-scoped. Both values are required; FloLogic rejects a key without its company ID |
| OAuth2 | `OAuthAuth(client_id, tokens, on_refresh=...)` | For a FloLogic-registered app. Refresh tokens **rotate**: persist the new one in `on_refresh`, which runs before it is used |
| Your own | `AccessTokenAuth(get_token)` | When something else (Home Assistant, say) manages the OAuth lifecycle |

Tokens are renewed five minutes before their JWT `exp`, and a 401 triggers one
forced renewal and retry before it is reported as `FloLogicAuthError`.
`build_authorize_url()` and `async_exchange_code()` cover the start of the
OAuth flow.

### Reading

`async_refresh()` lists the VIDs (hourly by default, or with
`rediscover=True`) and reads their status ten at a time, the documented
maximum. `Valve` wraps each status object and keeps it in `valve.raw`.

| Property | Meaning |
| --- | --- |
| `vid`, `device_type`, `address` | Identity. There is no name in the Web API |
| `mode`, `control_mode`, `status` | The `ValveMode` bitfield, the settable mode, one headline status |
| `shutoff_reason`, `problem` | Why it is closed (`MANUAL` only when nothing caused it), and what else is wrong |
| `is_water_off`, `flow_state`, `is_water_flowing` | |
| `last_flow_change`, `temperature_f` | |
| `flow_sensitivity_oz_per_min` | `DripRate` |
| `home_limit_minutes`, `away_limit_minutes`, `away_turns_water_off` | `AwayTime == 0` is documented as "water off" |
| `bypass_enabled`, `bypass_minutes` | |
| `low_temp_alert_enabled` / `_f`, `low_temp_shutoff_enabled` / `_f` | Thresholds include the device's temperature offset |
| `pre_alert_minutes` | |

There is no poll loop in the library. Call `async_refresh()` on a timer no
faster than `MIN_POLL_INTERVAL` (30 s). FloLogic documents no rate limit.

### Writing

```python
await client.async_set_mode(["414B91", "93DBB1"], ControlMode.AWAY)
await client.async_set_flow_sensitivity("414B91", 2.5)
```

A 200 from FloLogic means the request was accepted, not that the valve acted.
So each write reads the valves back until they report the change, resends to
any that have not, and raises `FloLogicCommandError` if they never do. Pass
`confirm=False` to skip that. `ControlMode.DISABLED` is refused before sending,
because the Web API does not accept it.

**Flow sensitivity has a hidden floor.** On the app protocol, FloLogic
silently discards a flow sensitivity below the valve's winter flow
sensitivity, and the Web API does not expose the winter setting. Such a write
would be accepted and never applied, and surfaces here as a
`FloLogicCommandError` saying exactly that.

### Callbacks

Callbacks are company-level webhooks, called with GET after FloLogic
substitutes placeholders into the URL.

```python
from pyflologic import CallbackDefinition, build_callback_url, verify_callback

registered = build_callback_url("https://example.com/hooks/flologic")
await client.async_add_callback(CallbackDefinition(registered))  # keeps others'

# In your web handler, with the raw query string and the request headers:
event = verify_callback(secret, registered, raw_query, headers)
print(event.vid, event.event_type, event.reason, event.current_mode)
```

`POST /Callbacks` replaces the company's entire list, so
`async_add_callback()` and `async_remove_callbacks()` read, merge and write,
leaving other consumers' definitions alone.

`verify_callback()` checks the five-minute timestamp window and the
`X-FloLogic-Signature` HMAC in constant time. FloLogic signs the exact URL it
requested, and a receiver behind a proxy or relay sees a different host. So the
signed URL is rebuilt from the template you registered plus the raw query
string, with fallbacks for relays that re-encode the query.

## Differences from the documentation

These were observed against the live API on 2026-10-13:

- The paging envelope is PascalCase (`Items`, `TotalCount`…), not camelCase as
  documented. Lookups here ignore case.
- Timestamps carry no offset (`2026-10-13T15:40:10`). They are UTC and are
  parsed as such.
- `GET /System/Status` answers an unknown VID with `200 []`, not 404, and
  requires `vid_list`.
- A VID is the same identifier the app protocol calls `uuid`.
- `LastFlowDateTime` is the app protocol's `lastFlowChange`, not
  `lastNewFlow`. It moves when a flow is promoted and when it stops, so it is
  not when the current flow began.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run mypy
```

Tests run against an in-process fake API. The scripts in `tools/` talk to the
real one, using credentials from a gitignored `.env` (see `.env.example`):

| Tool | |
| --- | --- |
| `capture.py` | Read-only. Saves every read endpoint's raw response to `captures/` |
| `snapshot.py` | Read-only. What the library makes of each valve; `--diff` two moments |
| `compare.py` | Read-only. The same valves through this library and through `pyflologic-unofficial` |
| `control_test.py` | **Writes.** Changes a valve and restores it, timing how long FloLogic takes |

## License

MIT
