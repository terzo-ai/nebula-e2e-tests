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

import json
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
            raise AuthError(_format_http_error("Analytics login", url, resp))

    return _extract_token_from_http_response(resp, step="Analytics login")


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
            raise AuthError(_format_http_error("auth-service token exchange", url, resp))

    return _extract_token_from_http_response(resp, step="auth-service exchange")


async def fetch_access_token(config: E2EConfig) -> str:
    """Run the full two-step flow and return the Nebula gateway bearer token.

    Called by the `access_token` pytest fixture when `config.token` is empty.

    Step 1 — POST ``/_/api/auth/login/password`` with the CSRF token and
             capture the ``x-access-token`` value from the response's
             ``Set-Cookie`` header.
    Step 2 — POST ``{auth_service_url}/auth/token`` with that cookie
             value as the ``X-Access-Token`` header; the response JSON
             contains ``{"token": "...<gateway bearer>..."}``.

    Step 2's host is cluster-internal (``*.product-internal.terzocloud.com``)
    and is only reachable from the Dev cluster / bastion — GitHub-hosted
    runners must set ``E2E_TOKEN`` manually to short-circuit this flow.
    """
    x_access_token = await fetch_analytics_session_cookie(config)
    return await fetch_auth_service_bearer(config, x_access_token)


async def fetch_analytics_session_cookie(config: E2EConfig) -> str:
    """POST the Analytics login and return the `x-access-token` cookie value.

    The UI contract-drive endpoint (`/_/api/contract-drive/<id>/add`)
    authenticates the caller via that cookie. Login itself only needs
    the CSRF token (sent as both the `X-XSRF-TOKEN` header and the
    `XSRF-TOKEN=<token>` cookie — no separate session cookie is
    required beforehand). Max-Age on the cookie is ~24 h, so refetching
    it per test run keeps the value fresh.
    """
    if not (config.analytics_email and config.analytics_password):
        raise AuthError(
            "analytics_email / analytics_password must be set "
            "(E2E_ANALYTICS_EMAIL / E2E_ANALYTICS_PASSWORD) — cannot mint "
            "the UI session cookie"
        )
    if not config.analytics_xsrf_token:
        raise AuthError(
            "analytics_xsrf_token must be set (E2E_ANALYTICS_XSRF_TOKEN) — "
            "Analytics login requires a CSRF token"
        )

    url = f"{config.analytics_base_url.rstrip('/')}/_/api/auth/login/password"
    headers = {
        "Origin": config.analytics_base_url,
        "Referer": f"{config.analytics_base_url.rstrip('/')}/login",
        "Content-Type": "application/json",
        "X-XSRF-TOKEN": config.analytics_xsrf_token,
        "Cookie": f"XSRF-TOKEN={config.analytics_xsrf_token}",
    }
    body = {
        "email": config.analytics_email,
        "password": config.analytics_password,
        "type": "b",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise AuthError(_format_http_error("Analytics login (UI cookie)", url, resp))

    # httpx parses Set-Cookie into resp.cookies. Fall back to scanning the
    # raw Set-Cookie headers for servers that emit odd cookie attributes.
    cookie = resp.cookies.get("x-access-token")
    if not cookie:
        for set_cookie in resp.headers.get_list("set-cookie"):
            first = set_cookie.split(";", 1)[0].strip()
            name, _, value = first.partition("=")
            if name.lower() == "x-access-token" and value:
                cookie = value
                break
    if not cookie:
        raise AuthError(
            "x-access-token cookie not present in Analytics login response. "
            f"Set-Cookie headers: {resp.headers.get_list('set-cookie')}"
        )
    return cookie


def _format_http_error(label: str, url: str, resp: httpx.Response) -> str:
    """Format a 4xx/5xx response with the headers most often used to explain
    method / redirect / auth failures: Allow, Location, WWW-Authenticate.
    """
    diag_headers = {
        name: resp.headers.get(name)
        for name in ("allow", "location", "www-authenticate", "content-type")
        if resp.headers.get(name) is not None
    }
    return (
        f"{label} failed: {resp.status_code} {resp.reason_phrase}\n"
        f"  url: {url}\n"
        f"  diagnostic headers: {diag_headers}\n"
        f"  response body: {resp.text[:2000]}"
    )


def _extract_token_from_http_response(resp: httpx.Response, *, step: str) -> str:
    """Find a token in the HTTP response. Tries, in order:
      1. JSON body (common shapes via _extract_token_from_response)
      2. Plain-text body that looks like a single-line token
      3. Response headers: authorization, x-access-token, access-token, token
    On complete failure, dumps status / content-type / body preview / header keys
    in the raised AuthError so we can see what the server actually returned.
    """
    # 1. JSON body
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        data = None
    if data is not None:
        try:
            return _extract_token_from_response(data, step=step)
        except AuthError:
            pass  # fall through to header / text-body lookups

    # 2. Plain-text body (single-line, reasonable length)
    body_stripped = (resp.text or "").strip()
    if body_stripped and "\n" not in body_stripped and " " not in body_stripped and 16 <= len(body_stripped) <= 4096:
        return body_stripped

    # 3. Response headers
    for header_name in ("authorization", "x-access-token", "access-token", "token"):
        value = resp.headers.get(header_name, "").strip()
        if value:
            if value.lower().startswith("bearer "):
                value = value[7:].strip()
            if value:
                return value

    location = resp.headers.get("location", "(none)")
    raise AuthError(
        f"{step}: could not find token in response.\n"
        f"  status: {resp.status_code} {resp.reason_phrase}\n"
        f"  content-type: {resp.headers.get('content-type', '(none)')!r}\n"
        f"  location: {location!r}\n"
        f"  body (first 1000 chars): {resp.text[:1000]!r}\n"
        f"  response header keys: {sorted(resp.headers.keys())}"
    )


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
