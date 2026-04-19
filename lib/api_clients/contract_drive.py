"""Client for the UI contract-drive file upload flow.

The Contract Drive UI no longer POSTs multipart data to a single `/add`
endpoint. Uploads now run in three steps:

    1. POST /_/api/contract-drive/{drive_id}/upload?fileName=&sizeBytes=&contentType=
       with a JSON body of ``{}``. Response is
       ``{"ufid": <str>, "uploadUrl": <azure blob SAS url>, "expiresInSeconds": <int|null>}``.

    2. PUT <uploadUrl> with the file bytes, ``x-ms-blob-type: BlockBlob``
       and the same ``Content-Type`` used in step 1. This talks directly
       to Azure Blob Storage — do NOT forward the Analytics session
       cookie or the CSRF token on this leg.

    3. POST /_/api/contract-drive/{drive_id}/confirm-upload?ufid=&fileName=&sizeBytes=
       with a JSON body of ``{}``. Tells the backend the blob is in
       place so it can start the ingestion pipeline.

Auth for steps 1 and 3 is the CSRF double-submit pair (``X-XSRF-TOKEN``
header + ``XSRF-TOKEN`` cookie) plus the ``x-access-token`` session
cookie returned by the Analytics login endpoint. Callers supply both
pieces; the fixture in ``conftest.py`` mints the session cookie per run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from lib.blob_upload import put_to_sas_url

logger = logging.getLogger(__name__)


@dataclass
class ContractDriveUploadResult:
    """Aggregated outcome of the 3-step contract-drive upload.

    ``init_raw`` carries the body of step 1 verbatim (the part the report
    renders under the test-case panel). ``ufid`` / ``upload_url`` are
    promoted to top-level for convenience; both come from the init body.
    """

    init_status_code: int = 0
    init_raw: dict[str, Any] = field(default_factory=dict)
    blob_put_status_code: int = 0
    confirm_status_code: int = 0
    confirm_raw: dict[str, Any] = field(default_factory=dict)
    ufid: str | None = None
    upload_url: str | None = None


def _json_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        body: Any = resp.json()
    except (ValueError, json.JSONDecodeError):
        return {"_raw": resp.text}
    return body if isinstance(body, dict) else {"_raw": body}


def _extract_ufid(data: Any) -> str | None:
    """Best-effort ufid extraction from a JSON body.

    The init response carries ``ufid`` at the top level, but we also
    check ``document_id`` / ``documentId`` and one level deep under
    ``data`` / ``result`` to stay resilient if the shape shifts.
    """
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
    """Async client for the 3-step UI contract-drive upload flow.

    Steps 1 and 3 target the Analytics host and reuse the same CSRF +
    session-cookie auth as the rest of the UI. Step 2 talks directly to
    Azure Blob via the SAS URL returned in step 1 and delegates to the
    shared :func:`lib.blob_upload.put_to_sas_url` helper (same one the
    gateway presigned-upload flow uses), so the session cookie never
    leaks to Azure.
    """

    INIT_UPLOAD_PATH_TEMPLATE = "/_/api/contract-drive/{drive_id}/upload"
    CONFIRM_UPLOAD_PATH_TEMPLATE = "/_/api/contract-drive/{drive_id}/confirm-upload"
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

    def _analytics_headers(self) -> dict[str, str]:
        return {
            "Referer": self.LIST_REFERER_TEMPLATE.format(base_url=self._base_url),
            "Origin": self._base_url,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "X-XSRF-TOKEN": self._xsrf_token,
            "Cookie": self._cookie,
        }

    async def init_upload(
        self,
        drive_id: int,
        filename: str,
        size_bytes: int,
        content_type: str = "application/pdf",
    ) -> httpx.Response:
        """Step 1 — ask the backend for a ufid + Azure blob SAS URL."""
        query = urlencode({
            "fileName": filename,
            "sizeBytes": size_bytes,
            "contentType": content_type,
        })
        path = (
            f"{self.INIT_UPLOAD_PATH_TEMPLATE.format(drive_id=drive_id)}?{query}"
        )
        resp = await self._client.post(
            path, headers=self._analytics_headers(), content=b"{}",
        )
        _raise_on_error(resp, step="init-upload")
        return resp

    async def put_blob(
        self,
        upload_url: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> httpx.Response:
        """Step 2 — upload the bytes directly to Azure Blob via SAS URL.

        Shared with the gateway presigned-upload path via
        :func:`lib.blob_upload.put_to_sas_url`. Forwards the Analytics
        host as ``Referer`` to mirror the browser's network trace
        (``https://mafia.terzocloud.com/``).
        """
        return await put_to_sas_url(
            upload_url,
            content,
            content_type=content_type,
            referer=f"{self._base_url}/",
        )

    async def confirm_upload(
        self,
        drive_id: int,
        ufid: str,
        filename: str,
        size_bytes: int,
    ) -> httpx.Response:
        """Step 3 — tell the backend the blob is staged and ingestion can begin."""
        query = urlencode({
            "ufid": ufid,
            "fileName": filename,
            "sizeBytes": size_bytes,
        })
        path = (
            f"{self.CONFIRM_UPLOAD_PATH_TEMPLATE.format(drive_id=drive_id)}?{query}"
        )
        resp = await self._client.post(
            path, headers=self._analytics_headers(), content=b"{}",
        )
        _raise_on_error(resp, step="confirm-upload")
        return resp

    async def upload_file(
        self,
        drive_id: int,
        filename: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> ContractDriveUploadResult:
        """Run the full 3-step upload and return an aggregated result.

        Each step raises ``RuntimeError`` on non-2xx, with diagnostic
        headers and a body snippet so failures are self-diagnostic.
        """
        size_bytes = len(content)
        result = ContractDriveUploadResult()

        init = await self.init_upload(
            drive_id=drive_id,
            filename=filename,
            size_bytes=size_bytes,
            content_type=content_type,
        )
        result.init_status_code = init.status_code
        result.init_raw = _json_body(init)
        result.ufid = _extract_ufid(result.init_raw)
        upload_url = result.init_raw.get("uploadUrl")
        result.upload_url = upload_url if isinstance(upload_url, str) else None

        if not result.ufid or not result.upload_url:
            raise RuntimeError(
                "contract-drive init-upload response missing ufid/uploadUrl: "
                f"{result.init_raw}"
            )

        blob_put = await self.put_blob(
            upload_url=result.upload_url,
            content=content,
            content_type=content_type,
        )
        result.blob_put_status_code = blob_put.status_code

        confirm = await self.confirm_upload(
            drive_id=drive_id,
            ufid=result.ufid,
            filename=filename,
            size_bytes=size_bytes,
        )
        result.confirm_status_code = confirm.status_code
        result.confirm_raw = _json_body(confirm)

        logger.info(
            "contract-drive upload complete: init=%s put=%s confirm=%s ufid=%s",
            result.init_status_code,
            result.blob_put_status_code,
            result.confirm_status_code,
            result.ufid,
        )
        return result


def _raise_on_error(resp: httpx.Response, *, step: str) -> None:
    """Raise with diagnostic detail on non-2xx so failures are self-explanatory."""
    if resp.status_code < 400:
        return
    debug_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() in {
            "www-authenticate", "x-error", "x-request-id",
            "x-amzn-requestid", "x-ms-request-id", "x-trace-id",
            "content-type", "set-cookie", "location",
        }
    }
    body_snippet = resp.text[:2000] if resp.text else "(empty body)"
    raise RuntimeError(
        f"contract-drive {step} failed: {resp.status_code} {resp.reason_phrase}\n"
        f"  url: {resp.request.url}\n"
        f"  response headers: {debug_headers}\n"
        f"  response body: {body_snippet}"
    )
