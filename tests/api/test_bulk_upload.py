"""E2E: bulk-upload → observe full pipeline events via Event Hub.

Invokes POST {E2E_BASE_URL}/api/v1/documents/bulk-upload with a single item,
extracts the ufid from the response, then hands off to
:func:`lib.pipeline_runner.run_pipeline_stages` for the shared Event Hub
verification (same walker the presigned and UI tests use).

The HTML pipeline report is rendered at session teardown and uploaded as
the `pipeline-report-nightly` / `pipeline-report` workflow artifact.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from lib.api_clients.gateway_file_ingestion import (
    BulkUploadItem,
    GatewayFileIngestionClient,
)
from lib.config import E2EConfig
from lib.pipeline_runner import run_pipeline_stages
from lib.report import PipelineReport, StepStatus
from lib.run_context import RunContext

if TYPE_CHECKING:
    from lib.event_hub import EventHubListener

logger = logging.getLogger(__name__)


TEST_CASE_NAME = "Bulk Upload"

# Markers: `-m e2e` runs the whole suite; `-m bulk_upload` targets just this file.
pytestmark = [pytest.mark.e2e, pytest.mark.bulk_upload]


async def test_bulk_upload_full_pipeline(
    gateway_doc_client: GatewayFileIngestionClient,
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
    # Suffix the filename so doc-reader (which filters by exact name) can
    # never confuse this document with the identically-run-tagged one the
    # UI upload test registers in the same session.
    filename = f"nebulae2etest-{run_ctx.run_id}-bulk.pdf"

    # ------------------------------------------------------------------
    # Stage 1 — File Ingestion Service: PASS when we get HTTP 202 + ufid.
    # ------------------------------------------------------------------
    response = await gateway_doc_client.bulk_upload(
        items=[
            BulkUploadItem(
                name=filename,
                processing_mode="EXTRACT",
                content_type="application/pdf",
                size_bytes=len(generated_contract_pdf),
                document_type="contract",
                source_url=bulk_upload_source_url,
            ),
        ],
        source="BULK_IMPORT",
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
        "File Ingestion Service",
        "bulk-upload accepted (HTTP 202 + ufid returned)",
        StepStatus.PASS,
        details=(
            f"POST /api/v1/documents/bulk-upload → "
            f"HTTP {response.status_code}, ufid={ufid}"
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
