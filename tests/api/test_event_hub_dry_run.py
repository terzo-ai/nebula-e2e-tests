"""Dry-run: connect to Event Hub and print all events for 60 seconds.

No bulk-upload, no auth, no pipeline stages — just listen to every child
hub and dump event bodies to console.
Proves the connection string, hub names, and consumer group are correct.

Set TARGET_UFIDS to filter for one or more documents.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from lib.config import E2EConfig

logger = logging.getLogger(__name__)

LISTEN_SECONDS = 60

TARGET_UFIDS: tuple[str, ...] = ()

HUB_NAMES: tuple[str, ...] = (
    "terzo-ai-contract-document-events",
    "terzo-ai-contract-metadata-events",
    "terzo-ai-contract-outbox-events",
    "terzo-ai-ocr-events",
    "terzo-ai-platforms-file-ingest-events",
)

LOOKBACK_HOURS = 2


async def test_event_hub_dry_run(config: E2EConfig) -> None:
    """Connect to all child hubs and print every matching event."""
    if not config.event_hub_connection_string:
        pytest.skip("E2E_EVENT_HUB_CONNECTION_STRING not set")

    try:
        from azure.eventhub.aio import EventHubConsumerClient
    except ImportError:
        pytest.skip("azure-eventhub not installed")

    consumer_group = config.event_hub_consumer_group
    partition_ids = ("0", "1", "2", "3")

    starting_position = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    target_set = {u for u in TARGET_UFIDS if u}
    event_count = 0
    matched_count = 0
    per_ufid_counts: dict[str, int] = {u: 0 for u in target_set}

    def make_on_event(hub: str):
        async def on_event(partition_context, event):
            nonlocal event_count, matched_count
            if event is None:
                return
            event_count += 1
            try:
                body = event.body_as_str()
                payload = json.loads(body) if body else {}
            except Exception:
                body = event.body_as_str() if event else ""
                payload = {}

            data = payload.get("data", {})
            action = data.get("action", "")
            doc_id = (
                data.get("documentId")
                or data.get("ufid")
                or data.get("document_id", "")
            )
            event_id = payload.get("id", "")

            if target_set and doc_id not in target_set:
                return

            matched_count += 1
            if doc_id in per_ufid_counts:
                per_ufid_counts[doc_id] += 1
            logger.warning(
                "[MATCH #%d] hub=%s partition=%s seq=%s event_id=%s action=%s doc_id=%s",
                matched_count, hub, partition_context.partition_id,
                event.sequence_number, event_id, action, doc_id,
            )
            logger.warning("  body=%s", body[:1200])

        return on_event

    logger.warning("Hubs: %s", ", ".join(HUB_NAMES))
    logger.warning("Consumer group: %s", consumer_group)
    logger.warning("Lookback: %d hours from %s", LOOKBACK_HOURS, starting_position.isoformat())
    logger.warning("Target UFIDs: %s", ", ".join(target_set) if target_set else "(all)")
    logger.warning("Listening for %ds across %d hubs × %d partitions...",
                   LISTEN_SECONDS, len(HUB_NAMES), len(partition_ids))

    consumers = []
    tasks = []
    try:
        for hub in HUB_NAMES:
            handler = make_on_event(hub)
            for pid in partition_ids:
                consumer = EventHubConsumerClient.from_connection_string(
                    config.event_hub_connection_string,
                    consumer_group=consumer_group,
                    eventhub_name=hub,
                )
                consumers.append(consumer)
                tasks.append(
                    asyncio.create_task(
                        consumer.receive(
                            on_event=handler,
                            partition_id=pid,
                            starting_position=starting_position,
                        )
                    )
                )

        await asyncio.sleep(LISTEN_SECONDS)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        for c in consumers:
            await c.close()

    logger.warning(
        "Done. Total events scanned: %d, matched: %d (targets=%s)",
        event_count, matched_count,
        ", ".join(f"{u}={n}" for u, n in per_ufid_counts.items()) or "all",
    )
