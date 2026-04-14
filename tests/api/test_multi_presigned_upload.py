"""E2E test: multiple presigned uploads in parallel → all reach EXTRACTION_QUEUED.

Uploads all test files from the configured source (Azure Blob / local dir / in-memory).
If fewer test files than file_count, the first file is reused with different names.
"""

import asyncio

import pytest

from lib.api_clients.document_service import DocumentServiceClient
from lib.config import E2EConfig
from lib.fixtures import FixtureFile
from lib.polling import poll_until
from lib.run_context import RunContext


async def _upload_single_file(
    doc_client: DocumentServiceClient,
    filename: str,
    file_bytes: bytes,
    content_type: str = "application/pdf",
) -> str:
    """Initiate + upload + confirm a single file. Returns the ufid."""
    upload = await doc_client.initiate_upload(
        name=filename,
        content_type=content_type,
        size_bytes=len(file_bytes),
    )
    await doc_client.upload_to_sas(upload.upload_url, file_bytes, content_type)
    await doc_client.confirm_upload(upload.ufid)
    return upload.ufid


@pytest.mark.parametrize("file_count", [3, 5, 10])
async def test_multi_presigned_upload_all_reach_extraction_queued(
    doc_client: DocumentServiceClient,
    config: E2EConfig,
    test_files: list[FixtureFile],
    run_ctx: RunContext,
    file_count: int,
) -> None:
    """Upload N files concurrently via presigned SAS URLs and verify all
    progress through the pipeline to EXTRACTION_QUEUED.
    """
    ufids: list[str] = []

    # Step 1: Upload all files concurrently
    async with asyncio.TaskGroup() as tg:
        tasks = []
        for i in range(file_count):
            # Cycle through available test files, reuse if fewer than file_count
            test_file = test_files[i % len(test_files)]
            filename = run_ctx.tag_filename(test_file.name, index=i)
            tasks.append(
                tg.create_task(
                    _upload_single_file(
                        doc_client, filename, test_file.content, test_file.content_type,
                    )
                )
            )
    ufids = [t.result() for t in tasks]
    for ufid in ufids:
        run_ctx.register(ufid)
    assert len(ufids) == file_count

    # Step 2: Poll all documents until EXTRACTION_QUEUED (concurrently)
    async with asyncio.TaskGroup() as tg:
        poll_tasks = [
            tg.create_task(
                poll_until(
                    lambda ufid=ufid: doc_client.get_document(ufid),
                    lambda d: d.processing_state == "EXTRACTION_QUEUED",
                    timeout=config.full_pipeline_timeout,
                    interval=config.poll_interval,
                    backoff=config.poll_backoff,
                    max_interval=config.poll_max_interval,
                    description=f"EXTRACTION_QUEUED for {ufid}",
                )
            )
            for ufid in ufids
        ]
    results = [t.result() for t in poll_tasks]

    # Step 3: Verify all reached EXTRACTION_QUEUED
    for doc in results:
        assert doc.processing_state == "EXTRACTION_QUEUED", (
            f"Document {doc.ufid} stuck at {doc.processing_state}"
        )

    # Step 4: Verify all have OCR artifacts
    for ufid in ufids:
        artifacts = await doc_client.get_artifacts(ufid)
        ocr_artifacts = [a for a in artifacts.artifacts if a.type == "OCR_PDF"]
        assert len(ocr_artifacts) > 0, f"No OCR_PDF artifact for {ufid}"

    # Documents kept for post-run inspection. Run cleanup.py to delete.
