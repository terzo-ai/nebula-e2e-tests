import logging
import os
import pathlib
from datetime import datetime, timezone

import pytest

from lib.api_clients.contract_drive import ContractDriveClient
from lib.api_clients.doc_reader import DocReaderClient
from lib.api_clients.file_ingestion import FileIngestionClient
from lib.api_clients.gateway_file_ingestion import GatewayFileIngestionClient
from lib.auth import (
    AuthError,
    fetch_access_token,
    fetch_analytics_session_cookie,
    is_token_expired,
)
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
      1. E2E_TOKEN env var (manual override) → `config.token`, if it's
         still valid. A JWT whose `exp` has passed is discarded and the
         two-step flow runs instead.
      2. Two-step auth flow: Analytics login → auth-service exchange
    """
    if config.token:
        if not is_token_expired(config.token):
            return config.token
        print(
            "\n  E2E_TOKEN is expired — falling back to auth-service "
            f"at {config.auth_service_url}"
        )
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


@pytest.fixture
def generated_contract_pdf(
    run_ctx: RunContext, request: pytest.FixtureRequest
) -> bytes:
    """Fresh 1-page Contract Agreement PDF bytes, regenerated per test.

    Each test invocation gets a byte-unique PDF (the document reference
    embeds `run_id` + test-node name + a short random suffix) so repeat
    tests inside one session never collide with content-addressed dedup
    on the server. ~30–60 KB.
    """
    import uuid
    doc_ref = f"{run_ctx.run_id}-{request.node.name}-{uuid.uuid4().hex[:4]}"
    pdf = generate_contract_pdf(doc_ref)
    print(
        f"\n  Generated contract PDF: {len(pdf)} bytes "
        f"(test={request.node.name}, ref={doc_ref})"
    )
    return pdf


@pytest.fixture
def bulk_upload_source_url(
    config: E2EConfig,
    run_ctx: RunContext,
    generated_contract_pdf: bytes,
    pipeline_report: PipelineReport,
    request: pytest.FixtureRequest,
):
    """Upload the per-test PDF to Azure Blob and return a SAS URL that
    bulk-upload's worker can fetch. Cleans up the blob at teardown.

    Function-scoped to match `generated_contract_pdf` — each test gets
    its own freshly uploaded blob (distinct blob name = run_id + test
    name) so runs never reuse a stale body or collide on the container.

    Falls back to `config.bulk_upload_source_url` (the static pre-staged
    URL) when the fixtures connection string isn't configured — keeps
    local runs working without requiring the secret.
    """
    if not config.fixtures_connection_string:
        msg = (
            "E2E_FIXTURES_CONNECTION_STRING unset — using pre-staged URL "
            f"{config.bulk_upload_source_url} (no per-test upload, no cleanup)."
        )
        print(f"\n  {msg}")
        yield config.bulk_upload_source_url
        return

    upload_tag = f"{run_ctx.run_id}-{request.node.name}"
    try:
        upload: SourceUpload = upload_contract_pdf(
            content=generated_contract_pdf,
            run_id=upload_tag,
            connection_string=config.fixtures_connection_string,
            container=config.upload_container,
        )
    except SourceUploadError as e:
        # Record + re-raise so the session fails fast rather than running
        # bulk-upload against a stale URL and hiding the real cause.
        pipeline_report.record_error(f"Per-test source upload failed: {e}")
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

        # Write Slack payload JSON files for the GitHub Action notification
        # steps. Two messages are produced:
        #
        #   * `slack-payload.json` — the compact main-channel summary
        #     (Block Kit blocks + a fallback `text` for push notifications).
        #   * `slack-thread-payload.json` — the per-stage breakdown. The
        #     workflow fills in `channel` + `thread_ts` from the main
        #     message's response before posting.
        #
        # E2E_SLACK_CHANNEL_ID env var (wired from a GitHub repo/env variable)
        # overrides the default when set.
        slack_channel_id = os.environ.get("E2E_SLACK_CHANNEL_ID", "").strip() or "C0ARRGXRY5P"
        import json as _json

        from lib.slack_report import (
            build_fallback_text,
            build_main_blocks,
            build_thread_blocks,
            build_thread_fallback_text,
        )

        slack_payload = {
            "channel": slack_channel_id,
            "text": build_fallback_text(report),
            "blocks": build_main_blocks(report),
        }
        payload_path = output_dir / "slack-payload.json"
        payload_path.write_text(_json.dumps(slack_payload), encoding="utf-8")
        print(f"  Slack payload: {payload_path}")

        thread_payload = {
            "text": build_thread_fallback_text(report),
            "blocks": build_thread_blocks(report),
        }
        thread_payload_path = output_dir / "slack-thread-payload.json"
        thread_payload_path.write_text(
            _json.dumps(thread_payload), encoding="utf-8"
        )
        print(f"  Slack thread payload: {thread_payload_path}")

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
async def doc_client(config: E2EConfig, access_token: str) -> FileIngestionClient:
    client = FileIngestionClient(
        base_url=config.base_url,
        tenant_id=config.tenant_id,
        access_token=access_token,
    )
    yield client
    await client.close()


@pytest.fixture
async def gateway_doc_client(
    config: E2EConfig, access_token: str
) -> GatewayFileIngestionClient:
    """Client targeting file-ingestion via the terzoai-gateway (for bulk-upload etc.)."""
    client = GatewayFileIngestionClient(
        base_url=config.base_url,
        tenant_id=config.tenant_id,
        access_token=access_token,
    )
    yield client
    await client.close()


@pytest.fixture
async def doc_reader_client(
    config: E2EConfig, access_token: str
) -> DocReaderClient:
    """Client for the Nebula doc-reader service (paginated document listing).

    Used by upload flows that don't return a ufid directly (e.g. UI
    contract-drive). The same gateway host + Bearer token are reused,
    so no extra auth wiring is required.
    """
    client = DocReaderClient(
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

    Performs an Analytics login (`POST /_/api/auth/login/password`) with
    the CSRF token to fetch a fresh `x-access-token` session cookie,
    then hands both cookies to the client. Skips with a clear message
    when any required input is missing, so the test is self-explanatory
    in environments without UI auth configured.
    """
    if not config.analytics_xsrf_token:
        msg = (
            "UI contract-drive upload needs an XSRF token — set "
            "E2E_ANALYTICS_XSRF_TOKEN"
        )
        pipeline_report.record_error(msg)
        pytest.skip(msg)
    try:
        x_access_token = await fetch_analytics_session_cookie(config)
    except AuthError as e:
        msg = f"Analytics login failed — cannot fetch x-access-token: {e}"
        pipeline_report.record_error(msg)
        pytest.skip(msg)
    client = ContractDriveClient(
        base_url=config.analytics_base_url,
        xsrf_token=config.analytics_xsrf_token,
        x_access_token=x_access_token,
    )
    yield client
    await client.close()
