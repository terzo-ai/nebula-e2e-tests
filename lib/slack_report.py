"""Slack Block Kit builders for the E2E pipeline report.

Two messages are produced per run:

  * **Main channel message** — compact summary. Passing scenarios collapse
    to one line with a stage count. Failing scenarios surface the failing
    stage inline so on-call can triage from the push-notification preview.

  * **Thread reply** — full stage-by-stage breakdown per scenario. The
    failing stage is marked with ``← FAILED HERE``.

Functions are pure (no I/O, no Slack client calls). ``conftest.py`` writes
the returned block list to JSON so the ``slackapi/slack-github-action``
step can POST it verbatim.

A *scenario* is a group of documents sharing the same ``test_case`` label
(e.g. "Bulk Upload", "UI File Upload endpoint"). The main message adds
exactly one line per scenario, so the channel stays scannable as new
upload scenarios get added.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from lib.report import DocumentTrace, PipelineReport, PipelineStep, StepStatus

if TYPE_CHECKING:
    Block = dict[str, object]


_MAIN_FAIL_REASON_LIMIT = 140
_MAIN_UNVERIFIED_REASON_LIMIT = 140
_THREAD_STAGE_NAME_LIMIT = 120
_MAIN_SECTION_TEXT_LIMIT = 2800  # Slack allows 3000; keep headroom.
_THREAD_SECTION_TEXT_LIMIT = 2800
_UFID_SHORT_LEN = 8

# Scenario-status categories. We distinguish three buckets so on-call
# can tell "it worked" from "we couldn't verify it" from "it broke":
#
#   * pass        — every stage produced a PASS.
#   * unverified  — no FAILs, but at least one SKIPPED/PENDING/PARTIAL.
#                   Typical cause: observability (Event Hub) not wired,
#                   so we saw the upload succeed but can't prove the
#                   pipeline ran end-to-end.
#   * fail        — at least one stage FAILed.
_SCENARIO_PASS = "pass"
_SCENARIO_UNVERIFIED = "unverified"
_SCENARIO_FAIL = "fail"


# ---------------------------------------------------------------------------
# Grouping + status helpers (pure)
# ---------------------------------------------------------------------------


def _scenario_groups(
    report: PipelineReport,
) -> list[tuple[str, list[DocumentTrace]]]:
    order: list[str] = []
    buckets: dict[str, list[DocumentTrace]] = {}
    for doc in report.documents:
        key = doc.test_case or "Uncategorized"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(doc)
    return [(name, buckets[name]) for name in order]


def _scenario_stages(docs: list[DocumentTrace]) -> list[PipelineStep]:
    stages: list[PipelineStep] = []
    for doc in docs:
        stages.extend(doc.steps)
    return stages


def _scenario_status(docs: list[DocumentTrace]) -> str:
    """Three-way bucketing: ``pass`` / ``unverified`` / ``fail``."""
    stages = _scenario_stages(docs)
    if not stages:
        return _SCENARIO_UNVERIFIED
    statuses = {s.status for s in stages}
    if StepStatus.FAIL in statuses:
        return _SCENARIO_FAIL
    if statuses - {StepStatus.PASS}:
        return _SCENARIO_UNVERIFIED
    return _SCENARIO_PASS


def _first_failing_stage(stages: list[PipelineStep]) -> PipelineStep | None:
    for stage in stages:
        if stage.status == StepStatus.FAIL:
            return stage
    return None


def _first_unverified_stage(stages: list[PipelineStep]) -> PipelineStep | None:
    for stage in stages:
        if stage.status in (
            StepStatus.SKIPPED,
            StepStatus.PENDING,
            StepStatus.PARTIAL,
        ):
            return stage
    return None


@dataclass(frozen=True)
class _Counts:
    passed: int
    unverified: int
    failed: int
    total: int


def _counts(report: PipelineReport) -> _Counts:
    passed = unverified = failed = 0
    for _, docs in _scenario_groups(report):
        status = _scenario_status(docs)
        if status == _SCENARIO_PASS:
            passed += 1
        elif status == _SCENARIO_UNVERIFIED:
            unverified += 1
        else:
            failed += 1
    return _Counts(
        passed=passed,
        unverified=unverified,
        failed=failed,
        total=passed + unverified + failed,
    )


def _overall_status(report: PipelineReport) -> str:
    """Report-level status — same three-way bucketing as scenarios."""
    if report.errors:
        return _SCENARIO_FAIL
    groups = _scenario_groups(report)
    if not groups:
        return _SCENARIO_FAIL
    statuses = {_scenario_status(docs) for _, docs in groups}
    if _SCENARIO_FAIL in statuses:
        return _SCENARIO_FAIL
    if _SCENARIO_UNVERIFIED in statuses:
        return _SCENARIO_UNVERIFIED
    return _SCENARIO_PASS


# ---------------------------------------------------------------------------
# Env + duration + text helpers
# ---------------------------------------------------------------------------


def _short_env(base_url: str) -> str:
    """Map a base URL to a short env label for the header context."""
    if not base_url:
        return "?"
    host = (urlparse(base_url).hostname or base_url).lower()
    # Order matters: check the more specific labels first.
    for needle, label in (
        ("prod", "Prod"),
        ("staging", "Stg"),
        ("-stg", "Stg"),
        (".stg.", "Stg"),
        ("-qa", "QA"),
        (".qa.", "QA"),
        ("-dev", "Dev"),
        (".dev.", "Dev"),
        ("mafia", "Dev"),
    ):
        if needle in host:
            return label
    return host


def _duration_str(report: PipelineReport) -> str:
    if not report.started_at or not report.ended_at:
        return ""
    try:
        start = datetime.fromisoformat(report.started_at)
        end = datetime.fromisoformat(report.ended_at)
    except ValueError:
        return ""
    secs = (end - start).total_seconds()
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs:.0f}s"
    mins = int(secs // 60)
    rem = int(round(secs - mins * 60))
    return f"{mins}m {rem}s"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "\u2026"


def _ufid_short(ufid: str, keep: int = _UFID_SHORT_LEN) -> str:
    if len(ufid) <= keep + 1:
        return ufid
    return f"{ufid[:keep]}\u2026"


def _failure_reason(stage: PipelineStep) -> str:
    # `details` carries the failure cause ("extraction never completed"),
    # `description` is the attempted step ("extraction completed"). Prefer
    # the cause in the main channel — the description is already captured
    # by the stage name and would only add noise to the one-liner.
    reason = stage.details or stage.description or stage.status.value.lower()
    return _truncate(reason, _MAIN_FAIL_REASON_LIMIT)


def _unverified_reason(stage: PipelineStep) -> str:
    reason = stage.details or stage.description or stage.status.value.lower()
    return _truncate(reason, _MAIN_UNVERIFIED_REASON_LIMIT)


# ---------------------------------------------------------------------------
# Fallback text (push notification preview)
# ---------------------------------------------------------------------------


def build_fallback_text(report: PipelineReport) -> str:
    counts = _counts(report)
    overall = _overall_status(report)

    if overall == _SCENARIO_PASS:
        return f":large_green_circle: E2E PASS {counts.passed}/{counts.total}"

    if overall == _SCENARIO_UNVERIFIED:
        prefix = f":large_yellow_circle: E2E UNVERIFIED {counts.passed}/{counts.total}"
        for name, docs in _scenario_groups(report):
            if _scenario_status(docs) != _SCENARIO_UNVERIFIED:
                continue
            stage = _first_unverified_stage(_scenario_stages(docs))
            if stage:
                return f"{prefix} — {name} unverified at {stage.service}"
            return f"{prefix} — {name} unverified"
        return prefix

    prefix = f":red_circle: E2E FAIL {counts.passed}/{counts.total}"
    for name, docs in _scenario_groups(report):
        if _scenario_status(docs) != _SCENARIO_FAIL:
            continue
        stage = _first_failing_stage(_scenario_stages(docs))
        if stage:
            return f"{prefix} — {name} failed at {stage.service}"
        return f"{prefix} — {name} failed"
    return prefix


# ---------------------------------------------------------------------------
# Main message builders
# ---------------------------------------------------------------------------


def _header_block(report: PipelineReport) -> dict:
    counts = _counts(report)
    overall = _overall_status(report)
    summary = f"{counts.passed}/{counts.total} passed"
    if counts.unverified:
        summary += f", {counts.unverified} unverified"
    if overall == _SCENARIO_PASS:
        text = f":large_green_circle: E2E Pipeline — PASS  |  {summary}"
    elif overall == _SCENARIO_UNVERIFIED:
        text = f":large_yellow_circle: E2E Pipeline — UNVERIFIED  |  {summary}"
    else:
        text = f":red_circle: E2E Pipeline — FAIL  |  {summary}"
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": text, "emoji": True},
    }


def _meta_context_block(report: PipelineReport) -> dict:
    bits = [f"*Run:* `{report.run_id}`", f"*Env:* {_short_env(report.environment)}"]
    dur = _duration_str(report)
    if dur:
        bits.append(f"*Duration:* {dur}")
    if report.github_actions_url:
        bits.append(f"<{report.github_actions_url}|GitHub Actions Run>")
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "  •  ".join(bits)}],
    }


def _main_failing_scenario_text(name: str, docs: list[DocumentTrace]) -> str:
    stages = _scenario_stages(docs)
    failing = _first_failing_stage(stages)

    if failing is None:
        line = f":x: *{name}* — failed"
    else:
        reason = _failure_reason(failing)
        line = f":x: *{name}* — failed at {failing.service} ({reason})"

    # Include the full UFID for failing scenarios so on-call can copy-paste
    # straight into logs/dashboards without opening the thread.
    if docs:
        ufids = ", ".join(f"`{doc.ufid}`" for doc in docs[:3])
        line += f"\n        UFID: {ufids}"
    return _truncate(line, _MAIN_SECTION_TEXT_LIMIT)


def _main_unverified_scenario_text(
    name: str, docs: list[DocumentTrace]
) -> str:
    stages = _scenario_stages(docs)
    total = len(stages)
    ran = sum(1 for s in stages if s.status == StepStatus.PASS)
    first_skipped = _first_unverified_stage(stages)

    bits = [f":warning: *{name}* — unverified"]
    if total:
        bits.append(f"({ran}/{total} stages ran")
        if first_skipped:
            bits[-1] += f" — {_unverified_reason(first_skipped)}"
        bits[-1] += ")"
    elif first_skipped:
        bits.append(f"({_unverified_reason(first_skipped)})")

    line = " ".join(bits)
    # Include the full UFID so on-call can pull logs for the run that
    # silently couldn't be verified.
    if docs:
        ufids = ", ".join(f"`{doc.ufid}`" for doc in docs[:3])
        line += f"\n        UFID: {ufids}"
    return _truncate(line, _MAIN_SECTION_TEXT_LIMIT)


def _main_passing_scenarios_text(
    groups: list[tuple[str, list[DocumentTrace]]],
) -> str:
    lines: list[str] = []
    for name, docs in groups:
        stages = _scenario_stages(docs)
        total = len(stages)
        passed = sum(1 for s in stages if s.status == StepStatus.PASS)
        if total:
            lines.append(
                f":white_check_mark: *{name}* — passed ({passed}/{total} stages)"
            )
        else:
            lines.append(f":white_check_mark: *{name}* — passed")
    return _truncate("\n".join(lines), _MAIN_SECTION_TEXT_LIMIT)


def _main_errors_block(errors: list[str]) -> dict:
    lines = [":rotating_light: *Session errors:*"]
    for err in errors[:3]:
        lines.append(f"> {_truncate(err, 200)}")
    if len(errors) > 3:
        lines.append(f"> …and {len(errors) - 3} more")
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": _truncate("\n".join(lines), _MAIN_SECTION_TEXT_LIMIT),
        },
    }


def build_main_blocks(report: PipelineReport) -> list[dict]:
    blocks: list[dict] = [_header_block(report), _meta_context_block(report)]

    groups = _scenario_groups(report)
    failing = [(n, d) for n, d in groups if _scenario_status(d) == _SCENARIO_FAIL]
    unverified = [
        (n, d) for n, d in groups if _scenario_status(d) == _SCENARIO_UNVERIFIED
    ]
    passing = [(n, d) for n, d in groups if _scenario_status(d) == _SCENARIO_PASS]

    # One section per failing or unverified scenario — each needs its own
    # paragraph because the inline reason changes per scenario. Passing
    # scenarios collapse into a single section so the message length
    # stays bounded regardless of how many scenarios are added later.
    for name, docs in failing:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _main_failing_scenario_text(name, docs),
                },
            }
        )
    for name, docs in unverified:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _main_unverified_scenario_text(name, docs),
                },
            }
        )
    if passing:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _main_passing_scenarios_text(passing),
                },
            }
        )

    if report.errors:
        blocks.append(_main_errors_block(report.errors))

    if not groups and not report.errors:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":warning: No scenarios recorded — check the run log.",
                },
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":thread: Full stage breakdown in thread",
                }
            ],
        }
    )
    return blocks


# ---------------------------------------------------------------------------
# Thread reply builders
# ---------------------------------------------------------------------------


def _stage_line(stage: PipelineStep, marker: str = "") -> str:
    if stage.status == StepStatus.PASS:
        icon = ":white_check_mark:"
    elif stage.status == StepStatus.SKIPPED:
        icon = ":fast_forward:"
    elif stage.status == StepStatus.PENDING:
        icon = ":hourglass_flowing_sand:"
    elif stage.status == StepStatus.PARTIAL:
        icon = ":warning:"
    else:
        icon = ":x:"

    description = _truncate(stage.description, _THREAD_STAGE_NAME_LIMIT)
    text = f"  {icon} {stage.service} — {description}"
    if stage.details and stage.details != stage.description:
        text += f" _{_truncate(stage.details, 120)}_"
    if marker:
        text += f"  *{marker}*"
    return text


def _thread_scenario_section_text(name: str, docs: list[DocumentTrace]) -> str:
    status = _scenario_status(docs)
    if status == _SCENARIO_PASS:
        header_icon = ":white_check_mark:"
    elif status == _SCENARIO_UNVERIFIED:
        header_icon = ":warning:"
    else:
        header_icon = ":x:"

    ufid_part = (
        "  " + " ".join(f"`{_ufid_short(doc.ufid)}`" for doc in docs)
        if docs
        else ""
    )
    lines: list[str] = [f"{header_icon} *{name}*{ufid_part}"]

    stages = _scenario_stages(docs)
    if status == _SCENARIO_PASS:
        if stages:
            lines.append(f"  _All {len(stages)} stages passed._")
        else:
            lines.append("  _No stages recorded._")
    else:
        marker_target = (
            StepStatus.FAIL if status == _SCENARIO_FAIL else None
        )
        marker_placed = False
        for stage in stages:
            if status == _SCENARIO_FAIL:
                marker = (
                    "\u2190 FAILED HERE"
                    if not marker_placed and stage.status == marker_target
                    else ""
                )
            else:
                marker = (
                    "\u2190 NOT VERIFIED"
                    if not marker_placed
                    and stage.status
                    in (
                        StepStatus.SKIPPED,
                        StepStatus.PENDING,
                        StepStatus.PARTIAL,
                    )
                    else ""
                )
            if marker:
                marker_placed = True
            lines.append(_stage_line(stage, marker=marker))
        if not stages:
            fallback = (
                "_No stages recorded before the failure._"
                if status == _SCENARIO_FAIL
                else "_No stages recorded — pipeline could not be verified._"
            )
            lines.append(f"  {fallback}")

    return _truncate("\n".join(lines), _THREAD_SECTION_TEXT_LIMIT)


def build_thread_blocks(report: PipelineReport) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":clipboard: Stage details — all scenarios",
                "emoji": True,
            },
        }
    ]

    groups = _scenario_groups(report)
    if not groups:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No scenarios were recorded for this run._",
                },
            }
        )
        return blocks

    for name, docs in groups:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _thread_scenario_section_text(name, docs),
                },
            }
        )

    if report.errors:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":rotating_light: *Session errors:*\n"
                        + "\n".join(
                            f"> {_truncate(e, 300)}" for e in report.errors[:10]
                        )
                    ),
                },
            }
        )

    return blocks


def build_thread_fallback_text(report: PipelineReport) -> str:
    counts = _counts(report)
    parts = [f"{counts.passed}/{counts.total} scenarios passed"]
    if counts.unverified:
        parts.append(f"{counts.unverified} unverified")
    return "Stage details — " + ", ".join(parts)
