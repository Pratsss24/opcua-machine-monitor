"""OEE from a stream of tag changes.

OEE = Availability x Performance x Quality, per the usual definition:

    Availability = run time / planned production time
    Performance  = (ideal cycle time x total count) / run time
    Quality      = good count / total count

Two things decide whether the number means anything, and both are about the
denominator rather than the arithmetic:

1. **Planned production time is not in the machine data.** A controller
   reports IDLE for a planned break and IDLE for waiting on material, and
   they are the same state on the wire. Only the shift plan knows which was
   which, so planned stops are passed in, not inferred. Counting a planned
   break as downtime is the most common way an OEE figure comes out wrong.

2. **Counters reset.** A parts counter that goes 418 -> 0 at a job change has
   not produced -418 parts. Totals here are the sum of positive deltas, so a
   reset costs nothing but the parts in flight.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

PRODUCTIVE_STATES = frozenset({"RUNNING"})


@dataclass
class StateInterval:
    state: str
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class OEEResult:
    availability: float
    performance: float
    quality: float
    oee: float

    run_seconds: float
    planned_production_seconds: float
    observed_seconds: float
    planned_stop_seconds: float
    downtime_by_state: dict[str, float]

    total_count: int
    good_count: int
    scrap_count: int

    ideal_cycle_seconds: float
    actual_mean_cycle_seconds: float
    intervals: list[StateInterval] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def theoretical_max_parts(self) -> float:
        if self.ideal_cycle_seconds <= 0:
            return math.nan
        return self.planned_production_seconds / self.ideal_cycle_seconds

    def losses(self) -> dict[str, float]:
        """Parts lost to each factor, as a waterfall from the theoretical max.

        Expressing the losses in parts rather than percentages is what makes
        an OEE presentation land with a production manager.
        """
        maximum = self.theoretical_max_parts
        after_a = maximum * self.availability
        after_p = after_a * self.performance
        after_q = after_p * self.quality
        return {
            "theoretical": maximum,
            "availability_loss": maximum - after_a,
            "performance_loss": after_a - after_p,
            "quality_loss": after_p - after_q,
            "good_parts": after_q,
        }


def build_intervals(
    changes: list[tuple[datetime, object]],
    end: datetime | None = None,
) -> list[StateInterval]:
    """Turn state changes into closed intervals.

    Consecutive duplicate states are merged: a server that republishes the
    same value would otherwise create zero-length intervals and inflate the
    transition count.
    """
    if not changes:
        return []

    cleaned: list[tuple[datetime, str]] = []
    for ts, value in changes:
        state = str(value)
        if cleaned and cleaned[-1][1] == state:
            continue
        cleaned.append((ts, state))

    intervals: list[StateInterval] = []
    for i, (ts, state) in enumerate(cleaned):
        stop = cleaned[i + 1][0] if i + 1 < len(cleaned) else end
        if stop is None or stop <= ts:
            continue
        intervals.append(StateInterval(state=state, start=ts, end=stop))
    return intervals


def counter_total(changes: list[tuple[datetime, object]]) -> int:
    """Total from a monotonic counter, tolerant of resets.

    Sums positive deltas only, and seeds from the first observed value so a
    collector that attaches mid-shift does not count the parts made before it
    connected.
    """
    values = [float(v) for _, v in changes if v is not None]
    if not values:
        return 0
    total = 0.0
    previous = values[0]
    for value in values[1:]:
        delta = value - previous
        if delta > 0:
            total += delta
        previous = value
    return int(round(total))


def compute(
    state_changes: list[tuple[datetime, object]],
    produced_changes: list[tuple[datetime, object]],
    scrapped_changes: list[tuple[datetime, object]],
    cycle_changes: list[tuple[datetime, object]],
    ideal_cycle_seconds: float,
    planned_stop_seconds: float = 0.0,
    end: datetime | None = None,
) -> OEEResult:
    warnings: list[str] = []

    all_times = [ts for ts, _ in state_changes + produced_changes]
    if not all_times:
        raise ValueError("no samples to analyse")
    start = min(all_times)
    finish = end or max(all_times)
    observed = (finish - start).total_seconds()

    intervals = build_intervals(state_changes, end=finish)
    downtime: dict[str, float] = {}
    run_seconds = 0.0
    for interval in intervals:
        if interval.state in PRODUCTIVE_STATES:
            run_seconds += interval.seconds
        else:
            downtime[interval.state] = downtime.get(interval.state, 0.0) + interval.seconds

    planned_production = observed - planned_stop_seconds
    if planned_production <= 0:
        raise ValueError("planned stops exceed the observed window")
    if planned_stop_seconds == 0.0:
        warnings.append(
            "No planned stops were declared, so the whole observed window counts as "
            "planned production time. If the shift included a break, availability is "
            "understated."
        )

    total_count = counter_total(produced_changes)
    scrap_count = counter_total(scrapped_changes)
    good_count = max(0, total_count - scrap_count)
    if scrap_count > total_count:
        warnings.append("Scrap counter exceeds the parts counter - check the tag mapping.")

    cycles = [float(v) for _, v in cycle_changes if v is not None and float(v) > 0]
    mean_cycle = sum(cycles) / len(cycles) if cycles else math.nan

    availability = run_seconds / planned_production if planned_production > 0 else 0.0
    performance = (
        (ideal_cycle_seconds * total_count) / run_seconds if run_seconds > 0 else 0.0
    )
    quality = good_count / total_count if total_count > 0 else 0.0

    if performance > 1.0:
        warnings.append(
            f"Performance is {performance:.1%}, above 100%. The machine cannot beat its "
            f"own ideal cycle time, so the declared ideal of {ideal_cycle_seconds:.1f} s "
            "is too slow - correct it rather than reporting the number."
        )

    return OEEResult(
        availability=availability,
        performance=performance,
        quality=quality,
        oee=availability * performance * quality,
        run_seconds=run_seconds,
        planned_production_seconds=planned_production,
        observed_seconds=observed,
        planned_stop_seconds=planned_stop_seconds,
        downtime_by_state=downtime,
        total_count=total_count,
        good_count=good_count,
        scrap_count=scrap_count,
        ideal_cycle_seconds=ideal_cycle_seconds,
        actual_mean_cycle_seconds=mean_cycle,
        intervals=intervals,
        warnings=warnings,
    )


def from_store(conn, session_id: int, ideal_cycle_seconds: float,
               planned_stop_seconds: float = 0.0) -> OEEResult:
    """Convenience wrapper that pulls the four tags OEE needs out of SQLite."""
    import store as _store

    return compute(
        state_changes=_store.load_tag(conn, session_id, "MachineState"),
        produced_changes=_store.load_tag(conn, session_id, "PartsProduced"),
        scrapped_changes=_store.load_tag(conn, session_id, "PartsScrapped"),
        cycle_changes=_store.load_tag(conn, session_id, "CycleTime"),
        ideal_cycle_seconds=ideal_cycle_seconds,
        planned_stop_seconds=planned_stop_seconds,
    )
