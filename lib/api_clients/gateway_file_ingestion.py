"""Gateway-based client for the Nebula file-ingestion service.

`base_url` is the bare gateway host (e.g. `https://terzoai-gateway-dev.terzocloud.com`).
The service-path prefix `/nebula/file-ingestion/api/v1` is added by the client,
so requests land at `{base_url}/nebula/file-ingestion/api/v1/...`.
In CI, `base_url` is driven by the E2E_BASE_URL repo variable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class BulkUploadItem:
    """One entry in a bulk-upload request's `items[]` array."""

    name: str
    content_type: str
    size_bytes: int
    document_type: str
    source_url: str
    processing_mode: str = "EXTRACT"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "processingMode": self.processing_mode,
            "contentType": self.content_type,
            "sizeBytes": self.size_bytes,
            "documentType": self.document_type,
            "sourceUrl": self.source_url,
        }


@dataclass
class PresignedUploadInitiation:
    """Parsed response from POST /api/v1/documents/upload.

    The server reserves a ufid, writes a pending row, and hands back a
    time-limited Azure Blob SAS URL that the caller PUTs the file bytes to.
    `raw` retains the full decoded body for report/debug inspection.
    """

    ufid: str
    upload_url: str
    expires_at: str
    blob_path: str
    raw: dict[str, Any] = field(default_factory=dict)
    status_code: int = 0

    @classmethod
    def from_json(cls, data: Any, status_code: int = 0) -> "PresignedUploadInitiation":
        src = data if isinstance(data, dict) else {}
        return cls(
            ufid=str(src.get("ufid", "")),
            upload_url=str(src.get("uploadUrl", "")),
            expires_at=str(src.get("expiresAt", "")),
            blob_path=str(src.get("blobPath", "")),
            raw=src if src else {"_raw": data},
            status_code=status_code,
        )


@dataclass
class FileUploadedResponse:
    """Parsed response from POST /api/v1/documents/{ufid}/file-uploaded.

    The server records the upload completion and enqueues the pipeline
    event. The response shape isn't contract-locked, so we retain the
    decoded body verbatim for the HTML report.
    """

    raw: dict[str, Any] = field(default_factory=dict)
    status_code: int = 0


@dataclass
class BulkUploadResponse:
    """Parsed response from POST /api/v1/documents/bulk-upload.

    The exact response shape isn't locked down yet — `ufids` is extracted
    defensively from common JSON paths. `raw` always contains the full
    decoded body for fallback inspection. `status_code` is the HTTP status
    returned by the gateway (expected: 202 Accepted).
    """

    ufids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    status_code: int = 0

    @classmethod
    def from_json(cls, data: Any, status_code: int = 0) -> "BulkUploadResponse":
        return cls(
            ufids=_extract_ufids(data),
            raw=data if isinstance(data, dict) else {"_raw": data},
            status_code=status_code,
        )


def _extract_ufids(data: Any) -> list[str]:
    """Extract ufid strings from a bulk-upload response.

    The server response shape is:
        {
          "totalItems": 1, "accepted": 1, "rejected": 0,
          "results": [
            {"ufid": "...", "name": "...", "status": "FILE_UPLOAD_QUEUED", "error": null}
          ]
        }

    Prefer the documented `results[].ufid` path. Fall back to a recursive
    walk only if the shape differs, so future server changes don't
    immediately break the tests — with a log line so the drift is visible.
    """
    found: list[str] = []
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    v = item.get("ufid") or item.get("document_id") or item.get("documentId")
                    if isinstance(v, str) and v:
                        found.append(v)
    if not found:
        _walk_for_ufids(data, found)
        if found:
            logger.warning(
                "bulk-upload response missing `results[].ufid`; fell back to "
                "recursive walk. Check server contract."
            )
    # De-dupe while preserving order
    seen: set[str] = set()
    return [u for u in found if not (u in seen or seen.add(u))]


def _walk_for_ufids(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for key in ("ufid", "document_id", "documentId"):
            v = node.get(key)
            if isinstance(v, str) and v:
                out.append(v)
        for v in node.values():
            _walk_for_ufids(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_for_ufids(v, out)


class GatewayFileIngestionClient:
    """Async HTTP client for Nebula file-ingestion service (via terzoai-gateway).

    base_url is the bare gateway host (no service prefix). The
    `/nebula/file-ingestion/api/v1` prefix is added here so callers
    can share one `E2E_BASE_URL` repo variable across clients.
    """

    SERVICE_PREFIX = "/nebula/file-ingestion/api/v1"

    def __init__(self, base_url: str, tenant_id: int, access_token: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        headers = {"X-Tenant-Id": str(self._tenant_id)}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=60.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def bulk_upload(
        self,
        items: list[BulkUploadItem],
        source: str = "BULK_IMPORT",
    ) -> BulkUploadResponse:
        """POST {base_url}/nebula/file-ingestion/api/v1/documents/bulk-upload.

        Server pulls each item from its `source_url` — no client-side upload
        step needed (unlike the singular presigned-upload flow). Downstream
        pipeline events (com.terzo.document.*) fire asynchronously and can
        be observed via the Event Hub listener.
        """
        payload = {
            "source": source,
            "items": [item.to_json() for item in items],
        }
        resp = await self._client.post(
            f"{self.SERVICE_PREFIX}/documents/bulk-upload", json=payload
        )
        if resp.status_code >= 400:
            # Include the server's response body and relevant headers in the
            # exception so failures are self-diagnostic (auth scheme mismatches,
            # expired tokens, etc. typically put the reason in the body or in
            # WWW-Authenticate / x-error / x-request-id headers).
            debug_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() in {
                    "www-authenticate", "x-error", "x-request-id",
                    "x-amzn-requestid", "x-trace-id", "content-type",
                }
            }
            body_snippet = resp.text[:2000] if resp.text else "(empty body)"
            auth_sent = self._client.headers.get("Authorization", "(none)")
            auth_preview = (
                auth_sent[:12] + "..." + auth_sent[-6:]
                if len(auth_sent) > 24 else "(short/none)"
            )
            raise RuntimeError(
                f"bulk-upload failed: {resp.status_code} {resp.reason_phrase}\n"
                f"  url: {resp.request.url}\n"
                f"  sent Authorization header: {auth_preview}\n"
                f"  response headers: {debug_headers}\n"
                f"  response body: {body_snippet}"
            )
        parsed = BulkUploadResponse.from_json(resp.json(), status_code=resp.status_code)
        logger.info(
            "bulk-upload parsed: status=%s accepted=%s rejected=%s totalItems=%s ufids=%s",
            parsed.status_code,
            parsed.raw.get("accepted"),
            parsed.raw.get("rejected"),
            parsed.raw.get("totalItems"),
            parsed.ufids,
        )
        return parsed

    async def initiate_presigned_upload(
        self,
        name: str,
        processing_mode: str = "EXTRACT",
    ) -> PresignedUploadInitiation:
        """POST {base_url}/nebula/file-ingestion/api/v1/documents/upload.

        Server reserves a ufid and returns an Azure Blob SAS `uploadUrl`
        the caller PUTs bytes to. This is step 1 of the presigned-upload
        flow; follow with `put_to_upload_url` and `mark_file_uploaded`.
        """
        payload = {"name": name, "processingMode": processing_mode}
        resp = await self._client.post(
            f"{self.SERVICE_PREFIX}/documents/upload", json=payload
        )
        self._raise_for_status(resp, "initiate presigned upload")
        parsed = PresignedUploadInitiation.from_json(
            resp.json(), status_code=resp.status_code
        )
        logger.info(
            "presigned-upload initiated: status=%s ufid=%s blobPath=%s expiresAt=%s",
            parsed.status_code, parsed.ufid, parsed.blob_path, parsed.expires_at,
        )
        return parsed

    async def put_to_upload_url(
        self,
        upload_url: str,
        file_bytes: bytes,
        content_type: str = "application/pdf",
    ) -> int:
        """PUT raw bytes directly to the presigned Azure Blob SAS URL.

        Uses a one-shot client so the Bearer / X-Tenant-Id headers carried
        by the gateway client do not leak into the Azure Blob request
        (Azure rejects unknown headers on SAS PUTs).
        """
        async with httpx.AsyncClient(timeout=120.0) as raw_client:
            resp = await raw_client.put(
                upload_url,
                content=file_bytes,
                headers={
                    "x-ms-blob-type": "BlockBlob",
                    "Content-Type": content_type,
                },
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Azure Blob PUT failed: {resp.status_code} {resp.reason_phrase}\n"
                    f"  url: {upload_url.split('?')[0]}\n"
                    f"  response body: {resp.text[:2000] or '(empty)'}"
                )
            return resp.status_code

    async def mark_file_uploaded(self, ufid: str, name: str) -> FileUploadedResponse:
        """POST {base_url}/nebula/file-ingestion/api/v1/documents/{ufid}/file-uploaded.

        Signals the server that bytes have landed in blob storage; this is
        what enqueues the downstream pipeline (UPLOAD_QUEUED → ...). The
        filename is re-sent here to match the gateway's expected payload.
        """
        resp = await self._client.post(
            f"{self.SERVICE_PREFIX}/documents/{ufid}/file-uploaded",
            json={"name": name},
        )
        self._raise_for_status(resp, f"mark file-uploaded for ufid={ufid}")
        raw: dict[str, Any] = {}
        if resp.text:
            try:
                decoded = resp.json()
                raw = decoded if isinstance(decoded, dict) else {"_raw": decoded}
            except ValueError:
                raw = {"_raw": resp.text[:2000]}
        return FileUploadedResponse(raw=raw, status_code=resp.status_code)

    def _raise_for_status(self, resp: httpx.Response, op: str) -> None:
        """Shared 4xx/5xx formatter: dump body + diagnostic headers + auth preview."""
        if resp.status_code < 400:
            return
        debug_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() in {
                "www-authenticate", "x-error", "x-request-id",
                "x-amzn-requestid", "x-trace-id", "content-type",
            }
        }
        body_snippet = resp.text[:2000] if resp.text else "(empty body)"
        auth_sent = self._client.headers.get("Authorization", "(none)")
        auth_preview = (
            auth_sent[:12] + "..." + auth_sent[-6:]
            if len(auth_sent) > 24 else "(short/none)"
        )
        raise RuntimeError(
            f"{op} failed: {resp.status_code} {resp.reason_phrase}\n"
            f"  url: {resp.request.url}\n"
            f"  sent Authorization header: {auth_preview}\n"
            f"  response headers: {debug_headers}\n"
            f"  response body: {body_snippet}"
        )
