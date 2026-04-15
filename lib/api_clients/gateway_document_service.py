"""Gateway-based client for the Nebula document-service.

`base_url` is the bare gateway host (e.g. `https://terzoai-gateway-dev.terzocloud.com`).
The service-path prefix `/nebula/document-service/api/v1` is added by the client,
so requests land at `{base_url}/nebula/document-service/api/v1/...`.
In CI, `base_url` is driven by the E2E_BASE_URL repo variable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class BulkUploadItem:
    """One entry in a bulk-upload request's `items[]` array."""

    name: str
    content_type: str
    size_bytes: int
    document_type: str
    source_url: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "contentType": self.content_type,
            "sizeBytes": self.size_bytes,
            "documentType": self.document_type,
            "sourceUrl": self.source_url,
        }


@dataclass
class BulkUploadResponse:
    """Parsed response from POST /api/v1/documents/bulk-upload.

    The exact response shape isn't locked down yet — `ufids` is extracted
    defensively from common JSON paths. `raw` always contains the full
    decoded body for fallback inspection.
    """

    ufids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: Any) -> "BulkUploadResponse":
        return cls(ufids=_extract_ufids(data), raw=data if isinstance(data, dict) else {"_raw": data})


def _extract_ufids(data: Any) -> list[str]:
    """Find ufid strings in common response shapes.

    Checks (in order):
      {"ufid": "..."}
      {"items": [{"ufid": "..."}, ...]}
      {"documents": [{"ufid": "..."}, ...]}
      {"data": {...}} recursively
      Any top-level list of dicts with a ufid key
    Also accepts `document_id` and `documentId` as aliases.
    """
    found: list[str] = []
    _walk_for_ufids(data, found)
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


class GatewayDocumentServiceClient:
    """Async HTTP client for Nebula document-service (via terzoai-gateway).

    base_url is the bare gateway host (no service prefix). The
    `/nebula/document-service/api/v1` prefix is added here so callers
    can share one `E2E_BASE_URL` repo variable across clients.
    """

    SERVICE_PREFIX = "/nebula/document-service/api/v1"

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
        truncated: bool = True,
        total_items: int | None = None,
    ) -> BulkUploadResponse:
        """POST {base_url}/nebula/document-service/api/v1/documents/bulk-upload.

        Server pulls each item from its `source_url` — no client-side upload
        step needed (unlike the singular presigned-upload flow). Downstream
        pipeline events (com.terzo.document.*) fire asynchronously and can
        be observed via the Event Hub listener.
        """
        payload = {
            "source": source,
            "_truncated": truncated,
            "_totalItems": total_items if total_items is not None else len(items),
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
        return BulkUploadResponse.from_json(resp.json())
