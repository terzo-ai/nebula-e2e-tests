"""Event Hub listener: captures pipeline events during E2E test execution.

Connects to Azure Event Hub, listens for events matching watched document IDs,
and captures them for assertion and reporting. Events are tracked by their
``data.action`` field (e.g. UPLOADED, OCR_COMPLETED) and CloudEvents ``id``.

Usage:
    async with EventHubListener(conn_str, hub_name) as listener:
        listener.watch(ufid)
        # ... run test pipeline ...
        assert listener.has_event(ufid, "OCR_QUEUED")
        assert not listener.find_duplicates()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CapturedEvent:
    event_id: str  # CloudEvents "id" field
    action: str  # data.action (e.g. "OCR_COMPLETED")
    document_id: str  # data.document_id
    timestamp: float  # time.monotonic() when received
    received_at: str  # ISO 8601 wall-clock time
    sequence_number: int
    partition_id: str
    payload: dict[str, Any]


class EventTimeoutError(Exception):
    def __init__(self, ufid: str, action: str, timeout: float):
        self.ufid = ufid
        self.action = action
        self.timeout = timeout
        super().__init__(
            f"Timed out waiting for action '{action}' "
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

    # Default partition IDs to subscribe to. Azure Event Hub's "all partitions"
    # receive mode (omitting partition_id) is broken in this environment — the
    # portal shows the same bug in the "All partition IDs" dropdown option.
    # We work around it by explicitly fanning out one receive task per partition.
    DEFAULT_PARTITION_IDS: tuple[str, ...] = ("0", "1", "2", "3")

    # After spawning receive tasks, pause briefly before returning from
    # __aenter__ so the AMQP links for every partition have time to open and
    # subscribe. Without this, a caller can fire an API request fast enough
    # that the first few pipeline events are emitted before the receivers
    # are actually listening, causing flaky missed-event failures.
    STARTUP_WARMUP_SECONDS: float = 2.0

    # Fail-safe lookback: start reading the stream from N minutes before
    # "now" so that if the AMQP subscribe is slower than STARTUP_WARMUP_SECONDS
    # (or the API is called before __aenter__ completes), we still replay any
    # events the producer emitted in the immediate past. 10 minutes comfortably
    # covers slow subscribes without pulling in stale history from prior runs —
    # the event filter in on_event() only captures events for watched ufids
    # anyway, and a ufid is only created by this run.
    STARTUP_LOOKBACK_MINUTES: float = 10.0

    def __init__(
        self,
        connection_string: str,
        event_hub_name: str | list[str],
        consumer_group: str = "terzo-ai-nebula-e2e-tests-probe",
        timeout: float = 120.0,
        partition_ids: list[str] | tuple[str, ...] | None = None,
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
        self._partition_ids: tuple[str, ...] = tuple(
            partition_ids if partition_ids is not None else self.DEFAULT_PARTITION_IDS
        )
        if not self._partition_ids:
            raise ValueError("partition_ids must contain at least one partition id")
        self._watched_ufids: set[str] = set()
        self._events: list[CapturedEvent] = []
        self._consumers: list[Any] = []
        self._receive_tasks: list[asyncio.Task] = []
        self._new_event = asyncio.Event()
        # Set on __aenter__ to "now - STARTUP_LOOKBACK_MINUTES".
        self._starting_position: datetime | None = None
        # Diagnostic counters — bumped for *every* inbound event, not just
        # ones matching a watched ufid, so log output can distinguish
        # "receiver is silent" from "receiver is alive but filtering".
        self._total_events_received: int = 0
        self._events_per_partition: dict[str, int] = {}

    async def __aenter__(self) -> EventHubListener:
        try:
            from azure.eventhub.aio import EventHubConsumerClient
        except ImportError:
            raise RuntimeError(
                "azure-eventhub is required for Event Hub listening. "
                "Install with: uv add azure-eventhub"
            )

        # Fail-safe: subscribe from N minutes before "now" so events emitted
        # during the AMQP subscribe window are still replayed to us.
        self._starting_position = datetime.now(timezone.utc) - timedelta(
            minutes=self.STARTUP_LOOKBACK_MINUTES
        )

        for hub_name in self._event_hub_names:
            # Create a separate consumer per partition. The Azure SDK's
            # consumer.receive() uses an internal EventProcessor that holds
            # a lock — calling receive() multiple times on the same consumer
            # for different partitions causes all but the first to block
            # silently (on_event never fires). One consumer per partition
            # avoids this entirely.
            for partition_id in self._partition_ids:
                consumer = EventHubConsumerClient.from_connection_string(
                    self._connection_string,
                    consumer_group=self._consumer_group,
                    eventhub_name=hub_name,
                )
                self._consumers.append(consumer)
                self._receive_tasks.append(
                    asyncio.create_task(
                        self._receive_loop(consumer, hub_name, partition_id)
                    )
                )
            logger.info(
                "Event Hub listener started: hub=%s group=%s partitions=%s "
                "starting_from=%s",
                hub_name, self._consumer_group, ",".join(self._partition_ids),
                self._starting_position,
            )

        # Give AMQP links time to open and subscribe before the caller fires
        # the first producer-side request.
        if self.STARTUP_WARMUP_SECONDS > 0:
            await asyncio.sleep(self.STARTUP_WARMUP_SECONDS)
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

    async def _receive_loop(
        self, consumer: Any, hub_name: str, partition_id: str
    ) -> None:
        """Background task: receive events from a single partition of one hub."""
        async def on_event(partition_context, event):
            if event is None:
                return
            try:
                body = event.body_as_str()
                payload = json.loads(body) if body else {}
            except (json.JSONDecodeError, Exception):
                payload = {"_raw": event.body_as_str() if event else ""}

            # Log raw event body for debugging — shows ALL inbound events
            logger.warning(
                "[EventHub RAW] hub=%s partition=%s seq=%s body=%s",
                hub_name, partition_context.partition_id,
                event.sequence_number, body[:500],
            )

            # Extract action, document ID, and CloudEvents id from the payload.
            data = payload.get("data", payload)
            action = data.get("action", "")
            doc_id = (
                data.get("documentId")
                or data.get("ufid")
                or data.get("document_id", "")
            )
            event_id = payload.get("id", "")

            # Diagnostic bookkeeping: count *every* inbound event so logs can
            # prove the receiver is actually live even when nothing matches a
            # watched ufid.
            self._total_events_received += 1
            part_key = f"{hub_name}/{partition_context.partition_id}"
            self._events_per_partition[part_key] = (
                self._events_per_partition.get(part_key, 0) + 1
            )
            logger.debug(
                "Inbound event: hub=%s partition=%s action=%s doc=%s id=%s seq=%s watched=%s",
                hub_name, partition_context.partition_id, action, doc_id,
                event_id, event.sequence_number, doc_id in self._watched_ufids,
            )

            if doc_id in self._watched_ufids:
                captured = CapturedEvent(
                    event_id=event_id,
                    action=action,
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
                print(
                    f"  [EventHub] id={event_id} action={action} "
                    f"document_id={doc_id} partition={partition_context.partition_id}"
                )
                logger.info(
                    "Captured event: hub=%s action=%s id=%s doc=%s partition=%s seq=%d",
                    hub_name, action, event_id, doc_id,
                    partition_context.partition_id, captured.sequence_number,
                )

            await partition_context.update_checkpoint(event)

        logger.warning(
            "Starting receive loop: hub=%s partition=%s group=%s starting_from=%s",
            hub_name, partition_id, self._consumer_group, self._starting_position,
        )
        try:
            await consumer.receive(
                on_event=on_event,
                partition_id=partition_id,
                starting_position=self._starting_position,
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "Event Hub receive loop error (hub=%s partition=%s)",
                hub_name, partition_id,
            )

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

    def has_event(self, ufid: str, action: str | tuple[str, ...]) -> bool:
        """Check if a specific action was captured for a document.

        Accepts a single action string or a tuple of action strings
        (matches if any of them was captured).
        """
        if isinstance(action, tuple):
            return any(
                e.document_id == ufid and e.action in action
                for e in self._events
            )
        return any(
            e.document_id == ufid and e.action == action
            for e in self._events
        )

    def actions_for(self, ufid: str) -> list[str]:
        """Distinct actions captured for a ufid, in arrival order.

        Primary diagnostic for "what did the listener actually see for
        this document?" Used by waits/timeouts and the end-of-test summary
        so CI logs always show the observed event stream for the ufid.
        """
        seen: list[str] = []
        for event in self.events_for(ufid):
            if event.action not in seen:
                seen.append(event.action)
        return seen

    def find_duplicates(self) -> list[tuple[CapturedEvent, CapturedEvent]]:
        """Find pairs of events with same (document_id, action)."""
        seen: dict[tuple[str, str], CapturedEvent] = {}
        duplicates: list[tuple[CapturedEvent, CapturedEvent]] = []
        for event in self.events:
            key = (event.document_id, event.action)
            if key in seen:
                duplicates.append((seen[key], event))
            else:
                seen[key] = event
        return duplicates

    # How often wait_for_event emits a "still waiting" heartbeat line.
    WAIT_HEARTBEAT_SECONDS: float = 15.0

    async def wait_for_any_event(
        self,
        ufid: str,
        timeout: float | None = None,
    ) -> CapturedEvent:
        """Block until ANY event for a ufid arrives, or timeout.

        Used to confirm the Event Hub listener is actively receiving events
        for the watched document — the first event (typically action=UPLOADED)
        is enough to prove the listener is wired up and the producer is
        emitting. The per-stage timeout resets on each call.
        """
        # Sentinel action "" signals "match first event for this ufid".
        return await self.wait_for_event(ufid, action="", timeout=timeout)

    async def wait_for_event(
        self,
        ufid: str,
        action: str | tuple[str, ...],
        timeout: float | None = None,
    ) -> CapturedEvent:
        """Block until an event with a matching action arrives or timeout.

        If ``action`` is an empty string, the first event captured for
        ``ufid`` (regardless of action) satisfies the wait.
        If ``action`` is a tuple, any of the listed actions satisfies the wait.
        """
        timeout = timeout or self._timeout
        deadline = time.monotonic() + timeout
        hubs = ",".join(self._event_hub_names)
        action_label = action if action else "<any>"
        logger.info(
            "Waiting for event: action=%s ufid=%s timeout=%.1fs "
            "(hubs=%s group=%s partitions=%s total_events=%d per_partition=%s "
            "captured_actions_for_ufid=%s)",
            action_label, ufid, timeout,
            hubs, self._consumer_group, ",".join(self._partition_ids),
            self._total_events_received, self._events_per_partition,
            self.actions_for(ufid),
        )
        last_heartbeat = time.monotonic()

        def _matches(event: CapturedEvent) -> bool:
            if event.document_id != ufid:
                return False
            if not action:
                return True
            if isinstance(action, tuple):
                return event.action in action
            return event.action == action

        while True:
            for event in self._events:
                if _matches(event):
                    logger.info(
                        "Resolved event: action=%s ufid=%s actual_action=%s "
                        "id=%s hub=%s partition=%s seq=%d",
                        action_label, ufid, event.action, event.event_id,
                        hubs, event.partition_id, event.sequence_number,
                    )
                    return event

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "Timed out waiting for event: action=%s ufid=%s after %.1fs "
                    "(hubs=%s group=%s total_events=%d per_partition=%s "
                    "captured_for_ufid=%d captured_actions_for_ufid=%s)",
                    action_label, ufid, timeout,
                    hubs, self._consumer_group,
                    self._total_events_received, self._events_per_partition,
                    len(self.events_for(ufid)),
                    self.actions_for(ufid),
                )
                raise EventTimeoutError(ufid, str(action) or "<any>", timeout)

            # Periodic heartbeat so a long wait is observable in logs rather
            # than appearing hung.
            now = time.monotonic()
            if now - last_heartbeat >= self.WAIT_HEARTBEAT_SECONDS:
                logger.info(
                    "Still waiting: action=%s ufid=%s elapsed=%.0fs remaining=%.0fs "
                    "(hubs=%s group=%s total_events=%d per_partition=%s "
                    "captured_for_ufid=%d captured_actions_for_ufid=%s)",
                    action_label, ufid, timeout - remaining, remaining,
                    hubs, self._consumer_group,
                    self._total_events_received, self._events_per_partition,
                    len(self.events_for(ufid)),
                    self.actions_for(ufid),
                )
                last_heartbeat = now

            try:
                await asyncio.wait_for(self._new_event.wait(), timeout=min(remaining, 2.0))
            except asyncio.TimeoutError:
                pass
