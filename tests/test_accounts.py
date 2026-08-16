"""Tests for the local credential loader in tools/accounts.py.

Not library code, but a silent credential corruption here surfaces as an
authentication failure against a real account, which is a genuinely expensive
thing to debug.
"""

from __future__ import annotations

import os

import pytest
from accounts import CredentialError, _unquote, available_accounts, load_env, resolve


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Keep tests away from the developer's real .env and real credentials."""
    for key in list(os.environ):
        if key.startswith("FLOLOGIC_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("accounts.ENV_PATH", tmp_path / "absent.env")
    monkeypatch.setattr("accounts.DEVICE_STORE", tmp_path / "devices.json")


def write_env(tmp_path, body: str):
    """Write a .env file and return its path."""
    path = tmp_path / ".env"
    path.write_text(body)
    return path


class TestQuoteHandling:
    """Passwords are full of punctuation; stripping it is not optional."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('"quoted"', "quoted"),
            ("'quoted'", "quoted"),
            ("bare", "bare"),
            # The case that motivated this: a real password ending in a quote.
            ('>>\'f9"bN6r"?Yykp8h"ntk;\'', '>>\'f9"bN6r"?Yykp8h"ntk;\''),
            ("'unbalanced", "'unbalanced"),
            ("unbalanced'", "unbalanced'"),
            ("\"mixed'", "\"mixed'"),
            ("'", "'"),
            ("", ""),
        ],
    )
    def test_only_matched_pairs_are_stripped(self, raw, expected):
        assert _unquote(raw) == expected


class TestLoadEnv:
    """Parsing rules, including the ones that must not apply."""

    def test_loads_values(self, tmp_path, monkeypatch):
        path = write_env(tmp_path, "FLOLOGIC_A_EMAIL=a@example.com\n")
        monkeypatch.setattr("accounts.ENV_PATH", path)
        load_env(path)
        assert os.environ["FLOLOGIC_A_EMAIL"] == "a@example.com"

    def test_existing_environment_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLOLOGIC_A_EMAIL", "exported@example.com")
        path = write_env(tmp_path, "FLOLOGIC_A_EMAIL=file@example.com\n")
        load_env(path)
        assert os.environ["FLOLOGIC_A_EMAIL"] == "exported@example.com"

    def test_comments_and_blanks_are_skipped(self, tmp_path):
        path = write_env(tmp_path, "# comment\n\n  \nFLOLOGIC_A_EMAIL=a\n")
        load_env(path)
        assert os.environ["FLOLOGIC_A_EMAIL"] == "a"

    def test_a_hash_inside_a_value_is_kept(self, tmp_path):
        # '#' is a perfectly ordinary password character.
        path = write_env(tmp_path, "FLOLOGIC_A_PASSWORD=pa#ss#word\n")
        load_env(path)
        assert os.environ["FLOLOGIC_A_PASSWORD"] == "pa#ss#word"

    def test_equals_inside_a_value_is_kept(self, tmp_path):
        path = write_env(tmp_path, "FLOLOGIC_A_PASSWORD=a=b=c\n")
        load_env(path)
        assert os.environ["FLOLOGIC_A_PASSWORD"] == "a=b=c"

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        load_env(tmp_path / "nope")


class TestResolve:
    """Account selection, especially its refusal to guess."""

    def test_named_account(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLOLOGIC_DAVID_EMAIL", "d@example.com")
        monkeypatch.setenv("FLOLOGIC_DAVID_PASSWORD", "secret")
        credentials = resolve("david")
        assert credentials.email == "d@example.com"
        assert credentials.account == "david"

    def test_account_names_are_case_insensitive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLOLOGIC_DAVID_EMAIL", "d@example.com")
        monkeypatch.setenv("FLOLOGIC_DAVID_PASSWORD", "secret")
        assert resolve("DaViD").email == "d@example.com"

    def test_several_accounts_must_be_named(self, monkeypatch, tmp_path):
        # Picking whichever sorted first could run a control test against the
        # wrong house.
        for name in ("DAVID", "SAMPLE"):
            monkeypatch.setenv(f"FLOLOGIC_{name}_EMAIL", "x@example.com")
            monkeypatch.setenv(f"FLOLOGIC_{name}_PASSWORD", "secret")
        with pytest.raises(CredentialError, match="name one with --account"):
            resolve()

    def test_a_single_account_needs_no_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLOLOGIC_SOLO_EMAIL", "s@example.com")
        monkeypatch.setenv("FLOLOGIC_SOLO_PASSWORD", "secret")
        assert resolve().account == "solo"

    def test_unknown_account_lists_what_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLOLOGIC_DAVID_EMAIL", "d@example.com")
        monkeypatch.setenv("FLOLOGIC_DAVID_PASSWORD", "secret")
        with pytest.raises(CredentialError, match="david"):
            resolve("nosuch")

    def test_no_credentials_at_all(self, monkeypatch, tmp_path):
        with pytest.raises(CredentialError, match="No credentials found"):
            resolve()

    def test_an_account_missing_its_password_is_not_offered(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("FLOLOGIC_HALF_EMAIL", "h@example.com")
        assert available_accounts() == []


class TestDeviceIdentity:
    """Identities must be stable across runs, and distinct across accounts."""

    def test_identity_is_reused(self, monkeypatch, tmp_path):
        store = tmp_path / "devices.json"
        monkeypatch.setattr("accounts.DEVICE_STORE", store)
        monkeypatch.setenv("FLOLOGIC_DAVID_EMAIL", "d@example.com")
        monkeypatch.setenv("FLOLOGIC_DAVID_PASSWORD", "secret")
        # A fresh identity per run would register a throwaway device each time.
        assert resolve("david").device == resolve("david").device

    def test_accounts_get_distinct_identities(self, monkeypatch, tmp_path):
        for name in ("DAVID", "SAMPLE"):
            monkeypatch.setenv(f"FLOLOGIC_{name}_EMAIL", "x@example.com")
            monkeypatch.setenv(f"FLOLOGIC_{name}_PASSWORD", "secret")
        assert resolve("david").device.code != resolve("sample").device.code

    def test_the_store_is_not_world_readable(self, monkeypatch, tmp_path):
        store = tmp_path / "devices.json"
        monkeypatch.setattr("accounts.DEVICE_STORE", store)
        monkeypatch.setenv("FLOLOGIC_DAVID_EMAIL", "d@example.com")
        monkeypatch.setenv("FLOLOGIC_DAVID_PASSWORD", "secret")
        resolve("david")
        assert store.stat().st_mode & 0o077 == 0

    def test_a_corrupt_store_is_replaced(self, monkeypatch, tmp_path):
        store = tmp_path / "devices.json"
        store.write_text("{not json")
        monkeypatch.setattr("accounts.DEVICE_STORE", store)
        monkeypatch.setenv("FLOLOGIC_DAVID_EMAIL", "d@example.com")
        monkeypatch.setenv("FLOLOGIC_DAVID_PASSWORD", "secret")
        assert resolve("david").device.code.startswith("AND-")
