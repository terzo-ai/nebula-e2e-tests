"""Two-step auth flow for Nebula E2E tests.

  Step 1 (public):  POST {analytics_base_url}/_/api/auth/login/password
                    with email + password + X-XSRF-TOKEN + Cookie
                    → returns an Analytics ACCESS_TOKEN.

  Step 2 (internal): POST {auth_service_url}/auth/token
                    with X-Access-Token: <Analytics ACCESS_TOKEN>
                    and {userId, email, tenantId, grantType}
                    → returns the bearer token the Nebula gateway expects.

The auth-service is `product-internal.terzocloud.com` and is only reachable
from inside the Dev cluster. So step 2 will fail from GitHub-hosted runners
— use `E2E_TOKEN` (a manually-minted bearer) as an override in that case.
"""

from __future__ import annotations

from typing import Any

import httpx

from lib.config import E2EConfig


class AuthError(RuntimeError):
    """Raised when either step of the auth flow fails."""


async def fetch_analytics_access_token(config: E2EConfig) -> str:
    """Step 1: log in to Analytics and return the ACCESS_TOKEN.

    Uses the `analytics_*` config fields (email, password, xsrf_token, cookie).
    """
    if not (config.analytics_email and config.analytics_password):
        raise AuthError(
            "analytics_email / analytics_password must be set (E2E_ANALYTICS_EMAIL / "
            "E2E_ANALYTICS_PASSWORD) to run the two-step auth flow"
        )
    if not (config.analytics_xsrf_token and config.analytics_cookie):
        raise AuthError(
            "analytics_xsrf_token / analytics_cookie must be set "
            "(E2E_ANALYTICS_XSRF_TOKEN / E2E_ANALYTICS_COOKIE)"
        )

    url = f"{config.analytics_base_url.rstrip('/')}/_/api/auth/login/password"
    headers = {
        "Origin": config.analytics_base_url,
        "Referer": f"{config.analytics_base_url.rstrip('/')}/login",
        "Content-Type": "application/json",
        "X-XSRF-TOKEN": config.analytics_xsrf_token,
        "Cookie": config.analytics_cookie,
    }
    body = {
        "email": config.analytics_email,
        "password": config.analytics_password,
        "type": "b",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise AuthError(
                f"Analytics login failed: {resp.status_code} {resp.reason_phrase}\n"
                f"  url: {url}\n"
                f"  response body: {resp.text[:2000]}"
            )

    return _extract_token_from_response(resp.json(), step="Analytics login")


async def fetch_auth_service_bearer(config: E2EConfig, analytics_access_token: str) -> str:
    """Step 2: exchange the Analytics ACCESS_TOKEN for a Nebula gateway bearer."""
    url = f"{config.auth_service_url.rstrip('/')}/auth/token"
    headers = {
        "Content-Type": "application/json",
        "X-Access-Token": analytics_access_token,
    }
    body = {
        "userId": str(config.auth_user_id),
        "email": config.auth_email,
        "tenantId": str(config.tenant_id),
        "grantType": "session_token",
    }

    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise AuthError(
                f"auth-service token exchange failed: "
                f"{resp.status_code} {resp.reason_phrase}\n"
                f"  url: {url}\n"
                f"  response body: {resp.text[:2000]}"
            )

    return _extract_token_from_response(resp.json(), step="auth-service exchange")


async def fetch_access_token(config: E2EConfig) -> str:
    """Run the full two-step flow and return the Nebula gateway bearer token.

    Called by the `access_token` pytest fixture when `config.token` is empty.
    """
    analytics_token = await fetch_analytics_access_token(config)
    return await fetch_auth_service_bearer(config, analytics_token)


def _extract_token_from_response(data: Any, *, step: str) -> str:
    """Find a token string in common response shapes.

    Accepts the token at any of: accessToken, access_token, token, access-token,
    data.*, result.* — or a plain string body.
    """
    if isinstance(data, str):
        if data.strip():
            return data.strip()
        raise AuthError(f"{step}: response body is empty string")

    if not isinstance(data, dict):
        raise AuthError(f"{step}: unexpected response type {type(data).__name__}: {data!r}")

    for key in ("accessToken", "access_token", "token", "access-token"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value

    # Some APIs nest under `data` or `result`
    for container in ("data", "result"):
        nested = data.get(container)
        if isinstance(nested, dict):
            for key in ("accessToken", "access_token", "token", "access-token"):
                value = nested.get(key)
                if isinstance(value, str) and value:
                    return value

    raise AuthError(
        f"{step}: could not extract token from response. Keys seen: "
        f"{list(data.keys())}. Full response: {data!r}"
    )
