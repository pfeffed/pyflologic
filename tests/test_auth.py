"""Token handling: both FloLogic flows, renewal, and rejection."""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest

from pyflologic import (
    AccessTokenAuth,
    ApiKeyAuth,
    FloLogicAuthError,
    FloLogicClient,
    OAuthAuth,
    OAuthTokens,
    async_exchange_code,
    build_authorize_url,
)
from pyflologic.auth import token_expiry

from .conftest import ClientFactory
from .fake_api import API_KEY, COMPANY_ID, FakeApi, make_token


class TestTokenExpiry:
    def test_reads_the_exp_claim(self):
        token = make_token("x", lifetime=3600, issued=1_000_000)
        assert token_expiry(token) == 1_003_600

    @pytest.mark.parametrize("token", ["", "not-a-jwt", "a.!!!.c", "a.e30.c"])
    def test_unreadable_tokens_have_no_expiry(self, token: str):
        assert token_expiry(token) is None


class TestApiKeyFlow:
    async def test_one_token_serves_many_requests(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_refresh()
        await client.async_get_callbacks()
        assert api.token_requests == 1

    async def test_a_wrong_key_says_so(self, make_client: ClientFactory):
        client = make_client(auth=ApiKeyAuth("wrong", COMPANY_ID))
        with pytest.raises(FloLogicAuthError, match="Invalid API key"):
            await client.async_refresh()

    async def test_a_wrong_company_says_so(self, make_client: ClientFactory):
        """The two 401s differ only in their text, which is the useful part."""
        client = make_client(auth=ApiKeyAuth(API_KEY, 999))
        with pytest.raises(FloLogicAuthError, match="Invalid company ID"):
            await client.async_refresh()

    async def test_a_token_near_expiry_is_renewed_before_use(
        self, make_client: ClientFactory, api: FakeApi
    ):
        api.token_lifetime = 60  # inside the renewal margin from the start
        client = make_client()
        await client.async_refresh()  # lists VIDs, then reads status
        await client.async_refresh()  # reads status
        assert api.token_requests == 3  # every request found the token stale

    async def test_a_rejected_token_is_renewed_once_and_retried(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_refresh()
        api.revoke_tokens()
        await client.async_refresh(rediscover=True)
        assert api.token_requests == 2

    async def test_a_second_rejection_is_an_auth_error(
        self, client: FloLogicClient, api: FakeApi
    ):
        await client.async_refresh()
        api.fail_next = [(401, ""), (401, "")]
        with pytest.raises(FloLogicAuthError):
            await client.async_refresh(rediscover=True)

    async def test_concurrent_requests_share_one_token_request(
        self, client: FloLogicClient, api: FakeApi
    ):
        await asyncio.gather(*(client.async_get_callbacks() for _ in range(5)))
        assert api.token_requests == 1

    async def test_expiry_is_exposed(self, client: FloLogicClient):
        auth = client.auth
        assert isinstance(auth, ApiKeyAuth)
        assert auth.expires_at is None
        await client.async_refresh()
        assert auth.expires_at is not None
        assert auth.expires_at > time.time() + 3600


class TestOAuthFlow:
    def test_authorize_url_carries_the_documented_parameters(self):
        url = build_authorize_url("client-1", "https://my.example/cb", "s1")
        assert url == (
            "https://dashboard.myflologic.com/dashboard?client_id=client-1"
            "&redirect_uri=https%3A%2F%2Fmy.example%2Fcb&state=s1"
        )

    def test_authorize_url_accepts_extra_parameters(self):
        url = build_authorize_url(
            "c", "https://x/cb", "s", extra={"response_type": "code"}
        )
        assert url.endswith("&response_type=code")

    async def test_a_code_is_exchanged_for_tokens(
        self, api: FakeApi, session: aiohttp.ClientSession
    ):
        tokens = await async_exchange_code(
            session,
            code="code-1",
            client_id="client-1",
            client_secret="client-secret",
            redirect_uri="https://my.example/cb",
            base_url=api.url,
        )
        assert tokens.refresh_token == "refresh-1"
        assert tokens.token_type == "Bearer"
        assert tokens.expires_at is not None

    async def test_a_used_code_is_refused(
        self, api: FakeApi, session: aiohttp.ClientSession
    ):
        api.auth_codes.clear()
        with pytest.raises(FloLogicAuthError, match="invalid_grant"):
            await async_exchange_code(
                session,
                code="code-1",
                client_id="client-1",
                redirect_uri="https://my.example/cb",
                base_url=api.url,
            )

    async def test_refresh_rotates_and_reports_before_use(
        self, api: FakeApi, make_client: ClientFactory
    ):
        """The new refresh token must reach the caller before anything else.

        Rotation invalidates the old one immediately, so a caller that only
        learns of the new token after the request has nothing valid to store
        if the process dies in between.
        """
        seen: list[str] = []

        def on_refresh(tokens: OAuthTokens) -> None:
            seen.append(tokens.refresh_token)
            # Not yet used: the refresh itself is still the latest request.
            assert api.paths()[-1] == "/oauth/token"

        stale = OAuthTokens(access_token="expired", refresh_token="refresh-0")
        auth = OAuthAuth("client-1", stale, on_refresh=on_refresh)
        client = make_client(auth=auth)
        await client.async_refresh()
        assert seen == ["refresh-1"]
        assert auth.tokens.refresh_token == "refresh-1"
        assert "refresh-0" not in api.refresh_tokens

    async def test_an_async_refresh_callback_is_awaited(
        self, make_client: ClientFactory
    ):
        seen: list[str] = []

        async def on_refresh(tokens: OAuthTokens) -> None:
            await asyncio.sleep(0)
            seen.append(tokens.refresh_token)

        stale = OAuthTokens(access_token="expired", refresh_token="refresh-0")
        client = make_client(auth=OAuthAuth("client-1", stale, on_refresh=on_refresh))
        await client.async_refresh()
        assert seen == ["refresh-1"]

    async def test_a_dead_refresh_token_is_an_auth_error(
        self, make_client: ClientFactory
    ):
        stale = OAuthTokens(access_token="expired", refresh_token="refresh-gone")
        client = make_client(auth=OAuthAuth("client-1", stale))
        with pytest.raises(FloLogicAuthError, match="invalid_grant"):
            await client.async_refresh()

    async def test_a_valid_access_token_is_used_without_refreshing(
        self, api: FakeApi, make_client: ClientFactory
    ):
        tokens = OAuthTokens(access_token=api.issue_token(), refresh_token="refresh-0")
        client = make_client(auth=OAuthAuth("client-1", tokens))
        await client.async_refresh()
        assert "refresh-0" in api.refresh_tokens


class TestAccessTokenAuth:
    async def test_presents_the_callers_token(
        self, api: FakeApi, make_client: ClientFactory
    ):
        token = api.issue_token()
        calls = 0

        async def get_token() -> str:
            nonlocal calls
            calls += 1
            return token

        client = make_client(auth=AccessTokenAuth(get_token))
        await client.async_refresh()
        assert calls >= 1
        assert api.token_requests == 0
