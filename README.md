# Nebula E2E Tests

End-to-end tests for the Nebula document processing pipeline — validates the full flow from **file upload through OCR to extraction** across three microservices.

---

## Table of Contents

1. [Overview](#overview)
2. [What This Repo Tests](#what-this-repo-tests)
3. [Architecture](#architecture)
4. [How It Works](#how-it-works)
5. [Test File Sources](#test-file-sources)
6. [Tech Stack](#tech-stack)
7. [Project Structure](#project-structure)
8. [Test Suite](#test-suite) — incl. [Bulk-Upload + Event-Hub Verification](#bulk-upload--event-hub-verification-test_bulk_uploadpy)
9. [Run Tagging & Identification](#run-tagging--identification)
10. [Cleanup](#cleanup)
11. [Environment & Configuration](#environment--configuration) — gateway, bulk-upload, two-step auth, Event Hub
12. [Running Tests](#running-tests)
13. [Authentication](#authentication) — `E2E_TOKEN` override + Analytics→auth-service two-step flow
14. [Polling Strategy](#polling-strategy)
15. [CI/CD Integration](#cicd-integration) — workflows, K8s Job, [Pipeline HTML Report](#pipeline-html-report), [multi-hub Event Hub listening](#event-hub-multi-hub-listening)
16. [Troubleshooting](#troubleshooting)
17. [Future Work](#future-work)

---

## Overview

The Nebula document processing pipeline spans **four** independent microservices:

| Service | Role |
|---------|------|
| **Document Service** | Upload orchestration, bulk-upload acceptance, metadata, event routing |
| **OCR Service** | OCR via Azure Document Intelligence |
| **Extraction Service** | AI data extraction from OCR output |
| **Ingestion Service** | Final ingestion into downstream systems (Drive, search, analytics) |

Services communicate asynchronously via **Azure Event Hub** (5 child hubs under the `terzo-ai-nebula-events-dev` namespace) using **CloudEvents v1.0**. There are no synchronous dependencies — the only way to verify the pipeline works end-to-end is to submit a document and watch it flow through every stage. That's what this repo does.

---

## What This Repo Tests

Two end-to-end paths against the Nebula gateway (`https://terzoai-gateway-dev.terzocloud.com/nebula/document-service`):

### 1. Bulk-upload + full pipeline event verification (primary daily run)

`tests/api/test_bulk_upload.py::test_bulk_upload_full_pipeline`:

- POST to `/api/v1/documents/bulk-upload` with a single `sourceUrl` item
- Extract the `ufid` from the response
- Listen to **all 5 child Event Hubs in parallel** for the 7 `com.terzo.document.*` events the pipeline should emit
- Record each event as a **PASS / FAIL row** in the HTML pipeline report, attributed to one of the 4 services
- If `com.terzo.document.failed` fires, mark remaining steps FAIL and surface the `failure_reason` payload field

### 2. Presigned SAS upload + pipeline progression (legacy)

`tests/api/test_single_presigned_upload.py` and `test_multi_presigned_upload.py`:

- 3-step flow (initiate → upload to Azure Blob → confirm)
- Polls `processingState` through `CONFIRMED → OCR_QUEUED → OCR_COMPLETED → EXTRACTION_QUEUED`
- Multi-file variants run with 3, 5, and 10 files concurrently
- Smoke test (`test_smoke.py`) — connectivity, auth, basic CRUD

---

## Architecture

### Bulk-upload + event-driven verification (primary path)

```
  nebula-e2e-tests
       │
       │  POST /nebula/document-service/api/v1/documents/bulk-upload
       │       Authorization: Bearer <token>
       │       X-Tenant-Id: 1000012
       │       { source: BULK_IMPORT, items: [{ name, sourceUrl, ... }] }
       │
       ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │     terzoai-gateway-dev.terzocloud.com  (Nebula API gateway)         │
  │                                                                      │
  │     202 Accepted  → { results: [{ ufid, status: FILE_UPLOAD_QUEUED }]│
  │              │                                                       │
  │              ▼                                                       │
  │   Document Service ──► fetches sourceUrl ──► writes to Blob          │
  │        │                                                             │
  │        │     ┌──── Azure Event Hub Namespace ────────────────────┐   │
  │        │     │  terzo-ai-nebula-events-dev                       │   │
  │        ├────►│  ├─ contract-document-events                      │   │
  │        │     │  ├─ contract-metadata-events                      │   │
  │        │     │  ├─ contract-outbox-events                        │   │
  │        │     │  ├─ ocr-events                                    │   │
  │        │     │  └─ platforms-file-ingest-events                  │   │
  │        │     └───┬────────────────────────────────────────────┬──┘   │
  │        │         │                                            │      │
  │   OCR Service    │   Extraction Service     Ingestion Service │      │
  │        │         │         │                       │          │      │
  │        ├──ocr.completed ──►│                       │          │      │
  │        │                   ├─auto_extraction.completed─►      │      │
  │        │                   │                       ├──ingestion.completed
  │        │                                                                
  └──────────────────────────────────────────────────────────────────────┘
       ▲
       │   E2E listener (this repo) subscribes to ALL 5 child hubs
       │   in parallel and matches events by document_id == ufid.
       │   Each captured event becomes a row in the HTML pipeline report.
       │
       │   Tracked event types (per docs/EVENT_TYPES.md):
       │     com.terzo.document.uploaded                      (Event Hub stage uses the
       │                                                       first event — typically this)
       │     com.terzo.document.ocr.completed
       │     com.terzo.document.auto_extraction.completed
       │     com.terzo.document.ingestion.completed
       │     com.terzo.document.failed   (short-circuits remaining stages to FAIL)
```

### Legacy presigned-upload path

```
  nebula-e2e-tests
       │
       │  1. POST /api/v1/documents/upload      → ufid + SAS URL
       │  2. PUT  {SAS URL}                     → upload bytes
       │  3. POST /api/v1/documents/{ufid}/confirm
       │  4. Poll GET /api/v1/documents/{ufid}  → processingState
       │  5. GET  /api/v1/documents/{ufid}/artifacts
       ▼
   gateway → Document Service → ... (same downstream pipeline)
```

### Where files come from

Test PDFs can come from three sources (checked in order):

1. **Azure File Share** (`fs-terzo-ai-dev`) — mounted as a directory in the K8s pod
2. **Azure Blob container** — downloaded at test startup
3. **In-memory fallback** — generates a minimal valid PDF (default, zero config)

The PDF bytes are uploaded from the test process to **Azure Blob Storage** via the presigned SAS URL — the same path the frontend uses.

### Impact on mafia.terzocloud.com

**Yes, tests create real documents on the Dev environment.** Each test run:

- Creates documents in the document-service database (tenant `1000012`)
- Uploads files to Azure Blob Storage under `1000012/raw/{ufid}/`
- Triggers real OCR processing via Azure Document Intelligence (OCR Service)
- Triggers real extraction (Extraction Service) and ingestion (Ingestion Service)
- **Documents are kept** — never auto-deleted, so you can inspect results after each run
- **Run-tagged** — every file is prefixed with a unique run ID (e.g., `e2e-20260414-030000-a1b2c3-0-contract.pdf`) so you can trace which nightly run created it
- **Cleanup is separate** — run `cleanup.py` or a K8s CronJob when you're done reviewing

---

## How It Works

### Single file test flow

```
Test Process                    mafia.terzocloud.com                Azure Blob Storage
    │                                   │                                │
    ├─── POST /upload ────────────────► │                                │
    │◄── 201 {ufid, uploadUrl} ────────┤                                │
    │                                   │                                │
    ├─── PUT {uploadUrl} ──────────────────────────────────────────────► │
    │◄── 201 Created ──────────────────────────────────────────────────┤ │
    │                                   │                                │
    ├─── POST /{ufid}/confirm ────────► │                                │
    │◄── 201 {status: CONFIRMED} ──────┤                                │
    │                                   │                                │
    │    [outbox poller → Event Hub → document-service consumes]         │
    │                                   │                                │
    ├─── GET /{ufid} (poll) ──────────► │                                │
    │◄── {processingState: OCR_QUEUED} ┤                                │
    │                                   │                                │
    │    [ocr-service runs Azure Document Intelligence]                  │
    │                                   │                                │
    ├─── GET /{ufid} (poll) ──────────► │                                │
    │◄── {processingState: OCR_COMPLETED}                                │
    │                                   │                                │
    ├─── GET /{ufid}/artifacts ───────► │                                │
    │◄── {artifacts: [OCR_PDF, ...]} ──┤                                │
    │                                   │                                │
    ├─── GET /{ufid} (poll) ──────────► │                                │
    │◄── {processingState: EXTRACTION_QUEUED} ✅                         │
    │                                   │                                │
    ├─── DELETE /{ufid} (cleanup) ────► │                                │
```

### Multi-file test flow

Same as above, but steps 1-3 run for N files **concurrently** using `asyncio.TaskGroup`. Then all N documents are polled in parallel until they all reach `EXTRACTION_QUEUED`.

---

## Test File Sources

Tests need PDF files to upload. Three sources are supported, checked in this order:

### Option 1: Azure File Share (recommended for real contracts)

The Azure File Share `fs-terzo-ai-dev` is mounted into the K8s pod. Drop PDFs into a folder and the tests pick them up.

**Upload files to the file share:**

```bash
# Via Azure CLI
az storage file upload-batch \
  --destination fs-terzo-ai-dev/e2e-fixtures \
  --source ./my-test-pdfs/ \
  --account-name stterzoaidev

# Or via Azure Portal
# Storage accounts → stterzoaidev → File shares → fs-terzo-ai-dev → Upload
```

**In the K8s Job**, the file share is mounted at `/mnt/e2e-fixtures`:

```yaml
env:
- name: E2E_FIXTURES_DIR
  value: /mnt/e2e-fixtures
volumes:
- name: test-fixtures
  azureFile:
    shareName: fs-terzo-ai-dev
    secretName: azure-storage-secret
    readOnly: true
```

### Option 2: Azure Blob container

Set `E2E_FIXTURES_CONNECTION_STRING` and `E2E_FIXTURES_CONTAINER` — tests download all PDFs from the container at startup.

### Option 3: In-memory fallback (default)

If no external source is configured, tests generate a minimal valid 1-page PDF in memory. This is fine for pipeline validation but doesn't test with realistic documents.

| Source | Config needed | Best for |
|--------|--------------|----------|
| Azure File Share | `E2E_FIXTURES_DIR` | Real contracts, easy to add/remove files |
| Azure Blob container | Connection string + container name | CI environments without PVC |
| In-memory | Nothing | Quick pipeline smoke tests |

---

## Tech Stack

| Category | Technology |
|----------|-----------|
| **Language** | Python 3.12+ |
| **Test framework** | pytest + pytest-asyncio |
| **HTTP client** | httpx (async) |
| **Configuration** | pydantic-settings (env vars + `.env` file) |
| **Reporting** | allure-pytest |
| **Package manager** | uv |
| **CI** | GitHub Actions |

---

## Project Structure

```
nebula-e2e-tests/
├── pyproject.toml                     # deps, pytest config (live log capture, asyncio mode)
├── conftest.py                        # shared fixtures: config, access_token, doc_client,
│                                      #   gateway_doc_client, event_listener, pipeline_report
├── cleanup.py                         # delete e2e-* tagged docs from prior runs
├── .env.example                       # env template
├── README.md
├── docs/
│   ├── EVENT_TYPES.md                 # CloudEvents v1.0 event spec for the pipeline
│   └── KUBECTL_GUIDE.md
│
├── .github/workflows/
│   ├── e2e-post-deploy.yml            # repository_dispatch [deploy-complete] + workflow_dispatch
│   └── e2e-nightly.yml                # cron 3 AM UTC + workflow_dispatch (test_path input)
│
├── k8s/
│   ├── e2e-job.yml                    # K8s Job: in-cluster auth + bulk-upload pipeline test
│   └── e2e-cleanup-job.yml            # cleanup script as separate Job
│
├── lib/
│   ├── config.py                      # E2EConfig — all settings via E2E_* env vars
│   ├── auth.py                        # Two-step auth: Analytics login → auth-service exchange
│   ├── event_hub.py                   # Multi-hub Event Hub listener (1 client per child hub)
│   ├── polling.py                     # poll_until() with exponential backoff
│   ├── fixtures.py                    # PDF source resolution (dir / blob / in-memory)
│   ├── report.py                      # PipelineReport — generates HTML pipeline report
│   ├── run_context.py                 # unique run-id tagging for traceability
│   └── api_clients/
│       ├── document_service.py        # mafia/legacy client (singular upload + presigned SAS)
│       └── gateway_document_service.py  # Nebula gateway client (bulk-upload + Bearer auth)
│
└── tests/
    ├── api/
    │   ├── test_smoke.py                       # connectivity, auth, basic CRUD
    │   ├── test_single_presigned_upload.py     # 1 file → presigned-upload pipeline
    │   ├── test_multi_presigned_upload.py      # N files concurrent → presigned pipeline
    │   └── test_bulk_upload.py                 # bulk-upload + 7-event Event Hub verification
    └── ui/                            # Playwright tests (future)
```

---

## Test Suite

### Smoke Tests (`test_smoke.py`)

Quick validation that connectivity, auth, and basic API operations work. **Run these first.**

| Test | What it verifies |
|------|-----------------|
| `test_mafia_is_reachable` | DNS + HTTPS connectivity to mafia.terzocloud.com |
| `test_access_token_available` | Access token was obtained (auto-fetched or manual) |
| `test_can_list_documents` | Authenticated API call succeeds (not 401/403) |
| `test_initiate_upload` | Presigned upload returns a valid SAS URL |
| `test_get_nonexistent_document_returns_404` | 404 response for unknown UFID |

### Single Upload Pipeline (`test_single_presigned_upload.py`)

Uploads one PDF and verifies the full pipeline progression.

| Step | API call | Assertion |
|------|----------|-----------|
| 1 | `POST /api/v1/documents/upload` | Returns `ufid` + SAS `uploadUrl` |
| 2 | `PUT {uploadUrl}` (Azure Blob) | 201 Created |
| 3 | `POST /api/v1/documents/{ufid}/confirm` | `status == CONFIRMED` |
| 4 | Poll `GET /api/v1/documents/{ufid}` | `processingState == OCR_QUEUED` (15s timeout) |
| 5 | Poll `GET /api/v1/documents/{ufid}` | `processingState == OCR_COMPLETED` (300s timeout) |
| 6 | `GET /api/v1/documents/{ufid}/artifacts` | `OCR_PDF` artifact exists |
| 7 | Poll `GET /api/v1/documents/{ufid}` | `processingState == EXTRACTION_QUEUED` (15s timeout) |

### Multi Upload Pipeline (`test_multi_presigned_upload.py`)

Parametrized test that runs with **3, 5, and 10 files** uploaded concurrently.

| Step | What happens |
|------|-------------|
| 1 | Upload N files in parallel (`asyncio.TaskGroup`) |
| 2 | Poll all N documents in parallel until `EXTRACTION_QUEUED` (600s timeout) |
| 3 | Verify all N documents have `OCR_PDF` artifacts |

### Bulk-Upload + Event-Hub Verification (`test_bulk_upload.py`)

The primary daily-run test. Submits one bulk-upload item, then watches Event Hub for the full pipeline progression.

| Step | Service | Event / API call | Per-stage timeout |
|------|---------|------------------|-------------------|
| 01 | **Document Service** | `POST /api/v1/documents/bulk-upload` → HTTP 202, ufid extracted | — |
| 02 | **Event Hub** | First event captured for the ufid (proves listener wiring) | 600s |
| 03 | **OCR Service** | `com.terzo.document.ocr.completed` | 600s |
| 04 | **Extraction Service** | `com.terzo.document.auto_extraction.completed` | 600s |
| 05 | **Ingestion Service** | `com.terzo.document.ingestion.completed` | 600s |

Each step becomes a row in `reports/pipeline-<run-id>.html` with PASS / FAIL / SKIPPED status.

**Per-stage timeout semantics.** Each stage has its own 10-minute wall-clock budget that resets on stage entry — a slow OCR stage does not eat into the Extraction or Ingestion budget. If a stage's expected event does not arrive within 600s, the test **force-kills**: that stage is recorded FAIL, all downstream stages are recorded SKIPPED, and `pytest.fail()` aborts immediately so the run can never hang past ~10 minutes on a single stage.

**Failure event.** If `com.terzo.document.failed` fires for the ufid at any point, remaining steps are recorded FAIL with the `failure_reason` payload field and the test fails.

**Event Hub not configured.** If `E2E_EVENT_HUB_CONNECTION_STRING` is unset, steps 02-05 are recorded SKIPPED and the test calls `pytest.skip()` so the daily run doesn't fail on missing infra.

---

## Run Tagging & Identification

Every test run gets a unique **run ID** like `e2e-20260414-030000-a1b2c3`. All documents created in that run are tagged with this ID in their filename:

```
e2e-20260414-030000-a1b2c3-0-contract.pdf     ← first file in run
e2e-20260414-030000-a1b2c3-1-contract.pdf     ← second file
e2e-20260414-030000-a1b2c3-2-invoice.pdf      ← third file (different source PDF)
```

This makes it easy to:
- **Trace** which nightly run created a specific document
- **Distinguish** concurrent runs (e.g., two developers testing simultaneously)
- **Clean up** all documents from a specific run
- **Debug** failures by matching the run ID in logs to documents in the database

The run ID is printed at the start of every test session.

---

## Cleanup

**Tests never delete documents.** This lets you inspect uploaded files, OCR results, and pipeline state in mafia after each run.

Cleanup is a **separate step** you run when you're done reviewing:

### Cleanup script

Deletes all documents with the `e2e-` filename prefix for tenant 1000012:

```bash
# From Dev cluster
uv run python cleanup.py
```

Output:
```
Cleaning up e2e test documents on mafia.terzocloud.com...
  Deleted: e2e-20260414-030000-a1b2c3-0-contract.pdf (ufid-1)
  Deleted: e2e-20260414-030000-a1b2c3-1-contract.pdf (ufid-2)
Done. Deleted 2 e2e documents.
```

### Cleanup as a separate K8s Job

Run after the test Job completes (or on a schedule):

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: nebula-e2e-cleanup
  namespace: nebula
spec:
  template:
    spec:
      containers:
      - name: cleanup
        image: ghcr.io/terzo-ai/nebula-e2e-tests:latest
        command: ["uv", "run", "python", "cleanup.py"]
      restartPolicy: Never
```

### Nightly cleanup

The cleanup script can also run as a scheduled CronJob to catch any orphans:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: nebula-e2e-cleanup-nightly
  namespace: nebula
spec:
  schedule: "0 5 * * *"   # 5 AM UTC, after nightly tests at 3 AM
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: cleanup
            image: ghcr.io/terzo-ai/nebula-e2e-tests:latest
            command: ["uv", "run", "python", "cleanup.py"]
          restartPolicy: Never
```

---

## Environment & Configuration

All configuration is via environment variables (prefix `E2E_`) or a `.env` file. Empty env vars are ignored — defaults apply.

### Core (gateway + tenant)

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_BASE_URL` | `https://mafia.terzocloud.com` | API base URL. In CI: `https://terzoai-gateway-dev.terzocloud.com` (gateway). The bulk-upload client appends its own `/nebula/document-service/api/v1` prefix. |
| `E2E_TENANT_ID` | `1000012` | Tenant for Dev environment |

### Bulk-upload payload

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_BULK_UPLOAD_SOURCE_URL` | `https://stterzoaidev.file.core.windows.net/fs-terzo-ai-dev` | Source URL the worker pod fetches the test PDF from |
| `E2E_BULK_UPLOAD_FILE_NAME` | `tz_nebula_e2e.pdf` | Filename used in the bulk-upload request |

### Auth — manual override (preferred when reachable)

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_TOKEN` | — | Bearer token for the Nebula gateway. When set, the two-step auth flow is skipped. |

### Auth — two-step flow (used when `E2E_TOKEN` is empty)

**Step 1 — Analytics login** (public, runs from anywhere)

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_ANALYTICS_BASE_URL` | `https://mafia.terzocloud.com` | Analytics login host |
| `E2E_ANALYTICS_EMAIL` | — | Login email |
| `E2E_ANALYTICS_PASSWORD` | — | Login password (secret) |
| `E2E_ANALYTICS_XSRF_TOKEN` | — | `X-XSRF-TOKEN` header value (secret) |
| `E2E_ANALYTICS_COOKIE` | — | `Cookie` header value (secret) |

**Step 2 — auth-service token exchange** (Dev cluster only)

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_AUTH_SERVICE_URL` | `https://auth-service-dev1.product-internal.terzocloud.com` | Internal auth-service base URL |
| `E2E_AUTH_USER_ID` | `1000129` | User ID claim |
| `E2E_AUTH_EMAIL` | `paventhan@terzocloud.com` | Email claim |

### Event Hub (optional — enables the 4-stage pipeline verification)

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_EVENT_HUB_CONNECTION_STRING` | — | Namespace-scoped SAS connection string (secret). When unset, the bulk-upload test skips event verification. |
| `E2E_EVENT_HUB_NAME` | `terzo-ai-contract-document-events` | Comma-separated list of child hub names. The listener fans out to each. Default matches `OCRM_EVENTHUB_NAME`. |
| `E2E_EVENT_HUB_CONSUMER_GROUP` | `terzo-ai-extraction-platform` | Consumer group. Default matches `OCRM_EVENTHUB_CONSUMER_GROUP`. |
| `E2E_EVENT_HUB_LISTEN_TIMEOUT` | `120.0` | Default per-event wait timeout (overridden to 600s per stage by the bulk-upload test). |

### Fixtures (legacy presigned-upload tests only)

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_FIXTURES_DIR` | — | Path to directory with test PDFs |
| `E2E_FIXTURES_CONNECTION_STRING` | — | Azure Blob connection string |
| `E2E_FIXTURES_CONTAINER` | `e2e-test-fixtures` | Azure Blob container name |
| `E2E_FULL_PIPELINE_TIMEOUT` | `600` | Max seconds per document for full pipeline |
| `E2E_POLL_INTERVAL` | `2.0` | Initial polling interval (seconds) |

---

## Running Tests

### From the Dev cluster (recommended)

Tests run **from inside the Dev cluster** where the auth service is reachable. The access token is auto-fetched — zero configuration needed.

**As a Kubernetes Job:**

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: nebula-e2e-tests
  namespace: nebula
spec:
  backoffLimit: 0
  template:
    spec:
      containers:
      - name: e2e
        image: ghcr.io/terzo-ai/nebula-e2e-tests:latest
        command: ["uv", "run", "pytest", "tests/api/", "-v", "--alluredir=/results"]
        env:
        - name: E2E_FIXTURES_DIR
          value: /mnt/e2e-fixtures
        volumeMounts:
        - name: test-fixtures
          mountPath: /mnt/e2e-fixtures
          readOnly: true
        - name: results
          mountPath: /results
      volumes:
      - name: test-fixtures
        azureFile:
          shareName: fs-terzo-ai-dev
          secretName: azure-storage-secret   # K8s secret with storage account key
          readOnly: true
      - name: results
        emptyDir: {}
      restartPolicy: Never
```

**Upload test PDFs to the file share** (one-time setup):

```bash
# Azure Portal: Storage accounts → stterzoaidev → File shares → fs-terzo-ai-dev → Upload
# Or Azure CLI:
az storage file upload-batch \
  --destination fs-terzo-ai-dev/e2e-fixtures \
  --source ./my-test-pdfs/ \
  --account-name stterzoaidev
```

**View results:**

```bash
# Follow logs in real-time
kubectl logs job/nebula-e2e-tests -n nebula -f

# After completion
kubectl logs job/nebula-e2e-tests -n nebula
```

**Via GitHub Actions** — results appear in the Actions tab as Allure report artifacts.

### Locally

Two paths depending on what the runner can reach:

| | **Dev cluster (K8s Job / self-hosted)** | **GitHub-hosted runner / laptop** |
|---|---|---|
| Target | `terzoai-gateway-dev.terzocloud.com` (gateway) | same |
| Auth | Two-step auto-fetch works | Set `E2E_TOKEN` to a manually-minted bearer |
| Event Hub | Reachable | Reachable (it's Azure-public) |
| Status | **Fully automatic** | **Works with `E2E_TOKEN` override** |

To run locally:

```bash
cp .env.example .env
# Edit .env — at minimum set E2E_TOKEN to a valid bearer
uv sync
uv run pytest tests/api/test_bulk_upload.py -v
```

---

## Authentication

The Nebula gateway requires an `Authorization: Bearer <token>` header on every request. The token is short-lived (~1h JWT). There are two ways to obtain it:

### Option 1 — Manual override (`E2E_TOKEN`)

Set the `E2E_TOKEN` env var (or repo secret) to a pre-minted bearer. `conftest.py:22-23` short-circuits the auto-fetch flow when this is set. Useful when the auth service isn't reachable from the test runner (e.g. GitHub-hosted runners).

### Option 2 — Two-step auto-fetch flow

When `E2E_TOKEN` is empty, the test mints a fresh bearer per session:

```
                ┌──────────── Step 1 (public) ────────────┐
                │  POST mafia.terzocloud.com/_/api/auth/login/password
                │       Headers: X-XSRF-TOKEN, Cookie, Origin, Referer
                │       Body:    { email, password, type: "b" }
                │       → returns ACCESS_TOKEN
                └──────────────────┬──────────────────────┘
                                   │
                ┌──────────── Step 2 (cluster only) ─────┐
                │  POST auth-service-dev1.product-internal/auth/token
                │       Headers: X-Access-Token: <ACCESS_TOKEN>
                │       Body:    { userId, email, tenantId,
                │                  grantType: "session_token" }
                │       → returns Nebula gateway bearer token
                └──────────────────┬──────────────────────┘
                                   │
                                   ▼
                  Authorization: Bearer <token>
                  X-Tenant-Id: 1000012
                  → POST /nebula/document-service/api/v1/documents/bulk-upload
```

Step 1 works from any network. Step 2's host is internal to the Dev cluster, so the auto-fetch flow is **only fully working from inside the Dev cluster** (K8s Job, self-hosted runner, etc.). From GitHub-hosted runners, set `E2E_TOKEN` instead.

The token extractor is forgiving — it accepts the token from the JSON body (`accessToken` / `access_token` / `token` / `access-token`, root or nested under `data` / `result`), a single-line plain-text body, or response headers (`Authorization`, `X-Access-Token`, `Access-Token`, `Token`, with optional `Bearer ` prefix stripping).

On any 4xx/5xx, the raised `AuthError` includes the response body (first 2KB), `WWW-Authenticate`, `Allow`, and `Location` headers — so failures are self-diagnostic.

---

## Polling Strategy

After upload confirmation, tests poll `GET /api/v1/documents/{ufid}` with exponential backoff.

```python
async def poll_until(check_fn, predicate, timeout, interval=2.0, backoff=1.5, max_interval=15.0)
```

| Stage Transition | What triggers it | Expected duration | Test timeout |
|-----------------|-----------------|-------------------|--------------|
| CONFIRMED → OCR_QUEUED | Outbox poller (500ms cycle) | < 2s | 15s |
| OCR_QUEUED → OCR_COMPLETED | Azure Document Intelligence via ocr-service | 10–120s | 300s |
| OCR_COMPLETED → EXTRACTION_QUEUED | document-service event consumer | < 2s | 15s |
| EXTRACTION_QUEUED → EXTRACTION_COMPLETED | Extraction Service worker | 10–60s | 180s |
| **Full pipeline** | All stages | 30–180s | **600s** |

Raises `PollTimeoutError` with the last observed `processingState` if timeout is exceeded.

---

## CI/CD Integration

### Workflows

| Workflow | Trigger | What runs |
|---|---|---|
| `e2e-nightly.yml` | `cron: "0 3 * * *"` (3 AM UTC) + `workflow_dispatch` | `pytest tests/` (or whatever `test_path` input specifies) |
| `e2e-post-deploy.yml` | `repository_dispatch [deploy-complete]` + `workflow_dispatch` | `pytest tests/api/` |

The nightly workflow accepts a `test_path` input on manual dispatch:

```bash
gh workflow run e2e-nightly.yml \
  --repo terzo-ai/nebula-e2e-tests --ref main \
  -f test_path=tests/api/test_bulk_upload.py
```

### Required GitHub config

Variables (Settings → Secrets and variables → Actions → Variables):

| Name | Example value |
|---|---|
| `E2E_BASE_URL` | `https://terzoai-gateway-dev.terzocloud.com` |
| `E2E_TENANT_ID` | `1000012` |
| `E2E_BULK_UPLOAD_SOURCE_URL` | `https://stterzoaidev.file.core.windows.net/.../tz_nebula_e2e.pdf` |
| `E2E_BULK_UPLOAD_FILE_NAME` | `tz_nebula_e2e.pdf` |
| `E2E_EVENT_HUB_NAME` | comma-separated list of 5 child hubs |
| `E2E_ANALYTICS_EMAIL` | login email |

Secrets:

| Name | Notes |
|---|---|
| `E2E_TOKEN` | Manually-minted bearer (workaround for GitHub runners not reaching internal auth-service) |
| `E2E_ANALYTICS_PASSWORD` | Login password for two-step flow |
| `E2E_ANALYTICS_XSRF_TOKEN` | `X-XSRF-TOKEN` header value |
| `E2E_ANALYTICS_COOKIE` | Cookie header value |
| `E2E_EVENT_HUB_CONNECTION_STRING` | Namespace-scoped SAS string |

### K8s Job (in-cluster execution)

`k8s/e2e-job.yml` runs the bulk-upload test from inside the Dev cluster, where the auth-service is reachable:

```bash
# Optional: enables event-hub verification
kubectl create secret generic nebula-e2e-secrets -n terzo-squad \
  --from-literal=event-hub-connection-string='Endpoint=sb://...'

kubectl delete job nebula-e2e-tests -n terzo-squad --ignore-not-found
kubectl apply -f k8s/e2e-job.yml
kubectl logs -n terzo-squad -l app=nebula-e2e-tests -c fetch-token -f
kubectl logs -n terzo-squad -l app=nebula-e2e-tests -c e2e -f
```

The init container POSTs to `auth-service-dev1` directly (with the static dev key) to mint a fresh bearer, writes it to a shared volume, and the main container picks it up as `E2E_TOKEN`. No GitHub secrets needed for token refresh in this path.

### Artifacts

Both workflows upload two artifacts on every run (success or failure):

| Artifact | Path | Description |
|----------|------|-------------|
| `pipeline-report-nightly` / `pipeline-report` | `reports/` | Self-contained HTML report with one row per pipeline step (PASS / FAIL / SKIPPED), grouped by service. |
| `allure-results-nightly` / `allure-results` | `allure-results/` | Raw allure JSON for `allure generate ...` if you want a full Allure HTML site. |

### Pipeline HTML Report

`lib/report.py` generates a self-contained `pipeline-<run-id>.html` at session teardown. For the bulk-upload test, the report lays out:

- **Header**: run ID, environment (`base_url`), tenant, start time, overall PASS/FAIL
- **Per-document table**: one row per recorded step with service / description / status / details
- **Step details**: the timestamp, partition (`{hub-name}/{partition-id}`), and sequence number for each captured event
- **Errors banner**: any session-level failures (auth, fixture setup, etc.)

Open the artifact directly in a browser — no server, no allure CLI needed.

### Event Hub multi-hub listening

`lib/event_hub.py:EventHubListener` accepts a single hub name **or** a comma-separated list. Internally it spawns one `EventHubConsumerClient` per hub and merges captured events into a single `_events` list, so callers see one timeline. Partition IDs are namespaced as `{hub-name}/{partition-id}` (e.g. `terzo-ai-ocr-events/4`) so we can tell which child hub each event came from. Set `starting_position="@latest"`; the session-scoped `event_listener` fixture starts before any test body runs to avoid missing the first event.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `ConnectError: nodename not known` | Auth service DNS not resolvable | Run from inside the Dev cluster, not locally |
| `401 Unauthorized` | Access token missing, expired, or invalid | In-cluster: auto-fetch handles this. Check auth service is up |
| `403 Forbidden` | Token lacks permissions for tenant | Verify `E2E_AUTH_USER_ID` has access to tenant `1000012` |
| `PollTimeoutError: OCR_COMPLETED` | OCR service slow or down | Check ocr-service pods. Azure DI may be throttling |
| `PollTimeoutError: EXTRACTION_QUEUED` | document-service didn't route event | Check Event Hub consumer lag, outbox_events table |
| `No OCR_PDF artifact` | OCR completed but artifact not recorded | Check document_artifact table for the ufid |
| `ConnectError on SAS URL PUT` | Azure Blob unreachable | Check network/firewall from test runner to Azure |

---

## Future Work

### Infrastructure (the biggest blocker)

- **Auto token refresh from GitHub-hosted runners** — the auth-service is internal-only, so GitHub runs need `E2E_TOKEN` refreshed manually every ~1h. Pick one:
  - **Self-hosted runner inside the Dev cluster** — auto-fetch flow just works
  - **Daily `nebula-e2e-tests` K8s CronJob** — already mostly built (`k8s/e2e-job.yml`); convert to `apiVersion: batch/v1, kind: CronJob` with a daily schedule
  - **Scheduled token-refresh Action** that runs hourly, mints from inside the cluster, and pushes back into the GitHub secret via REST

### Test coverage

- **Tighten bulk-upload response assertions** — currently we only check ufid extraction; lock down the full `{totalItems, accepted, rejected, results[]}` shape
- **Drive API upload tests** — `PUT /api/internal/drive/upload` (needs CLM password)
- **Playwright UI tests** — automate Drive file upload (needs `data-testid` from frontend)
- **Negative tests** — corrupt files, oversized files, missing tenant, duplicate uploads, expired sourceUrl
- **Performance baselines** — track pipeline duration per stage over time using event timestamps captured by `EventHubListener`
- **Multiple-item bulk uploads** — current test sends one item; verify N items in a single request all reach completion

### Reporting

- **Pipeline event timeline visualization** — currently HTML report shows per-step status; add a horizontal timeline view from the captured event timestamps
- **Slack / Teams notification on failure** — wire a webhook step to the workflows
- **Trend dashboard** — historical pass-rate per service, mean pipeline duration

### Done in this iteration

- ✅ Bulk-upload via Nebula gateway (`POST /nebula/document-service/api/v1/documents/bulk-upload`)
- ✅ Two-step Analytics → auth-service token flow with manual `E2E_TOKEN` override
- ✅ Multi-hub Event Hub listener (5 child hubs in parallel)
- ✅ Per-event PASS/FAIL pipeline HTML report attributed to 4 services
- ✅ K8s Job manifest aligned with current code
- ✅ Self-diagnostic 4xx/5xx errors in auth and gateway clients
