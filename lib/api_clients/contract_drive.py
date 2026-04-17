"""Client for the UI contract-drive file upload endpoint.

Target: POST {analytics_base_url}/_/api/contract-drive/{drive_id}/add

Unlike the Nebula gateway clients, this endpoint lives on the Analytics/mafia
host and uses two cookies to authenticate:

    1. ``XSRF-TOKEN=<token>`` — the CSRF double-submit value (also sent as the
       ``X-XSRF-TOKEN`` header).
    2. ``x-access-token=<session>`` — the session cookie returned by the
       Analytics login endpoint (``POST /_/api/auth/login/password``).

Callers supply both pieces explicitly; the fixture in ``conftest.py`` mints a
fresh session cookie per run via ``lib.auth.fetch_analytics_session_cookie``.

The request is `multipart/form-data` with two parts:
    file   — binary file content
    drive  — JSON string `{"driveId": <id>}`

On HTTP 200, the response body is returned verbatim (as a dict when JSON,
or wrapped in ``{"_raw": <text>}`` otherwise) so the caller can print it
into the pipeline report exactly as requested.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ContractDriveAddResponse:
    """Parsed response from POST /_/api/contract-drive/{drive_id}/add."""

    status_code: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    ufid: str | None = None

    @classmethod
    def from_http(cls, resp: httpx.Response) -> "ContractDriveAddResponse":
        try:
            body: Any = resp.json()
        except (ValueError, json.JSONDecodeError):
            body = {"_raw": resp.text}
        raw = body if isinstance(body, dict) else {"_raw": body}
        return cls(
            status_code=resp.status_code,
            raw=raw,
            ufid=_extract_ufid(body),
        )


def _extract_ufid(data: Any) -> str | None:
    """Best-effort ufid extraction — the exact response shape isn't locked
    down, so we walk common paths (ufid / document_id / documentId) at the
    top level and one level deep under `data` / `result`."""
    if not isinstance(data, dict):
        return None
    for key in ("ufid", "document_id", "documentId"):
        v = data.get(key)
        if isinstance(v, str) and v:
            return v
    for container in ("data", "result"):
        nested = data.get(container)
        if isinstance(nested, dict):
            for key in ("ufid", "document_id", "documentId"):
                v = nested.get(key)
                if isinstance(v, str) and v:
                    return v
        if isinstance(nested, list) and nested and isinstance(nested[0], dict):
            for key in ("ufid", "document_id", "documentId"):
                v = nested[0].get(key)
                if isinstance(v, str) and v:
                    return v
    return None


class ContractDriveClient:
    """Async client for `POST /_/api/contract-drive/{drive_id}/add`.

    Auth = CSRF double-submit (X-XSRF-TOKEN header + XSRF-TOKEN cookie)
    PLUS a session cookie (`x-access-token`) obtained from the Analytics
    login. Callers pass both pieces in; the client assembles the Cookie
    header as `XSRF-TOKEN=<xsrf>; x-access-token=<session>`.
    """

    UPLOAD_PATH_TEMPLATE = "/_/api/contract-drive/{drive_id}/add"
    LIST_REFERER_TEMPLATE = "{base_url}/b/contract_drive/list/all"

    def __init__(
        self,
        base_url: str,
        xsrf_token: str,
        x_access_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._xsrf_token = xsrf_token
        parts = [f"XSRF-TOKEN={xsrf_token}"]
        if x_access_token:
            parts.append(f"x-access-token={x_access_token}")
        self._cookie = "; ".join(parts)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=60.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def add_file(
        self,
        drive_id: int,
        filename: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> ContractDriveAddResponse:
        """Upload a file to the contract-drive and return the parsed response.

        Raises RuntimeError on any non-2xx status, with diagnostic headers
        and a body snippet so failures are self-diagnostic.
        """
        path = self.UPLOAD_PATH_TEMPLATE.format(drive_id=drive_id)
        referer = self.LIST_REFERER_TEMPLATE.format(base_url=self._base_url)
        headers = {
            "Referer": referer,
            "Origin": self._base_url,
            "X-XSRF-TOKEN": self._xsrf_token,
            "Cookie": self._cookie,
        }
        files = {"file": (filename, content, content_type)}
        data = {"drive": json.dumps({"driveId": drive_id})}

        resp = await self._client.post(
            path, headers=headers, files=files, data=data
        )
        if resp.status_code >= 400:
            debug_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() in {
                    "www-authenticate", "x-error", "x-request-id",
                    "x-amzn-requestid", "x-trace-id", "content-type",
                    "set-cookie", "location",
                }
            }
            body_snippet = resp.text[:2000] if resp.text else "(empty body)"
            raise RuntimeError(
                f"contract-drive add failed: {resp.status_code} {resp.reason_phrase}\n"
                f"  url: {resp.request.url}\n"
                f"  response headers: {debug_headers}\n"
                f"  response body: {body_snippet}"
            )

        parsed = ContractDriveAddResponse.from_http(resp)
        logger.info(
            "contract-drive add parsed: status=%s ufid=%s body_keys=%s",
            parsed.status_code,
            parsed.ufid,
            sorted(parsed.raw.keys()) if parsed.raw else "[]",
        )
        return parsed
