import logging
import os
import pathlib
from datetime import datetime, timezone

import pytest

from lib.api_clients.contract_drive import ContractDriveClient
from lib.api_clients.document_service import DocumentServiceClient
from lib.api_clients.gateway_document_service import GatewayDocumentServiceClient
from lib.auth import fetch_access_token
from lib.config import E2EConfig
from lib.fixtures import FixtureFile, load_test_files
from lib.pdf_generator import generate_contract_pdf
from lib.report import PipelineReport
from lib.run_context import RunContext
from lib.source_upload import (
    SourceUpload,
    SourceUploadError,
    delete_blob,
    upload_contract_pdf,
)


@pytest.fixture(scope="session")
def config() -> E2EConfig:
    return E2EConfig()


@pytest.fixture(scope="session")
async def access_token(config: E2EConfig, pipeline_report: PipelineReport) -> str:
    """Get bearer token for Nebula gateway requests.

    Preference order:
      1. E2E_TOKEN env var (manual override) → `config.token`
      2. Two-step auth flow: Analytics login → auth-service exchange
    """
    if config.token:
        return config.token
    try:
        return await fetch_access_token(config)
    except Exception as e:
        msg = (
            f"Could not fetch access token from auth service: {e}\n"
            f"Auth service at {config.auth_service_url} is not reachable.\n"
            f"Run from the Dev cluster where the auth service is accessible."
        )
        pipeline_report.record_error(
            f"Auth failure: {e} — auth service at {config.auth_service_url} is not reachable"
        )
        pytest.fail(msg)


@pytest.fixture(scope="session")
def run_ctx() -> RunContext:
    """Unique run context shared across all tests in this session."""
    ctx = RunContext()
    print(f"\n  Run ID: {ctx.run_id}")
    return ctx


@pytest.fixture(scope="session")
def test_files(config: E2EConfig) -> list[FixtureFile]:
    """Load test files from configured source (Azure Blob / local dir / in-memory)."""
    files = load_test_files(config)
    print(f"\n  Loaded {len(files)} test file(s): {[f.name for f in files]}")
    return files


@pytest.fixture(scope="session")
def sample_pdf(test_files: list[FixtureFile]) -> bytes:
    """First test file's content — for single-file tests."""
    return test_files[0].content


@pytest.fixture(scope="session")
def generated_contract_pdf(run_ctx: RunContext) -> bytes:
    """Fresh 1-page Contract Agreement PDF bytes, unique to this run_id.

    Keeps scheduled runs from hitting content-dedup in the pipeline. ~30–60 KB.
    """
    pdf = generate_contract_pdf(run_ctx.run_id)
    print(f"\n  Generated contract PDF: {len(pdf)} bytes for run {run_ctx.run_id}")
    return pdf


@pytest.fixture(scope="session")
def bulk_upload_source_url(
    config: E2EConfig,
    run_ctx: RunContext,
    generated_contract_pdf: bytes,
    pipeline_report: PipelineReport,
):
    """Upload the per-run PDF to Azure Blob and return a SAS URL that
    bulk-upload's worker can fetch. Cleans up the blob at teardown.

    Falls back to `config.bulk_upload_source_url` (the static pre-staged
    URL) when the fixtures connection string isn't configured — keeps
    local runs working without requiring the secret.
    """
    if not config.fixtures_connection_string:
        msg = (
            "E2E_FIXTURES_CONNECTION_STRING unset — using pre-staged URL "
            f"{config.bulk_upload_source_url} (no per-run upload, no cleanup)."
        )
        print(f"\n  {msg}")
        yield config.bulk_upload_source_url
        return

    try:
        upload: SourceUpload = upload_contract_pdf(
            content=generated_contract_pdf,
            run_id=run_ctx.run_id,
            connection_string=config.fixtures_connection_string,
            container=config.upload_container,
        )
    except SourceUploadError as e:
        # Record + re-raise so the session fails fast rather than running
        # bulk-upload against a stale URL and hiding the real cause.
        pipeline_report.record_error(f"Per-run source upload failed: {e}")
        raise

    print(
        f"\n  Uploaded source PDF: {upload.container}/{upload.blob_name} "
        f"→ SAS URL (2h TTL)"
    )
    try:
        yield upload.sas_url
    finally:
        delete_blob(upload, connection_string=config.fixtures_connection_string)


@pytest.fixture(scope="session")
async def event_listener(config: E2EConfig):
    """Event Hub listener — captures pipeline events during test execution.

    Yields None if Event Hub is not configured (tests still pass without event data).
    """
    if not config.event_hub_connection_string:
        yield None
        return

    from lib.event_hub import EventHubListener

    async with EventHubListener(
        connection_string=config.event_hub_connection_string,
        event_hub_name=config.event_hub_name,
        consumer_group=config.event_hub_consumer_group,
        timeout=config.event_hub_listen_timeout,
    ) as listener:
        yield listener


@pytest.fixture(scope="session")
def pipeline_report(config: E2EConfig, run_ctx: RunContext):
    """Collects pipeline step results across all tests, generates HTML report at teardown."""
    # Build GitHub Actions run URL from standard env vars (set automatically in CI).
    gh_server = os.environ.get("GITHUB_SERVER_URL", "")
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    gh_run_id = os.environ.get("GITHUB_RUN_ID", "")
    gh_url = f"{gh_server}/{gh_repo}/actions/runs/{gh_run_id}" if gh_run_id else ""

    report = PipelineReport(
        run_id=run_ctx.run_id,
        environment=config.base_url,
        tenant_id=config.tenant_id,
        started_at=datetime.now(timezone.utc).isoformat(),
        github_actions_url=gh_url,
    )
    # Stream log records into the report so the HTML log panel is populated.
    formatter = logging.Formatter("%(message)s")
    handler = report.install_log_handler(level=logging.INFO)
    handler.setFormatter(formatter)
    try:
        yield report
    finally:
        logging.getLogger().removeHandler(handler)

        report.finalize()
        output_dir = pathlib.Path(config.report_output_dir)
        path = report.save(output_dir / f"pipeline-{run_ctx.run_id}.html")
        print(f"\n  Pipeline report: {path}")

        # Write Slack payload JSON for the GitHub Action notification step.
        # E2E_SLACK_CHANNEL_ID env var (wired from a GitHub repo/env variable)
        # overrides the default when set.
        slack_channel_id = os.environ.get("E2E_SLACK_CHANNEL_ID", "").strip() or "C0ARRGXRY5P"
        import json as _json
        slack_payload = {
            "channel": slack_channel_id,
            "text": report.slack_summary(),
        }
        payload_path = output_dir / "slack-payload.json"
        payload_path.write_text(_json.dumps(slack_payload), encoding="utf-8")
        print(f"  Slack payload: {payload_path}")

        # Always-on DM summary for paventhan@terzocloud.com. The workflow
        # step that sends this is NOT gated by E2E_SLACK_NOTIFY — so
        # paventhan gets a run_id + link on every scheduled run, even
        # when channel notifications are disabled. Text-only payload;
        # the workflow resolves the email to a user_id via
        # users.lookupByEmail and fills in `channel` before posting.
        dm_text = (
            f":test_tube: *E2E run* `{run_ctx.run_id}` — "
            f"{report.overall_status.value}"
        )
        if gh_url:
            dm_text += f" · <{gh_url}|GitHub Actions run>"
        dm_text += f"\nEnvironment: `{config.base_url}`"
        dm_text += f"\nTenant: `{config.tenant_id}`"
        dm_payload_path = output_dir / "slack-dm-payload.json"
        dm_payload_path.write_text(
            _json.dumps({"text": dm_text, "recipient_email": "paventhan@terzocloud.com"}),
            encoding="utf-8",
        )
        print(f"  Slack DM payload (paventhan): {dm_payload_path}")


@pytest.fixture
async def doc_client(config: E2EConfig, access_token: str) -> DocumentServiceClient:
    client = DocumentServiceClient(
        base_url=config.base_url,
        tenant_id=config.tenant_id,
        access_token=access_token,
    )
    yield client
    await client.close()


@pytest.fixture
async def gateway_doc_client(
    config: E2EConfig, access_token: str
) -> GatewayDocumentServiceClient:
    """Client targeting document-service via the terzoai-gateway (for bulk-upload etc.)."""
    client = GatewayDocumentServiceClient(
        base_url=config.base_url,
        tenant_id=config.tenant_id,
        access_token=access_token,
    )
    yield client
    await client.close()


@pytest.fixture
async def contract_drive_client(
    config: E2EConfig, pipeline_report: PipelineReport
) -> ContractDriveClient:
    """Client for the UI contract-drive upload endpoint on the Analytics host.

    Uses CSRF double-submit auth — the XSRF token is sent as both the
    header and the cookie, so only `E2E_ANALYTICS_XSRF_TOKEN` is needed.
    Skips cleanly when unset so the test is self-explanatory in
    environments where UI auth isn't configured.
    """
    if not config.analytics_xsrf_token:
        msg = (
            "UI contract-drive upload needs an XSRF token — set "
            "E2E_ANALYTICS_XSRF_TOKEN"
        )
        pipeline_report.record_error(msg)
        pytest.skip(msg)
    client = ContractDriveClient(
        base_url=config.analytics_base_url,
        xsrf_token=config.analytics_xsrf_token,
    )
    yield client
    await client.close()
