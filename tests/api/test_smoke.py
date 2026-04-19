"""Smoke tests: verify connectivity, auth, and basic CRUD on mafia.terzocloud.com.

DISABLED — only the bulk-upload endpoint is under E2E coverage right now.
Remove the module-level `pytestmark` below to re-enable.
"""

import httpx
import pytest

from lib.api_clients.file_ingestion import FileIngestionClient
from lib.config import E2EConfig

pytestmark = pytest.mark.skip(
    reason="Smoke tests disabled — only bulk-upload is under E2E coverage"
)


class TestConnectivity:

    async def test_mafia_is_reachable(self, config: E2EConfig) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(config.base_url)
            assert resp.status_code != 0, f"Cannot reach {config.base_url}"

    async def test_access_token_available(self, access_token: str) -> None:
        assert access_token, "No access token available"


class TestAuth:

    async def test_can_list_documents(self, doc_client: FileIngestionClient) -> None:
        data = await doc_client.list_documents(size=1)
        assert "content" in data, f"Unexpected response: {data}"


class TestBasicApiOperations:

    async def test_initiate_upload(self, doc_client: FileIngestionClient) -> None:
        upload = await doc_client.initiate_upload(
            name="e2e-smoke-test.pdf",
            content_type="application/pdf",
            size_bytes=1024,
        )
        assert upload.ufid
        assert upload.upload_url
        assert "blob.core.windows.net" in upload.upload_url

    async def test_get_nonexistent_document_returns_404(
        self, doc_client: FileIngestionClient,
    ) -> None:
        resp = await doc_client._client.get(
            "/api/v1/documents/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404
