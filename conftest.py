import logging
import os
import pathlib
from datetime import datetime, timezone

import pytest

from lib.api_clients.document_service import DocumentServiceClient
from lib.api_clients.gateway_document_service import GatewayDocumentServiceClient
from lib.auth import fetch_access_token
from lib.config import E2EConfig
from lib.fixtures import FixtureFile, load_test_files
from lib.report import PipelineReport
from lib.run_context import RunContext


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
