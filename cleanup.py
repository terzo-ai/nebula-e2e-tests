"""Cleanup: delete all e2e test documents from the Dev environment.

Can be run:
- Automatically after the test suite (via conftest autouse fixture)
- As a standalone script: uv run python cleanup.py
- As a separate K8s Job after the test Job completes

Finds documents by listing tenant 1000012 and filtering by 'e2e-' filename prefix.
"""

import asyncio
import sys

from lib.api_clients.document_service import DocumentServiceClient
from lib.auth import fetch_access_token
from lib.config import E2EConfig


async def cleanup_e2e_documents(max_pages: int = 10) -> int:
    """Delete all documents with 'e2e-' prefix for the configured tenant.

    Returns the number of documents deleted.
    """
    config = E2EConfig()

    # Get bearer token: manual override or two-step auth flow
    if config.token:
        token = config.token
    else:
        token = await fetch_access_token(config)

    client = DocumentServiceClient(
        base_url=config.base_url,
        tenant_id=config.tenant_id,
        access_token=token,
    )

    deleted = 0
    try:
        for page in range(max_pages):
            data = await client.list_documents(size=100)
            content = data.get("content", [])
            if not content:
                break

            e2e_docs = [
                doc for doc in content
                if doc.get("name", "").startswith("e2e-")
            ]

            for doc in e2e_docs:
                ufid = doc["ufid"]
                try:
                    await client.delete_document(ufid)
                    deleted += 1
                    print(f"  Deleted: {doc['name']} ({ufid})")
                except Exception as e:
                    print(f"  Failed to delete {ufid}: {e}")

            # If this page had no e2e docs, stop paginating
            if not e2e_docs:
                break
    finally:
        await client.close()

    return deleted


async def main() -> None:
    print("Cleaning up e2e test documents on mafia.terzocloud.com...")
    deleted = await cleanup_e2e_documents()
    print(f"\nDone. Deleted {deleted} e2e documents.")


if __name__ == "__main__":
    asyncio.run(main())
