from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class UploadInitiation:
    ufid: str
    upload_url: str
    expires_at: str
    blob_path: str


@dataclass
class DocumentResponse:
    id: int
    ufid: str
    tenant_id: int
    name: str
    status: str
    processing_state: str
    version: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "DocumentResponse":
        return cls(
            id=data["id"],
            ufid=data["ufid"],
            tenant_id=data["tenantId"],
            name=data["name"],
            status=data["status"],
            processing_state=data["processingState"],
            version=data["version"],
        )


@dataclass
class ArtifactResponse:
    type: str
    status: str
    blob_path: str | None
    download_url: str | None
    source_service: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ArtifactResponse":
        return cls(
            type=data["type"],
            status=data["status"],
            blob_path=data.get("blobPath"),
            download_url=data.get("downloadUrl"),
            source_service=data.get("sourceService"),
        )


@dataclass
class DocumentArtifactsResponse:
    ufid: str
    stage: str
    artifacts: list[ArtifactResponse]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "DocumentArtifactsResponse":
        return cls(
            ufid=data["ufid"],
            stage=data["stage"],
            artifacts=[ArtifactResponse.from_json(a) for a in data["artifacts"]],
        )


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


class DocumentServiceClient:
    """Typed async HTTP client for the document-service API on mafia.terzocloud.com."""

    def __init__(self, base_url: str, tenant_id: int, access_token: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        headers = {"X-Tenant-Id": str(self._tenant_id)}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def initiate_upload(
        self,
        name: str,
        content_type: str,
        size_bytes: int,
        source: str = "E2E_TEST",
        document_type: str = "CONTRACT",
    ) -> UploadInitiation:
        resp = await self._client.post(
            "/api/v1/documents/upload",
            json={
                "name": name,
                "contentType": content_type,
                "sizeBytes": size_bytes,
                "source": source,
                "documentType": document_type,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return UploadInitiation(
            ufid=data["ufid"],
            upload_url=data["uploadUrl"],
            expires_at=data["expiresAt"],
            blob_path=data["blobPath"],
        )

    async def upload_to_sas(self, sas_url: str, file_bytes: bytes, content_type: str = "application/pdf") -> None:
        """Upload file bytes directly to Azure Blob via the presigned SAS URL."""
        async with httpx.AsyncClient(timeout=120.0) as raw_client:
            resp = await raw_client.put(
                sas_url,
                content=file_bytes,
                headers={
                    "x-ms-blob-type": "BlockBlob",
                    "Content-Type": content_type,
                },
            )
            resp.raise_for_status()

    async def confirm_upload(self, ufid: str) -> DocumentResponse:
        resp = await self._client.post(f"/api/v1/documents/{ufid}/confirm")
        resp.raise_for_status()
        return DocumentResponse.from_json(resp.json())

    async def get_document(self, ufid: str) -> DocumentResponse:
        resp = await self._client.get(f"/api/v1/documents/{ufid}")
        resp.raise_for_status()
        return DocumentResponse.from_json(resp.json())

    async def get_artifacts(self, ufid: str) -> DocumentArtifactsResponse:
        resp = await self._client.get(f"/api/v1/documents/{ufid}/artifacts")
        resp.raise_for_status()
        return DocumentArtifactsResponse.from_json(resp.json())

    async def list_documents(self, size: int = 1) -> dict:
        resp = await self._client.get("/api/v1/documents", params={"size": size})
        resp.raise_for_status()
        return resp.json()

    async def delete_document(self, ufid: str) -> None:
        resp = await self._client.delete(f"/api/v1/documents/{ufid}")
        resp.raise_for_status()

    async def bulk_upload(
        self,
        items: list[BulkUploadItem],
        source: str = "BULK_IMPORT",
        truncated: bool = True,
        total_items: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/documents/bulk-upload.

        Server pulls each item from its `source_url` — no client-side upload
        step needed (unlike the singular presigned-upload flow).

        When base_url points at the gateway (E2E_BASE_URL in CI), this hits:
          {base_url}/api/v1/documents/bulk-upload
        """
        payload = {
            "source": source,
            "_truncated": truncated,
            "_totalItems": total_items if total_items is not None else len(items),
            "items": [item.to_json() for item in items],
        }
        resp = await self._client.post("/api/v1/documents/bulk-upload", json=payload)
        resp.raise_for_status()
        return resp.json()
