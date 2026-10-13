"""Building, verifying and decoding FloLogic callbacks.

FloLogic delivers events by calling a URL with GET -- no body -- after
substituting placeholders such as ``{vid}`` and ``{event_type}`` into it and
URL-encoding each value. When the company has a signing secret, every call
carries::

    X-FloLogic-Timestamp: <unix seconds>
    X-FloLogic-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<final URL>">

The signed URL is the exact URL FloLogic requested. A receiver behind a proxy
or relay usually cannot see that URL directly -- the host and scheme change on
the way -- but it knows the template it registered, and the query string
survives intact. :func:`callback_url_from_request` rebuilds the signed URL from
those two, which is the reconstruction this module verifies against.

Nothing here needs a web framework; hand it the raw query string and headers.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

from .const import (
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    SIGNATURE_PREFIX,
    SIGNATURE_TOLERANCE,
)
from .exceptions import FloLogicSignatureError
from .models import CallbackEvent

__all__ = [
    "CALLBACK_VARIABLES",
    "build_callback_url",
    "callback_url_candidates",
    "callback_url_from_request",
    "compute_signature",
    "parse_callback",
    "verify_callback",
    "verify_signature",
]

CALLBACK_VARIABLES: tuple[str, ...] = (
    "vid",
    "event_type",
    "reason",
    "previous_mode",
    "current_mode",
    "source_vid",
    "device_type",
    "temperature",
    "temperature_threshold",
    "interval_seconds",
    "update_target",
    "occurred_at",
)
"""Every documented placeholder except the aliases.

``{message}`` repeats ``{reason}`` and ``{new_mode}`` repeats
``{current_mode}``; asking for both of a pair only lengthens the URL.
"""

_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
_HEX_ESCAPE = re.compile(r"%[0-9A-F]{2}")


def build_callback_url(
    base_url: str, variables: Iterable[str] = CALLBACK_VARIABLES
) -> str:
    """Return ``base_url`` with a ``name={name}`` query pair per variable.

    Each placeholder is keyed by its own name, which is the layout
    :func:`parse_callback` reads back. A query the base URL already carries is
    kept, ahead of the added pairs.
    """
    pairs = "&".join(f"{name}={{{name}}}" for name in variables)
    if not pairs:
        return base_url
    separator = "&" if urlsplit(base_url).query else "?"
    return f"{base_url}{separator}{pairs}"


def compute_signature(secret: str, timestamp: str | int, url: str) -> str:
    """Return the ``sha256=...`` signature FloLogic sends for ``url``."""
    digest = hmac.new(
        secret.encode(), f"{timestamp}.{url}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def _check_timestamp(timestamp: str | None, now: float, tolerance: float) -> None:
    """Raise unless ``timestamp`` is present and within ``tolerance`` of now."""
    if not timestamp:
        raise FloLogicSignatureError(f"missing {HEADER_TIMESTAMP}")
    try:
        sent = float(timestamp)
    except ValueError:
        raise FloLogicSignatureError(f"malformed {HEADER_TIMESTAMP}") from None
    # Both directions: a timestamp from the future is as suspect as a stale
    # one, but allowing the same window absorbs ordinary clock skew.
    if abs(now - sent) > tolerance:
        raise FloLogicSignatureError(
            f"timestamp is {now - sent:+.0f}s from now, outside the "
            f"{tolerance:g}s window"
        )


def verify_signature(
    secret: str,
    url: str | Iterable[str],
    *,
    timestamp: str | None,
    signature: str | None,
    now: float | None = None,
    tolerance: float = SIGNATURE_TOLERANCE,
) -> None:
    """Raise :class:`FloLogicSignatureError` unless the callback is genuine.

    ``url`` may be several candidates for the URL FloLogic signed; the
    signature is accepted if it matches any of them. Each comparison is
    constant-time. The timestamp is checked first, so a replayed request is
    rejected without its signature being examined.
    """
    _check_timestamp(timestamp, time.time() if now is None else now, tolerance)
    if not signature:
        raise FloLogicSignatureError(f"missing {HEADER_SIGNATURE}")
    candidates = [url] if isinstance(url, str) else list(url)
    presented = signature.strip().lower()
    matched = False
    for candidate in candidates:
        expected = compute_signature(secret, str(timestamp), candidate)
        # No early exit: every candidate is compared regardless.
        matched |= hmac.compare_digest(expected, presented)
    if not matched:
        raise FloLogicSignatureError("signature does not match")


def callback_url_from_request(registered_url: str, raw_query: str) -> str:
    """Rebuild the URL FloLogic signed, from the template and the raw query.

    FloLogic signs the URL it actually requested: the registered template with
    each placeholder substituted. Everything before the query is known from
    the template, and the query arrives verbatim, so joining the two recreates
    the signed URL without depending on how a proxy rewrote the host.
    """
    base = registered_url.split("?", 1)[0]
    return f"{base}?{raw_query}" if raw_query else base


def _encoder(*, form: bool, lower_hex: bool) -> Callable[[str], str]:
    """Return a URL encoder in one of the styles .NET's encoders produce."""

    def encode(value: str) -> str:
        encoded = quote_plus(value, safe="-_.~") if form else quote(value, safe="-_.~")
        if lower_hex:
            encoded = _HEX_ESCAPE.sub(lambda match: match.group(0).lower(), encoded)
        return encoded

    return encode


def _substitute(
    template: str, values: Mapping[str, str], encode: Callable[[str], str]
) -> str:
    """Fill ``template``'s placeholders from ``values``, leaving unknown ones."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        return encode(values[name]) if name in values else match.group(0)

    return _PLACEHOLDER.sub(replace, template)


def _substituted_candidates(registered_url: str, raw_query: str) -> list[str]:
    """Rebuild the signed URL by substituting values into the template.

    A fallback for a relay that re-encodes the query on the way through. The
    encodings tried are the ones .NET's URL encoders produce: RFC 3986
    percent-encoding, or form encoding with ``+`` for spaces, each with upper-
    or lower-case hex.
    """
    values = {
        key.lower(): value
        for key, value in parse_qsl(raw_query, keep_blank_values=True)
    }
    candidates: list[str] = []
    for form in (False, True):
        for lower_hex in (False, True):
            encode = _encoder(form=form, lower_hex=lower_hex)
            candidate = _substitute(registered_url, values, encode)
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def callback_url_candidates(registered_url: str, raw_query: str) -> list[str]:
    """Return every URL :func:`verify_callback` will try, most likely first.

    Exposed so a receiver can log them when a delivery fails to verify: the
    one thing that diagnoses a mismatch is seeing what was reconstructed.
    """
    candidates = [callback_url_from_request(registered_url, raw_query)]
    candidates += [
        candidate
        for candidate in _substituted_candidates(registered_url, raw_query)
        if candidate not in candidates
    ]
    return candidates


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Return a header value, matching the name case-insensitively."""
    if name in headers:
        return headers[name]
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def verify_callback(
    secret: str,
    registered_url: str,
    raw_query: str,
    headers: Mapping[str, str],
    *,
    now: float | None = None,
    tolerance: float = SIGNATURE_TOLERANCE,
) -> CallbackEvent:
    """Verify one callback delivery and return the decoded event.

    ``registered_url`` is the template as it was registered with FloLogic,
    placeholders and all; ``raw_query`` is the query string exactly as
    received, undecoded. Raises :class:`FloLogicSignatureError` when the
    signature is missing, stale or wrong -- nothing is decoded before that
    check passes.
    """
    verify_signature(
        secret,
        callback_url_candidates(registered_url, raw_query),
        timestamp=_header(headers, HEADER_TIMESTAMP),
        signature=_header(headers, HEADER_SIGNATURE),
        now=now,
        tolerance=tolerance,
    )
    return parse_callback(raw_query)


def parse_callback(query: str | Mapping[str, str]) -> CallbackEvent:
    """Decode a callback's query, given raw or as already-parsed parameters.

    Does not verify anything; use :func:`verify_callback` for deliveries that
    should be signed.
    """
    params = (
        dict(parse_qsl(query, keep_blank_values=True))
        if isinstance(query, str)
        else dict(query)
    )
    return CallbackEvent.from_params(params)
