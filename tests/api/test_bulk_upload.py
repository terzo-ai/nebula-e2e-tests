"""E2E: bulk-upload → observe full pipeline events via Event Hub.

Invokes POST {E2E_BASE_URL}/api/v1/documents/bulk-upload with a single item,
extracts the ufid from the response, then watches Event Hub and records a
PASS/FAIL pipeline-report row for each stage based on ``data.action``:

    1. Document Service  : PASS on HTTP 202 + ufid returned
    2. Event Hub         : PASS on first event captured for the ufid
    3. Document Service  : PASS on action=UPLOAD_QUEUED
    4. Document Service  : PASS on action=UPLOAD
    5. OCR Service       : PASS on action=OCR_QUEUED
    6. Extraction Service: PASS on action=EXTRACTION_QUEUED
    7. Extraction Service: PASS on action=EXTRACTION_COMPLETED

Each stage has its own independent 10-minute timeout that starts when
the stage is entered — a slow OCR stage does not eat into the Extraction
or Ingestion budget. A hard timeout force-kills the test immediately
(remaining stages recorded as SKIPPED) so the job can never hang past
~10 minutes on a single stage. An event with action=FAILED also
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


FAILURE_ACTION = "FAILED"
TEST_CASE_NAME = "Bulk Upload"

# Markers: `-m e2e` runs the whole suite; `-m bulk_upload` targets just this file.
pytestmark = [pytest.mark.e2e, pytest.mark.bulk_upload]


@dataclass(frozen=True)
class PipelineStage:
    """One row in the pipeline report that waits on Event Hub.

    ``action`` = None means "first event captured for this ufid wins"
    (used for the Event Hub stage, which just proves the listener is
    receiving traffic for the document). A tuple of action strings means
    "any of these actions satisfies the stage". Each stage's ``timeout_s``
    starts fresh when the stage is entered, so the per-stage budget does
    not bleed between stages.
    """

    service: str
    description: str
    action: str | tuple[str, ...] | None
    timeout_s: float


# Per-stage wall-clock budget. Each stage gets a fresh 10-minute timer
# that starts when the stage is entered. If the timer expires without the
# expected event arriving, the test force-kills: that stage is recorded
# FAIL, remaining stages are recorded SKIPPED, and pytest.fail() aborts —
# the run can never hang past ~10 minutes on a single stage.
STAGE_TIMEOUT_S: float = 600.0

PIPELINE_STAGES: list[PipelineStage] = [
    PipelineStage(
        service="Event Hub",
        description="Event Hub listener received first event for ufid",
        action=None,
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="Document Service",
        description="Upload queued",
        action="UPLOAD_QUEUED",
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="Document Service",
        description="Upload completed",
        action="UPLOAD",
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="OCR Service",
        description="OCR queued",
        action="OCR_QUEUED",
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="Extraction Service",
        description="Extraction queued",
        action="EXTRACTION_QUEUED",
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="Extraction Service",
        description="Extraction completed",
        action="EXTRACTION_COMPLETED",
        timeout_s=STAGE_TIMEOUT_S,
    ),
]


async def test_bulk_upload_full_pipeline(
    gateway_doc_client: GatewayDocumentServiceClient,
    config: E2EConfig,
    run_ctx: RunContext,
    event_listener: "EventHubListener | None",
    pipeline_report: PipelineReport,
    bulk_upload_source_url: str,
    generated_contract_pdf: bytes,
) -> None:
    """Post one bulk-upload item and verify each pipeline stage.

    The per-run `bulk_upload_source_url` fixture uploads a freshly
    generated Contract Agreement PDF to Azure Blob and returns a 2h SAS
    URL — so scheduled runs never collide with a stale / cached object.
    """
    filename = f"nebulae2etest-{run_ctx.run_id}.pdf"

    # ------------------------------------------------------------------
    # Stage 1 — Document Service: PASS when we get HTTP 202 + ufid.
    # ------------------------------------------------------------------
    response = await gateway_doc_client.bulk_upload(
        items=[
            BulkUploadItem(
                name=filename,
                content_type="application/pdf",
                size_bytes=len(generated_contract_pdf),
                document_type="GENERAL",
                source_url=bulk_upload_source_url,
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
    pipeline_report.add_document(
        ufid,
        filename,
        test_case=TEST_CASE_NAME,
        response_body=response.raw,
    )
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

    await event_listener.watch(ufid)

    aborted = False
    for stage_idx, stage in enumerate(PIPELINE_STAGES):
        # Short-circuit: a prior stage already saw the failure action, or
        # one arrived between stages. Record each remaining stage as FAIL
        # with the failure_reason — no further Event Hub waits happen.
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

        # Per-stage timeout: `wait_for_event` / `wait_for_any_event` compute
        # their own deadline from `time.monotonic()` on each call, so the
        # budget resets cleanly when we advance from one stage to the next.
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
        except Exception as e:  # EventTimeoutError, etc.
            # If the failure action arrived during the wait, treat that as
            # the cause and let the short-circuit handle remaining stages.
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

            # Hard timeout with no failure action — force-kill the run.
            # Record this stage FAIL, remaining stages SKIPPED (they
            # genuinely never ran), attach captured events, then abort.
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

    # Fail the pytest run if the pipeline emitted a failure action, so CI
    # surfaces a red test in addition to the HTML report row.
    if event_listener.has_event(ufid, FAILURE_ACTION):
        failure_reason = _failure_reason(event_listener, ufid)
        _attach_events_to_last_step(pipeline_report, event_listener, ufid)
        pytest.fail(
            f"Pipeline failed for {ufid}: action={FAILURE_ACTION} received. "
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
    the stage loop (PASS, failure-action short-circuit, or force-kill).

    Also emits a single summary log line naming every action the
    listener received for this ufid — invaluable when diagnosing "the
    action we were waiting for never fired; what DID fire?" cases.
    """
    trace = pipeline_report._find_trace(ufid)
    if trace and trace.steps:
        trace.steps[-1].events = event_listener.events_for(ufid)

    captured_actions = event_listener.actions_for(ufid)
    events = event_listener.events_for(ufid)
    logger.info(
        "Event Hub summary for ufid=%s: %d event(s) captured, distinct actions=%s",
        ufid, len(events), captured_actions or "[]",
    )
    for event in events:
        logger.info(
            "  captured: action=%s id=%s document_id=%s partition=%s seq=%d received_at=%s",
            event.action, event.event_id, event.document_id,
            event.partition_id, event.sequence_number, event.received_at,
        )


def _failure_reason(event_listener: "EventHubListener", ufid: str) -> str:
    """Pull `failure_reason` from the FAILED event payload, if present."""
    for event in event_listener.events_for(ufid):
        if event.action == FAILURE_ACTION:
            data = event.payload.get("data") or event.payload
            return str(data.get("failure_reason", "(no failure_reason in payload)"))
    return "(no failure event captured)"
