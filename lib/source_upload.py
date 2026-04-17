"""Per-run upload of the generated contract PDF to Azure Blob, returning
a time-limited read-only SAS URL for use as ``bulk-upload.sourceUrl``.

Pipeline:
    1. Parse AccountName + AccountKey from the fixtures connection string
       (the same secret already used by ``lib/fixtures.py``).
    2. Upload the PDF bytes to
           {container}/nebulae2etest-<run_id>.pdf
       overwriting any prior artefact with the same name (there shouldn't
       be one — run_ids are unique per session).
    3. Mint a 2-hour read-only blob-scope SAS token and return the full
       https URL that bulk-upload's worker can fetch.
    4. On teardown the caller invokes ``delete`` so orphan blobs don't
       accumulate in the container.

Parses the account key out of the classic ADO-style connection string
(``DefaultEndpointsProtocol=...;AccountName=...;AccountKey=...;...``).
Passing ``AccountName`` + ``AccountKey`` directly to ``generate_blob_sas``
avoids needing a second round-trip for a user-delegation key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)

logger = logging.getLogger(__name__)


BLOB_NAME_PREFIX = "nebulae2etest"
SAS_TTL_HOURS = 2


@dataclass(frozen=True)
class SourceUpload:
    """Metadata for an uploaded source blob so the caller can both use
    the URL during the run and delete the blob at teardown."""

    sas_url: str
    blob_name: str
    container: str
    account_name: str


class SourceUploadError(RuntimeError):
    """Raised when the upload or SAS mint fails. Carries enough context
    (account/container/blob) to diagnose from CI logs."""


def _parse_conn_string(conn_str: str) -> dict[str, str]:
    """Parse the classic ``k=v;k=v`` Azure connection string.

    ``k=v`` parts with no ``=`` are ignored (defensive — Azure has never
    actually emitted such a thing, but a malformed secret shouldn't
    blow up the whole test suite with a ValueError).
    """
    out: dict[str, str] = {}
    for part in conn_str.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def upload_contract_pdf(
    content: bytes,
    run_id: str,
    connection_string: str,
    container: str,
) -> SourceUpload:
    """Upload PDF bytes and return a SAS URL for bulk-upload to fetch.

    Raises ``SourceUploadError`` with a diagnostic message on any failure.
    """
    conn_parts = _parse_conn_string(connection_string)
    account_name = conn_parts.get("AccountName")
    account_key = conn_parts.get("AccountKey")
    if not account_name or not account_key:
        raise SourceUploadError(
            "connection string missing AccountName / AccountKey — cannot "
            "mint SAS. Fix E2E_FIXTURES_CONNECTION_STRING."
        )

    blob_name = f"{BLOB_NAME_PREFIX}-{run_id}.pdf"

    try:
        service = BlobServiceClient.from_connection_string(connection_string)
        blob_client = service.get_blob_client(container=container, blob=blob_name)
        blob_client.upload_blob(
            content,
            overwrite=True,
            content_settings=ContentSettings(content_type="application/pdf"),
        )
    except Exception as e:
        raise SourceUploadError(
            f"upload failed to {container}/{blob_name}: {type(e).__name__}: {e}"
        ) from e

    try:
        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=SAS_TTL_HOURS),
        )
    except Exception as e:
        raise SourceUploadError(
            f"SAS mint failed for {container}/{blob_name}: {type(e).__name__}: {e}"
        ) from e

    sas_url = (
        f"https://{account_name}.blob.core.windows.net/"
        f"{container}/{blob_name}?{sas_token}"
    )
    logger.info(
        "uploaded contract PDF: container=%s blob=%s size=%d bytes, "
        "SAS TTL=%dh",
        container, blob_name, len(content), SAS_TTL_HOURS,
    )
    return SourceUpload(
        sas_url=sas_url,
        blob_name=blob_name,
        container=container,
        account_name=account_name,
    )


def delete_blob(
    upload: SourceUpload,
    connection_string: str,
) -> None:
    """Delete the uploaded blob. Logs (doesn't raise) on failure so a
    cleanup miss never fails a test that already completed."""
    try:
        service = BlobServiceClient.from_connection_string(connection_string)
        blob_client = service.get_blob_client(
            container=upload.container, blob=upload.blob_name
        )
        blob_client.delete_blob()
        logger.info(
            "deleted source blob: container=%s blob=%s",
            upload.container, upload.blob_name,
        )
    except Exception as e:
        logger.warning(
            "failed to delete source blob %s/%s — %s: %s",
            upload.container, upload.blob_name, type(e).__name__, e,
        )
