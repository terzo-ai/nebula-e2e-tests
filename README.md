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
8. [Test Suite](#test-suite)
9. [Run Tagging & Identification](#run-tagging--identification)
10. [Cleanup](#cleanup)
11. [Environment & Configuration](#environment--configuration)
12. [Running Tests](#running-tests)
13. [Authentication](#authentication)
14. [Polling Strategy](#polling-strategy)
15. [CI/CD Integration](#cicd-integration)
16. [Troubleshooting](#troubleshooting)
17. [Future Work](#future-work)

---

## Overview

The Nebula document processing pipeline spans three independent microservices:

| Service | Stack | Role |
|---------|-------|------|
| **document-service** | Java 25 / Spring Boot 4.0.3 | Upload orchestration, metadata, event routing, artifact registry |
| **ocr-service** | Python / Ray | OCR via Azure Document Intelligence |
| **ocrmodule-service** | Python / FastAPI + Celery | Data extraction, ingestion, Drive integration |

These services communicate asynchronously via **Azure Event Hub** using **CloudEvents v1.0**. There are no synchronous dependencies between them — the only way to verify the pipeline works end-to-end is to upload a document and watch it flow through every stage.

That's what this repo does.

---

## What This Repo Tests

- **Presigned SAS upload** — the same 3-step flow the frontend uses (initiate → upload to Azure → confirm)
- **Pipeline progression** — document moves through `CONFIRMED → OCR_QUEUED → OCR_COMPLETED → EXTRACTION_QUEUED`
- **Artifact creation** — OCR produces artifacts recorded in the artifact registry
- **Multi-file concurrency** — 3, 5, and 10 files uploaded in parallel, all reaching `EXTRACTION_QUEUED`
- **Basic CRUD** — list documents, initiate upload, 404 on nonexistent documents

---

## Architecture

```
  nebula-e2e-tests (this repo)
         │
         │  1. POST /api/v1/documents/upload          → get SAS URL + ufid
         │  2. PUT {SAS URL}                          → upload PDF to Azure Blob
         │  3. POST /api/v1/documents/{ufid}/confirm  → trigger pipeline
         │  4. Poll GET /api/v1/documents/{ufid}      → watch processingState
         │  5. GET /api/v1/documents/{ufid}/artifacts  → verify OCR artifacts
         │
         ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │                  mafia.terzocloud.com (Dev)                      │
  │                                                                  │
  │   API Gateway (access-token auth)                                │
  │        │                                                         │
  │        ▼                                                         │
  │   document-service ──► Azure Event Hub ──► ocr-service           │
  │        │                                        │                │
  │        │              ◄── ocr.completed ◄───────┘                │
  │        │                                                         │
  │        ├──► extraction.queued ──► ocrmodule-service              │
  │        │                                │                        │
  │        │    ◄── extraction.completed ◄──┘                        │
  │        │                                                         │
  │        └──► PROCESSED ✅                                         │
  └──────────────────────────────────────────────────────────────────┘
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
- Triggers real OCR processing via Azure Document Intelligence
- Triggers real extraction via ocrmodule-service
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
├── pyproject.toml                     # dependencies, pytest config
├── conftest.py                        # shared fixtures: doc_client, config, sample_pdf, access_token
├── .env.example                       # env template with defaults
├── .gitignore
├── README.md
│
├── .github/workflows/
│   ├── e2e-post-deploy.yml            # triggered after Dev deploy
│   └── e2e-nightly.yml               # nightly regression at 3 AM UTC
│
├── fixtures/                          # test PDF files (optional — in-memory fallback exists)
│   └── sample-contract-1page.pdf
│
├── lib/
│   ├── config.py                      # E2EConfig — all settings via env vars
│   ├── auth.py                        # Auto-fetch access token from auth service
│   ├── polling.py                     # poll_until() with exponential backoff
│   └── api_clients/
│       └── document_service.py        # typed async client for document-service API
│
└── tests/
    ├── api/
    │   ├── test_smoke.py              # connectivity, auth, basic CRUD
    │   ├── test_single_presigned_upload.py   # 1 file → full pipeline
    │   └── test_multi_presigned_upload.py    # N files → all reach EXTRACTION_QUEUED
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

All configuration is via environment variables (prefix `E2E_`) or a `.env` file.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `E2E_BASE_URL` | No | `https://mafia.terzocloud.com` | Target API base URL |
| `E2E_TENANT_ID` | No | `1000012` | Tenant (buyer_id) for the mafia Dev environment |
| `E2E_ACCESS_TOKEN` | No | — | Gateway auth token. Auto-fetched if not set |
| `E2E_AUTH_SERVICE_URL` | No | `https://auth-service-dev...` | Auth service URL (cluster-only) |
| `E2E_AUTH_SERVICE_KEY` | No | `9a264959-...` | Service key for auth token requests |
| `E2E_AUTH_USER_ID` | No | `1001359` | User ID for token generation |
| `E2E_AUTH_EMAIL` | No | `shankar@terzocloud.com` | Email for token generation |
| `E2E_FIXTURES_DIR` | No | — | Path to directory with test PDFs (e.g., mounted Azure File Share) |
| `E2E_FIXTURES_CONNECTION_STRING` | No | — | Azure Storage connection string for Blob-based fixtures |
| `E2E_FIXTURES_CONTAINER` | No | `e2e-test-fixtures` | Azure Blob container name for fixtures |
| `E2E_FULL_PIPELINE_TIMEOUT` | No | `600` | Max seconds per document for full pipeline |
| `E2E_POLL_INTERVAL` | No | `2.0` | Initial polling interval (seconds) |

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

### Locally (limited)

Local execution requires a valid `access-token` that can only be obtained from the auth service inside the Dev cluster. **Local is currently blocked.**

| | **Dev cluster** | **Local** |
|---|---|---|
| **Target** | mafia.terzocloud.com | mafia.terzocloud.com |
| **Auth** | Auto-fetched from auth service | Cannot obtain token |
| **Status** | **Works** | **Blocked** |

---

## Authentication

The API gateway on `mafia.terzocloud.com` requires an `access-token` header on every request.

### Token flow

```
conftest.py (access_token fixture)
     │
     ├── E2E_ACCESS_TOKEN env var set? ──► use it directly
     │
     └── Not set? ──► POST auth-service-dev.product-internal.terzocloud.com/auth/token
                           │
                           │  Headers:
                           │    Content-Type: application/json
                           │    X-Access-Token: {service_key}
                           │
                           │  Body:
                           │    {userId, email, tenantId, grantType: "session_token"}
                           │
                           └──► returns access token string
```

The auth service is **only reachable from within the Dev cluster**. When running as a K8s Job or GitHub Actions runner inside the cluster, tokens are auto-fetched with zero configuration.

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
| EXTRACTION_QUEUED → EXTRACTION_COMPLETED | ocrmodule-service Celery worker | 10–60s | 180s |
| **Full pipeline** | All stages | 30–180s | **600s** |

Raises `PollTimeoutError` with the last observed `processingState` if timeout is exceeded.

---

## CI/CD Integration

### Post-deploy trigger

After any Nebula service deploys to Dev, trigger E2E via `repository_dispatch`:

```yaml
name: E2E Tests (Dev - mafia.terzocloud.com)
on:
  repository_dispatch:
    types: [deploy-complete]
  workflow_dispatch:
```

### Nightly regression

Full suite runs at 3 AM UTC daily against mafia.terzocloud.com.

### Viewing results

| Where | What you see |
|-------|-------------|
| **GitHub Actions tab** | Pass/fail per test, full pytest output |
| **Allure artifacts** | Uploaded as `allure-results` artifact on every run |
| **K8s pod logs** | `kubectl logs job/nebula-e2e-tests -n nebula` |

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

- **Bulk upload tests** — `POST /api/v1/documents/bulk-upload` with source URLs (API not ready)
- **Drive API upload tests** — `PUT /api/internal/drive/upload` (needs CLM password)
- **Playwright UI tests** — automate Drive file upload on mafia.terzocloud.com (needs `data-testid` from frontend)
- **Full pipeline tests** — extend through `EXTRACTION_COMPLETED → INGESTION_QUEUED → PROCESSED`
- **Negative tests** — corrupt files, oversized files, missing tenant, duplicate uploads
- **Performance baselines** — track pipeline duration per stage over time
