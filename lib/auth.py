import httpx

from lib.config import E2EConfig


async def fetch_access_token(config: E2EConfig) -> str:
    """Fetch a session token from the auth service.

    Calls: POST {auth_service_url}/auth/token
    Headers: X-Access-Token: {auth_service_key}
    Body: {"userId": ..., "email": ..., "tenantId": ..., "grantType": "session_token"}

    Returns the access token string for use in the `Authorization: Bearer` header.
    """
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        resp = await client.post(
            f"{config.auth_service_url}/auth/token",
            headers={
                "Content-Type": "application/json",
                "X-Access-Token": config.auth_service_key,
            },
            json={
                "userId": config.auth_user_id,
                "email": config.auth_email,
                "tenantId": config.tenant_id,
                "grantType": "session_token",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        # The response may return the token in different shapes —
        # try common field names
        for key in ("access_token", "accessToken", "token", "access-token"):
            if key in data:
                return data[key]

        # If the response is a plain string
        if isinstance(data, str):
            return data

        raise ValueError(
            f"Could not extract access token from auth response: {data}"
        )
