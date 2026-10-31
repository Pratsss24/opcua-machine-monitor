"""OPC UA client: browse the machine, subscribe, log every change to SQLite.

Subscription, not polling. A poll at any interval misses every change between
polls, and on a machine the short states are the interesting ones -- a 40
second alarm that a 60 second poll never sees is 40 seconds of downtime that
never reaches the OEE number. A subscription asks the server to push each
change, with a queue so bursts survive.

The tag list is discovered by browsing, not hard-coded, so pointing this at a
machine with extra nodes picks them up without a code change.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from asyncua import Client, ua

import store

LOGGER = logging.getLogger("cnc.collector")

# How often the server is asked to publish. 200 ms is comfortably faster than
# anything a machining centre changes meaningfully, without flooding.
PUBLISH_INTERVAL_MS = 200
QUEUE_SIZE = 200


class SubscriptionHandler:
    """Receives notifications on the asyncua task and hands them to a queue.

    Nothing slow happens here. If this callback blocked on a database write,
    the subscription would fall behind and the server would start dropping
    notifications from its queue.
    """

    def __init__(self, queue: asyncio.Queue, names: dict[ua.NodeId, str]) -> None:
        self.queue = queue
        self.names = names
        self.dropped = 0

    def datachange_notification(self, node, value, data) -> None:
        received = datetime.now(timezone.utc)
        tag = self.names.get(node.nodeid, str(node))
        header = data.monitored_item.Value
        source_ts = header.SourceTimestamp or received
        server_ts = header.ServerTimestamp
        status = "Good" if header.StatusCode is None or header.StatusCode.is_good() else str(
            header.StatusCode
        )
        if source_ts.tzinfo is None:
            source_ts = source_ts.replace(tzinfo=timezone.utc)
        if server_ts is not None and server_ts.tzinfo is None:
            server_ts = server_ts.replace(tzinfo=timezone.utc)

        sample = store.Sample(
            tag=tag,
            value=value,
            source_ts=source_ts,
            server_ts=server_ts,
            received_ts=received,
            status=status,
        )
        try:
            self.queue.put_nowait(sample)
        except asyncio.QueueFull:
            # Say so rather than silently losing data: a dropped sample is a
            # hole in the OEE calculation, not a cosmetic problem.
            self.dropped += 1
            LOGGER.warning("local queue full, dropped a %s sample", tag)


async def discover_variables(client: Client) -> dict:
    """Browse Objects/Machine and return {NodeId: tag name} for every variable."""
    objects = client.nodes.objects
    machine = None
    for child in await objects.get_children():
        name = await child.read_browse_name()
        if name.Name == "Machine":
            machine = child
            break
    if machine is None:
        raise RuntimeError("no Machine object found under Objects - wrong server?")

    found: dict = {}
    for folder in await machine.get_children():
        folder_name = (await folder.read_browse_name()).Name
        for node in await folder.get_children():
            node_class = await node.read_node_class()
            if node_class != ua.NodeClass.Variable:
                continue
            tag = (await node.read_browse_name()).Name
            found[node] = tag
            LOGGER.debug("found %s/%s", folder_name, tag)
    return found


async def _writer(conn, session_id: int, queue: asyncio.Queue, flush_seconds: float) -> int:
    """Drain the queue into SQLite in batches until cancelled."""
    written = 0
    pending: list[store.Sample] = []
    while True:
        try:
            sample = await asyncio.wait_for(queue.get(), timeout=flush_seconds)
            pending.append(sample)
            # Opportunistically take whatever else is already waiting.
            while not queue.empty() and len(pending) < 500:
                pending.append(queue.get_nowait())
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            written += store.write_samples(conn, session_id, pending)
            raise
        if pending:
            written += store.write_samples(conn, session_id, pending)
            pending = []


async def collect(
    endpoint: str,
    db_path: Path,
    duration: float | None = None,
    flush_seconds: float = 1.0,
) -> dict:
    """Connect, subscribe to everything under Machine, log until done."""
    conn = store.connect(db_path)
    queue: asyncio.Queue = asyncio.Queue(maxsize=5000)

    async with Client(url=endpoint) as client:
        server_name = None
        try:
            server_name = (await client.nodes.server_state.read_browse_name()).Name
        except Exception:  # noqa: BLE001 - name is cosmetic, never fail the run for it
            pass

        variables = await discover_variables(client)
        names = {node.nodeid: tag for node, tag in variables.items()}
        LOGGER.info("subscribing to %d variables", len(names))

        session_id = store.start_session(conn, endpoint, server_name)
        handler = SubscriptionHandler(queue, names)
        subscription = await client.create_subscription(PUBLISH_INTERVAL_MS, handler)
        # queuesize > 1 keeps bursts that arrive inside one publishing cycle;
        # with the default of 1 the server overwrites and the burst is lost.
        await subscription.subscribe_data_change(
            list(variables), queuesize=QUEUE_SIZE
        )

        writer = asyncio.create_task(_writer(conn, session_id, queue, flush_seconds))
        try:
            if duration is None:
                while True:
                    await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(duration)
        except (KeyboardInterrupt, asyncio.CancelledError):
            LOGGER.info("stopping on request")
        finally:
            # Give the last notifications a moment to arrive before tearing down.
            await asyncio.sleep(min(1.0, flush_seconds))
            await subscription.delete()
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass
            store.finish_session(conn, session_id)

    info = store.session_info(conn, session_id)
    info["dropped"] = handler.dropped
    info["latency"] = store.latency_stats(conn, session_id)
    conn.close()
    return info
