"""Event Hub listener: captures pipeline events during E2E test execution.

Connects to Azure Event Hub, listens for events matching watched document IDs,
and captures them for assertion and reporting. Supports duplicate detection.

Usage:
    async with EventHubListener(conn_str, hub_name) as listener:
        listener.watch(ufid)
        # ... run test pipeline ...
        assert listener.has_event(ufid, "ocr.queued")
        assert not listener.find_duplicates()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CapturedEvent:
    event_type: str
    document_id: str
    timestamp: float  # time.monotonic() when received
    received_at: str  # ISO 8601 wall-clock time
    sequence_number: int
    partition_id: str
    payload: dict[str, Any]


class EventTimeoutError(Exception):
    def __init__(self, ufid: str, event_type: str, timeout: float):
        self.ufid = ufid
        self.event_type = event_type
        self.timeout = timeout
        super().__init__(
            f"Timed out waiting for event '{event_type}' "
            f"for document {ufid} after {timeout}s"
        )


class EventHubListener:
    """Async context manager that listens to one or more Event Hubs.

    Accepts a single hub name or a list of hub names (all under the same
    Event Hub namespace / connection string). Spins up one
    EventHubConsumerClient per hub and merges captured events into a
    single timeline, so callers can use watch()/wait_for_event() without
    caring which hub an event came from.
    """

    def __init__(
        self,
        connection_string: str,
        event_hub_name: str | list[str],
        consumer_group: str = "probe-test",
        timeout: float = 120.0,
    ) -> None:
        self._connection_string = connection_string
        if isinstance(event_hub_name, str):
            # Support comma-separated strings for easy env-var wiring.
            names = [n.strip() for n in event_hub_name.split(",") if n.strip()]
        else:
            names = [n for n in event_hub_name if n]
        if not names:
            raise ValueError("event_hub_name must contain at least one non-empty hub name")
        self._event_hub_names: list[str] = names
        self._consumer_group = consumer_group
        self._timeout = timeout
        self._watched_ufids: set[str] = set()
        self._events: list[CapturedEvent] = []
        self._consumers: list[Any] = []
        self._receive_tasks: list[asyncio.Task] = []
        self._new_event = asyncio.Event()

    async def __aenter__(self) -> EventHubListener:
        try:
            from azure.eventhub.aio import EventHubConsumerClient
        except ImportError:
            raise RuntimeError(
                "azure-eventhub is required for Event Hub listening. "
                "Install with: uv add azure-eventhub"
            )

        for hub_name in self._event_hub_names:
            consumer = EventHubConsumerClient.from_connection_string(
                self._connection_string,
                consumer_group=self._consumer_group,
                eventhub_name=hub_name,
            )
            self._consumers.append(consumer)
            self._receive_tasks.append(
                asyncio.create_task(self._receive_loop(consumer, hub_name))
            )
            logger.info(
                "Event Hub listener started: hub=%s group=%s",
                hub_name, self._consumer_group,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for task in self._receive_tasks:
            if not task.done():
                task.cancel()
        for task in self._receive_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Event Hub receive task ended with error")
        for consumer in self._consumers:
            try:
                await consumer.close()
            except Exception:
                logger.exception("Error closing EventHubConsumerClient")
        logger.info(
            "Event Hub listener stopped. Captured %d event(s) across %d hub(s).",
            len(self._events), len(self._event_hub_names),
        )

    async def _receive_loop(self, consumer: Any, hub_name: str) -> None:
        """Background task: receive events from all partitions of one hub."""
        async def on_event(partition_context, event):
            if event is None:
                return
            try:
                body = event.body_as_str()
                payload = json.loads(body) if body else {}
            except (json.JSONDecodeError, Exception):
                payload = {"_raw": event.body_as_str() if event else ""}

            # Extract event type and document ID from the payload.
            # Supports CloudEvents v1.0 and flat event formats.
            event_type = (
                payload.get("type")
                or payload.get("eventType")
                or payload.get("event_type", "unknown")
            )
            data = payload.get("data", payload)
            doc_id = (
                data.get("documentId")
                or data.get("ufid")
                or data.get("document_id", "")
            )

            if doc_id in self._watched_ufids:
                captured = CapturedEvent(
                    event_type=event_type,
                    document_id=doc_id,
                    timestamp=time.monotonic(),
                    received_at=datetime.now(timezone.utc).isoformat(),
                    sequence_number=event.sequence_number or 0,
                    partition_id=f"{hub_name}/{partition_context.partition_id}",
                    payload=payload,
                )
                self._events.append(captured)
                self._new_event.set()
                self._new_event.clear()
                logger.info(
                    "Captured event: hub=%s type=%s doc=%s partition=%s seq=%d",
                    hub_name, event_type, doc_id,
                    partition_context.partition_id, captured.sequence_number,
                )

            await partition_context.update_checkpoint(event)

        try:
            await consumer.receive(
                on_event=on_event,
                starting_position="@latest",
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Event Hub receive loop error (hub=%s)", hub_name)

    def watch(self, ufid: str) -> None:
        """Register a document ID to capture events for."""
        self._watched_ufids.add(ufid)

    @property
    def events(self) -> list[CapturedEvent]:
        """All captured events, ordered by receipt time."""
        return sorted(self._events, key=lambda e: e.timestamp)

    def events_for(self, ufid: str) -> list[CapturedEvent]:
        """Events filtered to a specific document ID."""
        return [e for e in self.events if e.document_id == ufid]

    def has_event(self, ufid: str, event_type: str) -> bool:
        """Check if a specific event type was captured for a document."""
        return any(
            e.document_id == ufid and e.event_type == event_type
            for e in self._events
        )

    def find_duplicates(self) -> list[tuple[CapturedEvent, CapturedEvent]]:
        """Find pairs of events with same (document_id, event_type)."""
        seen: dict[tuple[str, str], CapturedEvent] = {}
        duplicates: list[tuple[CapturedEvent, CapturedEvent]] = []
        for event in self.events:
            key = (event.document_id, event.event_type)
            if key in seen:
                duplicates.append((seen[key], event))
            else:
                seen[key] = event
        return duplicates

    async def wait_for_event(
        self,
        ufid: str,
        event_type: str,
        timeout: float | None = None,
    ) -> CapturedEvent:
        """Block until a specific event arrives or timeout."""
        timeout = timeout or self._timeout
        deadline = time.monotonic() + timeout

        while True:
            for event in self._events:
                if event.document_id == ufid and event.event_type == event_type:
                    return event

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EventTimeoutError(ufid, event_type, timeout)

            try:
                await asyncio.wait_for(self._new_event.wait(), timeout=min(remaining, 2.0))
            except asyncio.TimeoutError:
                pass
