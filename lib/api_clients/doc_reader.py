"""Client for the Nebula doc-reader service.

Used by tests whose upload endpoint does NOT return a ufid in the
response (notably the UI contract-drive flow). The pattern is:

    1. Upload a document — server accepts but returns no ufid.
    2. Poll doc-reader's paginated document list filtered on the
       filename we uploaded.
    3. Once the document appears, take its ufid and hand it to the
       Event Hub pipeline walker for downstream verification.

Reachable at ``{base_url}/nebula/doc-reader/api/v1/documents`` through
the terzoai-gateway. Auth is the same Bearer + ``X-Tenant-Id`` header
as file-ingestion, so the client shares ``E2E_TOKEN`` and
``E2E_TENANT_ID`` rather than adding new env vars.

The server uses Spring-style pagination: ``?page=N&size=M`` → a body
containing ``content[]`` plus ``last``/``totalPages`` flags we walk
until we find the filename or exhaust the pages. Filtering is done
client-side to match the existing ad-hoc curl-based workflow:

    curl -H "X-Tenant-Id: 1000012" \\
      '<base>/nebula/doc-reader/api/v1/documents?size=100' \\
      | jq '.content[] | select(.name == "Contract.pdf") | .ufid'
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class DocReaderClient:
    """Async HTTP client for the Nebula doc-reader service."""

    SERVICE_PREFIX = "/nebula/doc-reader/api/v1"

    # Safety cap on page walks — doc-reader in dev currently has tens
    # of thousands of rows for tenant 1000012, so we want to bail out
    # long before reading the full list when the filename we're after
    # clearly isn't there.
    DEFAULT_PAGE_SIZE = 100
    DEFAULT_MAX_PAGES = 10

    def __init__(self, base_url: str, tenant_id: int, access_token: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        headers = {"X-Tenant-Id": str(self._tenant_id)}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=60.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "DocReaderClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def list_documents(
        self,
        *,
        page: int = 0,
        size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """GET one page of documents.

        Returns the raw decoded JSON body so callers can read both
        ``content[]`` and the pagination envelope (``last``,
        ``totalPages``, etc.) — a specialised wrapper would lose
        information the tests sometimes want to log.
        """
        resp = await self._client.get(
            f"{self.SERVICE_PREFIX}/documents",
            params={"page": page, "size": size},
        )
        self._raise_for_status(resp, f"list documents (page={page}, size={size})")
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError(
                f"doc-reader list response was not a JSON object: {type(body).__name__}"
            )
        return body

    async def find_ufid_by_name(
        self,
        name: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> str | None:
        """Paginate the document list, return the ufid of the first
        match on ``name`` or ``None`` if not found within ``max_pages``.

        The server sorts newest-first by default (verified against dev),
        so a freshly uploaded file typically appears on page 0. The
        ``max_pages`` cap protects against unbounded walks when the
        filename never shows up (wrong tenant, failed upload, etc.).
        """
        scanned = 0
        for page in range(max_pages):
            body = await self.list_documents(page=page, size=page_size)
            content = body.get("content") or []
            scanned += len(content)
            for doc in content:
                if isinstance(doc, dict) and doc.get("name") == name:
                    ufid = doc.get("ufid") or doc.get("documentId") or doc.get("document_id")
                    if isinstance(ufid, str) and ufid:
                        logger.info(
                            "doc-reader resolved ufid=%s for name=%s on page=%d (scanned=%d)",
                            ufid, name, page, scanned,
                        )
                        return ufid
            if body.get("last") is True or not content:
                break
        logger.warning(
            "doc-reader could not resolve ufid for name=%s after scanning %d rows "
            "across up to %d pages",
            name, scanned, max_pages,
        )
        return None

    async def wait_for_ufid_by_name(
        self,
        name: str,
        *,
        timeout_s: float = 30.0,
        poll_interval_s: float = 2.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> str | None:
        """Retry :meth:`find_ufid_by_name` until ``timeout_s`` elapses.

        The doc-reader's view of a freshly uploaded document is
        eventually consistent — the file-ingestion service writes
        first, then doc-reader catches up a beat later. Tests that
        look up a ufid immediately after an upload need a short retry
        loop; the poll interval keeps HTTP noise low while we wait.
        """
        deadline = time.monotonic() + timeout_s
        attempts = 0
        while True:
            attempts += 1
            ufid = await self.find_ufid_by_name(
                name, page_size=page_size, max_pages=max_pages,
            )
            if ufid:
                logger.info(
                    "doc-reader resolved ufid=%s for name=%s after %d attempt(s)",
                    ufid, name, attempts,
                )
                return ufid
            if time.monotonic() >= deadline:
                logger.warning(
                    "doc-reader did not yield a ufid for name=%s within %.1fs "
                    "(attempts=%d)",
                    name, timeout_s, attempts,
                )
                return None
            await asyncio.sleep(poll_interval_s)

    def _raise_for_status(self, resp: httpx.Response, context: str) -> None:
        """Consistent self-diagnostic error for any non-2xx response.

        Mirrors the pattern used by :class:`GatewayFileIngestionClient`
        so failures across both clients read the same in CI logs.
        """
        if resp.status_code < 400:
            return
        debug_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() in {
                "www-authenticate", "x-error", "x-request-id",
                "x-amzn-requestid", "x-trace-id", "content-type",
            }
        }
        body_snippet = resp.text[:2000] if resp.text else "(empty body)"
        auth_sent = self._client.headers.get("Authorization", "(none)")
        auth_preview = (
            auth_sent[:12] + "..." + auth_sent[-6:]
            if len(auth_sent) > 24 else "(short/none)"
        )
        raise RuntimeError(
            f"doc-reader {context} failed: {resp.status_code} {resp.reason_phrase}\n"
            f"  url: {resp.request.url}\n"
            f"  sent Authorization header: {auth_preview}\n"
            f"  response headers: {debug_headers}\n"
            f"  response body: {body_snippet}"
        )
