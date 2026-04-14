"""HTML pipeline report: generates a standalone report showing pipeline progression.

Collects per-document step results during test execution and renders a self-contained
HTML report with status table, event timeline, and timing information.
"""

from __future__ import annotations

import pathlib
import time
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
    steps: list[PipelineStep] = field(default_factory=list)
    overall_status: StepStatus = StepStatus.PENDING

    def compute_status(self) -> None:
        if not self.steps:
            self.overall_status = StepStatus.PENDING
            return
        statuses = {s.status for s in self.steps}
        if StepStatus.FAIL in statuses:
            self.overall_status = StepStatus.FAIL
        elif StepStatus.PARTIAL in statuses or StepStatus.PENDING in statuses:
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


class PipelineReport:
    """Collects pipeline step results and generates an HTML report."""

    def __init__(
        self,
        run_id: str,
        environment: str,
        tenant_id: int,
        started_at: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.environment = environment
        self.tenant_id = tenant_id
        self.started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.ended_at: str | None = None
        self.documents: list[DocumentTrace] = []
        self._step_counter: dict[str, int] = {}

    def add_document(self, ufid: str, filename: str) -> DocumentTrace:
        trace = DocumentTrace(ufid=ufid, filename=filename)
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

    def render_html(self) -> str:
        return _render_html(self)

    def save(self, path: str | pathlib.Path) -> pathlib.Path:
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        html = self.render_html()
        path.write_text(html, encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_STATUS_COLORS = {
    StepStatus.PASS: ("#16a34a", "#dcfce7", "#166534"),       # green
    StepStatus.FAIL: ("#dc2626", "#fee2e2", "#991b1b"),       # red
    StepStatus.PARTIAL: ("#ea580c", "#ffedd5", "#9a3412"),     # orange
    StepStatus.SKIPPED: ("#6b7280", "#f3f4f6", "#374151"),     # gray
    StepStatus.PENDING: ("#6b7280", "#f3f4f6", "#374151"),     # gray
}


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


def _render_html(report: PipelineReport) -> str:
    overall = report.overall_status
    banner_bg, _, banner_fg = _STATUS_COLORS[overall]

    doc_rows = ""
    for doc in report.documents:
        dur = _fmt_duration(doc.total_duration_s)
        doc_rows += f"""
        <tr>
          <td>{doc.ufid[:12]}...</td>
          <td>{doc.filename}</td>
          <td>{_status_badge(doc.overall_status)}</td>
          <td>{dur}</td>
        </tr>"""

    detail_sections = ""
    for doc in report.documents:
        step_rows = ""
        for step in doc.steps:
            dur = _fmt_duration(step.duration_s)
            details_cell = step.details if step.details else ""
            # Build detail items
            detail_items = []
            if step.details:
                for line in step.details.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("\u2713") or line.startswith("\u2714"):
                        detail_items.append(f'<div class="detail-pass">{line}</div>')
                    elif line.startswith("\u2717") or line.startswith("\u00d7") or line.startswith("\u0394"):
                        detail_items.append(f'<div class="detail-fail">{line}</div>')
                    elif line.startswith("\u26a0"):
                        detail_items.append(f'<div class="detail-warn">{line}</div>')
                    else:
                        detail_items.append(f"<div>{line}</div>")
            details_html = "\n".join(detail_items) if detail_items else ""

            step_rows += f"""
            <tr>
              <td class="step-num">{step.step_number:02d}</td>
              <td>
                <div class="step-service"><strong>{step.service}</strong></div>
              </td>
              <td class="step-details">{details_html}</td>
              <td class="step-status">{_status_badge(step.status)}</td>
            </tr>"""

        # Event timeline
        event_section = ""
        events_for_doc = []
        for step in doc.steps:
            events_for_doc.extend(step.events)
        if events_for_doc:
            event_rows = ""
            for ev in events_for_doc:
                event_rows += f"""
                <tr>
                  <td><code>{ev.received_at}</code></td>
                  <td><code>{ev.event_type}</code></td>
                  <td>{ev.partition_id}</td>
                  <td>{ev.sequence_number}</td>
                </tr>"""
            event_section = f"""
            <div class="event-timeline">
              <h4>Event Hub Events</h4>
              <table class="events-table">
                <thead><tr>
                  <th>Received At</th><th>Event Type</th><th>Partition</th><th>Seq #</th>
                </tr></thead>
                <tbody>{event_rows}</tbody>
              </table>
            </div>"""

        doc_status_bg, _, _ = _STATUS_COLORS[doc.overall_status]
        detail_sections += f"""
        <div class="doc-section">
          <details open>
            <summary>
              <span class="doc-ufid">{doc.ufid}</span>
              <span class="doc-filename">{doc.filename}</span>
              {_status_badge(doc.overall_status)}
            </summary>
            <table class="pipeline-table">
              <thead>
                <tr>
                  <th class="col-num">#</th>
                  <th class="col-service">Step / Service</th>
                  <th class="col-details">Details</th>
                  <th class="col-status">Status</th>
                </tr>
              </thead>
              <tbody>{step_rows}</tbody>
            </table>
            {event_section}
          </details>
        </div>"""

    total_docs = len(report.documents)
    passed = sum(1 for d in report.documents if d.overall_status == StepStatus.PASS)
    failed = sum(1 for d in report.documents if d.overall_status == StepStatus.FAIL)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pipeline Report - {report.run_id}</title>
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
  .container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
  .meta-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px; margin-bottom: 24px;
  }}
  .meta-card {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px;
  }}
  .meta-card .label {{ font-size: 11px; text-transform: uppercase; color: var(--gray); letter-spacing: 0.05em; }}
  .meta-card .value {{ font-size: 14px; font-weight: 600; margin-top: 2px; }}
  .summary-stats {{
    display: flex; gap: 16px; margin-bottom: 24px;
  }}
  .stat {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px 20px; text-align: center; flex: 1;
  }}
  .stat .num {{ font-size: 28px; font-weight: 700; }}
  .stat .lbl {{ font-size: 12px; color: var(--gray); text-transform: uppercase; }}
  .stat.pass .num {{ color: var(--pass); }}
  .stat.fail .num {{ color: var(--fail); }}
  .badge {{
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: 12px; font-weight: 700; letter-spacing: 0.03em;
  }}
  .doc-section {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; margin-bottom: 16px; overflow: hidden;
  }}
  .doc-section summary {{
    padding: 14px 20px; cursor: pointer; display: flex;
    align-items: center; gap: 12px; font-size: 14px;
    border-bottom: 1px solid var(--border);
  }}
  .doc-section summary:hover {{ background: var(--bg); }}
  .doc-ufid {{ font-family: 'SF Mono', Consolas, monospace; font-size: 13px; color: var(--gray); }}
  .doc-filename {{ font-weight: 600; }}
  .pipeline-table {{
    width: 100%; border-collapse: collapse; font-size: 14px;
  }}
  .pipeline-table th {{
    text-align: left; padding: 10px 16px; background: var(--bg);
    font-size: 12px; text-transform: uppercase; color: var(--gray);
    letter-spacing: 0.04em; border-bottom: 1px solid var(--border);
  }}
  .pipeline-table td {{
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    vertical-align: top;
  }}
  .pipeline-table tr:last-child td {{ border-bottom: none; }}
  .col-num {{ width: 50px; }}
  .col-service {{ width: 200px; }}
  .col-status {{ width: 100px; text-align: center; }}
  .step-num {{ text-align: center; font-weight: 600; color: var(--gray); }}
  .step-service {{ font-size: 14px; }}
  .step-details {{ font-size: 13px; color: #4b5563; }}
  .step-status {{ text-align: center; }}
  .detail-pass {{ color: var(--pass); }}
  .detail-fail {{ color: var(--fail); }}
  .detail-warn {{ color: var(--partial); }}
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
  .events-table td {{ padding: 6px 10px; border-bottom: 1px solid var(--border); }}
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
    <div class="meta-grid">
      <div class="meta-card">
        <div class="label">Run ID</div>
        <div class="value">{report.run_id}</div>
      </div>
      <div class="meta-card">
        <div class="label">Environment</div>
        <div class="value">{report.environment}</div>
      </div>
      <div class="meta-card">
        <div class="label">Tenant</div>
        <div class="value">{report.tenant_id}</div>
      </div>
      <div class="meta-card">
        <div class="label">Started</div>
        <div class="value">{report.started_at}</div>
      </div>
    </div>

    <div class="summary-stats">
      <div class="stat">
        <div class="num">{total_docs}</div>
        <div class="lbl">Documents</div>
      </div>
      <div class="stat pass">
        <div class="num">{passed}</div>
        <div class="lbl">Passed</div>
      </div>
      <div class="stat fail">
        <div class="num">{failed}</div>
        <div class="lbl">Failed</div>
      </div>
    </div>

    <h2 style="font-size:18px;margin-bottom:16px;">Pipeline Progression</h2>

    {detail_sections}
  </div>
  <div class="footer">
    Generated by nebula-e2e-tests | {report.ended_at or report.started_at}
  </div>
</body>
</html>"""
