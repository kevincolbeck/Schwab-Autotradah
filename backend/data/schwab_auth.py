"""
Schwab OAuth2 token management.

Flow:
  1. Call get_auth_url() → open in browser
  2. Schwab redirects to redirect_uri with ?code=...
  3. Call exchange_code(code) → stores access + refresh tokens
  4. All subsequent calls use get_valid_token() which auto-refreshes when needed
"""

import time
import base64
import httpx
import asyncio
import logging
from urllib.parse import urlencode, urlparse, parse_qs
from dotenv import set_key, dotenv_values

from backend.config import settings, SCHWAB_API_BASE

logger = logging.getLogger(__name__)

ENV_FILE = ".env"

TOKEN_URL   = f"{SCHWAB_API_BASE}/v1/oauth/token"
AUTH_URL    = f"{SCHWAB_API_BASE}/v1/oauth/authorize"

# In-memory cache so we don't hit disk on every request
_access_token: str = settings.schwab_access_token
_refresh_token: str = settings.schwab_refresh_token
_token_expiry: float = settings.schwab_token_expiry   # unix timestamp


def get_auth_url() -> str:
    params = {
        "response_type": "code",
        "client_id": settings.schwab_client_id,
        "redirect_uri": settings.schwab_redirect_uri,
        "scope": "readonly",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _basic_auth_header() -> str:
    creds = f"{settings.schwab_client_id}:{settings.schwab_client_secret}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


def _persist_tokens(access: str, refresh: str, expiry: float) -> None:
    global _access_token, _refresh_token, _token_expiry
    _access_token = access
    _refresh_token = refresh
    _token_expiry = expiry
    set_key(ENV_FILE, "SCHWAB_ACCESS_TOKEN", access)
    set_key(ENV_FILE, "SCHWAB_REFRESH_TOKEN", refresh)
    set_key(ENV_FILE, "SCHWAB_TOKEN_EXPIRY", str(expiry))


async def exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens. Call once after browser redirect."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.schwab_redirect_uri,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    expiry = time.time() + data["expires_in"] - 60   # 60s buffer
    _persist_tokens(data["access_token"], data["refresh_token"], expiry)
    logger.info("Schwab tokens obtained via authorization code")
    return data


async def _refresh_access_token() -> None:
    global _access_token, _refresh_token, _token_expiry
    if not _refresh_token:
        raise RuntimeError("No refresh token stored. Run the OAuth2 flow first.")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": _refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    expiry = time.time() + data["expires_in"] - 60
    new_refresh = data.get("refresh_token", _refresh_token)
    _persist_tokens(data["access_token"], new_refresh, expiry)
    logger.info("Schwab access token refreshed")


async def get_valid_token() -> str:
    """Return a valid access token, refreshing if within 60s of expiry."""
    global _access_token, _token_expiry
    if not _access_token or time.time() >= _token_expiry:
        await _refresh_access_token()
    return _access_token


async def get_streamer_info() -> dict:
    """
    Fetch streaming connection metadata from the Schwab trader API.
    Returns dict with streamerSocketUrl, schwabClientCustomerId, etc.
    """
    token = await get_valid_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SCHWAB_API_BASE}/trader/v1/userPreference",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()

    # Extract streamer info from the response
    streamer_info = data.get("streamerInfo", [{}])[0]
    return streamer_info


def has_tokens() -> bool:
    return bool(_access_token and _refresh_token)
