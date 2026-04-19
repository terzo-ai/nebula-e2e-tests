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

After step 3, the same downstream stages as bulk-upload fire (UPLOAD,
OCR_QUEUED, EXTRACTION_QUEUED, EXTRACTION_COMPLETED). The test reuses
`PIPELINE_STAGES` + helpers from `test_bulk_upload` so a change there is
automatically reflected here.

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
from lib.report import PipelineReport, StepStatus
from lib.run_context import RunContext
from tests.api.test_bulk_upload import (
    FAILURE_ACTION,
    PIPELINE_STAGES,
    _attach_events_to_last_step,
    _failure_reason,
)

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
        "File Ingestion",
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
        for stage in PIPELINE_STAGES:
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.SKIPPED,
                details="Skipped — blob PUT failed, pipeline never triggered.",
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
            "File Ingestion",
            "Step 3 — POST /{ufid}/file-uploaded",
            StepStatus.FAIL,
            details=f"{type(e).__name__}: {e}",
        )
        for stage in PIPELINE_STAGES:
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.SKIPPED,
                details="Skipped — file-uploaded confirm failed, pipeline never triggered.",
            )
        raise

    pipeline_report.record_step(
        ufid,
        "File Ingestion",
        "Step 3 — POST /{ufid}/file-uploaded",
        StepStatus.PASS,
        details=(
            f"POST /api/v1/documents/{ufid}/file-uploaded → "
            f"HTTP {confirm.status_code}, body={confirm.raw or '(empty)'}"
        ),
    )

    # ------------------------------------------------------------------
    # Stages 4+ — Event Hub pipeline verification (shared with bulk-upload).
    # ------------------------------------------------------------------
    if event_listener is None:
        for stage in PIPELINE_STAGES:
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.SKIPPED,
                details="Event Hub not configured (E2E_EVENT_HUB_CONNECTION_STRING unset)",
            )
        pytest.skip("Event Hub not configured — pipeline event verification skipped")

    await event_listener.watch(ufid)

    aborted = False
    for stage_idx, stage in enumerate(PIPELINE_STAGES):
        if aborted or event_listener.has_event(ufid, FAILURE_ACTION):
            aborted = True
            failure_reason = _failure_reason(event_listener, ufid)
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.FAIL,
                details=(
                    f"Pipeline aborted — action={FAILURE_ACTION} received. "
                    f"failure_reason: {failure_reason}"
                ),
            )
            continue

        try:
            if stage.action is None:
                event = await event_listener.wait_for_any_event(
                    ufid, timeout=stage.timeout_s
                )
                detail_prefix = (
                    f"First event for ufid: action={event.action} "
                    f"id={event.event_id} at {event.received_at}"
                )
            else:
                event = await event_listener.wait_for_event(
                    ufid, stage.action, timeout=stage.timeout_s
                )
                detail_prefix = (
                    f"action={event.action} id={event.event_id} "
                    f"received at {event.received_at}"
                )
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.PASS,
                details=(
                    f"{detail_prefix} "
                    f"(partition={event.partition_id}, seq={event.sequence_number})"
                ),
            )
        except Exception as e:
            if event_listener.has_event(ufid, FAILURE_ACTION):
                aborted = True
                failure_reason = _failure_reason(event_listener, ufid)
                pipeline_report.record_step(
                    ufid, stage.service, stage.description,
                    StepStatus.FAIL,
                    details=(
                        f"action={FAILURE_ACTION} received during wait. "
                        f"failure_reason: {failure_reason}"
                    ),
                )
                continue

            expected_label = stage.action or "<any event for ufid>"
            timeout_msg = (
                f"Timed out after {stage.timeout_s:.0f}s waiting for "
                f"action={expected_label}. {type(e).__name__}: {e}"
            )
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.FAIL,
                details=timeout_msg,
            )
            for later in PIPELINE_STAGES[stage_idx + 1:]:
                pipeline_report.record_step(
                    ufid, later.service, later.description,
                    StepStatus.SKIPPED,
                    details=(
                        f"Skipped — run force-killed after {stage.service} "
                        f"timed out ({stage.timeout_s:.0f}s per-stage budget)."
                    ),
                )
            _attach_events_to_last_step(pipeline_report, event_listener, ufid)
            pytest.fail(
                f"Pipeline force-killed for {ufid}: {stage.service} stage "
                f"exceeded its {stage.timeout_s:.0f}s budget. {timeout_msg}"
            )

    if event_listener.has_event(ufid, FAILURE_ACTION):
        failure_reason = _failure_reason(event_listener, ufid)
        _attach_events_to_last_step(pipeline_report, event_listener, ufid)
        pytest.fail(
            f"Pipeline failed for {ufid}: action={FAILURE_ACTION} received. "
            f"failure_reason: {failure_reason}"
        )

    _attach_events_to_last_step(pipeline_report, event_listener, ufid)
