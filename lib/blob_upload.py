"""Shared helper for PUT-to-SAS-URL Azure Blob uploads.

Both the gateway presigned-upload flow and the UI contract-drive flow
deliver file bytes to Azure Blob via a server-issued SAS URL with the
same header contract:

    PUT <sas_url>
    x-ms-blob-type: BlockBlob
    Content-Type:   <content_type>

Azure rejects unknown request headers on SAS PUTs, so we always use a
fresh, bearer/cookie-free ``httpx.AsyncClient`` — never a client that
carries gateway or Analytics auth. Callers that want the browser's
``Referer`` echoed (the UI flow does, to mirror the captured curl) can
pass one explicitly.
"""

from __future__ import annotations

import httpx


async def put_to_sas_url(
    upload_url: str,
    content: bytes,
    content_type: str = "application/pdf",
    *,
    referer: str | None = None,
    timeout_s: float = 120.0,
) -> httpx.Response:
    """PUT file bytes to an Azure Blob SAS URL. Raise on non-2xx.

    Returns the raw ``httpx.Response`` so callers can pull status codes
    or headers (e.g. ``x-ms-request-id``) for reporting.
    """
    headers = {
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": content_type,
    }
    if referer:
        headers["Referer"] = referer

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.put(upload_url, content=content, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Azure Blob PUT failed: {resp.status_code} {resp.reason_phrase}\n"
                f"  url: {upload_url.split('?')[0]}\n"
                f"  response body: {resp.text[:2000] or '(empty)'}"
            )
        return resp
