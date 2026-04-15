"""E2E: bulk-upload → observe full pipeline events via Event Hub.

Invokes POST {E2E_BASE_URL}/api/v1/documents/bulk-upload with a single item,
extracts the ufid from the response, then watches Event Hub and records a
PASS/FAIL pipeline-report row for each stage:

    1. Document Service : PASS on HTTP 202 + ufid returned
    2. Event Hub        : PASS on first event captured for the ufid
    3. OCR Service      : PASS on com.terzo.document.ocr.completed
    4. Extraction Service: PASS on com.terzo.document.auto_extraction.completed
    5. Ingestion Service : PASS on com.terzo.document.ingestion.completed

Each stage has its own independent 10-minute timeout that starts when
the stage is entered — a slow OCR stage does not eat into the Extraction
or Ingestion budget. A hard timeout force-kills the test immediately
(remaining stages recorded as SKIPPED) so the job can never hang past
~10 minutes on a single stage. `com.terzo.document.failed` also
short-circuits remaining stages to FAIL with the payload's failure_reason.

The HTML pipeline report is rendered at session teardown and uploaded as
the `pipeline-report-nightly` / `pipeline-report` workflow artifact.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


FAILURE_EVENT = "com.terzo.document.failed"


@dataclass(frozen=True)
class PipelineStage:
    """One row in the pipeline report that waits on Event Hub.

    ``event_type`` = None means "first event captured for this ufid wins"
    (used for the Event Hub stage, which just proves the listener is
    receiving traffic for the document). Each stage's ``timeout_s`` starts
    fresh when the stage is entered, so the per-stage budget does not
    bleed between stages.
    """

    service: str
    description: str
    event_type: str | None
    timeout_s: float


# Per-stage wall-clock budget. Each stage gets a fresh 10-minute timer
# that starts when the stage is entered. If the timer expires without the
# expected event arriving, the test force-kills: that stage is recorded
# FAIL, remaining stages are recorded SKIPPED, and pytest.fail() aborts —
# the run can never hang past ~10 minutes on a single stage.
STAGE_TIMEOUT_S: float = 600.0

# Ordered per docs/EVENT_TYPES.md.
PIPELINE_STAGES: list[PipelineStage] = [
    PipelineStage(
        service="Event Hub",
        description="Event Hub listener received first event for ufid",
        event_type=None,
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="OCR Service",
        description="OCR completed",
        event_type="com.terzo.document.ocr.completed",
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="Extraction Service",
        description="AI Extraction completed",
        event_type="com.terzo.document.auto_extraction.completed",
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="Ingestion Service",
        description="Ingestion completed",
        event_type="com.terzo.document.ingestion.completed",
        timeout_s=STAGE_TIMEOUT_S,
    ),
]


async def test_bulk_upload_full_pipeline(
    gateway_doc_client: GatewayDocumentServiceClient,
    config: E2EConfig,
    run_ctx: RunContext,
    event_listener: "EventHubListener | None",
    pipeline_report: PipelineReport,
) -> None:
    """Post one bulk-upload item and verify each pipeline stage."""
    filename = config.bulk_upload_file_name

    # ------------------------------------------------------------------
    # Stage 1 — Document Service: PASS when we get HTTP 202 + ufid.
    # ------------------------------------------------------------------
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
        f"bulk-upload response contained no ufid. "
        f"status={response.status_code} raw={response.raw}"
    )
    assert response.status_code == 202, (
        f"bulk-upload expected HTTP 202 Accepted, got {response.status_code}. "
        f"raw={response.raw}"
    )

    ufid = response.ufids[0]
    run_ctx.register(ufid)
    pipeline_report.add_document(ufid, filename)
    print(
        f"\nbulk-upload → status={response.status_code} ufid={ufid} "
        f"raw={response.raw}"
    )

    pipeline_report.record_step(
        ufid,
        "Document Service",
        "bulk-upload accepted (HTTP 202 + ufid returned)",
        StepStatus.PASS,
        details=(
            f"POST /api/v1/documents/bulk-upload → "
            f"HTTP {response.status_code}, ufid={ufid}"
        ),
    )

    # ------------------------------------------------------------------
    # Stages 2–5 — Event Hub / OCR / Extraction / Ingestion.
    # ------------------------------------------------------------------
    if event_listener is None:
        for stage in PIPELINE_STAGES:
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.SKIPPED,
                details="Event Hub not configured (E2E_EVENT_HUB_CONNECTION_STRING unset)",
            )
        pytest.skip("Event Hub not configured — pipeline event verification skipped")

    event_listener.watch(ufid)

    aborted = False
    for stage_idx, stage in enumerate(PIPELINE_STAGES):
        # Short-circuit: a prior stage already saw the failure event, or
        # one arrived between stages. Record each remaining stage as FAIL
        # with the failure_reason — no further Event Hub waits happen.
        if aborted or event_listener.has_event(ufid, FAILURE_EVENT):
            aborted = True
            failure_reason = _failure_reason(event_listener, ufid)
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.FAIL,
                details=(
                    f"Pipeline aborted — {FAILURE_EVENT} received. "
                    f"failure_reason: {failure_reason}"
                ),
            )
            continue

        # Per-stage timeout: `wait_for_event` / `wait_for_any_event` compute
        # their own deadline from `time.monotonic()` on each call, so the
        # budget resets cleanly when we advance from one stage to the next.
        try:
            if stage.event_type is None:
                event = await event_listener.wait_for_any_event(
                    ufid, timeout=stage.timeout_s
                )
                detail_prefix = (
                    f"First event for ufid: {event.event_type} "
                    f"at {event.received_at}"
                )
            else:
                event = await event_listener.wait_for_event(
                    ufid, stage.event_type, timeout=stage.timeout_s
                )
                detail_prefix = (
                    f"Event {event.event_type} received at {event.received_at}"
                )
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.PASS,
                details=(
                    f"{detail_prefix} "
                    f"(partition={event.partition_id}, seq={event.sequence_number})"
                ),
            )
        except Exception as e:  # EventTimeoutError, etc.
            # If the failure event arrived during the wait, treat that as
            # the cause and let the short-circuit handle remaining stages.
            if event_listener.has_event(ufid, FAILURE_EVENT):
                aborted = True
                failure_reason = _failure_reason(event_listener, ufid)
                pipeline_report.record_step(
                    ufid, stage.service, stage.description,
                    StepStatus.FAIL,
                    details=(
                        f"{FAILURE_EVENT} received during wait. "
                        f"failure_reason: {failure_reason}"
                    ),
                )
                continue

            # Hard timeout with no failure event — force-kill the run.
            # Record this stage FAIL, remaining stages SKIPPED (they
            # genuinely never ran), attach captured events, then abort.
            expected_label = stage.event_type or "<any event for ufid>"
            timeout_msg = (
                f"Timed out after {stage.timeout_s:.0f}s waiting for "
                f"{expected_label}. {type(e).__name__}: {e}"
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

    # Fail the pytest run if the pipeline emitted a failure event, so CI
    # surfaces a red test in addition to the HTML report row.
    if event_listener.has_event(ufid, FAILURE_EVENT):
        failure_reason = _failure_reason(event_listener, ufid)
        _attach_events_to_last_step(pipeline_report, event_listener, ufid)
        pytest.fail(
            f"Pipeline failed for {ufid}: {FAILURE_EVENT} received. "
            f"failure_reason: {failure_reason}"
        )

    _attach_events_to_last_step(pipeline_report, event_listener, ufid)


def _attach_events_to_last_step(
    pipeline_report: PipelineReport,
    event_listener: "EventHubListener",
    ufid: str,
) -> None:
    """Attach captured Event Hub events to the last report step so they
    render in the HTML timeline, regardless of which code path finished
    the stage loop (PASS, failure-event short-circuit, or force-kill).

    Also emits a single summary log line naming every event type the
    listener received for this ufid — invaluable when diagnosing "the
    event we were waiting for never fired; what DID fire?" cases.
    """
    trace = pipeline_report._find_trace(ufid)
    if trace and trace.steps:
        trace.steps[-1].events = event_listener.events_for(ufid)

    captured_types = event_listener.event_types_for(ufid)
    events = event_listener.events_for(ufid)
    logger.info(
        "Event Hub summary for ufid=%s: %d event(s) captured, distinct types=%s",
        ufid, len(events), captured_types or "[]",
    )
    for event in events:
        logger.info(
            "  captured: type=%s partition=%s seq=%d received_at=%s",
            event.event_type, event.partition_id,
            event.sequence_number, event.received_at,
        )


def _failure_reason(event_listener: "EventHubListener", ufid: str) -> str:
    """Pull `failure_reason` from the document.failed payload, if present."""
    for event in event_listener.events_for(ufid):
        if event.event_type == FAILURE_EVENT:
            data = event.payload.get("data") or event.payload
            return str(data.get("failure_reason", "(no failure_reason in payload)"))
    return "(no failure event captured)"
