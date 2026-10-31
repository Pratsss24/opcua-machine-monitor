"""Simulation of one shift on a CNC machining centre.

Deliberately pure: this module builds a complete timeline of tag changes for a
shift and knows nothing about OPC UA, sockets or time. The server replays the
timeline; the tests check it. Keeping the physics out of the async I/O is what
makes any of this testable.

A timeline entry is (seconds_into_shift, tag_name, value). Only *changes* are
emitted, because that is what an OPC UA subscription delivers -- a value that
does not change produces no notification, and anything downstream has to cope
with that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# Machine states. These mirror the coarse states a real controller exposes;
# an OPC UA for Machine Tools server would carry a richer enumeration.
SETUP = "SETUP"
RUNNING = "RUNNING"
IDLE = "IDLE"
ALARM = "ALARM"
MAINTENANCE = "MAINTENANCE"

STATES = (SETUP, RUNNING, IDLE, ALARM, MAINTENANCE)

# Which states count as productive for OEE availability.
PRODUCTIVE_STATES = frozenset({RUNNING})

ALARM_CODES = {
    4096: "Tool breakage detected on T07",
    4097: "Chip conveyor jam",
    4098: "Coolant level low",
    4099: "Spindle load exceeded limit",
}


@dataclass
class ShiftConfig:
    """Everything about the shift that a planner would know in advance."""

    shift_seconds: float = 8 * 3600.0
    planned_break_seconds: float = 30 * 60.0
    setup_seconds: float = 25 * 60.0
    ideal_cycle_seconds: float = 42.0
    mean_cycle_seconds: float = 45.5
    cycle_sigma_seconds: float = 3.2
    scrap_rate: float = 0.021
    alarms_per_shift: float = 4.0
    alarm_minutes_range: tuple[float, float] = (4.0, 19.0)
    material_waits_per_shift: float = 3.0
    material_wait_minutes_range: tuple[float, float] = (3.0, 11.0)
    maintenance_seconds: float = 12 * 60.0
    job: str = "JOB-2026-4471"
    program: str = "O4471_HOUSING_OP20"

    @property
    def planned_production_seconds(self) -> float:
        """Planned production time: the shift minus planned stops.

        The break is a planned stop and is excluded from the denominator.
        Including it is the single most common way to make an OEE number look
        worse than the process deserves.
        """
        return self.shift_seconds - self.planned_break_seconds


@dataclass
class Timeline:
    events: list[tuple[float, str, object]]
    duration: float
    config: ShiftConfig
    # Ground truth kept for the tests; a real machine would not hand this over.
    truth: dict[str, float] = field(default_factory=dict)


def _append(events: list, t: float, tag: str, value) -> None:
    events.append((round(t, 3), tag, value))


def build_timeline(config: ShiftConfig | None = None, seed: int = 20261031) -> Timeline:
    """Build one shift of tag changes.

    Order of the shift: setup, then production interrupted by alarms, material
    waits and one maintenance stop, with a planned break in the middle.
    """
    cfg = config or ShiftConfig()
    rng = np.random.default_rng(seed)
    events: list[tuple[float, str, object]] = []

    # Static identification -- published once at t=0, as a real server would
    # hold them as non-changing nodes.
    _append(events, 0.0, "Manufacturer", "Heller")
    _append(events, 0.0, "Model", "H 2000")
    _append(events, 0.0, "SerialNumber", "H2K-558214")
    _append(events, 0.0, "JobIdentifier", cfg.job)
    _append(events, 0.0, "ActiveProgram", cfg.program)
    _append(events, 0.0, "PartsProduced", 0)
    _append(events, 0.0, "PartsScrapped", 0)
    _append(events, 0.0, "ToolNumber", 7)
    _append(events, 0.0, "AlarmCode", 0)
    _append(events, 0.0, "ActiveAlarm", "")

    # Plan the interruptions up front so they can be placed in time order.
    n_alarms = int(rng.poisson(cfg.alarms_per_shift))
    n_waits = int(rng.poisson(cfg.material_waits_per_shift))

    interruptions: list[tuple[float, float, str, int]] = []
    production_start = cfg.setup_seconds
    production_end = cfg.shift_seconds

    for _ in range(n_alarms):
        start = rng.uniform(production_start + 600, production_end - 900)
        minutes = rng.uniform(*cfg.alarm_minutes_range)
        code = int(rng.choice(list(ALARM_CODES)))
        interruptions.append((start, minutes * 60.0, ALARM, code))

    for _ in range(n_waits):
        start = rng.uniform(production_start + 600, production_end - 900)
        minutes = rng.uniform(*cfg.material_wait_minutes_range)
        interruptions.append((start, minutes * 60.0, IDLE, 0))

    # One maintenance stop and the planned break.
    interruptions.append((rng.uniform(0.55, 0.75) * cfg.shift_seconds,
                          cfg.maintenance_seconds, MAINTENANCE, 0))
    break_start = 0.5 * cfg.shift_seconds
    interruptions.append((break_start, cfg.planned_break_seconds, IDLE, -1))

    interruptions.sort(key=lambda item: item[0])

    # Drop overlaps rather than modelling nested stops: a real controller
    # reports one state at a time, so the timeline must too.
    merged: list[tuple[float, float, str, int]] = []
    for start, duration, state, code in interruptions:
        if merged and start < merged[-1][0] + merged[-1][1]:
            continue
        merged.append((start, duration, state, code))

    # --- walk the shift -------------------------------------------------
    _append(events, 0.0, "MachineState", SETUP)
    _append(events, 0.0, "SpindleSpeed", 0.0)
    _append(events, 0.0, "SpindleLoad", 0.0)
    _append(events, 0.0, "FeedOverride", 100.0)
    _append(events, 0.0, "AxisTemperature", 21.4)

    produced = 0
    scrapped = 0
    run_seconds = 0.0
    cycle_times: list[float] = []
    temperature = 21.4

    t = cfg.setup_seconds
    _append(events, t, "MachineState", RUNNING)
    _append(events, t, "SpindleSpeed", 4200.0)

    for start, duration, state, code in merged:
        # Produce parts until this interruption begins.
        while t + cfg.mean_cycle_seconds <= start:
            cycle = float(rng.normal(cfg.mean_cycle_seconds, cfg.cycle_sigma_seconds))
            cycle = max(cycle, cfg.ideal_cycle_seconds * 0.92)
            if t + cycle > start:
                break
            t += cycle
            run_seconds += cycle
            produced += 1
            cycle_times.append(cycle)

            is_scrap = rng.random() < cfg.scrap_rate
            if is_scrap:
                scrapped += 1
                _append(events, t, "PartsScrapped", scrapped)
            _append(events, t, "PartsProduced", produced)
            _append(events, t, "CycleTime", round(cycle, 2))

            # Spindle load and temperature drift during the cut.
            load = float(np.clip(rng.normal(68.0, 7.0), 20.0, 100.0))
            _append(events, t, "SpindleLoad", round(load, 1))
            temperature = min(38.0, temperature + rng.uniform(0.004, 0.02))
            if produced % 12 == 0:
                _append(events, t, "AxisTemperature", round(temperature, 2))

        # Enter the interruption.
        t = max(t, start)
        _append(events, t, "MachineState", state)
        _append(events, t, "SpindleSpeed", 0.0)
        _append(events, t, "SpindleLoad", 0.0)
        if state == ALARM:
            _append(events, t, "AlarmCode", code)
            _append(events, t, "ActiveAlarm", ALARM_CODES[code])

        t += duration
        # Cooling while stopped.
        temperature = max(21.4, temperature - duration * 0.0016)
        _append(events, t, "AxisTemperature", round(temperature, 2))
        if state == ALARM:
            _append(events, t, "AlarmCode", 0)
            _append(events, t, "ActiveAlarm", "")
            # A tool break means a tool change on restart.
            if code == 4096:
                _append(events, t, "ToolNumber", int(rng.integers(1, 24)))
        _append(events, t, "MachineState", RUNNING)
        _append(events, t, "SpindleSpeed", 4200.0)

    # Produce to the end of the shift.
    while t + cfg.mean_cycle_seconds <= cfg.shift_seconds:
        cycle = float(rng.normal(cfg.mean_cycle_seconds, cfg.cycle_sigma_seconds))
        cycle = max(cycle, cfg.ideal_cycle_seconds * 0.92)
        if t + cycle > cfg.shift_seconds:
            break
        t += cycle
        run_seconds += cycle
        produced += 1
        cycle_times.append(cycle)
        if rng.random() < cfg.scrap_rate:
            scrapped += 1
            _append(events, t, "PartsScrapped", scrapped)
        _append(events, t, "PartsProduced", produced)
        _append(events, t, "CycleTime", round(cycle, 2))
        _append(events, t, "SpindleLoad", round(float(np.clip(rng.normal(68.0, 7.0), 20, 100)), 1))

    _append(events, cfg.shift_seconds, "MachineState", IDLE)
    _append(events, cfg.shift_seconds, "SpindleSpeed", 0.0)
    _append(events, cfg.shift_seconds, "SpindleLoad", 0.0)

    events.sort(key=lambda item: item[0])

    truth = {
        # Time spent on *completed* cycles. This is deliberately not the same
        # as the RUNNING interval time an analyser measures: when a stop
        # interrupts a part mid-cycle, the machine was running but no part
        # came out. That difference is a genuine performance loss, not an
        # accounting error.
        "cutting_seconds": run_seconds,
        "produced": float(produced),
        "scrapped": float(scrapped),
        "mean_cycle": float(np.mean(cycle_times)) if cycle_times else math.nan,
        "planned_production_seconds": cfg.planned_production_seconds,
    }
    return Timeline(events=events, duration=cfg.shift_seconds, config=cfg, truth=truth)
