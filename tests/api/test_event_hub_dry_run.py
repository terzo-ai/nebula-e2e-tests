"""Dry-run: connect to Event Hub and print all events for 30 seconds.

No bulk-upload, no auth, no pipeline stages — just listen to
terzo-ai-contract-document-events and dump every event body to console.
Proves the connection string, hub name, and consumer group are correct.
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
LISTEN_SECONDS = 30


async def test_event_hub_dry_run(config: E2EConfig) -> None:
    """Connect to Event Hub and print every event received for 30s."""
    if not config.event_hub_connection_string:
        pytest.skip("E2E_EVENT_HUB_CONNECTION_STRING not set")

    try:
        from azure.eventhub.aio import EventHubConsumerClient
    except ImportError:
        pytest.skip("azure-eventhub not installed")

    hub_name = "terzo-ai-contract-document-events"
    consumer_group = "$Default"
    partition_ids = ("0", "1", "2", "3")

    # Start reading from 5 minutes ago to catch recent events.
    starting_position = datetime.now(timezone.utc) - timedelta(minutes=5)

    event_count = 0

    async def on_event(partition_context, event):
        nonlocal event_count
        if event is None:
            return
        event_count += 1
        try:
            body = event.body_as_str()
            payload = json.loads(body) if body else {}
        except Exception:
            body = event.body_as_str() if event else ""
            payload = {}

        # Extract key fields
        data = payload.get("data", {})
        action = data.get("action", "")
        doc_id = (
            data.get("documentId")
            or data.get("ufid")
            or data.get("document_id", "")
        )
        event_id = payload.get("id", "")

        logger.warning(
            "[DRY-RUN #%d] partition=%s seq=%s event_id=%s action=%s doc_id=%s",
            event_count, partition_context.partition_id,
            event.sequence_number, event_id, action, doc_id,
        )
        logger.warning("  body=%s", body[:800])

    logger.warning("Connecting to Event Hub: %s", hub_name)
    logger.warning("Consumer group: %s", consumer_group)
    logger.warning("Starting position: %s", starting_position.isoformat())
    logger.warning("Listening for %ds...", LISTEN_SECONDS)

    consumer = EventHubConsumerClient.from_connection_string(
        config.event_hub_connection_string,
        consumer_group=consumer_group,
        eventhub_name=hub_name,
    )

    tasks = []
    try:
        for pid in partition_ids:
            tasks.append(
                asyncio.create_task(
                    consumer.receive(
                        on_event=on_event,
                        partition_id=pid,
                        starting_position=starting_position,
                    )
                )
            )

        # Let it run for LISTEN_SECONDS, then stop.
        await asyncio.sleep(LISTEN_SECONDS)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        await consumer.close()

    logger.warning("Done. Total events received: %d", event_count)

    if event_count == 0:
        logger.warning(
            "Zero events received. Check connection string, hub name, and consumer group."
        )
