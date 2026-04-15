from pydantic_settings import BaseSettings


class E2EConfig(BaseSettings):
    model_config = {
        "env_prefix": "E2E_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_ignore_empty": True,
    }

    # All tests hit mafia.terzocloud.com (Dev environment)
    base_url: str = "https://mafia.terzocloud.com"
    tenant_id: int = 1000012

    # Auth service — auto-fetches access token (reachable from Dev cluster)
    auth_service_url: str = "http://auth-service-mwc1.product-internal.terzocloud.com"
    auth_service_key: str = "e9c4455dc79d417b986d0386eb7b3d25"
    auth_user_id: int = 1001359
    auth_email: str = "shankar@terzocloud.com"

    # Gateway auth — auto-fetched from auth service, or set manually
    access_token: str = ""

    # Test file sources (checked in order: mounted dir → azure blob → in-memory)
    # Option 1: Local directory or mounted Azure File Share (fs-terzo-ai-dev)
    #   In K8s: mount PVC at /mnt/e2e-fixtures → set E2E_FIXTURES_DIR=/mnt/e2e-fixtures
    fixtures_dir: str = ""
    # Option 2: Azure Blob container with test fixtures
    fixtures_connection_string: str = ""
    fixtures_container: str = "e2e-test-fixtures"

    # Drive API credentials (only needed for Drive upload tests)
    drive_client_id: str = "AI-001"
    drive_user_name: str = "AIEXTRACT"
    drive_password: str = ""

    # Polling & timeouts
    full_pipeline_timeout: int = 600
    poll_interval: float = 2.0
    poll_backoff: float = 1.5
    poll_max_interval: float = 15.0

    # Event Hub (optional — when set, tests capture pipeline events)
    event_hub_connection_string: str = ""
    event_hub_name: str = ""
    event_hub_consumer_group: str = "probe-test"
    event_hub_listen_timeout: float = 120.0

    # Pipeline report
    report_output_dir: str = "reports"
