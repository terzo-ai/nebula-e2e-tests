from pydantic_settings import BaseSettings


class E2EConfig(BaseSettings):
    model_config = {
        "env_prefix": "E2E_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_ignore_empty": True,
    }

    # Base URL for all API traffic. In CI this is set to the gateway URL
    # (including service prefix) via the E2E_BASE_URL repo variable, e.g.
    # https://terzoai-gateway-dev.terzocloud.com/nebula/document-service
    # The default here keeps local dev backwards-compat with mafia.
    base_url: str = "https://mafia.terzocloud.com"
    tenant_id: int = 1000012

    # Source URL for bulk-upload test fixtures (blob/file share already populated)
    bulk_upload_source_url: str = "https://stterzoaidev.file.core.windows.net/fs-terzo-ai-dev"

    # Filename used for the bulk-upload test payload. Override via
    # E2E_BULK_UPLOAD_FILE_NAME if you rotate the fixture file.
    bulk_upload_file_name: str = "tz_nebula_e2e.pdf"

    # --- Auth (two-step flow) ---
    # Step 1: log into Analytics (public) with email/password to get an ACCESS_TOKEN.
    # Step 2: call auth-service (internal) with that ACCESS_TOKEN as X-Access-Token
    # to exchange for the bearer token used against the Nebula gateway.
    #
    # If `token` is set directly (via E2E_TOKEN), both steps are skipped.

    # Step 1 — Analytics login
    analytics_base_url: str = "https://mafia.terzocloud.com"
    analytics_email: str = ""       # via E2E_ANALYTICS_EMAIL secret
    analytics_password: str = ""    # via E2E_ANALYTICS_PASSWORD secret
    analytics_xsrf_token: str = ""  # via E2E_ANALYTICS_XSRF_TOKEN secret
    analytics_cookie: str = ""      # via E2E_ANALYTICS_COOKIE secret

    # Step 2 — auth-service token exchange (reachable from Dev cluster)
    auth_service_url: str = "https://auth-service-dev1.product-internal.terzocloud.com"
    auth_user_id: int = 1000129
    auth_email: str = "paventhan@terzocloud.com"

    # Final bearer token for gateway requests. If set manually via E2E_TOKEN,
    # the two-step flow above is skipped.
    token: str = ""

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
