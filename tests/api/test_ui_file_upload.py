"""E2E: UI file upload via the 3-step contract-drive flow.

The Contract Drive UI now uploads in three legs:

    1. POST /_/api/contract-drive/{drive_id}/upload → ufid + SAS URL.
    2. PUT <SAS URL> with the file bytes (Azure Blob).
    3. POST /_/api/contract-drive/{drive_id}/confirm-upload.

Steps 1 and 3 live on the Analytics host (``mafia.terzocloud.com``) and
are session-authenticated with ``X-XSRF-TOKEN`` + ``Cookie``. Step 2
talks directly to Azure Blob via the SAS URL returned by step 1 — no
session auth involved.

The test:

    1. Run the full 3-step upload; each leg raises on non-2xx.
    2. Record the aggregate result into the pipeline report so the
       init response (ufid + uploadUrl) renders under the test-case panel.
    3. Hand the ufid (returned directly in step 1) to the shared Event
       Hub walker for pipeline verification. If init didn't carry a
       ufid for some reason, fall back to a filename lookup against
       doc-reader before giving up.
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
    """Upload one file through the UI contract-drive 3-step flow and
    verify pipeline progression. The init step returns the ufid
    directly; we fall back to a doc-reader lookup only if it doesn't.
    """
    # Unique suffix so doc-reader's filename filter resolves the ufid for
    # this run's UI upload and not the bulk-upload test's identically-
    # run-tagged document.
    filename = f"nebulae2etest-{run_ctx.run_id}-ui.pdf"

    # ------------------------------------------------------------------
    # Stage 1 — UI endpoint: PASS when all three legs succeed.
    # ------------------------------------------------------------------
    result = await contract_drive_client.upload_file(
        drive_id=config.ui_upload_drive_id,
        filename=filename,
        content=generated_contract_pdf,
        content_type="application/pdf",
    )

    assert result.confirm_status_code == 200, (
        f"UI file upload expected confirm-upload HTTP 200, got "
        f"{result.confirm_status_code}. confirm_raw={result.confirm_raw}"
    )

    # ------------------------------------------------------------------
    # Stage 2 — Resolve the ufid. The init response carries it directly;
    # fall back to doc-reader by filename only if something stripped it.
    # ------------------------------------------------------------------
    resolved_ufid = result.ufid
    resolved_via_doc_reader = False
    if not resolved_ufid:
        logger.info(
            "UI upload init response carried no ufid — resolving via "
            "doc-reader by filename=%s (timeout=%.0fs)",
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
        response_body=result.init_raw,
    )
    print(
        f"\nui-file-upload → init={result.init_status_code} "
        f"put={result.blob_put_status_code} "
        f"confirm={result.confirm_status_code} "
        f"ufid_in_response={result.ufid} resolved_ufid={resolved_ufid} "
        f"init_raw={result.init_raw}"
    )

    drive_id = config.ui_upload_drive_id
    detail = (
        f"3-step upload OK: "
        f"POST /_/api/contract-drive/{drive_id}/upload → HTTP {result.init_status_code}; "
        f"PUT <blob> → HTTP {result.blob_put_status_code}; "
        f"POST /_/api/contract-drive/{drive_id}/confirm-upload → HTTP {result.confirm_status_code}"
    )
    if result.ufid:
        detail += f", ufid={result.ufid}"
    pipeline_report.record_step(
        ufid,
        "File Ingestion Service",
        "UI contract-drive upload accepted (init + blob PUT + confirm)",
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
