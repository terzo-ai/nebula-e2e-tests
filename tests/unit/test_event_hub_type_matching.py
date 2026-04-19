"""Unit tests for CloudEvents ``type``-based matching on EventHubListener.

Exercises the listener's matching surface without touching Azure — we
inject synthetic ``CapturedEvent`` instances onto the internal buffer
and assert the public helpers behave correctly. Covers:

  * ``has_event_type`` — string + tuple matching, ufid filter
  * ``types_for`` — distinct-in-order semantics
  * ``wait_for_event_type`` — immediate resolution, tuple match, timeout
  * ``EventTimeoutError`` — carries the original matcher verbatim

The action-based API is already exercised implicitly by the pipeline
runner tests; here we focus on the type-based path added in the
CloudEvents migration.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from lib.event_hub import CapturedEvent, EventHubListener, EventTimeoutError


UFID = "test-ufid-0000"
OTHER_UFID = "test-ufid-9999"


def _listener() -> EventHubListener:
    """A listener that never talks to Azure — we only poke its buffers."""
    listener = EventHubListener(
        connection_string="Endpoint=sb://fake/;SharedAccessKeyName=x;SharedAccessKey=y",
        event_hub_name="fake-hub",
    )
    # Needed for wait_for_event_type's async event signaling.
    listener._new_event = asyncio.Event()
    return listener


def _event(
    ufid: str,
    event_type: str,
    action: str = "",
    seq: int = 1,
) -> CapturedEvent:
    return CapturedEvent(
        event_id=f"id-{seq}",
        event_type=event_type,
        action=action,
        document_id=ufid,
        timestamp=time.monotonic(),
        received_at="2026-04-19T12:00:00+00:00",
        sequence_number=seq,
        partition_id="fake-hub/0",
        payload={"type": event_type, "subject": ufid},
    )


# ---------------------------------------------------------------------------
# has_event_type
# ---------------------------------------------------------------------------


def test_has_event_type_matches_single_type() -> None:
    listener = _listener()
    listener._events.append(_event(UFID, "com.terzo.document.ocr.queued"))

    assert listener.has_event_type(UFID, "com.terzo.document.ocr.queued")
    assert not listener.has_event_type(UFID, "com.terzo.document.ocr.completed")


def test_has_event_type_accepts_tuple_of_types() -> None:
    listener = _listener()
    listener._events.append(_event(UFID, "com.terzo.document.uploaded"))

    # Tuple-match: any one of the listed types satisfies the check.
    assert listener.has_event_type(
        UFID,
        ("com.terzo.document.upload.queued", "com.terzo.document.uploaded"),
    )
    assert not listener.has_event_type(
        UFID,
        ("com.terzo.document.ocr.queued", "com.terzo.document.ocr.completed"),
    )


def test_has_event_type_is_scoped_to_ufid() -> None:
    listener = _listener()
    listener._events.append(_event(OTHER_UFID, "com.terzo.document.uploaded"))

    # Other doc's event must not leak into our ufid's match set.
    assert not listener.has_event_type(UFID, "com.terzo.document.uploaded")
    assert listener.has_event_type(OTHER_UFID, "com.terzo.document.uploaded")


# ---------------------------------------------------------------------------
# types_for
# ---------------------------------------------------------------------------


def test_types_for_returns_distinct_types_in_arrival_order() -> None:
    listener = _listener()
    listener._events.extend([
        _event(UFID, "com.terzo.document.upload.queued", seq=1),
        _event(UFID, "com.terzo.document.uploaded", seq=2),
        # Duplicate of the first type — should be collapsed.
        _event(UFID, "com.terzo.document.upload.queued", seq=3),
        _event(UFID, "com.terzo.document.ocr.queued", seq=4),
    ])

    assert listener.types_for(UFID) == [
        "com.terzo.document.upload.queued",
        "com.terzo.document.uploaded",
        "com.terzo.document.ocr.queued",
    ]


def test_types_for_excludes_other_ufids() -> None:
    listener = _listener()
    listener._events.extend([
        _event(UFID, "com.terzo.document.uploaded", seq=1),
        _event(OTHER_UFID, "com.terzo.document.ocr.queued", seq=2),
    ])

    assert listener.types_for(UFID) == ["com.terzo.document.uploaded"]


# ---------------------------------------------------------------------------
# wait_for_event_type
# ---------------------------------------------------------------------------


async def test_wait_for_event_type_returns_immediately_when_already_captured() -> None:
    listener = _listener()
    already = _event(UFID, "com.terzo.document.ocr.completed", seq=7)
    listener._events.append(already)

    # Already in the buffer → must resolve on the first scan without
    # waiting out the timeout.
    start = time.monotonic()
    event = await listener.wait_for_event_type(
        UFID, "com.terzo.document.ocr.completed", timeout=5.0
    )
    assert event is already
    assert (time.monotonic() - start) < 1.0, "should not have waited"


async def test_wait_for_event_type_matches_any_from_tuple() -> None:
    listener = _listener()
    listener._events.append(_event(UFID, "com.terzo.document.uploaded", seq=3))

    event = await listener.wait_for_event_type(
        UFID,
        (
            "com.terzo.document.upload.queued",
            "com.terzo.document.uploaded",
        ),
        timeout=5.0,
    )
    assert event.event_type == "com.terzo.document.uploaded"


async def test_wait_for_event_type_times_out_with_matcher_in_error() -> None:
    listener = _listener()
    # Buffer is empty → wait must hit the deadline and raise.
    missing_type = "com.terzo.document.auto_extraction.completed"

    with pytest.raises(EventTimeoutError) as exc_info:
        await listener.wait_for_event_type(UFID, missing_type, timeout=0.5)

    err = exc_info.value
    assert err.ufid == UFID
    # The matcher surfaces verbatim on the exception — critical for CI
    # diagnostics so the missing type is obvious from stacktrace alone.
    assert err.matcher == missing_type
    # Back-compat: .action alias still resolves to the same matcher so
    # any caller that still reads ``.action`` keeps working.
    assert err.action == missing_type
    assert missing_type in str(err)
