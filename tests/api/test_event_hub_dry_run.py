"""Dry-run: connect to Event Hub and print all events for 60 seconds.

No bulk-upload, no auth, no pipeline stages — just listen to
terzo-ai-contract-document-events and dump every event body to console.
Proves the connection string, hub name, and consumer group are correct.

Set TARGET_UFID to filter for a specific document's events.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from lib.config import E2EConfig

logger = logging.getLogger(__name__)

# How long to listen before stopping.
LISTEN_SECONDS = 60

# Set to a ufid to filter — only events for this document are printed.
# Set to "" to print ALL events.
TARGET_UFID = "5f2f04d9-77be-4606-9b2d-224c1f40ee0b"

# How far back to look (hours). Event Hub retention must cover this.
LOOKBACK_HOURS = 24


async def test_event_hub_dry_run(config: E2EConfig) -> None:
    """Connect to Event Hub and print every event received."""
    if not config.event_hub_connection_string:
        pytest.skip("E2E_EVENT_HUB_CONNECTION_STRING not set")

    try:
        from azure.eventhub.aio import EventHubConsumerClient
    except ImportError:
        pytest.skip("azure-eventhub not installed")

    hub_name = "terzo-ai-contract-document-events"
    consumer_group = config.event_hub_consumer_group
    partition_ids = ("0", "1", "2", "3")

    starting_position = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    event_count = 0
    matched_count = 0

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

        # If filtering for a specific ufid, skip non-matching events
        if TARGET_UFID and doc_id != TARGET_UFID:
            return

        matched_count += 1
        logger.warning(
            "[MATCH #%d] partition=%s seq=%s event_id=%s action=%s doc_id=%s",
            matched_count, partition_context.partition_id,
            event.sequence_number, event_id, action, doc_id,
        )
        logger.warning("  body=%s", body[:1000])

    logger.warning("Connecting to Event Hub: %s", hub_name)
    logger.warning("Consumer group: %s", consumer_group)
    logger.warning("Lookback: %d hours from %s", LOOKBACK_HOURS, starting_position.isoformat())
    logger.warning("Target UFID: %s", TARGET_UFID or "(all)")
    logger.warning("Listening for %ds...", LISTEN_SECONDS)

    # One consumer per partition for reliable receive
    consumers = []
    tasks = []
    try:
        for pid in partition_ids:
            consumer = EventHubConsumerClient.from_connection_string(
                config.event_hub_connection_string,
                consumer_group=consumer_group,
                eventhub_name=hub_name,
            )
            consumers.append(consumer)
            tasks.append(
                asyncio.create_task(
                    consumer.receive(
                        on_event=on_event,
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
        "Done. Total events scanned: %d, matched: %d (ufid=%s)",
        event_count, matched_count, TARGET_UFID or "all",
    )
