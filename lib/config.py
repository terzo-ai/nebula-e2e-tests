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

    # Fallback source URL for bulk-upload when per-run upload is
    # unavailable (e.g. local dev without `E2E_FIXTURES_CONNECTION_STRING`).
    # Scheduled runs never hit this — the `bulk_upload_source_url` session
    # fixture generates + uploads a fresh PDF and returns its SAS URL.
    bulk_upload_source_url: str = "https://stterzoaidev.file.core.windows.net/fs-terzo-ai-dev"

    # Azure Blob container where each run uploads its fresh contract PDF.
    # Reuses `fixtures_connection_string`; blobs land at
    # `{upload_container}/nebulae2etest-<run_id>.pdf` and are deleted at
    # session teardown.
    upload_container: str = "e2e-test-fixtures"

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

    # UI contract-drive file upload (POST /_/api/contract-drive/{drive_id}/add).
    # Runs against the Analytics/mafia host and uses the same session
    # credentials as the Analytics login (X-XSRF-TOKEN + Cookie).
    ui_upload_drive_id: int = 1
    # File to send as the multipart `file` part. Override via
    # E2E_UI_UPLOAD_FILE_NAME if you want a different fixture here.
    ui_upload_file_name: str = "tz_nebula_e2e.pdf"

    # Polling & timeouts
    full_pipeline_timeout: int = 600
    poll_interval: float = 2.0
    poll_backoff: float = 1.5
    poll_max_interval: float = 15.0

    # Event Hub (optional — when set, tests capture pipeline events).
    # Defaults match the OCRM platform wiring:
    #   OCRM_EVENTHUB_NAME           = terzo-ai-contract-document-events
    #   OCRM_EVENTHUB_CONSUMER_GROUP = terzo-ai-extraction-platform
    event_hub_connection_string: str = ""
    event_hub_name: str = "terzo-ai-contract-document-events"
    event_hub_consumer_group: str = "terzo-ai-nebula-e2e-tests-probe"
    event_hub_listen_timeout: float = 120.0

    # Pipeline report
    report_output_dir: str = "reports"

    # Slack notification (controlled via E2E_SLACK_NOTIFY GitHub variable).
    # Set to false to skip Slack notifications.
    slack_notify: bool = True
