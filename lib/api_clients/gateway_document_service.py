from dataclasses import dataclass
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


class GatewayDocumentServiceClient:
    """Typed async HTTP client for the Nebula document-service via terzoai-gateway.

    Targets: {gateway_base_url}/nebula/document-service/api/v1/...

    Distinct from `DocumentServiceClient` (which targets mafia.terzocloud.com
    directly with no service prefix). Used for gateway-native endpoints like
    bulk-upload that take pre-existing sourceUrls instead of the presigned-SAS
    + confirm flow.
    """

    PATH_PREFIX = "/nebula/document-service/api/v1"

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
    ) -> dict[str, Any]:
        """POST /nebula/document-service/api/v1/documents/bulk-upload.

        Server pulls each item from its `source_url` — no client-side upload
        step needed (unlike the singular presigned-upload flow).
        """
        payload = {
            "source": source,
            "_truncated": truncated,
            "_totalItems": total_items if total_items is not None else len(items),
            "items": [item.to_json() for item in items],
        }
        resp = await self._client.post(
            f"{self.PATH_PREFIX}/documents/bulk-upload",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()
