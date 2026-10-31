"""A simulated CNC machining centre exposed as an OPC UA server.

The address space is shaped like a real machine-tool server rather than a flat
bag of tags: identification, monitoring, production and alarms as separate
folders under one Machine object. That structure is what makes a client able
to browse a machine it has never seen, which is the point of OPC UA over a
bespoke TCP protocol.

Values come from `machine.build_timeline`, replayed at `speed` times real time
so a full eight-hour shift can be collected in a couple of minutes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from asyncua import Server, ua

from machine import ShiftConfig, build_timeline

LOGGER = logging.getLogger("cnc.server")

NAMESPACE = "http://pratsss24.github.io/cnc-demo"
DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:4840/cnc/"

# Explicit variant types. Letting asyncua infer from the first value is how a
# counter that starts at int 0 later rejects a float, so every tag declares
# its type once.
TAG_TYPES: dict[str, ua.VariantType] = {
    "Manufacturer": ua.VariantType.String,
    "Model": ua.VariantType.String,
    "SerialNumber": ua.VariantType.String,
    "MachineState": ua.VariantType.String,
    "SpindleSpeed": ua.VariantType.Double,
    "SpindleLoad": ua.VariantType.Double,
    "FeedOverride": ua.VariantType.Double,
    "AxisTemperature": ua.VariantType.Double,
    "ToolNumber": ua.VariantType.Int32,
    "JobIdentifier": ua.VariantType.String,
    "ActiveProgram": ua.VariantType.String,
    "PartsProduced": ua.VariantType.UInt32,
    "PartsScrapped": ua.VariantType.UInt32,
    "CycleTime": ua.VariantType.Double,
    "AlarmCode": ua.VariantType.UInt32,
    "ActiveAlarm": ua.VariantType.String,
}

FOLDERS: dict[str, tuple[str, ...]] = {
    "Identification": ("Manufacturer", "Model", "SerialNumber"),
    "Monitoring": ("MachineState", "SpindleSpeed", "SpindleLoad",
                   "FeedOverride", "AxisTemperature", "ToolNumber"),
    "Production": ("JobIdentifier", "ActiveProgram", "PartsProduced",
                   "PartsScrapped", "CycleTime"),
    "Alarms": ("AlarmCode", "ActiveAlarm"),
}

INITIAL: dict[str, object] = {
    "Manufacturer": "",
    "Model": "",
    "SerialNumber": "",
    "MachineState": "UNKNOWN",
    "SpindleSpeed": 0.0,
    "SpindleLoad": 0.0,
    "FeedOverride": 100.0,
    "AxisTemperature": 20.0,
    "ToolNumber": 0,
    "JobIdentifier": "",
    "ActiveProgram": "",
    "PartsProduced": 0,
    "PartsScrapped": 0,
    "CycleTime": 0.0,
    "AlarmCode": 0,
    "ActiveAlarm": "",
}


@dataclass
class ServerHandle:
    server: Server
    nodes: dict[str, object]
    namespace_index: int


async def build_server(endpoint: str = DEFAULT_ENDPOINT) -> ServerHandle:
    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    server.set_server_name("CNC Machining Centre (simulated)")
    idx = await server.register_namespace(NAMESPACE)

    machine = await server.nodes.objects.add_object(idx, "Machine")
    nodes: dict[str, object] = {}

    for folder_name, tags in FOLDERS.items():
        folder = await machine.add_object(idx, folder_name)
        for tag in tags:
            variant = ua.Variant(INITIAL[tag], TAG_TYPES[tag])
            node = await folder.add_variable(idx, tag, variant)
            nodes[tag] = node

    return ServerHandle(server=server, nodes=nodes, namespace_index=idx)


async def write(handle: ServerHandle, tag: str, value: object,
                source_ts: datetime | None = None) -> None:
    """Write a value with its declared variant type and a source timestamp.

    The source timestamp is what a collector should trust: it is when the
    machine says the value changed, not when the network delivered it.

    Here it carries the *simulated* machine clock rather than wall clock, so a
    shift replayed at 240x still reports eight hours of production. This is
    not a trick for the demo -- it is what a source timestamp means. A real
    controller buffering during a network outage sends values whose source
    timestamps are minutes old, and a collector that used arrival time would
    smear them all into the moment the link came back.
    """
    node = handle.nodes[tag]
    variant = ua.Variant(value, TAG_TYPES[tag])
    data_value = ua.DataValue(variant)
    if source_ts is not None:
        data_value.SourceTimestamp = source_ts
    await node.write_value(data_value)


async def run(
    endpoint: str = DEFAULT_ENDPOINT,
    speed: float = 240.0,
    seed: int = 20261031,
    config: ShiftConfig | None = None,
    stop_after_shift: bool = True,
    start_delay: float = 0.0,
) -> None:
    timeline = build_timeline(config, seed=seed)
    handle = await build_server(endpoint)

    LOGGER.info("endpoint %s", endpoint)
    LOGGER.info("replaying %d tag changes over %.1f simulated hours at %gx "
                "(about %.0f s wall clock)",
                len(timeline.events), timeline.duration / 3600.0, speed,
                timeline.duration / speed)

    shift_start = datetime.now(timezone.utc).replace(microsecond=0)

    # Stamp the initial values with the shift start as well. Without this they
    # keep the wall-clock time the nodes were created at, which sits outside
    # the simulated shift and shows up downstream as a long phantom UNKNOWN
    # state before the shift begins.
    for tag in handle.nodes:
        await write(handle, tag, INITIAL[tag], source_ts=shift_start)

    async with handle.server:
        if start_delay:
            # Hold the shift until a collector has had time to subscribe, so
            # the capture covers the whole shift rather than starting late.
            await asyncio.sleep(start_delay)
        started = asyncio.get_running_loop().time()
        for sim_time, tag, value in timeline.events:
            target = started + sim_time / speed
            delay = target - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
            await write(handle, tag, value,
                        source_ts=shift_start + timedelta(seconds=sim_time))

        LOGGER.info("shift complete: %s parts, %s scrap",
                    int(timeline.truth["produced"]), int(timeline.truth["scrapped"]))
        if not stop_after_shift:
            while True:
                await asyncio.sleep(1.0)
