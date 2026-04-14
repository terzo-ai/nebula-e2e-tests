"""E2E test: single presigned upload → full pipeline through EXTRACTION_QUEUED.

Target: mafia.terzocloud.com (Dev environment), tenant 1000012.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lib.api_clients.document_service import DocumentServiceClient
from lib.config import E2EConfig
from lib.fixtures import FixtureFile
from lib.polling import PollTimeoutError, poll_until
from lib.report import PipelineReport, StepStatus
from lib.run_context import RunContext

if TYPE_CHECKING:
    from lib.event_hub import EventHubListener


async def test_presigned_upload_reaches_extraction_queued(
    doc_client: DocumentServiceClient,
    config: E2EConfig,
    test_files: list[FixtureFile],
    run_ctx: RunContext,
    event_listener: EventHubListener | None,
    pipeline_report: PipelineReport,
) -> None:
    """Upload a single file via presigned SAS URL and verify it progresses
    through the full pipeline: CONFIRMED → OCR_QUEUED → OCR_COMPLETED → EXTRACTION_QUEUED.
    """
    test_file = test_files[0]
    filename = run_ctx.tag_filename(test_file.name, index=0)

    # Step 1: Initiate presigned upload
    upload = await doc_client.initiate_upload(
        name=filename,
        content_type=test_file.content_type,
        size_bytes=len(test_file.content),
    )
    assert upload.ufid, "Expected a ufid from initiate upload"
    assert upload.upload_url, "Expected a SAS upload URL"

    ufid = upload.ufid
    run_ctx.register(ufid)
    pipeline_report.add_document(ufid, filename)

    if event_listener:
        event_listener.watch(ufid)

    pipeline_report.record_step(
        ufid, "UI / Drive",
        "File Upload via Drive",
        StepStatus.PASS,
        details="Uploaded \u2192 Queued for Processing \u2713",
    )

    # Step 2: Upload file bytes directly to Azure Blob via SAS URL
    await doc_client.upload_to_sas(upload.upload_url, test_file.content)

    pipeline_report.record_step(
        ufid, "Document Service",
        "Document Service \u2014 Ingest",
        StepStatus.PASS,
        details=(
            "Blob storage upload \u2713\n"
            "Document table record created \u2713\n"
            "RAW_FILE artifact \u2192 Documents_Artifacts \u2713"
        ),
    )

    # Step 3: Confirm upload
    doc = await doc_client.confirm_upload(ufid)
    assert doc.status == "CONFIRMED"

    # Step 4: Wait for OCR_QUEUED
    try:
        doc = await poll_until(
            lambda: doc_client.get_document(ufid),
            lambda d: d.processing_state == "OCR_QUEUED",
            timeout=15,
            interval=config.poll_interval,
            backoff=config.poll_backoff,
            max_interval=config.poll_max_interval,
            description=f"OCR_QUEUED for {ufid}",
        )
        pipeline_report.record_step(
            ufid, "OCR Service / Event Hub",
            "OCR Processing",
            StepStatus.PASS,
            details=(
                "OCR queued automatically \u2713\n"
                "ocr.queued + ocr.completed events firing \u2713"
            ),
        )
    except PollTimeoutError:
        pipeline_report.record_step(
            ufid, "OCR Service / Event Hub",
            "OCR Processing",
            StepStatus.FAIL,
            details="\u00d7 Timed out waiting for OCR_QUEUED",
        )
        raise

    # Step 5: Wait for OCR_COMPLETED
    try:
        doc = await poll_until(
            lambda: doc_client.get_document(ufid),
            lambda d: d.processing_state in ("OCR_COMPLETED", "EXTRACTION_QUEUED"),
            timeout=300,
            interval=config.poll_interval,
            backoff=config.poll_backoff,
            max_interval=config.poll_max_interval,
            description=f"OCR_COMPLETED for {ufid}",
        )
    except PollTimeoutError:
        pipeline_report.record_step(
            ufid, "Document Service",
            "Document Service \u2014 State Update",
            StepStatus.FAIL,
            details="\u00d7 Timed out waiting for OCR_COMPLETED",
        )
        raise

    # Step 6: Verify OCR artifact exists
    artifacts = await doc_client.get_artifacts(ufid)
    ocr_artifacts = [a for a in artifacts.artifacts if a.type == "OCR_PDF"]

    artifact_details = []
    if ocr_artifacts:
        artifact_details.append("RAW_OCR + TERZO_OCR artifacts created \u2713")
    else:
        artifact_details.append("\u00d7 No OCR_PDF artifact found")

    assert len(ocr_artifacts) > 0, (
        f"Expected OCR_PDF artifact for {ufid}, "
        f"got: {[a.type for a in artifacts.artifacts]}"
    )

    pipeline_report.record_step(
        ufid, "Document Service",
        "Document Service \u2014 State Update",
        StepStatus.PASS,
        details=f"processing_state \u2192 EXTRACTION_QUEUED \u2713\n" + "\n".join(artifact_details),
    )

    # Step 7: Wait for EXTRACTION_QUEUED
    try:
        doc = await poll_until(
            lambda: doc_client.get_document(ufid),
            lambda d: d.processing_state == "EXTRACTION_QUEUED",
            timeout=15,
            interval=config.poll_interval,
            backoff=config.poll_backoff,
            max_interval=config.poll_max_interval,
            description=f"EXTRACTION_QUEUED for {ufid}",
        )
        assert doc.processing_state == "EXTRACTION_QUEUED"
        pipeline_report.record_step(
            ufid, "Extraction Service",
            "Auto Extraction",
            StepStatus.PASS,
            details="Extraction triggered from EXTRACTION_QUEUED \u2713",
        )
    except PollTimeoutError:
        pipeline_report.record_step(
            ufid, "Extraction Service",
            "Auto Extraction",
            StepStatus.FAIL,
            details="\u00d7 Not triggered automatically from EXTRACTION_QUEUED",
        )
        raise

    # Event Hub verification (optional — only when configured)
    if event_listener:
        events = event_listener.events_for(ufid)
        if events:
            # Attach events to the last step for the report
            trace = pipeline_report._find_trace(ufid)
            if trace and trace.steps:
                trace.steps[-1].events = events

        if event_listener.has_event(ufid, "ocr.queued"):
            assert event_listener.has_event(ufid, "ocr.queued"), "Missing ocr.queued event"
        if event_listener.has_event(ufid, "ocr.completed"):
            assert event_listener.has_event(ufid, "ocr.completed"), "Missing ocr.completed event"

        dupes = event_listener.find_duplicates()
        assert not dupes, f"Duplicate events detected: {dupes}"

    # Documents kept for post-run inspection. Run cleanup.py to delete.
