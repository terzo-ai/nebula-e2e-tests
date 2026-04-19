"""Unit tests for the Slack Block Kit builders.

These exercise the pure formatting functions — no network, no pytest
session fixtures. They protect the channel contract:

  * Main message stays bounded in block count as scenarios grow
  * Failing stages surface inline in the main message
  * The thread reply carries the full stage-by-stage breakdown and marks
    the first failing stage with ``← FAILED HERE``
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lib.report import PipelineReport, StepStatus
from lib.slack_report import (
    build_fallback_text,
    build_main_blocks,
    build_thread_blocks,
    build_thread_fallback_text,
)


pytestmark = pytest.mark.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# Helpers — small DSL to hand-craft reports without touching the live
# pipeline. Using the real `PipelineReport`/`record_step` API keeps the
# test aligned with the production data model.
# ---------------------------------------------------------------------------


_PIPELINE_STAGES = [
    ("Document Service", "bulk-upload accepted (HTTP 202)"),
    ("Event Hub", "first event received"),
    ("Document Service", "upload queued"),
    ("Document Service", "upload completed"),
    ("OCR Service", "OCR queued"),
    ("Extraction Service", "extraction queued"),
    ("Extraction Service", "extraction completed"),
]


def _make_report(
    *,
    run_id: str = "e2e-20260417-140838-723a6f",
    env: str = "https://terzoai-gateway-dev.terzocloud.com",
    gh_url: str = "https://github.com/example/repo/actions/runs/1",
    duration_s: int = 607,
) -> PipelineReport:
    started = datetime(2026, 4, 17, 14, 8, 38, tzinfo=timezone.utc)
    report = PipelineReport(
        run_id=run_id,
        environment=env,
        tenant_id=1000012,
        started_at=started.isoformat(),
        github_actions_url=gh_url,
    )
    # The render path reads `ended_at` from `finalize()` in production;
    # pin it manually here so duration is deterministic.
    report.ended_at = (started + timedelta(seconds=duration_s)).isoformat()
    return report


def _add_scenario(
    report: PipelineReport,
    *,
    test_case: str,
    ufid: str,
    fail_at_index: int | None = None,
    fail_reason: str = "",
    skipped_from_index: int | None = None,
    skip_reason: str = "",
) -> None:
    """Append a 7-stage scenario.

    * ``fail_at_index`` — stages up to that index PASS, the one at that
      index FAILs, and later stages are marked PENDING (the run stopped).
    * ``skipped_from_index`` — stages before that index PASS, the rest
      are marked SKIPPED with ``skip_reason``. Used to model the "Event
      Hub not configured" flow where the upload succeeded but downstream
      stages couldn't be verified.
    """
    report.add_document(ufid=ufid, filename=f"{ufid}.pdf", test_case=test_case)
    for idx, (service, description) in enumerate(_PIPELINE_STAGES):
        if skipped_from_index is not None and idx >= skipped_from_index:
            status = StepStatus.SKIPPED
            details = skip_reason or "downstream verification skipped"
        elif fail_at_index is None or idx < fail_at_index:
            status = StepStatus.PASS
            details = ""
        elif idx == fail_at_index:
            status = StepStatus.FAIL
            details = fail_reason or "stage did not complete"
        else:
            status = StepStatus.PENDING
            details = ""
        report.record_step(
            ufid=ufid,
            service=service,
            description=description,
            status=status,
            details=details,
        )
    # Drive the document-level rollup the way `finalize()` does in production
    # — but skip `report.finalize()` itself so the `ended_at` that
    # `_make_report` pinned for deterministic duration isn't clobbered.
    for doc in report.documents:
        doc.compute_status()


# ---------------------------------------------------------------------------
# Main message shape
# ---------------------------------------------------------------------------


def test_all_pass_main_message_hides_stage_detail() -> None:
    report = _make_report()
    _add_scenario(report, test_case="Bulk Upload", ufid="bu-1")
    _add_scenario(report, test_case="UI File Upload", ufid="ui-1")
    _add_scenario(report, test_case="Direct Upload", ufid="du-1")

    blocks = build_main_blocks(report)
    as_text = _collect_text(blocks)

    assert _header_text(blocks).startswith(":large_green_circle:")
    assert "PASS  |  3/3 passed" in _header_text(blocks)
    # No failing stage detail leaks into the main channel when everything
    # is green — that's the whole point of the thread split.
    assert "FAILED HERE" not in as_text
    assert ":x:" not in as_text
    # All three scenarios must be acknowledged in one line each.
    assert as_text.count(":white_check_mark: *") == 3
    # And the stage count ("7/7 stages") is the scannable signal.
    assert "(7/7 stages)" in as_text


def test_mid_pipeline_fail_surfaces_failing_stage_in_main() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="Bulk Upload",
        ufid="67e850e5-0443-4221-b86a-a9c79bebe16b",
        fail_at_index=6,
        fail_reason="extraction never completed",
    )
    _add_scenario(report, test_case="UI File Upload", ufid="ui-1")
    _add_scenario(report, test_case="Direct Upload", ufid="du-1")

    blocks = build_main_blocks(report)
    as_text = _collect_text(blocks)

    assert _header_text(blocks).startswith(":red_circle:")
    assert "FAIL  |  2/3 passed" in _header_text(blocks)
    assert ":x: *Bulk Upload* — failed at Extraction Service" in as_text
    assert "extraction never completed" in as_text
    # Full UFID stays in the main message for the failing scenario so
    # on-call can copy-paste without expanding the thread.
    assert "`67e850e5-0443-4221-b86a-a9c79bebe16b`" in as_text
    # Passing scenarios still collapse to one line each.
    assert ":white_check_mark: *UI File Upload*" in as_text
    assert ":white_check_mark: *Direct Upload*" in as_text


def test_multiple_failures_list_each_scenario_in_main() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="Bulk Upload",
        ufid="bu-1",
        fail_at_index=6,
        fail_reason="extraction never completed",
    )
    _add_scenario(
        report,
        test_case="UI File Upload",
        ufid="ui-1",
        fail_at_index=1,
        fail_reason="event hub listener timed out",
    )
    _add_scenario(report, test_case="Direct Upload", ufid="du-1")

    blocks = build_main_blocks(report)
    as_text = _collect_text(blocks)

    assert _header_text(blocks).startswith(":red_circle:")
    assert "FAIL  |  1/3 passed" in _header_text(blocks)
    assert "failed at Extraction Service" in as_text
    assert "failed at Event Hub" in as_text
    assert ":white_check_mark: *Direct Upload*" in as_text


def test_main_message_scales_linearly_with_scenario_count() -> None:
    """6 scenarios must not explode the main-channel block budget.

    Failing scenarios each get their own block; passing scenarios share
    one. So main-channel blocks attributable to scenarios are bounded by
    ``num_failing + 1``, which is ``<= num_scenarios``.
    """
    report = _make_report()
    for i in range(5):
        _add_scenario(report, test_case=f"Scenario {i}", ufid=f"pass-{i}")
    _add_scenario(
        report,
        test_case="Scenario 5",
        ufid="fail-5",
        fail_at_index=6,
        fail_reason="extraction never completed",
    )

    blocks = build_main_blocks(report)

    section_blocks = [b for b in blocks if b["type"] == "section"]
    # 1 failing scenario + 1 aggregated passing bucket = 2 sections.
    assert len(section_blocks) <= 6  # ≤ 1 per scenario
    # Fixed overhead stays small: header + context + sections + tail context.
    assert len(blocks) <= 6 + 3


# ---------------------------------------------------------------------------
# Thread reply shape
# ---------------------------------------------------------------------------


def test_thread_reply_summarises_passing_scenarios_without_stages() -> None:
    report = _make_report()
    _add_scenario(report, test_case="Bulk Upload", ufid="bu-1")
    _add_scenario(report, test_case="UI File Upload", ufid="ui-1")

    blocks = build_thread_blocks(report)
    as_text = _collect_text(blocks)

    assert ":clipboard: Stage details — all scenarios" in _header_text(blocks)
    assert "All 7 stages passed" in as_text
    # Passing scenarios must NOT carry individual stage lines in the thread
    # — that's the noise reduction we promised.
    assert "extraction queued" not in as_text


def test_thread_reply_marks_the_failing_stage() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="Bulk Upload",
        ufid="67e850e5-0443-4221-b86a-a9c79bebe16b",
        fail_at_index=6,
        fail_reason="extraction never completed",
    )
    _add_scenario(report, test_case="UI File Upload", ufid="ui-1")

    blocks = build_thread_blocks(report)
    failing_section = _find_section_containing(blocks, "*Bulk Upload*")
    passing_section = _find_section_containing(blocks, "*UI File Upload*")

    # Failing scenario expands all stages, with the failing one flagged.
    assert failing_section.count(":white_check_mark: Document Service") >= 1
    assert ":x: Extraction Service — extraction completed" in failing_section
    assert "← FAILED HERE" in failing_section
    # Only one stage gets the ← FAILED HERE marker even if later stages
    # also carry a non-pass status.
    assert failing_section.count("← FAILED HERE") == 1
    # Truncated UFID (8 chars + ellipsis) in the thread.
    assert "`67e850e5…`" in failing_section

    # Passing scenario stays collapsed.
    assert "All 7 stages passed" in passing_section


def test_multiple_failures_each_get_their_own_thread_section() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="Bulk Upload",
        ufid="bu-1",
        fail_at_index=6,
        fail_reason="extraction never completed",
    )
    _add_scenario(
        report,
        test_case="UI File Upload",
        ufid="ui-1",
        fail_at_index=1,
        fail_reason="event hub listener timed out",
    )

    blocks = build_thread_blocks(report)
    bulk = _find_section_containing(blocks, "*Bulk Upload*")
    ui = _find_section_containing(blocks, "*UI File Upload*")

    assert "← FAILED HERE" in bulk
    assert "← FAILED HERE" in ui
    assert "extraction never completed" in bulk
    assert "event hub listener timed out" in ui


# ---------------------------------------------------------------------------
# Unverified scenarios — observability gap (e.g. Event Hub unwired).
# ---------------------------------------------------------------------------


def test_skipped_stages_roll_doc_up_to_partial() -> None:
    """Sanity check: the HTML rollup treats SKIPPED as non-pass too, so
    every consumer of ``DocumentTrace.overall_status`` stays honest."""
    report = _make_report()
    _add_scenario(
        report,
        test_case="UI File Upload",
        ufid="ui-unverified",
        skipped_from_index=1,
        skip_reason="Event Hub not configured",
    )
    doc = report.documents[0]
    assert doc.overall_status == StepStatus.PARTIAL


def test_unverified_scenario_uses_yellow_header_and_counts() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="UI File Upload",
        ufid="ui-unverified",
        skipped_from_index=1,
        skip_reason="Event Hub not configured",
    )
    _add_scenario(report, test_case="Bulk Upload", ufid="bu-1")

    blocks = build_main_blocks(report)
    header = _header_text(blocks)
    assert header.startswith(":large_yellow_circle:")
    assert "UNVERIFIED" in header
    # 1 pass + 1 unverified → 1/2 passed, 1 unverified.
    assert "1/2 passed" in header
    assert "1 unverified" in header


def test_unverified_scenario_surfaces_skip_reason_in_main() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="UI File Upload",
        ufid="ui-upload-e2e-20260417-140838-723a6f",
        skipped_from_index=1,
        skip_reason="Event Hub not configured (E2E_EVENT_HUB_CONNECTION_STRING unset)",
    )

    blocks = build_main_blocks(report)
    text = _collect_text(blocks)
    # Scenario line uses the warning emoji and reports the stages-ran ratio
    # plus the skip reason so on-call knows why it's yellow.
    assert ":warning: *UI File Upload* — unverified" in text
    assert "1/7 stages ran" in text
    assert "Event Hub not configured" in text
    # Full UFID still stays in the main message so on-call can pull logs.
    assert "`ui-upload-e2e-20260417-140838-723a6f`" in text


def test_unverified_thread_marks_first_skipped_stage() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="UI File Upload",
        ufid="ui-unverified",
        skipped_from_index=1,
        skip_reason="Event Hub not configured",
    )

    blocks = build_thread_blocks(report)
    section = _find_section_containing(blocks, "*UI File Upload*")
    # Warning emoji in the scenario header (not the green check).
    assert section.startswith(":warning: *UI File Upload*")
    # Stage lines keep :fast_forward: for SKIPPED and flag the first one
    # so the eye lands on where verification stopped.
    assert ":fast_forward: Event Hub" in section
    assert section.count("\u2190 NOT VERIFIED") == 1
    # No FAILED HERE marker — this isn't a failure.
    assert "FAILED HERE" not in section


def test_fail_beats_unverified_in_overall_status() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="UI File Upload",
        ufid="ui-unverified",
        skipped_from_index=1,
        skip_reason="Event Hub not configured",
    )
    _add_scenario(
        report,
        test_case="Bulk Upload",
        ufid="bu-fail",
        fail_at_index=6,
        fail_reason="extraction never completed",
    )

    blocks = build_main_blocks(report)
    header = _header_text(blocks)
    # With any FAIL present the overall stays red, but the unverified
    # tally is still surfaced so the yellow count is not lost.
    assert header.startswith(":red_circle:")
    assert "FAIL" in header
    assert "1 unverified" in header


def test_unverified_fallback_text_uses_yellow_circle() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="UI File Upload",
        ufid="ui-unverified",
        skipped_from_index=1,
        skip_reason="Event Hub not configured",
    )
    _add_scenario(report, test_case="Bulk Upload", ufid="bu-1")

    text = build_fallback_text(report)
    assert text.startswith(":large_yellow_circle: E2E UNVERIFIED 1/2")
    assert "UI File Upload unverified at Event Hub" in text


# ---------------------------------------------------------------------------
# Fallback text (push-notification preview)
# ---------------------------------------------------------------------------


def test_fallback_text_all_pass() -> None:
    report = _make_report()
    _add_scenario(report, test_case="Bulk Upload", ufid="bu-1")
    _add_scenario(report, test_case="UI File Upload", ufid="ui-1")

    assert build_fallback_text(report) == ":large_green_circle: E2E PASS 2/2"


def test_fallback_text_surfaces_failing_stage_name() -> None:
    report = _make_report()
    _add_scenario(
        report,
        test_case="Bulk Upload",
        ufid="bu-1",
        fail_at_index=6,
        fail_reason="extraction never completed",
    )
    _add_scenario(report, test_case="UI File Upload", ufid="ui-1")
    _add_scenario(report, test_case="Direct Upload", ufid="du-1")

    text = build_fallback_text(report)
    assert text.startswith(":red_circle: E2E FAIL 2/3")
    assert "Bulk Upload failed at Extraction Service" in text


def test_thread_fallback_text_counts_scenarios() -> None:
    report = _make_report()
    _add_scenario(report, test_case="Bulk Upload", ufid="bu-1")
    _add_scenario(
        report,
        test_case="UI File Upload",
        ufid="ui-1",
        fail_at_index=1,
    )
    assert build_thread_fallback_text(report) == (
        "Stage details — 1/2 scenarios passed"
    )


# ---------------------------------------------------------------------------
# Header context — run id, env short name, duration, GH link
# ---------------------------------------------------------------------------


def test_main_context_carries_run_metadata() -> None:
    report = _make_report(
        env="https://terzoai-gateway-dev.terzocloud.com",
        duration_s=607,
    )
    _add_scenario(report, test_case="Bulk Upload", ufid="bu-1")

    blocks = build_main_blocks(report)
    context_text = " ".join(
        e["text"]
        for b in blocks
        if b["type"] == "context"
        for e in b["elements"]
    )
    assert "`e2e-20260417-140838-723a6f`" in context_text
    assert "*Env:* Dev" in context_text
    assert "*Duration:* 10m 7s" in context_text
    assert "GitHub Actions Run" in context_text


# ---------------------------------------------------------------------------
# Tiny test helpers
# ---------------------------------------------------------------------------


def _collect_text(blocks: list[dict]) -> str:
    parts: list[str] = []
    for b in blocks:
        if b["type"] == "header":
            parts.append(b["text"]["text"])
        elif b["type"] == "section":
            parts.append(b["text"]["text"])
        elif b["type"] == "context":
            parts.extend(e["text"] for e in b["elements"])
    return "\n".join(parts)


def _header_text(blocks: list[dict]) -> str:
    for b in blocks:
        if b["type"] == "header":
            return b["text"]["text"]
    raise AssertionError("expected a header block")


def _find_section_containing(blocks: list[dict], needle: str) -> str:
    for b in blocks:
        if b["type"] != "section":
            continue
        text = b["text"]["text"]
        if needle in text:
            return text
    raise AssertionError(f"no section contained {needle!r}")
