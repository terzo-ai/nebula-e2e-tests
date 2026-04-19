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
    3. If the response carries a ufid, use it directly. If not (the
       contract-drive response doesn't reliably include one), resolve
       the ufid by paginating doc-reader and filtering on the filename
       we uploaded. Either way, the ufid is then handed to the shared
       Event Hub walker for pipeline verification.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from lib.api_clients.contract_drive import ContractDriveClient
from lib.api_clients.doc_reader import DocReaderClient
from lib.config import E2EConfig
from lib.pipeline_runner import run_pipeline_stages, skip_all_stages
from lib.report import PipelineReport, StepStatus
from lib.run_context import RunContext

if TYPE_CHECKING:
    from lib.event_hub import EventHubListener

logger = logging.getLogger(__name__)


TEST_CASE_NAME = "UI File Upload endpoint"

# How long to wait for doc-reader to surface a freshly uploaded file.
# File-ingestion writes first, doc-reader catches up a beat later — a
# short retry window is all we need in practice.
DOC_READER_RESOLVE_TIMEOUT_S = 30.0
DOC_READER_POLL_INTERVAL_S = 2.0

# Markers: `-m e2e` runs the whole suite; `-m ui_upload` targets just this file.
pytestmark = [pytest.mark.e2e, pytest.mark.ui_upload]


async def test_ui_file_upload_full_pipeline(
    contract_drive_client: ContractDriveClient,
    doc_reader_client: DocReaderClient,
    config: E2EConfig,
    generated_contract_pdf: bytes,
    run_ctx: RunContext,
    event_listener: "EventHubListener | None",
    pipeline_report: PipelineReport,
) -> None:
    """Upload one file through the UI contract-drive endpoint and verify
    the pipeline progression, resolving the ufid via doc-reader when the
    UI response doesn't include one.

    Uses the same per-run generated Contract Agreement PDF as the
    bulk-upload test, so scheduled runs never collide with a cached
    body on the server side.
    """
    # Unique suffix so doc-reader's filename filter resolves the ufid for
    # this run's UI upload and not the bulk-upload test's identically-
    # run-tagged document.
    filename = f"nebulae2etest-{run_ctx.run_id}-ui.pdf"

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

    # ------------------------------------------------------------------
    # Stage 2 — Resolve the ufid. Prefer the response body; fall back to
    # a doc-reader lookup paginated by filename. Only after we have (or
    # give up on) the ufid do we register the document in the report, so
    # the document key matches reality.
    # ------------------------------------------------------------------
    resolved_ufid = response.ufid
    resolved_via_doc_reader = False
    if not resolved_ufid:
        logger.info(
            "UI upload response carried no ufid — resolving via doc-reader by "
            "filename=%s (timeout=%.0fs)",
            filename, DOC_READER_RESOLVE_TIMEOUT_S,
        )
        resolved_ufid = await doc_reader_client.wait_for_ufid_by_name(
            filename,
            timeout_s=DOC_READER_RESOLVE_TIMEOUT_S,
            poll_interval_s=DOC_READER_POLL_INTERVAL_S,
        )
        resolved_via_doc_reader = True

    ufid = resolved_ufid or f"ui-upload-{run_ctx.run_id}"
    run_ctx.register(ufid)
    pipeline_report.add_document(
        ufid,
        filename,
        test_case=TEST_CASE_NAME,
        response_body=response.raw,
    )
    print(
        f"\nui-file-upload → status={response.status_code} "
        f"ufid_in_response={response.ufid} resolved_ufid={resolved_ufid} "
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
        "File Ingestion Service",
        "UI contract-drive upload accepted (HTTP 200)",
        StepStatus.PASS,
        details=detail,
    )

    # Record a dedicated row for the doc-reader resolution when we had
    # to fall back to it, so the report shows clearly whether pipeline
    # tracking was satisfied directly or required a secondary lookup.
    if resolved_via_doc_reader:
        if resolved_ufid:
            pipeline_report.record_step(
                ufid,
                "Doc Reader",
                "Resolve ufid by filename",
                StepStatus.PASS,
                details=(
                    f"GET /nebula/doc-reader/api/v1/documents?size=100 "
                    f"filtered on name={filename} → ufid={resolved_ufid}"
                ),
            )
        else:
            pipeline_report.record_step(
                ufid,
                "Doc Reader",
                "Resolve ufid by filename",
                StepStatus.FAIL,
                details=(
                    f"doc-reader did not surface a ufid for name={filename} "
                    f"within {DOC_READER_RESOLVE_TIMEOUT_S:.0f}s "
                    f"(poll every {DOC_READER_POLL_INTERVAL_S:.0f}s)"
                ),
            )

    # ------------------------------------------------------------------
    # Event Hub stages — only runnable when we have a real ufid. Either
    # from the UI response directly, or resolved via doc-reader. Without
    # one, record SKIPPED so the report still shows the intended shape.
    # ------------------------------------------------------------------
    if not resolved_ufid:
        skip_all_stages(
            pipeline_report, ufid,
            reason=(
                "Neither UI response nor doc-reader yielded a ufid — "
                "pipeline tracking skipped."
            ),
        )
        return

    await run_pipeline_stages(
        ufid,
        event_listener=event_listener,
        pipeline_report=pipeline_report,
    )
