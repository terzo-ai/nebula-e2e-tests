"""E2E: direct upload via presigned URL → observe full pipeline events via Event Hub.

Three-step gateway flow:

    1. POST  {base}/nebula/file-ingestion/api/v1/documents/upload
         body: { name, processingMode: "EXTRACT" }
         → { ufid, uploadUrl, expiresAt, blobPath }
    2. PUT   {uploadUrl}
         headers: x-ms-blob-type=BlockBlob, Content-Type=application/pdf
         body: raw PDF bytes
    3. POST  {base}/nebula/file-ingestion/api/v1/documents/{ufid}/file-uploaded
         body: { name }
         → enqueues UPLOAD_QUEUED on the pipeline

After step 3, the Event Hub walker in :mod:`lib.pipeline_runner` verifies
the same downstream stages as bulk-upload.

Each of the three HTTP calls lands as its own PASS / FAIL row in the HTML
pipeline report, so a failure at any step (initiate, blob PUT, or
file-uploaded) is pinpointed without reading server logs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from lib.api_clients.gateway_file_ingestion import GatewayFileIngestionClient
from lib.config import E2EConfig
from lib.pipeline_runner import run_pipeline_stages, skip_all_stages
from lib.report import PipelineReport, StepStatus
from lib.run_context import RunContext

if TYPE_CHECKING:
    from lib.event_hub import EventHubListener

logger = logging.getLogger(__name__)


TEST_CASE_NAME = "Presigned Upload"

# Markers: `-m e2e` runs the whole suite; `-m presigned_upload` targets just this file.
pytestmark = [pytest.mark.e2e, pytest.mark.presigned_upload]


async def test_presigned_upload_full_pipeline(
    gateway_doc_client: GatewayFileIngestionClient,
    config: E2EConfig,
    run_ctx: RunContext,
    event_listener: "EventHubListener | None",
    pipeline_report: PipelineReport,
    generated_contract_pdf: bytes,
) -> None:
    """Initiate → PUT to SAS → file-uploaded → verify each pipeline stage."""
    filename = f"nebulae2etest-{run_ctx.run_id}-presigned.pdf"

    # ------------------------------------------------------------------
    # Stage 1 — File Ingestion: initiate presigned upload (HTTP 200 + ufid).
    # ------------------------------------------------------------------
    initiation = await gateway_doc_client.initiate_presigned_upload(
        name=filename,
        processing_mode="EXTRACT",
    )
    assert initiation.ufid, (
        f"initiate-upload response carried no ufid. "
        f"status={initiation.status_code} raw={initiation.raw}"
    )
    assert initiation.upload_url, (
        f"initiate-upload response carried no uploadUrl. raw={initiation.raw}"
    )

    ufid = initiation.ufid
    run_ctx.register(ufid)
    pipeline_report.add_document(
        ufid,
        filename,
        test_case=TEST_CASE_NAME,
        response_body=initiation.raw,
    )
    print(
        f"\npresigned-upload initiated → status={initiation.status_code} "
        f"ufid={ufid} blobPath={initiation.blob_path} "
        f"expiresAt={initiation.expires_at}"
    )

    pipeline_report.record_step(
        ufid,
        "File Ingestion Service",
        "Step 1 — initiate presigned upload",
        StepStatus.PASS,
        details=(
            f"POST /api/v1/documents/upload → HTTP {initiation.status_code}, "
            f"ufid={ufid}, blobPath={initiation.blob_path}, "
            f"expiresAt={initiation.expires_at}"
        ),
    )

    # ------------------------------------------------------------------
    # Stage 2 — Azure Blob: PUT raw bytes to the SAS URL.
    # ------------------------------------------------------------------
    try:
        put_status = await gateway_doc_client.put_to_upload_url(
            initiation.upload_url,
            generated_contract_pdf,
            content_type="application/pdf",
        )
    except Exception as e:
        pipeline_report.record_step(
            ufid,
            "Azure Blob",
            "Step 2 — PUT bytes to presigned SAS URL",
            StepStatus.FAIL,
            details=f"{type(e).__name__}: {e}",
        )
        skip_all_stages(
            pipeline_report, ufid,
            reason="Skipped — blob PUT failed, pipeline never triggered.",
        )
        raise

    pipeline_report.record_step(
        ufid,
        "Azure Blob",
        "Step 2 — PUT bytes to presigned SAS URL",
        StepStatus.PASS,
        details=(
            f"PUT {initiation.upload_url.split('?')[0]} → HTTP {put_status}, "
            f"{len(generated_contract_pdf)} bytes, "
            f"Content-Type=application/pdf, x-ms-blob-type=BlockBlob"
        ),
    )

    # ------------------------------------------------------------------
    # Stage 3 — File Ingestion: mark file-uploaded (enqueues pipeline).
    # ------------------------------------------------------------------
    try:
        confirm = await gateway_doc_client.mark_file_uploaded(ufid, filename)
    except Exception as e:
        pipeline_report.record_step(
            ufid,
            "File Ingestion Service",
            "Step 3 — POST /{ufid}/file-uploaded",
            StepStatus.FAIL,
            details=f"{type(e).__name__}: {e}",
        )
        skip_all_stages(
            pipeline_report, ufid,
            reason="Skipped — file-uploaded confirm failed, pipeline never triggered.",
        )
        raise

    pipeline_report.record_step(
        ufid,
        "File Ingestion Service",
        "Step 3 — POST /{ufid}/file-uploaded",
        StepStatus.PASS,
        details=(
            f"POST /api/v1/documents/{ufid}/file-uploaded → "
            f"HTTP {confirm.status_code}, body={confirm.raw or '(empty)'}"
        ),
    )

    # ------------------------------------------------------------------
    # Event Hub stages — delegated to the shared pipeline walker.
    # ------------------------------------------------------------------
    await run_pipeline_stages(
        ufid,
        event_listener=event_listener,
        pipeline_report=pipeline_report,
    )
