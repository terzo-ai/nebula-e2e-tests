"""HTML pipeline report: generates a standalone report showing pipeline progression.

Collects per-document step results, session logs, and session errors during test
execution, then renders a self-contained HTML report with:

  * Header banner + run metadata
  * Summary stats (documents / steps / pass rate)
  * Dashboards — status donut, per-stage pass/fail bars, per-document duration bars
  * Captured log stream (collapsible, color-coded by level)
  * Errors banner
  * Pipeline flow graph per document:
      File Ingestion Service (uploaded/failed) → Event Hub → OCR → AI Extraction → Ingestion
    Stages downstream of File Ingestion Service derive their status from Event Hub events.
"""

from __future__ import annotations

import html
import json
import logging
import math
import pathlib
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lib.event_hub import CapturedEvent


class StepStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    PENDING = "PENDING"


@dataclass
class PipelineStep:
    step_number: int
    service: str
    description: str
    status: StepStatus
    started_at: float  # time.monotonic()
    ended_at: float | None = None
    details: str = ""
    events: list[Any] = field(default_factory=list)

    @property
    def duration_s(self) -> float | None:
        if self.ended_at is not None:
            return self.ended_at - self.started_at
        return None


@dataclass
class DocumentTrace:
    ufid: str
    filename: str
    test_case: str = ""
    steps: list[PipelineStep] = field(default_factory=list)
    overall_status: StepStatus = StepStatus.PENDING
    response_body: dict[str, Any] | None = None

    def compute_status(self) -> None:
        # Rollup rules, in priority order:
        #   * any FAIL  → FAIL
        #   * any PARTIAL/PENDING/SKIPPED → PARTIAL ("unverified")
        #   * else → PASS
        #
        # SKIPPED used to roll up to PASS, which masked cases where the
        # pipeline stages couldn't be verified (e.g. Event Hub not wired).
        # Treating SKIPPED as non-pass mirrors Slack's "unverified" state.
        if not self.steps:
            self.overall_status = StepStatus.PENDING
            return
        statuses = {s.status for s in self.steps}
        if StepStatus.FAIL in statuses:
            self.overall_status = StepStatus.FAIL
        elif statuses - {StepStatus.PASS}:
            self.overall_status = StepStatus.PARTIAL
        else:
            self.overall_status = StepStatus.PASS

    @property
    def total_duration_s(self) -> float | None:
        started = [s.started_at for s in self.steps if s.started_at]
        ended = [s.ended_at for s in self.steps if s.ended_at]
        if started and ended:
            return max(ended) - min(started)
        return None


@dataclass
class LogEntry:
    timestamp: str       # ISO-8601 wall clock
    level: str           # INFO / WARNING / ERROR / ...
    logger_name: str
    message: str


class PipelineReport:
    """Collects pipeline step results and generates an HTML report."""

    def __init__(
        self,
        run_id: str,
        environment: str,
        tenant_id: int,
        started_at: str | None = None,
        github_actions_url: str = "",
    ) -> None:
        self.run_id = run_id
        self.environment = environment
        self.tenant_id = tenant_id
        self.started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.ended_at: str | None = None
        self.github_actions_url = github_actions_url
        self.documents: list[DocumentTrace] = []
        self._step_counter: dict[str, int] = {}
        self.errors: list[str] = []
        self.logs: list[LogEntry] = []

    def add_document(
        self,
        ufid: str,
        filename: str,
        test_case: str = "",
        response_body: dict[str, Any] | None = None,
    ) -> DocumentTrace:
        trace = DocumentTrace(
            ufid=ufid,
            filename=filename,
            test_case=test_case,
            response_body=response_body,
        )
        self.documents.append(trace)
        self._step_counter[ufid] = 0
        return trace

    def record_step(
        self,
        ufid: str,
        service: str,
        description: str,
        status: StepStatus,
        details: str = "",
    ) -> PipelineStep:
        trace = self._find_trace(ufid)
        if trace is None:
            trace = self.add_document(ufid, ufid)

        self._step_counter.setdefault(ufid, 0)
        self._step_counter[ufid] += 1

        step = PipelineStep(
            step_number=self._step_counter[ufid],
            service=service,
            description=description,
            status=status,
            started_at=time.monotonic(),
            ended_at=time.monotonic(),
            details=details,
        )
        trace.steps.append(step)
        return step

    def record_error(self, message: str) -> None:
        """Record a session-level error (e.g., auth failure, config issue)."""
        self.errors.append(message)

    def record_log(self, level: str, logger_name: str, message: str) -> None:
        """Record a log line. Bounded at 2000 entries to keep the HTML small."""
        if len(self.logs) >= 2000:
            return
        self.logs.append(
            LogEntry(
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                level=level.upper(),
                logger_name=logger_name,
                message=message,
            )
        )

    def install_log_handler(
        self,
        level: int = logging.INFO,
        logger: logging.Logger | None = None,
    ) -> ReportLogHandler:
        """Attach a logging handler that streams records into `self.logs`.

        Returns the handler so the caller can detach it at teardown.
        """
        handler = ReportLogHandler(self, level=level)
        target = logger if logger is not None else logging.getLogger()
        target.addHandler(handler)
        # Don't lower the logger's own level if it's already permissive.
        if target.level == logging.NOTSET or target.level > level:
            target.setLevel(level)
        return handler

    def finalize(self) -> None:
        self.ended_at = datetime.now(timezone.utc).isoformat()
        for doc in self.documents:
            doc.compute_status()

    def _find_trace(self, ufid: str) -> DocumentTrace | None:
        for doc in self.documents:
            if doc.ufid == ufid:
                return doc
        return None

    @property
    def overall_status(self) -> StepStatus:
        if not self.documents:
            return StepStatus.PENDING
        statuses = {d.overall_status for d in self.documents}
        if StepStatus.FAIL in statuses:
            return StepStatus.FAIL
        if StepStatus.PARTIAL in statuses or StepStatus.PENDING in statuses:
            return StepStatus.PARTIAL
        return StepStatus.PASS

    def slack_summary(self) -> str:
        """Build a Slack markdown summary of the pipeline run."""
        _STATUS_EMOJI = {
            StepStatus.PASS: ":white_check_mark:",
            StepStatus.FAIL: ":x:",
            StepStatus.PARTIAL: ":warning:",
            StepStatus.SKIPPED: ":fast_forward:",
            StepStatus.PENDING: ":hourglass:",
        }
        overall = self.overall_status
        emoji = _STATUS_EMOJI.get(overall, ":grey_question:")

        # Duration
        dur = ""
        if self.started_at and self.ended_at:
            try:
                start = datetime.fromisoformat(self.started_at)
                end = datetime.fromisoformat(self.ended_at)
                dur = f"  |  *Duration:* {_fmt_duration((end - start).total_seconds())}"
            except Exception:
                pass

        lines = [
            f":test_tube: *E2E Pipeline Report* — {emoji} {overall.value}",
            f"*Run ID:* `{self.run_id}`  |  *Env:* {self.environment}{dur}",
        ]
        if self.github_actions_url:
            lines.append(
                f":link: <{self.github_actions_url}|View GitHub Actions Run>"
            )
        lines.append("")

        # Group by test_case so the Slack summary matches the HTML layout.
        groups = _group_by_test_case(self)
        for test_case_name, docs in groups:
            tc_status = _test_case_status(docs)
            tc_emoji = _STATUS_EMOJI.get(tc_status, ":grey_question:")
            lines.append(
                f"*{test_case_name}* — {tc_emoji} {tc_status.value}  "
                f"_({len(docs)} file{'s' if len(docs) != 1 else ''})_"
            )
            for doc in docs:
                doc_emoji = _STATUS_EMOJI.get(doc.overall_status, ":grey_question:")
                lines.append(
                    f"  *UFID:* `{doc.ufid}` — {doc_emoji} {doc.overall_status.value}"
                )
                for step in doc.steps:
                    step_emoji = _STATUS_EMOJI.get(step.status, ":grey_question:")
                    lines.append(
                        f"      {step_emoji} {step.service} — {step.description}"
                    )

        if self.errors:
            lines.append("")
            lines.append(":rotating_light: *Errors:*")
            for err in self.errors[:5]:
                lines.append(f"    {err[:200]}")

        return "\n".join(lines)

    def render_html(self) -> str:
        return _render_html(self)

    def save(self, path: str | pathlib.Path) -> pathlib.Path:
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        html_str = self.render_html()
        path.write_text(html_str, encoding="utf-8")
        return path


class ReportLogHandler(logging.Handler):
    """Logging handler that appends formatted records to a PipelineReport."""

    def __init__(self, report: PipelineReport, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._report = report

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self._report.record_log(record.levelname, record.name, msg)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_STATUS_COLORS = {
    StepStatus.PASS: ("#16a34a", "#dcfce7", "#166534"),       # green
    StepStatus.FAIL: ("#dc2626", "#fee2e2", "#991b1b"),       # red
    StepStatus.PARTIAL: ("#ea580c", "#ffedd5", "#9a3412"),    # orange
    StepStatus.SKIPPED: ("#6b7280", "#f3f4f6", "#374151"),    # gray
    StepStatus.PENDING: ("#6b7280", "#f3f4f6", "#374151"),    # gray
}

_LOG_LEVEL_COLORS = {
    "DEBUG":    "#6b7280",
    "INFO":     "#2563eb",
    "WARNING":  "#ea580c",
    "ERROR":    "#dc2626",
    "CRITICAL": "#7f1d1d",
}


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def _status_badge(status: StepStatus) -> str:
    fg, bg, _ = _STATUS_COLORS[status]
    return (
        f'<span class="badge" style="background:{bg};color:{fg};">'
        f"{status.value}</span>"
    )


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins = int(seconds // 60)
    secs = seconds % 60
    return f"{mins}m {secs:.0f}s"


# --- Dashboard builders ----------------------------------------------------


def _doc_status_counts(report: PipelineReport) -> dict[StepStatus, int]:
    counts: dict[StepStatus, int] = defaultdict(int)
    for doc in report.documents:
        counts[doc.overall_status] += 1
    return counts


def _step_status_counts(report: PipelineReport) -> dict[StepStatus, int]:
    counts: dict[StepStatus, int] = defaultdict(int)
    for doc in report.documents:
        for step in doc.steps:
            counts[step.status] += 1
    return counts


def _per_service_counts(
    report: PipelineReport,
) -> list[tuple[str, dict[StepStatus, int]]]:
    """List of (service, status->count), preserving first-seen order."""
    order: list[str] = []
    buckets: dict[str, dict[StepStatus, int]] = {}
    for doc in report.documents:
        for step in doc.steps:
            if step.service not in buckets:
                buckets[step.service] = defaultdict(int)
                order.append(step.service)
            buckets[step.service][step.status] += 1
    return [(svc, buckets[svc]) for svc in order]


def _donut_svg(counts: dict[StepStatus, int], size: int = 180) -> str:
    """A CSS/SVG donut chart showing status distribution."""
    total = sum(counts.values())
    if total == 0:
        return (
            f'<svg class="donut" viewBox="0 0 {size} {size}" width="{size}" height="{size}">'
            f'<circle cx="{size/2}" cy="{size/2}" r="{size/2 - 20}" '
            f'fill="none" stroke="#e5e7eb" stroke-width="22"/></svg>'
        )

    radius = size / 2 - 20
    circumference = 2 * math.pi * radius
    segments: list[str] = []
    offset = 0.0
    # Order the ring: PASS, PARTIAL, FAIL, SKIPPED, PENDING.
    order = [StepStatus.PASS, StepStatus.PARTIAL, StepStatus.FAIL,
             StepStatus.SKIPPED, StepStatus.PENDING]
    for status in order:
        n = counts.get(status, 0)
        if n == 0:
            continue
        frac = n / total
        length = frac * circumference
        color, _, _ = _STATUS_COLORS[status]
        segments.append(
            f'<circle cx="{size/2}" cy="{size/2}" r="{radius}" fill="none" '
            f'stroke="{color}" stroke-width="22" '
            f'stroke-dasharray="{length:.3f} {circumference - length:.3f}" '
            f'stroke-dashoffset="{-offset:.3f}" '
            f'transform="rotate(-90 {size/2} {size/2})"/>'
        )
        offset += length

    center_label = f"{total}"
    center_sub = "docs" if total != 1 else "doc"

    return (
        f'<svg class="donut" viewBox="0 0 {size} {size}" width="{size}" height="{size}">'
        f'<circle cx="{size/2}" cy="{size/2}" r="{radius}" fill="none" '
        f'stroke="#f3f4f6" stroke-width="22"/>'
        + "".join(segments) +
        f'<text x="{size/2}" y="{size/2 - 2}" text-anchor="middle" '
        f'class="donut-num">{center_label}</text>'
        f'<text x="{size/2}" y="{size/2 + 18}" text-anchor="middle" '
        f'class="donut-sub">{center_sub}</text>'
        f'</svg>'
    )


def _donut_legend(counts: dict[StepStatus, int]) -> str:
    items: list[str] = []
    for status in [StepStatus.PASS, StepStatus.PARTIAL, StepStatus.FAIL,
                   StepStatus.SKIPPED, StepStatus.PENDING]:
        n = counts.get(status, 0)
        if n == 0:
            continue
        color, _, _ = _STATUS_COLORS[status]
        items.append(
            f'<li><span class="dot" style="background:{color}"></span>'
            f'{status.value}<span class="legend-num">{n}</span></li>'
        )
    if not items:
        items.append('<li><span class="dot" style="background:#e5e7eb"></span>No data</li>')
    return '<ul class="legend">' + "".join(items) + '</ul>'


def _stage_bars_html(report: PipelineReport) -> str:
    """Stacked horizontal bars showing pass/partial/fail counts per service."""
    services = _per_service_counts(report)
    if not services:
        return '<div class="chart-empty">No pipeline steps recorded yet.</div>'

    max_total = max(sum(b.values()) for _, b in services) or 1
    rows: list[str] = []
    for service, bucket in services:
        total = sum(bucket.values())
        pass_n = bucket.get(StepStatus.PASS, 0)
        partial_n = bucket.get(StepStatus.PARTIAL, 0)
        fail_n = bucket.get(StepStatus.FAIL, 0)
        skipped_n = bucket.get(StepStatus.SKIPPED, 0)
        pending_n = bucket.get(StepStatus.PENDING, 0)

        # Width of the whole row's filled portion proportional to max_total.
        row_scale = total / max_total
        def seg(n: int, status: StepStatus) -> str:
            if n == 0:
                return ""
            pct = (n / total) * 100 * row_scale
            color, _, _ = _STATUS_COLORS[status]
            return (
                f'<span class="bar-seg" style="width:{pct:.2f}%;background:{color}" '
                f'title="{status.value}: {n}"></span>'
            )

        bar = (
            seg(pass_n, StepStatus.PASS)
            + seg(partial_n, StepStatus.PARTIAL)
            + seg(fail_n, StepStatus.FAIL)
            + seg(skipped_n, StepStatus.SKIPPED)
            + seg(pending_n, StepStatus.PENDING)
        )
        pass_rate = (pass_n / total * 100) if total else 0
        rows.append(
            f'<div class="chart-row">'
            f'<div class="chart-label" title="{_esc(service)}">{_esc(service)}</div>'
            f'<div class="chart-bar">{bar}</div>'
            f'<div class="chart-value">{pass_n}/{total}'
            f'<span class="chart-subvalue">{pass_rate:.0f}% pass</span></div>'
            f'</div>'
        )
    return '<div class="chart">' + "".join(rows) + '</div>'


def _doc_duration_bars_html(report: PipelineReport) -> str:
    """Horizontal bars showing total duration per document."""
    rows_data = [
        (doc, doc.total_duration_s or 0.0) for doc in report.documents
    ]
    if not rows_data:
        return '<div class="chart-empty">No documents recorded yet.</div>'

    max_dur = max(d for _, d in rows_data) or 1.0
    rows: list[str] = []
    for doc, dur in rows_data:
        pct = (dur / max_dur) * 100 if max_dur else 0
        color, _, _ = _STATUS_COLORS[doc.overall_status]
        short_ufid = doc.ufid[:8]
        label = f"{short_ufid} · {doc.filename}"
        rows.append(
            f'<div class="chart-row">'
            f'<div class="chart-label" title="{_esc(doc.ufid)}">{_esc(label)}</div>'
            f'<div class="chart-bar">'
            f'<span class="bar-seg" style="width:{pct:.2f}%;background:{color}"></span>'
            f'</div>'
            f'<div class="chart-value">{_fmt_duration(dur if dur else None)}</div>'
            f'</div>'
        )
    return '<div class="chart">' + "".join(rows) + '</div>'


def _logs_html(report: PipelineReport) -> str:
    if not report.logs:
        return (
            '<details class="logs-panel">'
            '<summary>Session Logs <span class="panel-count">0</span></summary>'
            '<div class="logs-empty">No log records captured during this run.</div>'
            '</details>'
        )
    by_level: dict[str, int] = defaultdict(int)
    for log in report.logs:
        by_level[log.level] += 1

    chips = "".join(
        f'<span class="log-chip" style="color:{_LOG_LEVEL_COLORS.get(lvl, "#6b7280")}">'
        f'{lvl} · {by_level[lvl]}</span>'
        for lvl in ("ERROR", "WARNING", "INFO", "DEBUG") if by_level.get(lvl)
    )

    rows: list[str] = []
    for log in report.logs:
        color = _LOG_LEVEL_COLORS.get(log.level, "#6b7280")
        rows.append(
            f'<div class="log-row">'
            f'<span class="log-ts">{_esc(log.timestamp)}</span>'
            f'<span class="log-level" style="color:{color}">{_esc(log.level)}</span>'
            f'<span class="log-name">{_esc(log.logger_name)}</span>'
            f'<span class="log-msg">{_esc(log.message)}</span>'
            f'</div>'
        )
    body = '<div class="logs-body">' + "".join(rows) + '</div>'
    return (
        '<details class="logs-panel">'
        f'<summary>Session Logs <span class="panel-count">{len(report.logs)}</span>'
        f'<span class="log-chips">{chips}</span></summary>'
        + body +
        '</details>'
    )


# --- Pipeline flow graph ---------------------------------------------------

# Stages in the pipeline flow graph, left to right. Each stage aggregates steps
# whose `service` matches any of the listed names. Stages after File Ingestion
# Service are driven entirely by Event Hub events captured during the run.
_FLOW_STAGES: list[tuple[str, tuple[str, ...]]] = [
    ("File Ingestion", ("File Ingestion Service", "UI / Drive")),
    ("OCR",            ("OCR Service",)),
    ("Auto Extraction", ("Auto Extraction Service", "Extraction Service")),
]

_FLOW_ICONS: dict[StepStatus, str] = {
    StepStatus.PASS:    "\u2713",  # ✓
    StepStatus.FAIL:    "\u2717",  # ✗
    StepStatus.PARTIAL: "!",
    StepStatus.SKIPPED: "\u2014",  # —
    StepStatus.PENDING: "\u2026",  # …
}


def _aggregate_status(steps: list[PipelineStep]) -> StepStatus:
    if not steps:
        return StepStatus.PENDING
    statuses = {s.status for s in steps}
    if StepStatus.FAIL in statuses:
        return StepStatus.FAIL
    if StepStatus.PARTIAL in statuses or StepStatus.PENDING in statuses:
        return StepStatus.PARTIAL
    if statuses == {StepStatus.SKIPPED}:
        return StepStatus.SKIPPED
    return StepStatus.PASS


def _stage_substatus(
    stage_name: str, steps: list[PipelineStep], status: StepStatus
) -> str:
    """Short human-readable label shown under the stage name."""
    if not steps:
        return "not run"
    if status == StepStatus.FAIL:
        return "failed"
    if status == StepStatus.SKIPPED:
        return "skipped"
    if status in (StepStatus.PARTIAL, StepStatus.PENDING):
        return "partial"
    if stage_name == "File Ingestion":
        return "uploaded"
    return "completed"


def _flow_node_html(
    stage_name: str, steps: list[PipelineStep], status: StepStatus
) -> str:
    color, bg, fg = _STATUS_COLORS[status]
    icon = _FLOW_ICONS[status]
    substatus = _stage_substatus(stage_name, steps, status)

    if steps:
        items: list[str] = []
        for step in steps:
            step_color, _, _ = _STATUS_COLORS[step.status]
            details_suffix = f" — {_esc(step.details)}" if step.details else ""
            items.append(
                f'<li><span class="flow-detail-status" style="color:{step_color}">'
                f'{step.status.value}</span> {_esc(step.description)}{details_suffix}</li>'
            )
        details_body = '<ul class="flow-details">' + "".join(items) + '</ul>'
    else:
        details_body = (
            '<div class="flow-details-empty">No steps recorded for this stage.</div>'
        )

    count = len(steps)
    summary_label = f"{count} step" + ("s" if count != 1 else "")

    return (
        f'<div class="flow-node flow-node-{status.value.lower()}">'
        f'<div class="flow-node-icon" style="color:{color};border-color:{color};">{icon}</div>'
        f'<div class="flow-node-name">{_esc(stage_name)}</div>'
        f'<div class="flow-node-status" style="color:{fg};background:{bg};">'
        f'{status.value} &middot; {_esc(substatus)}</div>'
        f'<details class="flow-node-details">'
        f'<summary>{summary_label}</summary>'
        f'{details_body}'
        f'</details>'
        f'</div>'
    )


# --- Payload masking -------------------------------------------------------

# Keys whose values should be fully redacted before being rendered into the
# HTML report. Matched case-insensitively as substrings of the key name.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"authorization|bearer|"
    r"token|password|passwd|secret|"
    r"api[_-]?key|client[_-]?secret|"
    r"signature|connection[_-]?string|shared[_-]?access[_-]?key|"
    r"cookie|xsrf|credit[_-]?card|\bssn\b",
    re.IGNORECASE,
)

# Azure SAS query-string parameters. When a value is a URL, these params are
# scrubbed individually so the rest of the URL is still readable.
_SENSITIVE_URL_PARAMS = {
    "sig", "sv", "se", "sp", "srt", "ss", "spr", "st",
    "skoid", "sktid", "skt", "ske", "sks", "skv",
}

_REDACTED = "***REDACTED***"


def _mask_url_params(url: str) -> str:
    if "?" not in url:
        return url
    head, _, qs = url.partition("?")
    masked_parts: list[str] = []
    for part in qs.split("&"):
        name, eq, value = part.partition("=")
        if eq and value and name.lower() in _SENSITIVE_URL_PARAMS:
            masked_parts.append(f"{name}=***")
        else:
            masked_parts.append(part)
    return head + "?" + "&".join(masked_parts)


def _mask_payload(value: Any, key: str = "") -> Any:
    """Recursively redact values whose key matches a sensitive pattern.

    Preserves structure so the rendered JSON stays readable.
    """
    if key and _SENSITIVE_KEY_PATTERN.search(key):
        return _REDACTED
    if isinstance(value, dict):
        return {k: _mask_payload(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_payload(v, key) for v in value]
    if isinstance(value, str):
        return _mask_url_params(value)
    return value


def _payload_json_html(payload: Any) -> str:
    """Render a masked payload as a collapsible <pre> block."""
    try:
        masked = _mask_payload(payload)
        rendered = json.dumps(masked, indent=2, default=str, sort_keys=True)
    except Exception as e:  # pragma: no cover — defensive
        rendered = f"<could not render payload: {type(e).__name__}: {e}>"
    return f'<pre class="payload-json">{_esc(rendered)}</pre>'


def _doc_flow_graph_html(doc: DocumentTrace) -> str:
    """Render the horizontal stage flow for a single document."""
    parts: list[str] = []
    for idx, (stage_name, services) in enumerate(_FLOW_STAGES):
        matching = [s for s in doc.steps if s.service in services]
        status = _aggregate_status(matching)
        parts.append(_flow_node_html(stage_name, matching, status))
        if idx < len(_FLOW_STAGES) - 1:
            parts.append('<div class="flow-arrow" aria-hidden="true">&rarr;</div>')
    return '<div class="flow-graph">' + "".join(parts) + '</div>'


# --- Test-case grouping ----------------------------------------------------
#
# Documents are grouped by `DocumentTrace.test_case` so the report reflects
# the logical test cases (e.g. "Bulk Upload", "UI File Upload endpoint")
# rather than a flat list of UFIDs. Each test-case panel shows:
#   - summary header: test case name, file count, overall status
#   - optional response body (captured from the upload endpoint)
#   - collapsible UFID cards; expanding a UFID reveals its pipeline flow
#     graph + Event Hub timeline (the same content the flat list showed).


def _group_by_test_case(
    report: PipelineReport,
) -> list[tuple[str, list[DocumentTrace]]]:
    """Group documents by test_case, preserving first-seen order."""
    order: list[str] = []
    buckets: dict[str, list[DocumentTrace]] = {}
    for doc in report.documents:
        key = doc.test_case or "Uncategorized"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(doc)
    return [(name, buckets[name]) for name in order]


def _test_case_status(docs: list[DocumentTrace]) -> StepStatus:
    """Aggregate status across all documents in a test case."""
    if not docs:
        return StepStatus.PENDING
    statuses = {d.overall_status for d in docs}
    if StepStatus.FAIL in statuses:
        return StepStatus.FAIL
    if StepStatus.PARTIAL in statuses or StepStatus.PENDING in statuses:
        return StepStatus.PARTIAL
    if statuses == {StepStatus.SKIPPED}:
        return StepStatus.SKIPPED
    return StepStatus.PASS


def _events_for_doc(doc: DocumentTrace) -> list[Any]:
    events: list[Any] = []
    for step in doc.steps:
        events.extend(step.events)
    return events


def _event_timeline_html(events: list[Any]) -> str:
    if not events:
        return ""
    event_rows = ""
    for ev in events:
        short_doc_id = str(ev.document_id)[:8] if ev.document_id else "-"
        short_event_id = str(ev.event_id)[:12] if ev.event_id else "-"
        event_type = getattr(ev, "event_type", "") or "-"
        payload_html = _payload_json_html(ev.payload)
        event_rows += f"""
            <tr>
              <td><code>{_esc(str(ev.received_at))}</code></td>
              <td><code>{_esc(str(event_type))}</code></td>
              <td><code>{_esc(str(ev.action) or "-")}</code></td>
              <td><code title="{_esc(str(ev.event_id))}">{_esc(short_event_id)}</code></td>
              <td><code title="{_esc(str(ev.document_id))}">{_esc(short_doc_id)}</code></td>
              <td>{_esc(str(ev.partition_id))}</td>
              <td>{_esc(str(ev.sequence_number))}</td>
              <td class="events-payload-cell">
                <details class="payload-toggle">
                  <summary>view</summary>
                  {payload_html}
                </details>
              </td>
            </tr>"""
    return f"""
        <div class="event-timeline">
          <h4>Event Hub Events
            <span class="payload-note">(payloads auto-masked — secrets/SAS params redacted)</span>
          </h4>
          <table class="events-table">
            <thead><tr>
              <th>Received At</th><th>Type</th><th>Action</th><th>Event ID</th>
              <th>Doc ID</th><th>Partition</th><th>Seq #</th><th>Payload</th>
            </tr></thead>
            <tbody>{event_rows}</tbody>
          </table>
        </div>"""


def _doc_card_html(doc: DocumentTrace) -> str:
    """One collapsible UFID card — closed by default so users can
    expand individual UFIDs to see their pipeline flow + events."""
    event_section = _event_timeline_html(_events_for_doc(doc))
    return f"""
        <div class="doc-section">
          <details>
            <summary>
              <span class="doc-ufid-chip" title="Universal File ID">UFID</span>
              <span class="doc-ufid">{_esc(doc.ufid)}</span>
              <span class="doc-filename">{_esc(doc.filename)}</span>
              {_status_badge(doc.overall_status)}
            </summary>
            <div class="doc-ufid-banner">
              <span class="doc-ufid-label">UFID</span>
              <code class="doc-ufid-value">{_esc(doc.ufid)}</code>
              <span class="doc-ufid-file">{_esc(doc.filename)}</span>
            </div>
            {_doc_flow_graph_html(doc)}
            {event_section}
          </details>
        </div>"""


def _test_case_response_html(docs: list[DocumentTrace]) -> str:
    """Render captured upload-response bodies (e.g. UI file-upload response)
    so they show up in the report per the requirement to 'capture the
    response body and print it in report'."""
    with_body = [d for d in docs if d.response_body is not None]
    if not with_body:
        return ""
    blocks: list[str] = []
    for doc in with_body:
        blocks.append(
            f'<div class="response-block">'
            f'<div class="response-label">Response for '
            f'<code>{_esc(doc.ufid)}</code> · '
            f'<span class="response-file">{_esc(doc.filename)}</span></div>'
            f'{_payload_json_html(doc.response_body)}'
            f'</div>'
        )
    return (
        '<div class="test-case-response">'
        '<h4>Upload Response Body</h4>'
        + "".join(blocks)
        + '</div>'
    )


def _test_case_sections_html(report: PipelineReport) -> str:
    """Top-level rendering: one collapsible panel per test case, with a
    summary header (file count, status breakdown) and cascaded UFID cards.
    """
    groups = _group_by_test_case(report)
    if not groups:
        return ""

    panels: list[str] = []
    for test_case_name, docs in groups:
        tc_status = _test_case_status(docs)
        _, bg, fg = _STATUS_COLORS[tc_status]
        status_counts: dict[StepStatus, int] = defaultdict(int)
        for doc in docs:
            status_counts[doc.overall_status] += 1
        count_chips: list[str] = []
        for status in (StepStatus.PASS, StepStatus.PARTIAL, StepStatus.FAIL,
                       StepStatus.SKIPPED, StepStatus.PENDING):
            n = status_counts.get(status, 0)
            if n == 0:
                continue
            color, _, _ = _STATUS_COLORS[status]
            count_chips.append(
                f'<span class="tc-chip" style="color:{color};">'
                f'{status.value} · {n}</span>'
            )
        chips_html = "".join(count_chips)

        file_count = len(docs)
        file_word = "file" if file_count == 1 else "files"
        ufid_cards = "".join(_doc_card_html(doc) for doc in docs)
        response_section = _test_case_response_html(docs)

        panels.append(f"""
        <details class="test-case-panel" open>
          <summary class="test-case-summary">
            <span class="tc-name">Pipeline Flow - {_esc(test_case_name)}</span>
            <span class="tc-badge" style="background:{bg};color:{fg};">
              {tc_status.value}
            </span>
            <span class="tc-meta">{file_count} {file_word} uploaded</span>
            <span class="tc-chips">{chips_html}</span>
          </summary>
          <div class="test-case-body">
            {response_section}
            <div class="test-case-docs-label">
              UFIDs ({file_count}) — click a UFID to expand its pipeline trace
            </div>
            {ufid_cards}
          </div>
        </details>""")
    return "".join(panels)


# --- Main render -----------------------------------------------------------


def _render_html(report: PipelineReport) -> str:
    overall = report.overall_status
    banner_bg, _, banner_fg = _STATUS_COLORS[overall]

    detail_sections = _test_case_sections_html(report)

    total_docs = len(report.documents)
    doc_counts = _doc_status_counts(report)
    passed = doc_counts.get(StepStatus.PASS, 0)
    failed = doc_counts.get(StepStatus.FAIL, 0)
    partial = doc_counts.get(StepStatus.PARTIAL, 0)

    step_counts = _step_status_counts(report)
    total_steps = sum(step_counts.values())
    steps_passed = step_counts.get(StepStatus.PASS, 0)
    steps_failed = step_counts.get(StepStatus.FAIL, 0)
    step_pass_rate = (steps_passed / total_steps * 100) if total_steps else 0.0

    # Session duration (wall clock, not step durations).
    try:
        start_dt = datetime.fromisoformat(report.started_at)
        end_dt = datetime.fromisoformat(report.ended_at) if report.ended_at else None
        wall_duration_s = (end_dt - start_dt).total_seconds() if end_dt else None
    except Exception:
        wall_duration_s = None

    errors_section = ""
    if report.errors:
        error_items = "\n".join(
            f'<div class="error-item">{_esc(e)}</div>' for e in report.errors
        )
        errors_section = f"""
    <div class="errors-banner">
      <h3>Errors</h3>
      {error_items}
    </div>"""

    empty_state = ""
    if not report.documents and not report.errors:
        empty_state = """
    <div class="empty-state">
      <div class="empty-icon">&#9888;</div>
      <div class="empty-title">No pipeline data recorded</div>
      <div class="empty-desc">Tests may have been skipped or errored before pipeline steps ran.
      Check the pytest output for details.</div>
    </div>"""

    donut_svg = _donut_svg(doc_counts)
    donut_legend = _donut_legend(doc_counts)
    stage_bars = _stage_bars_html(report)
    duration_bars = _doc_duration_bars_html(report)
    logs_section = _logs_html(report)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pipeline Report - {_esc(report.run_id)}</title>
<style>
  :root {{
    --pass: #16a34a; --pass-bg: #dcfce7;
    --fail: #dc2626; --fail-bg: #fee2e2;
    --partial: #ea580c; --partial-bg: #ffedd5;
    --gray: #6b7280; --gray-bg: #f3f4f6;
    --border: #e5e7eb;
    --bg: #f9fafb;
    --card-bg: #ffffff;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: #1f2937; line-height: 1.5;
  }}
  .banner {{
    background: {banner_bg}; color: white;
    padding: 20px 32px; display: flex; align-items: center; gap: 16px;
  }}
  .banner h1 {{ font-size: 20px; font-weight: 600; }}
  .banner .status-text {{ font-size: 14px; opacity: 0.9; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}

  .meta-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px; margin-bottom: 20px;
  }}
  .meta-card {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px;
  }}
  .meta-card .label {{ font-size: 11px; text-transform: uppercase; color: var(--gray); letter-spacing: 0.05em; }}
  .meta-card .value {{ font-size: 14px; font-weight: 600; margin-top: 2px; word-break: break-all; }}

  .summary-stats {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px; margin-bottom: 24px;
  }}
  .stat {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 18px; text-align: center;
  }}
  .stat .num {{ font-size: 26px; font-weight: 700; line-height: 1.1; }}
  .stat .lbl {{ font-size: 11px; color: var(--gray); text-transform: uppercase; letter-spacing: 0.04em; margin-top: 4px; }}
  .stat.pass .num {{ color: var(--pass); }}
  .stat.fail .num {{ color: var(--fail); }}
  .stat.partial .num {{ color: var(--partial); }}

  /* Dashboards */
  .dashboards {{
    display: grid; grid-template-columns: 280px 1fr; gap: 16px;
    margin-bottom: 24px;
  }}
  @media (max-width: 780px) {{ .dashboards {{ grid-template-columns: 1fr; }} }}
  .panel {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px 20px;
  }}
  .panel h3 {{
    font-size: 13px; color: var(--gray); text-transform: uppercase;
    letter-spacing: 0.05em; margin-bottom: 12px;
  }}
  .donut-wrap {{ display: flex; flex-direction: column; align-items: center; gap: 12px; }}
  .donut .donut-num {{ font-size: 26px; font-weight: 700; fill: #111827; }}
  .donut .donut-sub {{ font-size: 11px; fill: var(--gray); text-transform: uppercase; letter-spacing: 0.08em; }}
  .legend {{ list-style: none; width: 100%; font-size: 13px; }}
  .legend li {{
    display: flex; align-items: center; gap: 8px; padding: 3px 0;
  }}
  .legend .dot {{
    width: 10px; height: 10px; border-radius: 50%; display: inline-block;
  }}
  .legend .legend-num {{
    margin-left: auto; font-weight: 600; color: #1f2937;
  }}

  .charts-stack {{ display: flex; flex-direction: column; gap: 16px; }}
  .chart {{ display: flex; flex-direction: column; gap: 8px; }}
  .chart-row {{
    display: grid; grid-template-columns: 200px 1fr 120px;
    gap: 12px; align-items: center; font-size: 13px;
  }}
  .chart-label {{
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    font-weight: 500;
  }}
  .chart-bar {{
    height: 16px; background: var(--gray-bg); border-radius: 4px;
    display: flex; overflow: hidden;
  }}
  .bar-seg {{ height: 100%; display: inline-block; }}
  .chart-value {{
    text-align: right; font-variant-numeric: tabular-nums;
    color: #374151;
  }}
  .chart-subvalue {{
    display: block; font-size: 11px; color: var(--gray);
  }}
  .chart-empty {{
    font-size: 13px; color: var(--gray); padding: 8px 0;
  }}

  /* Logs */
  .logs-panel {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; margin-bottom: 24px; overflow: hidden;
  }}
  .logs-panel > summary {{
    padding: 14px 20px; cursor: pointer; font-weight: 600;
    display: flex; align-items: center; gap: 12px;
  }}
  .logs-panel > summary:hover {{ background: var(--bg); }}
  .panel-count {{
    font-size: 12px; background: var(--gray-bg); color: var(--gray);
    padding: 1px 8px; border-radius: 10px; font-weight: 500;
  }}
  .log-chips {{ margin-left: auto; display: flex; gap: 10px; font-size: 12px; font-weight: 600; }}
  .log-chip {{ letter-spacing: 0.04em; }}
  .logs-body {{
    max-height: 340px; overflow-y: auto;
    font-family: 'SF Mono', Consolas, monospace; font-size: 12px;
    border-top: 1px solid var(--border); background: #0f172a; color: #e5e7eb;
  }}
  .log-row {{
    display: grid; grid-template-columns: 170px 70px 220px 1fr;
    gap: 12px; padding: 4px 16px;
    border-bottom: 1px solid #1f2937; white-space: pre-wrap; word-break: break-word;
  }}
  .log-ts {{ color: #94a3b8; }}
  .log-level {{ font-weight: 700; letter-spacing: 0.04em; }}
  .log-name {{ color: #a5b4fc; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .log-msg {{ color: #e5e7eb; }}
  .logs-empty {{
    padding: 14px 20px; font-size: 13px; color: var(--gray);
    border-top: 1px solid var(--border);
  }}

  .badge {{
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: 12px; font-weight: 700; letter-spacing: 0.03em;
  }}

  /* Pipeline progression (flow graph at the bottom) */
  .section-heading {{
    font-size: 18px; margin: 16px 0 12px;
    padding-top: 8px; border-top: 1px solid var(--border);
  }}

  /* Test-case grouping — one collapsible panel per test case. */
  .test-case-panel {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 10px; margin-bottom: 20px; overflow: hidden;
    box-shadow: 0 1px 2px rgba(0,0,0,0.03);
  }}
  .test-case-summary {{
    padding: 16px 22px; cursor: pointer; display: flex;
    align-items: center; gap: 14px; flex-wrap: wrap;
    font-size: 15px; font-weight: 600; list-style: none;
    background: #f8fafc; border-bottom: 1px solid var(--border);
  }}
  .test-case-summary::-webkit-details-marker {{ display: none; }}
  .test-case-summary::before {{
    content: "\u25B8 "; font-size: 11px; color: var(--gray);
    margin-right: 2px; display: inline-block; width: 12px;
  }}
  .test-case-panel[open] > .test-case-summary::before {{ content: "\u25BE "; }}
  .test-case-summary:hover {{ background: #f1f5f9; }}
  .tc-name {{ font-size: 16px; color: #0f172a; }}
  .tc-badge {{
    display: inline-block; padding: 3px 10px; border-radius: 12px;
    font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
    text-transform: uppercase;
  }}
  .tc-meta {{
    font-size: 12px; color: var(--gray); font-weight: 500;
  }}
  .tc-chips {{
    display: flex; gap: 10px; margin-left: auto; font-size: 11px;
    font-weight: 700; letter-spacing: 0.04em;
  }}
  .tc-chip {{ text-transform: uppercase; }}
  .test-case-body {{ padding: 18px 22px; }}
  .test-case-docs-label {{
    font-size: 11px; color: var(--gray); text-transform: uppercase;
    letter-spacing: 0.06em; margin: 4px 0 10px;
  }}
  .test-case-response {{
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px; margin-bottom: 16px;
  }}
  .test-case-response h4 {{
    font-size: 12px; color: var(--gray); text-transform: uppercase;
    letter-spacing: 0.05em; margin-bottom: 8px;
  }}
  .response-block {{ margin-top: 8px; }}
  .response-block:first-of-type {{ margin-top: 0; }}
  .response-label {{
    font-size: 12px; color: #334155; margin-bottom: 4px;
  }}
  .response-file {{ color: var(--gray); }}

  .doc-section {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; margin-bottom: 12px; overflow: hidden;
  }}
  .doc-section summary {{
    padding: 14px 20px; cursor: pointer; display: flex;
    align-items: center; gap: 12px; font-size: 14px;
    border-bottom: 1px solid var(--border);
  }}
  .doc-section summary:hover {{ background: var(--bg); }}
  .doc-ufid-chip {{
    background: #1e293b; color: #e5e7eb; font-size: 10px;
    font-weight: 700; letter-spacing: 0.06em; padding: 2px 8px;
    border-radius: 4px; text-transform: uppercase;
  }}
  .doc-ufid {{ font-family: 'SF Mono', Consolas, monospace; font-size: 13px; color: #1f2937; }}
  .doc-filename {{ font-weight: 600; }}
  .doc-ufid-banner {{
    display: flex; align-items: center; gap: 10px;
    padding: 12px 20px; background: #f3f4f6;
    border-bottom: 1px solid var(--border); font-size: 13px;
  }}
  .doc-ufid-banner .doc-ufid-label {{
    font-size: 10px; font-weight: 700; letter-spacing: 0.06em;
    color: var(--gray); text-transform: uppercase;
  }}
  .doc-ufid-banner .doc-ufid-value {{
    font-family: 'SF Mono', Consolas, monospace;
    background: var(--card-bg); border: 1px solid var(--border);
    padding: 3px 10px; border-radius: 4px; font-size: 12px;
    color: #111827; user-select: all;
  }}
  .doc-ufid-banner .doc-ufid-file {{
    color: var(--gray); font-size: 12px; margin-left: auto;
  }}

  .flow-graph {{
    display: flex; align-items: stretch; gap: 4px;
    padding: 20px; overflow-x: auto;
    background: linear-gradient(180deg, #ffffff 0%, #f9fafb 100%);
  }}
  .flow-node {{
    flex: 1 1 0; min-width: 150px;
    background: var(--card-bg); border: 2px solid var(--border);
    border-radius: 10px; padding: 14px 12px;
    display: flex; flex-direction: column; align-items: center;
    gap: 8px; text-align: center;
    transition: box-shadow 0.15s ease;
  }}
  .flow-node:hover {{ box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .flow-node-pass {{ border-color: var(--pass); }}
  .flow-node-fail {{ border-color: var(--fail); }}
  .flow-node-partial {{ border-color: var(--partial); }}
  .flow-node-skipped, .flow-node-pending {{ border-color: var(--border); border-style: dashed; }}
  .flow-node-icon {{
    width: 40px; height: 40px; border-radius: 50%;
    border: 2px solid; display: flex; align-items: center;
    justify-content: center; font-size: 22px; font-weight: 700;
    line-height: 1;
  }}
  .flow-node-name {{ font-size: 14px; font-weight: 600; color: #1f2937; }}
  .flow-node-status {{
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 3px 10px; border-radius: 10px; font-weight: 700;
  }}
  .flow-node-details {{ width: 100%; font-size: 12px; }}
  .flow-node-details > summary {{
    cursor: pointer; color: var(--gray); padding: 4px 0;
    list-style: none; font-weight: 500;
  }}
  .flow-node-details > summary::-webkit-details-marker {{ display: none; }}
  .flow-node-details > summary::before {{
    content: "\u25B8 "; font-size: 10px; margin-right: 2px;
  }}
  .flow-node-details[open] > summary::before {{ content: "\u25BE "; }}
  .flow-node-details[open] > summary:hover {{ color: #1f2937; }}
  .flow-details {{
    margin-top: 4px; text-align: left; list-style: none;
    padding-left: 0;
  }}
  .flow-details li {{
    padding: 6px 0; border-bottom: 1px solid var(--border);
    word-break: break-word;
  }}
  .flow-details li:last-child {{ border-bottom: none; }}
  .flow-detail-status {{
    font-weight: 700; font-size: 10px; letter-spacing: 0.04em;
    margin-right: 4px;
  }}
  .flow-details-empty {{
    font-size: 12px; color: var(--gray); margin-top: 4px;
    font-style: italic; text-align: left;
  }}
  .flow-arrow {{
    align-self: center; font-size: 22px; font-weight: 700;
    color: #9ca3af; padding: 0 2px; flex: 0 0 auto;
  }}
  @media (max-width: 780px) {{
    .flow-graph {{ flex-direction: column; }}
    .flow-arrow {{ transform: rotate(90deg); padding: 4px 0; }}
  }}

  .event-timeline {{
    padding: 16px 20px; background: var(--bg);
    border-top: 1px solid var(--border);
  }}
  .event-timeline h4 {{ font-size: 13px; margin-bottom: 8px; color: var(--gray); }}
  .events-table {{
    width: 100%; border-collapse: collapse; font-size: 13px;
  }}
  .events-table th {{
    text-align: left; padding: 6px 10px; background: var(--card-bg);
    font-size: 11px; text-transform: uppercase; color: var(--gray);
    border-bottom: 1px solid var(--border);
  }}
  .events-table td {{ padding: 6px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  .events-payload-cell {{ width: 1%; white-space: nowrap; }}
  .payload-note {{
    font-weight: 400; text-transform: none; color: var(--gray);
    font-size: 11px; margin-left: 6px; letter-spacing: 0;
  }}
  .payload-toggle > summary {{
    cursor: pointer; font-size: 12px; color: #2563eb;
    list-style: none; font-weight: 500;
  }}
  .payload-toggle > summary::-webkit-details-marker {{ display: none; }}
  .payload-toggle > summary::before {{ content: "\u25B8 "; font-size: 10px; }}
  .payload-toggle[open] > summary::before {{ content: "\u25BE "; }}
  .payload-json {{
    margin-top: 6px; padding: 10px 12px;
    background: #0f172a; color: #e5e7eb;
    border-radius: 6px; font-family: 'SF Mono', Consolas, monospace;
    font-size: 11px; line-height: 1.5;
    max-height: 320px; overflow: auto; white-space: pre-wrap;
    word-break: break-word;
  }}
  .errors-banner {{
    background: var(--fail-bg); border: 1px solid var(--fail);
    border-radius: 8px; padding: 16px 20px; margin-bottom: 24px;
  }}
  .errors-banner h3 {{ font-size: 14px; color: var(--fail); margin-bottom: 8px; }}
  .error-item {{
    font-size: 13px; color: #991b1b; padding: 6px 0;
    border-bottom: 1px solid #fecaca; font-family: 'SF Mono', Consolas, monospace;
  }}
  .error-item:last-child {{ border-bottom: none; }}
  .empty-state {{
    text-align: center; padding: 60px 20px; color: var(--gray);
  }}
  .empty-icon {{ font-size: 48px; margin-bottom: 12px; }}
  .empty-title {{ font-size: 18px; font-weight: 600; color: #374151; margin-bottom: 8px; }}
  .empty-desc {{ font-size: 14px; max-width: 500px; margin: 0 auto; }}
  .footer {{
    text-align: center; padding: 24px; font-size: 12px; color: var(--gray);
  }}
</style>
</head>
<body>
  <div class="banner">
    <h1>Nebula E2E Pipeline Report</h1>
    <span class="status-text">Overall Status: {overall.value}</span>
  </div>
  <div class="container">

    <!-- Run metadata -->
    <div class="meta-grid">
      <div class="meta-card">
        <div class="label">Run ID</div>
        <div class="value">{_esc(report.run_id)}</div>
      </div>
      <div class="meta-card">
        <div class="label">Environment</div>
        <div class="value">{_esc(report.environment)}</div>
      </div>
      <div class="meta-card">
        <div class="label">Tenant</div>
        <div class="value">{report.tenant_id}</div>
      </div>
      <div class="meta-card">
        <div class="label">Started</div>
        <div class="value">{_esc(report.started_at)}</div>
      </div>
      <div class="meta-card">
        <div class="label">Wall Duration</div>
        <div class="value">{_fmt_duration(wall_duration_s)}</div>
      </div>
      {f'<div class="meta-card"><div class="label">GitHub Actions</div><div class="value"><a href="{_esc(report.github_actions_url)}" target="_blank" rel="noopener">View Run</a></div></div>' if report.github_actions_url else ''}
    </div>

    <!-- Summary stats -->
    <div class="summary-stats">
      <div class="stat">
        <div class="num">{total_docs}</div>
        <div class="lbl">Documents</div>
      </div>
      <div class="stat pass">
        <div class="num">{passed}</div>
        <div class="lbl">Passed</div>
      </div>
      <div class="stat partial">
        <div class="num">{partial}</div>
        <div class="lbl">Partial</div>
      </div>
      <div class="stat fail">
        <div class="num">{failed}</div>
        <div class="lbl">Failed</div>
      </div>
      <div class="stat">
        <div class="num">{total_steps}</div>
        <div class="lbl">Pipeline Steps</div>
      </div>
      <div class="stat pass">
        <div class="num">{step_pass_rate:.0f}%</div>
        <div class="lbl">Step Pass Rate</div>
      </div>
    </div>

    <!-- Dashboards -->
    <div class="dashboards">
      <div class="panel">
        <h3>Document Status</h3>
        <div class="donut-wrap">
          {donut_svg}
          {donut_legend}
        </div>
      </div>
      <div class="charts-stack">
        <div class="panel">
          <h3>Pipeline Stage Health <span style="font-weight:400;text-transform:none;color:#9ca3af;font-size:11px;">(pass / total steps per service)</span></h3>
          {stage_bars}
        </div>
        <div class="panel">
          <h3>Document Durations</h3>
          {duration_bars}
        </div>
      </div>
    </div>

    <!-- Logs -->
    {logs_section}

    {errors_section}
    {empty_state}

    <!-- Per-test-case pipeline flow — each panel lists its UFIDs and
         their pipeline flow + Event Hub timelines. -->
    <h2 class="section-heading">Pipeline Flow</h2>
    {detail_sections}
  </div>
  <div class="footer">
    Generated by nebula-e2e-tests | {_esc(report.ended_at or report.started_at)}
  </div>
</body>
</html>"""
