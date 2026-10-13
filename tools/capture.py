#!/usr/bin/env python3
"""Record exactly what the official Web API returns, before modeling any of it.

The Swagger definition carries no response schemas, so the documentation is
the only description of the payloads -- and documentation drifts. This walks
every read endpoint with plain aiohttp, independent of the library, and saves
each raw response with its status and headers.

    uv run python tools/capture.py
    uv run python tools/capture.py -o captures/before

Read-only: it calls GET endpoints only, and never the write or callback-
changing ones. Raw captures hold street addresses and account holders' names,
so they go to the gitignored captures/ directory, and what is printed here is
redacted.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from credentials import REPO_ROOT, CredentialError, resolve

BASE_URL = "https://api.myflologic.com"

REDACT = {"Address", "Email", "FirstName", "LastName", "CallbackURL", "Token"}


def redact(value: Any) -> Any:
    """Return ``value`` with personal fields masked, keeping their types visible."""
    if isinstance(value, dict):
        return {
            key: (f"<{type(item).__name__}>" if key in REDACT else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT's payload without verifying it, for inspection only."""
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


class Recorder:
    """Issues GETs and keeps every exchange for writing out."""

    def __init__(self, session: aiohttp.ClientSession, out: Path) -> None:
        """Record into ``out``."""
        self.session = session
        self.out = out
        self.token: str | None = None
        self.index = 0

    async def get(
        self,
        label: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        """GET one endpoint, save the exchange, and return status and body."""
        sent = dict(headers or {})
        if self.token:
            sent["Authorization"] = f"Bearer {self.token}"
        started = time.monotonic()
        async with self.session.get(
            f"{BASE_URL}{path}", params=params, headers=sent
        ) as response:
            text = await response.text()
            elapsed = time.monotonic() - started
            try:
                body: Any = json.loads(text) if text else None
            except ValueError:
                body = text
            self.index += 1
            record = {
                "label": label,
                "request": {"path": path, "params": params},
                "status": response.status,
                "elapsed_seconds": round(elapsed, 3),
                "headers": dict(response.headers),
                "body": body,
            }
            name = f"{self.index:02d}-{label}.json"
            (self.out / name).write_text(json.dumps(record, indent=2) + "\n")
            print(f"\n== {label}: GET {path} {params or ''}")
            print(f"   HTTP {response.status} in {elapsed:.2f}s -> {name}")
            interesting = {
                key: value
                for key, value in response.headers.items()
                if any(
                    word in key.lower() for word in ("rate", "limit", "retry", "quota")
                )
            }
            if interesting:
                print(f"   rate headers: {interesting}")
            return response.status, body


async def capture(out: Path) -> int:
    """Walk every read endpoint and record what comes back."""
    try:
        creds = resolve()
    except CredentialError as err:
        print(err, file=sys.stderr)
        return 2

    out.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        rec = Recorder(session, out)

        status, body = await rec.get(
            "token",
            "/Token",
            params={"company_id": creds.company_id},
            headers={"x-api-key": creds.api_key},
        )
        if status != 200 or not isinstance(body, dict):
            print(f"   token request failed: {body!r}")
            return 1
        print(f"   keys: {sorted(body)}")
        token = body.get("Token") or body.get("token")
        if not isinstance(token, str):
            print("   no token in the response")
            return 1
        rec.token = token
        claims = jwt_claims(token)
        print(f"   JWT claim names: {sorted(claims)}")
        for key in ("exp", "iat", "nbf"):
            if isinstance(claims.get(key), int):
                stamp = datetime.fromtimestamp(claims[key], UTC).isoformat()
                print(f"   {key}: {stamp}")

        status, body = await rec.get("system-all-default", "/System/All")
        print(f"   {json.dumps(redact(body))[:600]}")
        vids: list[str] = []
        if isinstance(body, dict):
            items = body.get("items") or body.get("Items") or []
            vids = [str(item) for item in items]
            pages = body.get("totalPages") or body.get("TotalPages") or 1
            for page in range(2, int(pages) + 1):
                _, more = await rec.get(
                    f"system-all-page{page}", "/System/All", params={"page": page}
                )
                if isinstance(more, dict):
                    more_items = more.get("items") or more.get("Items") or []
                    vids += [str(item) for item in more_items]

        # Paging parameter casing: the docs say page/pageSize, Swagger says
        # Page/PageSize. ASP.NET binds case-insensitively; confirm it.
        await rec.get(
            "system-all-paged-lower", "/System/All", params={"page": 1, "pageSize": 1}
        )
        await rec.get(
            "system-all-paged-pascal", "/System/All", params={"Page": 1, "PageSize": 1}
        )

        if vids:
            for start in range(0, len(vids), 10):
                chunk = vids[start : start + 10]
                status, body = await rec.get(
                    f"system-status-{start // 10 + 1}",
                    "/System/Status",
                    params={"vid_list": ",".join(chunk)},
                )
                print(json.dumps(redact(body), indent=2)[:4000])
        status, body = await rec.get("system-status-no-vids", "/System/Status")
        print(f"   {json.dumps(redact(body))[:400]}")
        await rec.get(
            "system-status-unknown-vid",
            "/System/Status",
            params={"vid_list": "ZZZ999"},
        )

        status, body = await rec.get("user-all", "/User/All")
        print(f"   {json.dumps(redact(body))[:800]}")
        user_ids: list[int] = []
        if isinstance(body, dict):
            items = body.get("items") or body.get("Items") or []
            user_ids = [
                item["Id"]
                for item in items
                if isinstance(item, dict) and isinstance(item.get("Id"), int)
            ]
        for user_id in user_ids[:5]:
            _, body = await rec.get(
                f"user-status-{user_id}", "/User/Status", params={"user_id": user_id}
            )
            print(f"   {json.dumps(redact(body))[:400]}")

        status, body = await rec.get("callbacks", "/Callbacks")
        print(f"   {json.dumps(redact(body))[:400]}")
        if isinstance(body, list):
            print(f"   {len(body)} callback definition(s) configured")

    print(f"\nraw captures in {out}")
    return 0


def main() -> int:
    """Parse arguments and run the capture."""
    parser = argparse.ArgumentParser(description=__doc__)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    parser.add_argument(
        "-o", "--out", type=Path, default=REPO_ROOT / "captures" / stamp
    )
    args = parser.parse_args()
    return asyncio.run(capture(args.out))


if __name__ == "__main__":
    sys.exit(main())
