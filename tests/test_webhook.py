"""Callback URLs, signatures, and decoding."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime
from urllib.parse import quote, quote_plus

import pytest

from pyflologic import (
    CALLBACK_VARIABLES,
    CallbackEventType,
    CallbackReason,
    DeviceType,
    FloLogicSignatureError,
    UpdateTarget,
    ValveMode,
    build_callback_url,
    callback_url_from_request,
    compute_signature,
    parse_callback,
    verify_callback,
    verify_signature,
)

SECRET = "s3cret"
NOW = 1_800_000_000

REGISTERED = build_callback_url("https://hooks.example/api/webhook/abc123")


def deliver(values: dict[str, str], *, template: str = REGISTERED) -> tuple[str, str]:
    """Substitute like FloLogic does; return the final URL and its raw query."""
    final = template
    for name in CALLBACK_VARIABLES:
        final = final.replace(f"{{{name}}}", quote(values.get(name, ""), safe=""))
    return final, final.split("?", 1)[1]


def signed(url: str, timestamp: int = NOW) -> dict[str, str]:
    return {
        "X-FloLogic-Timestamp": str(timestamp),
        "X-FloLogic-Signature": compute_signature(SECRET, timestamp, url),
    }


AUTO_SHUTOFF = {
    "vid": "414B91",
    "event_type": "AutoShutoff",
    "reason": "FlowTimeExceeded",
    "previous_mode": "Away",
    "current_mode": "Away,ShutOff,FlowTimeExceeded",
    "device_type": "WifiConnect",
    "occurred_at": "2026-10-13T16:01:02Z",
}


class TestBuildUrl:
    def test_every_variable_keyed_by_its_own_name(self):
        assert REGISTERED.startswith(
            "https://hooks.example/api/webhook/abc123?vid={vid}"
        )
        for name in CALLBACK_VARIABLES:
            assert f"{name}={{{name}}}" in REGISTERED

    def test_an_existing_query_is_kept(self):
        url = build_callback_url("https://h/x?token=1", ["vid"])
        assert url == "https://h/x?token=1&vid={vid}"

    def test_no_variables_leaves_the_url_alone(self):
        assert build_callback_url("https://h/x", []) == "https://h/x"


class TestVerifySignature:
    def test_a_genuine_signature_passes(self):
        url = "https://h/x?vid=1"
        headers = signed(url)
        verify_signature(
            SECRET,
            url,
            timestamp=headers["X-FloLogic-Timestamp"],
            signature=headers["X-FloLogic-Signature"],
            now=NOW,
        )

    def test_the_documented_payload_shape(self):
        """HMAC-SHA256 over "<timestamp>.<url>", hex, prefixed sha256=."""
        expected = hmac.new(b"k", b"1.u", hashlib.sha256).hexdigest()
        assert compute_signature("k", 1, "u") == f"sha256={expected}"

    @pytest.mark.parametrize("skew", [301, -301])
    def test_a_timestamp_outside_five_minutes_is_rejected(self, skew: int):
        url = "https://h/x"
        headers = signed(url, NOW + skew)
        with pytest.raises(FloLogicSignatureError, match="window"):
            verify_signature(
                SECRET,
                url,
                timestamp=headers["X-FloLogic-Timestamp"],
                signature=headers["X-FloLogic-Signature"],
                now=NOW,
            )

    def test_the_wrong_secret_is_rejected(self):
        url = "https://h/x"
        headers = signed(url)
        with pytest.raises(FloLogicSignatureError, match="does not match"):
            verify_signature(
                "other",
                url,
                timestamp=headers["X-FloLogic-Timestamp"],
                signature=headers["X-FloLogic-Signature"],
                now=NOW,
            )

    @pytest.mark.parametrize(
        ("timestamp", "signature", "message"),
        [
            (None, "sha256=x", "missing X-FloLogic-Timestamp"),
            ("soon", "sha256=x", "malformed"),
            (str(NOW), None, "missing X-FloLogic-Signature"),
        ],
    )
    def test_missing_or_malformed_headers_are_rejected(
        self, timestamp: str | None, signature: str | None, message: str
    ):
        with pytest.raises(FloLogicSignatureError, match=message):
            verify_signature(
                SECRET, "u", timestamp=timestamp, signature=signature, now=NOW
            )

    def test_uppercase_hex_is_accepted(self):
        headers = signed("u")
        verify_signature(
            SECRET,
            "u",
            timestamp=headers["X-FloLogic-Timestamp"],
            signature=headers["X-FloLogic-Signature"]
            .upper()
            .replace("SHA256=", "sha256="),
            now=NOW,
        )


class TestVerifyCallback:
    def test_a_relayed_delivery_verifies_from_template_and_raw_query(self):
        """The receiver sees another host; the template supplies the signed one."""
        final, raw_query = deliver(AUTO_SHUTOFF)
        event = verify_callback(SECRET, REGISTERED, raw_query, signed(final), now=NOW)
        assert event.vid == "414B91"

    def test_a_relay_that_decodes_the_query_still_verifies(self):
        final, raw_query = deliver(AUTO_SHUTOFF)
        decoded = raw_query.replace("%3A", ":").replace("%2C", ",")
        verify_callback(SECRET, REGISTERED, decoded, signed(final), now=NOW)

    def test_form_encoding_with_lowercase_hex_verifies(self):
        values = {**AUTO_SHUTOFF, "reason": "Two words"}
        final = REGISTERED
        for name in CALLBACK_VARIABLES:
            encoded = quote_plus(values.get(name, ""), safe="")
            encoded = re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), encoded)
            final = final.replace(f"{{{name}}}", encoded)
        # The relay passes on an RFC 3986 re-encoding instead.
        _, raw_query = deliver(values)
        assert raw_query not in final
        verify_callback(SECRET, REGISTERED, raw_query, signed(final), now=NOW)

    def test_a_tampered_value_fails(self):
        final, raw_query = deliver(AUTO_SHUTOFF)
        forged = raw_query.replace("AutoShutoff", "ModeChanged")
        with pytest.raises(FloLogicSignatureError):
            verify_callback(SECRET, REGISTERED, forged, signed(final), now=NOW)

    def test_header_names_are_case_insensitive(self):
        final, raw_query = deliver(AUTO_SHUTOFF)
        headers = {key.lower(): value for key, value in signed(final).items()}
        verify_callback(SECRET, REGISTERED, raw_query, headers, now=NOW)

    def test_the_url_is_rebuilt_from_the_template_base(self):
        assert (
            callback_url_from_request("https://a/b?vid={vid}", "vid=1")
            == "https://a/b?vid=1"
        )
        assert callback_url_from_request("https://a/b", "") == "https://a/b"


class TestParseCallback:
    def test_decodes_an_auto_shutoff(self):
        _, raw_query = deliver(AUTO_SHUTOFF)
        event = parse_callback(raw_query)
        assert event.event_type is CallbackEventType.AUTO_SHUTOFF
        assert event.reason is CallbackReason.FLOW_TIME_EXCEEDED
        assert event.previous_mode == ValveMode.AWAY
        assert event.current_mode == (
            ValveMode.AWAY | ValveMode.SHUTOFF | ValveMode.FLOW_TIME_EXCEEDED
        )
        assert event.device_type is DeviceType.WIFI_CONNECT
        assert event.occurred_at == datetime(2026, 10, 13, 16, 1, 2, tzinfo=UTC)
        assert event.temperature is None
        assert event.unknown_mode_names == []

    def test_decodes_temperature_and_timer_fields(self):
        event = parse_callback(
            "vid=1&event_type=PreAlert&reason=ShutoffPending&interval_seconds=300"
            "&temperature=39.5&temperature_threshold=40&update_target=ControlPanel"
        )
        assert event.event_type is CallbackEventType.PRE_ALERT
        assert event.interval_seconds == 300
        assert event.temperature == 39.5
        assert event.temperature_threshold == 40
        assert event.update_target is UpdateTarget.CONTROL_PANEL

    def test_unsubstituted_placeholders_read_as_absent(self):
        event = parse_callback("vid=1&event_type=Online&reason={reason}&source_vid=")
        assert event.reason is None
        assert event.reason_raw is None
        assert event.source_vid is None

    def test_aliases_are_accepted(self):
        event = parse_callback({"message": "DelayAway", "new_mode": "Away"})
        assert event.reason is CallbackReason.DELAY_AWAY
        assert event.current_mode == ValveMode.AWAY

    def test_unknown_names_are_kept_raw(self):
        event = parse_callback(
            "event_type=NewThing&reason=Mystery&current_mode=Home,Hovering"
        )
        assert event.event_type is None
        assert event.event_type_raw == "NewThing"
        assert event.reason is None
        assert event.reason_raw == "Mystery"
        assert event.current_mode == ValveMode.HOME
        assert event.unknown_mode_names == ["Hovering"]
