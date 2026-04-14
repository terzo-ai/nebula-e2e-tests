import pytest

from lib.api_clients.document_service import DocumentServiceClient
from lib.auth import fetch_access_token
from lib.config import E2EConfig
from lib.fixtures import FixtureFile, load_test_files
from lib.run_context import RunContext


@pytest.fixture(scope="session")
def config() -> E2EConfig:
    return E2EConfig()


@pytest.fixture(scope="session")
async def access_token(config: E2EConfig) -> str:
    """Get access token: manual override or auto-fetch from auth service."""
    if config.access_token:
        return config.access_token
    try:
        return await fetch_access_token(config)
    except Exception as e:
        pytest.fail(
            f"Could not fetch access token from auth service: {e}\n"
            f"Auth service at {config.auth_service_url} is not reachable.\n"
            f"Run from the Dev cluster where the auth service is accessible."
        )


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
async def doc_client(config: E2EConfig, access_token: str) -> DocumentServiceClient:
    client = DocumentServiceClient(
        base_url=config.base_url,
        tenant_id=config.tenant_id,
        access_token=access_token,
    )
    yield client
    await client.close()
