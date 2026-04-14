"""Test file source: loads PDFs from Azure Blob, local dir, or generates in-memory.

Priority:
1. Azure Blob container (E2E_FIXTURES_CONNECTION_STRING + E2E_FIXTURES_CONTAINER)
2. Local directory / mounted Azure File Share (E2E_FIXTURES_DIR)
3. In-memory fallback (minimal valid PDF)
"""

import pathlib
from dataclasses import dataclass

from lib.config import E2EConfig


@dataclass
class FixtureFile:
    name: str
    content: bytes
    content_type: str = "application/pdf"


def load_test_files(config: E2EConfig) -> list[FixtureFile]:
    """Load test files from configured source. Returns at least 1 file."""
    # Option 1: Azure Blob container
    if config.fixtures_connection_string:
        return _load_from_azure_blob(config)

    # Option 2: Local directory or mounted file share
    if config.fixtures_dir:
        return _load_from_directory(config.fixtures_dir)

    # Option 3: Check default fixtures/ dir
    default_dir = pathlib.Path(__file__).parent.parent / "fixtures"
    if default_dir.exists() and any(default_dir.glob("*.pdf")):
        return _load_from_directory(str(default_dir))

    # Fallback: in-memory minimal PDF
    return [FixtureFile(name="generated-test-contract.pdf", content=_minimal_pdf())]


def _load_from_azure_blob(config: E2EConfig) -> list[FixtureFile]:
    """Download all PDFs from an Azure Blob container."""
    try:
        from azure.storage.blob import ContainerClient
    except ImportError:
        raise RuntimeError(
            "azure-storage-blob is required for Azure Blob fixtures. "
            "Install with: uv add azure-storage-blob"
        )

    client = ContainerClient.from_connection_string(
        config.fixtures_connection_string,
        container_name=config.fixtures_container,
    )
    files = []
    for blob in client.list_blobs():
        if blob.name.lower().endswith(".pdf"):
            data = client.download_blob(blob.name).readall()
            files.append(FixtureFile(name=blob.name, content=data))

    if not files:
        raise RuntimeError(
            f"No PDF files found in Azure container '{config.fixtures_container}'"
        )
    return files


def _load_from_directory(dir_path: str) -> list[FixtureFile]:
    """Load all PDFs from a local directory or mounted file share."""
    path = pathlib.Path(dir_path)
    if not path.exists():
        raise RuntimeError(f"Fixtures directory not found: {dir_path}")

    files = []
    for pdf in sorted(path.glob("*.pdf")):
        files.append(FixtureFile(name=pdf.name, content=pdf.read_bytes()))

    if not files:
        raise RuntimeError(f"No PDF files found in {dir_path}")
    return files


def _minimal_pdf() -> bytes:
    """Generate a minimal valid PDF with text content for OCR."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        b"4 0 obj\n<< /Length 44 >>\nstream\n"
        b"BT /F1 12 Tf 100 700 Td (Test Contract) Tj ET\n"
        b"endstream\nendobj\n"
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000282 00000 n \n"
        b"0000000380 00000 n \n"
        b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
        b"startxref\n456\n%%EOF"
    )
