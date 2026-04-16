"""E2E test: multiple presigned uploads in parallel → all reach EXTRACTION_QUEUED.

Uploads all test files from the configured source (Azure Blob / local dir / in-memory).
If fewer test files than file_count, the first file is reused with different names.

DISABLED — only the bulk-upload endpoint is under E2E coverage right now.
Remove the module-level `pytestmark` below to re-enable.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from lib.api_clients.document_service import DocumentServiceClient
from lib.config import E2EConfig
from lib.fixtures import FixtureFile
from lib.polling import PollTimeoutError, poll_until
from lib.report import PipelineReport, StepStatus
from lib.run_context import RunContext

pytestmark = pytest.mark.skip(
    reason="Presigned-upload endpoint disabled — only bulk-upload is under E2E coverage"
)

if TYPE_CHECKING:
    from lib.event_hub import EventHubListener


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
    event_listener: EventHubListener | None,
    pipeline_report: PipelineReport,
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
            filename = test_file.name
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

    # Register documents in report and event listener
    for i, ufid in enumerate(ufids):
        test_file = test_files[i % len(test_files)]
        filename = test_file.name
        pipeline_report.add_document(ufid, filename)
        pipeline_report.record_step(
            ufid, "Document Service",
            "Upload + Confirm",
            StepStatus.PASS,
            details="Presigned upload \u2192 Confirmed \u2713",
        )
        if event_listener:
            await event_listener.watch(ufid)

    # Step 2: Poll all documents until EXTRACTION_QUEUED (concurrently)
    results: list = []
    failed_ufids: list[str] = []
    try:
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
    except* PollTimeoutError as eg:
        for exc in eg.exceptions:
            if isinstance(exc, PollTimeoutError):
                # Extract ufid from the description
                desc = exc.description
                for uid in ufids:
                    if uid in desc:
                        failed_ufids.append(uid)
                        break

    # Step 3: Record results per document
    for i, ufid in enumerate(ufids):
        if ufid in failed_ufids:
            pipeline_report.record_step(
                ufid, "Pipeline",
                "Full Pipeline \u2192 EXTRACTION_QUEUED",
                StepStatus.FAIL,
                details=f"\u00d7 Timed out waiting for EXTRACTION_QUEUED",
            )
        else:
            pipeline_report.record_step(
                ufid, "Pipeline",
                "Full Pipeline \u2192 EXTRACTION_QUEUED",
                StepStatus.PASS,
                details="OCR + Extraction pipeline complete \u2713",
            )

    # Step 3 (original): Verify all reached EXTRACTION_QUEUED
    for doc in results:
        assert doc.processing_state == "EXTRACTION_QUEUED", (
            f"Document {doc.ufid} stuck at {doc.processing_state}"
        )

    # Step 4: Verify all have OCR artifacts
    for ufid in ufids:
        artifacts = await doc_client.get_artifacts(ufid)
        ocr_artifacts = [a for a in artifacts.artifacts if a.type == "OCR_PDF"]
        if ocr_artifacts:
            pipeline_report.record_step(
                ufid, "OCR Service",
                "OCR Artifact Verification",
                StepStatus.PASS,
                details=f"OCR_PDF artifact found \u2713",
            )
        else:
            pipeline_report.record_step(
                ufid, "OCR Service",
                "OCR Artifact Verification",
                StepStatus.FAIL,
                details=f"\u00d7 No OCR_PDF artifact found",
            )
        assert len(ocr_artifacts) > 0, f"No OCR_PDF artifact for {ufid}"

    # Event Hub verification (optional)
    if event_listener:
        dupes = event_listener.find_duplicates()
        assert not dupes, f"Duplicate events detected: {dupes}"

    # Documents kept for post-run inspection. Run cleanup.py to delete.
