"""Run context: unique run ID, file naming, and tracking.

Every test run gets a unique ID like 'e2e-20260413-030000-a1b2c3'.
All documents uploaded in that run share this prefix, making it easy to:
- Identify which nightly run created a document
- Clean up all documents from a specific run
- Distinguish concurrent or sequential runs
"""

import time
from dataclasses import dataclass, field


@dataclass
class RunContext:
    """Tracks all documents created in a single test run."""

    run_id: str = field(default_factory=lambda: _generate_run_id())
    uploaded_ufids: list[str] = field(default_factory=list)

    def tag_filename(self, original_name: str, index: int = 0) -> str:
        """Prefix a filename with the run ID for traceability.

        Example: 'contract.pdf' → 'e2e-20260413-030000-a1b2c3-0-contract.pdf'
        """
        return f"{self.run_id}-{index}-{original_name}"

    def register(self, ufid: str) -> None:
        """Track a document created in this run."""
        self.uploaded_ufids.append(ufid)


def _generate_run_id() -> str:
    """Generate a unique run ID: e2e-YYYYMMDD-HHMMSS-{short_hash}."""
    import hashlib
    import os

    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    short_hash = hashlib.sha256(os.urandom(8)).hexdigest()[:6]
    return f"e2e-{timestamp}-{short_hash}"
