"""E2E test: single presigned upload → full pipeline through EXTRACTION_QUEUED.

Target: mafia.terzocloud.com (Dev environment), tenant 1000012.
"""

from lib.api_clients.document_service import DocumentServiceClient
from lib.config import E2EConfig
from lib.fixtures import FixtureFile
from lib.polling import poll_until
from lib.run_context import RunContext


async def test_presigned_upload_reaches_extraction_queued(
    doc_client: DocumentServiceClient,
    config: E2EConfig,
    test_files: list[FixtureFile],
    run_ctx: RunContext,
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

    # Step 2: Upload file bytes directly to Azure Blob via SAS URL
    await doc_client.upload_to_sas(upload.upload_url, test_file.content)

    # Step 3: Confirm upload
    doc = await doc_client.confirm_upload(ufid)
    assert doc.status == "CONFIRMED"

    # Step 4: Wait for OCR_QUEUED
    doc = await poll_until(
        lambda: doc_client.get_document(ufid),
        lambda d: d.processing_state == "OCR_QUEUED",
        timeout=15,
        interval=config.poll_interval,
        backoff=config.poll_backoff,
        max_interval=config.poll_max_interval,
        description=f"OCR_QUEUED for {ufid}",
    )

    # Step 5: Wait for OCR_COMPLETED
    doc = await poll_until(
        lambda: doc_client.get_document(ufid),
        lambda d: d.processing_state in ("OCR_COMPLETED", "EXTRACTION_QUEUED"),
        timeout=300,
        interval=config.poll_interval,
        backoff=config.poll_backoff,
        max_interval=config.poll_max_interval,
        description=f"OCR_COMPLETED for {ufid}",
    )

    # Step 6: Verify OCR artifact exists
    artifacts = await doc_client.get_artifacts(ufid)
    ocr_artifacts = [a for a in artifacts.artifacts if a.type == "OCR_PDF"]
    assert len(ocr_artifacts) > 0, (
        f"Expected OCR_PDF artifact for {ufid}, "
        f"got: {[a.type for a in artifacts.artifacts]}"
    )

    # Step 7: Wait for EXTRACTION_QUEUED
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
    # Documents kept for post-run inspection. Run cleanup.py to delete.
