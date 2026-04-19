"""Shared Event Hub pipeline-stage walker used by every E2E upload test.

The three upload paths (bulk-upload, presigned-upload, UI contract-drive)
diverge in how they introduce a document to the pipeline, but converge on
the same downstream verification: walk a fixed list of pipeline stages
and, for each one, wait on Event Hub for the matching ``data.action``.

This module owns that convergence. Tests call :func:`run_pipeline_stages`
once and get identical reporting semantics: per-stage PASS/FAIL/SKIPPED
rows in the HTML report, per-stage 10-minute budget, FAILED short-circuit
with downstream SKIPPED rows, and force-kill on timeout.

Keep the stage definitions (:data:`PIPELINE_STAGES`) and the walker
co-located — updating one in isolation is how we ended up with the
UPLOAD/UPLOADED mismatch previously.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from lib.report import PipelineReport, StepStatus

if TYPE_CHECKING:
    from lib.event_hub import EventHubListener

logger = logging.getLogger(__name__)


FAILURE_ACTION = "FAILED"

# Per-stage wall-clock budget. Each stage gets a fresh 10-minute timer
# that starts when the stage is entered. If the timer expires without the
# expected event arriving, the test force-kills: that stage is recorded
# FAIL, remaining stages are recorded SKIPPED, and pytest.fail() aborts —
# the run can never hang past ~10 minutes on a single stage.
STAGE_TIMEOUT_S: float = 600.0

EVENT_HUB_UNSET_REASON = (
    "Event Hub not configured (E2E_EVENT_HUB_CONNECTION_STRING unset)"
)


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


# Tuple (not list) so the module-level default exposed via
# ``skip_all_stages(stages=PIPELINE_STAGES)`` and
# ``run_pipeline_stages(stages=PIPELINE_STAGES)`` cannot be mutated
# in-place by a caller who thought they were working on a local copy.
PIPELINE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage(
        service="Event Hub",
        description="Event Hub listener received first event for ufid",
        action=None,
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="File Ingestion Service",
        description="Upload queued",
        action="UPLOAD_QUEUED",
        timeout_s=STAGE_TIMEOUT_S,
    ),
    PipelineStage(
        service="File Ingestion Service",
        description="Upload completed",
        action="UPLOADED",
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
)


def skip_all_stages(
    pipeline_report: PipelineReport,
    ufid: str,
    *,
    reason: str,
    stages: Sequence[PipelineStage] = PIPELINE_STAGES,
) -> None:
    """Record every pipeline stage as SKIPPED with a shared reason.

    Used by tests that know upfront they can't run the Event Hub stages
    (e.g. presigned-upload after blob PUT fails, UI upload when the
    response has no ufid). The walker itself also uses this for the
    "Event Hub not configured" path.
    """
    for stage in stages:
        pipeline_report.record_step(
            ufid, stage.service, stage.description,
            StepStatus.SKIPPED,
            details=reason,
        )


def attach_events_to_last_step(
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


def failure_reason(event_listener: "EventHubListener", ufid: str) -> str:
    """Pull ``failure_reason`` from the FAILED event payload, if present."""
    for event in event_listener.events_for(ufid):
        if event.action == FAILURE_ACTION:
            data = event.payload.get("data") or event.payload
            return str(data.get("failure_reason", "(no failure_reason in payload)"))
    return "(no failure event captured)"


async def run_pipeline_stages(
    ufid: str,
    *,
    event_listener: "EventHubListener | None",
    pipeline_report: PipelineReport,
    stages: Sequence[PipelineStage] = PIPELINE_STAGES,
) -> None:
    """Walk the Event Hub pipeline stages for ``ufid``.

    Semantics (identical across bulk-upload, presigned-upload, UI upload):

    * ``event_listener is None`` → record every stage SKIPPED with an
      "Event Hub not configured" reason and ``pytest.skip`` the test.
    * A FAILED action seen before or during a stage wait → record that
      stage FAIL with the payload's ``failure_reason``, mark the run as
      aborted, and let remaining stages record FAIL with the same reason.
    * Stage timeout with no FAILED action → record stage FAIL, record
      downstream stages SKIPPED ("force-killed after X"), attach captured
      events to the report, and ``pytest.fail`` — so the run can never
      hang past one stage's budget.
    * All stages passed but a FAILED action sits in the captured stream
      anyway → ``pytest.fail`` with the payload's ``failure_reason``.

    The caller stays responsible for the upload handoff (register ufid,
    record the upload-accepted row, call ``event_listener.watch(ufid)``
    is invoked here, not by the caller). This lets the three tests stay
    focused on their unique upload-initiation flow.
    """
    if event_listener is None:
        skip_all_stages(pipeline_report, ufid, reason=EVENT_HUB_UNSET_REASON, stages=stages)
        pytest.skip("Event Hub not configured — pipeline event verification skipped")

    await event_listener.watch(ufid)

    aborted = False
    for stage_idx, stage in enumerate(stages):
        # Short-circuit: a prior stage already saw the failure action, or
        # one arrived between stages. Record each remaining stage as FAIL
        # with the failure_reason — no further Event Hub waits happen.
        if aborted or event_listener.has_event(ufid, FAILURE_ACTION):
            aborted = True
            reason = failure_reason(event_listener, ufid)
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.FAIL,
                details=(
                    f"Pipeline aborted — action={FAILURE_ACTION} received. "
                    f"failure_reason: {reason}"
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
                reason = failure_reason(event_listener, ufid)
                pipeline_report.record_step(
                    ufid, stage.service, stage.description,
                    StepStatus.FAIL,
                    details=(
                        f"action={FAILURE_ACTION} received during wait. "
                        f"failure_reason: {reason}"
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
            for later in stages[stage_idx + 1:]:
                pipeline_report.record_step(
                    ufid, later.service, later.description,
                    StepStatus.SKIPPED,
                    details=(
                        f"Skipped — run force-killed after {stage.service} "
                        f"timed out ({stage.timeout_s:.0f}s per-stage budget)."
                    ),
                )
            attach_events_to_last_step(pipeline_report, event_listener, ufid)
            pytest.fail(
                f"Pipeline force-killed for {ufid}: {stage.service} stage "
                f"exceeded its {stage.timeout_s:.0f}s budget. {timeout_msg}"
            )

    # Fail the pytest run if the pipeline emitted a failure action, so CI
    # surfaces a red test in addition to the HTML report row.
    if event_listener.has_event(ufid, FAILURE_ACTION):
        reason = failure_reason(event_listener, ufid)
        attach_events_to_last_step(pipeline_report, event_listener, ufid)
        pytest.fail(
            f"Pipeline failed for {ufid}: action={FAILURE_ACTION} received. "
            f"failure_reason: {reason}"
        )

    attach_events_to_last_step(pipeline_report, event_listener, ufid)
