"""E2E test: bulk-upload via document-service.

Target: {E2E_BASE_URL}/api/v1/documents/bulk-upload, tenant E2E_TENANT_ID.
In CI, E2E_BASE_URL is set to the gateway URL:
  https://terzoai-gateway-dev.terzocloud.com/nebula/document-service

Unlike the presigned-upload flow (test_single_presigned_upload.py), bulk-upload
takes pre-existing sourceUrls — the server pulls each item from its source URL,
so there's no client-side upload step.
"""

from __future__ import annotations

import pytest

from lib.api_clients.document_service import BulkUploadItem, DocumentServiceClient
from lib.config import E2EConfig


async def test_bulk_upload_accepts_single_item(
    doc_client: DocumentServiceClient,
    config: E2EConfig,
) -> None:
    """POST /api/v1/documents/bulk-upload with one item.

    Mirrors the canonical curl: source=BULK_IMPORT, _truncated=true,
    _totalItems=1000, single item in items[]. Asserts the server accepts
    the request (2xx) and returns a parseable JSON body.
    """
    items = [
        BulkUploadItem(
            name="tz_nebula_e2e.pdf",
            content_type="application/pdf",
            size_bytes=93160,
            document_type="GENERAL",
            source_url=config.bulk_upload_source_url,
        ),
    ]

    result = await doc_client.bulk_upload(
        items=items,
        source="BULK_IMPORT",
        truncated=True,
        total_items=1000,
    )

    # Response shape isn't yet locked down — assert we got a JSON body back
    # (raise_for_status in the client already caught 4xx/5xx) and log it so
    # assertions can be tightened in a follow-up once the shape is known.
    assert result is not None, "bulk-upload returned an empty response body"
    print(f"\nbulk-upload response: {result}")


@pytest.mark.skip(reason="enable once the batch-status endpoint is identified")
async def test_bulk_upload_items_reach_processing(
    doc_client: DocumentServiceClient,
    config: E2EConfig,
) -> None:
    """Placeholder: verify that bulk-uploaded items progress through the pipeline.

    Needs the batch/document status endpoint. Fill in once documented.
    """
    raise NotImplementedError
