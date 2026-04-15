"""E2E: bulk-upload → observe full pipeline events via Event Hub.

Invokes POST {E2E_BASE_URL}/api/v1/documents/bulk-upload with a single item,
extracts the ufid from the response, then watches Event Hub for each pipeline
event (com.terzo.document.*) and records a PASS/FAIL pipeline-report step for
each one. The HTML report is rendered at session teardown and uploaded as the
`pipeline-report-nightly` / `pipeline-report` artifact by the workflows.

Event sequence (from docs/EVENT_TYPES.md):
  uploaded → ocr.queued → ocr.completed → extraction.queued →
  extraction.completed → ingestion.queued → ingestion.completed

`com.terzo.document.failed` is monitored throughout — if it fires, remaining
steps are marked FAIL with the failure_reason from the payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from lib.api_clients.gateway_document_service import (
    BulkUploadItem,
    GatewayDocumentServiceClient,
)
from lib.config import E2EConfig
from lib.report import PipelineReport, StepStatus
from lib.run_context import RunContext

if TYPE_CHECKING:
    from lib.event_hub import EventHubListener


FAILURE_EVENT = "com.terzo.document.failed"


@dataclass(frozen=True)
class ExpectedEvent:
    event_type: str
    service: str
    description: str
    timeout_s: float


# Logical pipeline order (docs/EVENT_TYPES.md). Timeouts are generous —
# worker-pod download (`document.uploaded`) and OCR/extraction/ingestion
# completions can take minutes; the intermediate *.queued events fire
# synchronously inside Document Service right after the prior completion.
PIPELINE_EVENTS: list[ExpectedEvent] = [
    ExpectedEvent("com.terzo.document.uploaded",             "Document Service",    "Document uploaded (worker-pod download complete)", 300),
    ExpectedEvent("com.terzo.document.ocr.queued",           "Document Service",    "OCR queued",                                       30),
    ExpectedEvent("com.terzo.document.ocr.completed",        "OCR Service",         "OCR completed",                                    300),
    ExpectedEvent("com.terzo.document.extraction.queued",    "Document Service",    "Extraction queued",                                30),
    ExpectedEvent("com.terzo.document.extraction.completed", "Extraction Service",  "Extraction completed",                             300),
    ExpectedEvent("com.terzo.document.ingestion.queued",     "Document Service",    "Ingestion queued",                                 30),
    ExpectedEvent("com.terzo.document.ingestion.completed",  "Ingestion Service",   "Ingestion completed",                              300),
]


async def test_bulk_upload_full_pipeline(
    gateway_doc_client: GatewayDocumentServiceClient,
    config: E2EConfig,
    run_ctx: RunContext,
    event_listener: "EventHubListener | None",
    pipeline_report: PipelineReport,
) -> None:
    """Post one bulk-upload item and verify all com.terzo.document.* events fire.

    Each expected event becomes a row in the pipeline HTML report with
    PASS / FAIL / SKIPPED status.
    """
    filename = run_ctx.tag_filename(config.bulk_upload_file_name, index=0)

    # 1. Post bulk-upload
    response = await gateway_doc_client.bulk_upload(
        items=[
            BulkUploadItem(
                name=filename,
                content_type="application/pdf",
                size_bytes=93160,
                document_type="GENERAL",
                source_url=config.bulk_upload_source_url,
            ),
        ],
        source="BULK_IMPORT",
        truncated=True,
        total_items=1000,
    )

    assert response.ufids, (
        f"bulk-upload response contained no ufid. Raw response: {response.raw}"
    )
    ufid = response.ufids[0]
    run_ctx.register(ufid)
    pipeline_report.add_document(ufid, filename)
    print(f"\nbulk-upload → ufid={ufid}, raw={response.raw}")

    # Record the upload itself as the first step
    pipeline_report.record_step(
        ufid,
        "Gateway / Document Service",
        "bulk-upload accepted",
        StepStatus.PASS,
        details=f"POST /api/v1/documents/bulk-upload → ufid {ufid}",
    )

    # 2. If Event Hub isn't configured, record remaining steps as SKIPPED and return.
    if event_listener is None:
        for expected in PIPELINE_EVENTS:
            pipeline_report.record_step(
                ufid, expected.service, expected.description,
                StepStatus.SKIPPED,
                details="Event Hub not configured (E2E_EVENT_HUB_CONNECTION_STRING unset)",
            )
        pipeline_report.record_step(
            ufid, "Event Hub", "No com.terzo.document.failed event observed",
            StepStatus.SKIPPED,
            details="Event Hub not configured",
        )
        pytest.skip("Event Hub not configured — pipeline event verification skipped")

    event_listener.watch(ufid)

    # 3. Wait for each expected event in order. Short-circuit on document.failed.
    aborted = False
    for expected in PIPELINE_EVENTS:
        if aborted or event_listener.has_event(ufid, FAILURE_EVENT):
            aborted = True
            failure_reason = _failure_reason(event_listener, ufid)
            pipeline_report.record_step(
                ufid, expected.service, expected.description,
                StepStatus.FAIL,
                details=f"Pipeline aborted — {FAILURE_EVENT} received. "
                        f"failure_reason: {failure_reason}",
            )
            continue

        try:
            event = await event_listener.wait_for_event(
                ufid, expected.event_type, timeout=expected.timeout_s
            )
            pipeline_report.record_step(
                ufid, expected.service, expected.description,
                StepStatus.PASS,
                details=(
                    f"Event received at {event.received_at} "
                    f"(partition={event.partition_id}, seq={event.sequence_number})"
                ),
            )
        except Exception as e:  # EventTimeoutError, etc.
            # Check one more time — failure might have arrived during the wait
            if event_listener.has_event(ufid, FAILURE_EVENT):
                aborted = True
                failure_reason = _failure_reason(event_listener, ufid)
                pipeline_report.record_step(
                    ufid, expected.service, expected.description,
                    StepStatus.FAIL,
                    details=f"{FAILURE_EVENT} received during wait. "
                            f"failure_reason: {failure_reason}",
                )
            else:
                pipeline_report.record_step(
                    ufid, expected.service, expected.description,
                    StepStatus.FAIL,
                    details=f"Timed out after {expected.timeout_s}s waiting for "
                            f"{expected.event_type}. {type(e).__name__}: {e}",
                )

    # 4. Final step: explicit assertion that no failure event fired.
    if event_listener.has_event(ufid, FAILURE_EVENT):
        failure_reason = _failure_reason(event_listener, ufid)
        pipeline_report.record_step(
            ufid, "Event Hub", "No com.terzo.document.failed event observed",
            StepStatus.FAIL,
            details=f"failure_reason: {failure_reason}",
        )
        pytest.fail(
            f"Pipeline failed for {ufid}: {FAILURE_EVENT} received. "
            f"failure_reason: {failure_reason}"
        )
    else:
        pipeline_report.record_step(
            ufid, "Event Hub", "No com.terzo.document.failed event observed",
            StepStatus.PASS,
            details="No failure events captured during pipeline run",
        )

    # 5. Attach captured events to the last report step for visibility in the HTML.
    trace = pipeline_report._find_trace(ufid)
    if trace and trace.steps:
        trace.steps[-1].events = event_listener.events_for(ufid)


def _failure_reason(event_listener: "EventHubListener", ufid: str) -> str:
    """Pull `failure_reason` from the document.failed payload, if present."""
    for event in event_listener.events_for(ufid):
        if event.event_type == FAILURE_EVENT:
            data = event.payload.get("data") or event.payload
            return str(data.get("failure_reason", "(no failure_reason in payload)"))
    return "(no failure event captured)"
