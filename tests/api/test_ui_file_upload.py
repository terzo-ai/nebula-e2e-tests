"""E2E: UI file upload via `POST /_/api/contract-drive/{drive_id}/add`.

This is the endpoint the Contract Drive UI hits when a user drops a file
into the drive. Unlike bulk-upload (which runs against the Nebula
gateway and uses a Bearer token), this endpoint lives on the Analytics
host (``mafia.terzocloud.com``) and is session-authenticated with
``X-XSRF-TOKEN`` + ``Cookie`` — the same credentials the Analytics
login step already has.

The test:

    1. POST multipart/form-data (file + drive={"driveId": <id>}) to the
       contract-drive endpoint.
    2. Assert HTTP 200 and capture the response body into the pipeline
       report so it renders under the test-case panel.
    3. If the response carries a ufid, watch Event Hub through the same
       pipeline stages bulk-upload tracks. If it doesn't, the test still
       passes on the upload-accepted check alone — we don't fail the
       whole run over an undocumented response shape.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from lib.api_clients.contract_drive import ContractDriveClient
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


TEST_CASE_NAME = "UI File Upload endpoint"

# Markers: `-m e2e` runs the whole suite; `-m ui_upload` targets just this file.
pytestmark = [pytest.mark.e2e, pytest.mark.ui_upload]


async def test_ui_file_upload_full_pipeline(
    contract_drive_client: ContractDriveClient,
    config: E2EConfig,
    generated_contract_pdf: bytes,
    run_ctx: RunContext,
    event_listener: "EventHubListener | None",
    pipeline_report: PipelineReport,
) -> None:
    """Upload one file through the UI contract-drive endpoint and verify
    the pipeline progression when the response yields a ufid.

    Uses the same per-run generated Contract Agreement PDF as the
    bulk-upload test, so scheduled runs never collide with a cached
    body on the server side.
    """
    filename = f"nebulae2etest-{run_ctx.run_id}.pdf"

    # ------------------------------------------------------------------
    # Stage 1 — UI endpoint: PASS when we get HTTP 200.
    # ------------------------------------------------------------------
    response = await contract_drive_client.add_file(
        drive_id=config.ui_upload_drive_id,
        filename=filename,
        content=generated_contract_pdf,
        content_type="application/pdf",
    )

    assert response.status_code == 200, (
        f"UI file upload expected HTTP 200, got {response.status_code}. "
        f"raw={response.raw}"
    )

    # Without a ufid there's nothing to track through Event Hub — record
    # the accepted upload under a synthetic key so it still shows up in
    # the report, then stop.
    ufid = response.ufid or f"ui-upload-{run_ctx.run_id}"
    run_ctx.register(ufid)
    pipeline_report.add_document(
        ufid,
        filename,
        test_case=TEST_CASE_NAME,
        response_body=response.raw,
    )
    print(
        f"\nui-file-upload → status={response.status_code} ufid={response.ufid} "
        f"raw={response.raw}"
    )

    detail = (
        f"POST /_/api/contract-drive/{config.ui_upload_drive_id}/add → "
        f"HTTP {response.status_code}"
    )
    if response.ufid:
        detail += f", ufid={response.ufid}"
    pipeline_report.record_step(
        ufid,
        "Document Service",
        "UI contract-drive upload accepted (HTTP 200)",
        StepStatus.PASS,
        details=detail,
    )

    # ------------------------------------------------------------------
    # Stages 2+ — only runnable when the response contained a real ufid
    # AND Event Hub is configured. Otherwise record SKIPPED so the report
    # still shows the intended pipeline shape and why it didn't run.
    # ------------------------------------------------------------------
    if not response.ufid:
        for stage in PIPELINE_STAGES:
            pipeline_report.record_step(
                ufid, stage.service, stage.description,
                StepStatus.SKIPPED,
                details="No ufid in UI upload response — pipeline tracking skipped.",
            )
        return

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
