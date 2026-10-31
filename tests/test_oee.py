"""Checks against hand-computed values and against the simulation's own truth.

Run with `python tests/test_oee.py` (no pytest needed) or `pytest tests/`.

These exercise the pure layers -- the shift simulation and the OEE maths -- so
they need no server and no network. That separation is the reason they can
exist at all.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import oee  # noqa: E402
from machine import RUNNING, STATES, ShiftConfig, build_timeline  # noqa: E402

T0 = datetime(2026, 10, 31, 6, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# --- counters -------------------------------------------------------------

def test_counter_sums_increments() -> None:
    changes = [(at(i), float(i)) for i in range(1, 6)]
    assert oee.counter_total(changes) == 4  # 1 -> 5 is four parts


def test_counter_survives_a_reset() -> None:
    """418 -> 0 at a job change is not minus 418 parts."""
    changes = [(at(0), 400.0), (at(1), 418.0), (at(2), 0.0), (at(3), 12.0)]
    assert oee.counter_total(changes) == 30  # 18 before the reset, 12 after


def test_counter_ignores_history_before_the_collector_attached() -> None:
    """Seeding from the first observed value, not from zero."""
    changes = [(at(0), 1000.0), (at(1), 1003.0)]
    assert oee.counter_total(changes) == 3


def test_counter_of_empty_series_is_zero() -> None:
    assert oee.counter_total([]) == 0


# --- state intervals ------------------------------------------------------

def test_intervals_close_against_the_end() -> None:
    changes = [(at(0), "SETUP"), (at(100), RUNNING)]
    intervals = oee.build_intervals(changes, end=at(300))
    assert [i.state for i in intervals] == ["SETUP", RUNNING]
    assert intervals[0].seconds == 100
    assert intervals[1].seconds == 200


def test_repeated_state_values_do_not_create_intervals() -> None:
    """A server republishing the same value must not inflate the stop count."""
    changes = [(at(0), RUNNING), (at(10), RUNNING), (at(20), RUNNING), (at(30), "IDLE")]
    intervals = oee.build_intervals(changes, end=at(40))
    assert [i.state for i in intervals] == [RUNNING, "IDLE"]
    assert intervals[0].seconds == 30


def test_last_interval_is_dropped_without_an_end() -> None:
    intervals = oee.build_intervals([(at(0), RUNNING)], end=None)
    assert intervals == []


# --- OEE arithmetic -------------------------------------------------------

def _synthetic(run_seconds: float, window: float, count: int, scrap: int,
               ideal: float, planned_stop: float = 0.0):
    """A machine that runs for run_seconds then idles, making count parts."""
    states = [(at(0), RUNNING), (at(run_seconds), "IDLE")]
    produced = [(at(0), 0.0)] + [
        (at(run_seconds * (i + 1) / count), float(i + 1)) for i in range(count)
    ]
    scrapped = [(at(0), 0.0)] + [
        (at(run_seconds * (i + 1) / max(scrap, 1)), float(i + 1)) for i in range(scrap)
    ]
    return oee.compute(
        state_changes=states,
        produced_changes=produced,
        scrapped_changes=scrapped,
        cycle_changes=[(at(1), ideal)],
        ideal_cycle_seconds=ideal,
        planned_stop_seconds=planned_stop,
        end=at(window),
    )


def test_oee_matches_a_hand_computed_case() -> None:
    """8000 s run in a 10000 s window, 160 parts at a 40 s ideal, 8 scrap.

    A = 8000/10000 = 0.80
    P = 40*160/8000 = 0.80
    Q = 152/160     = 0.95
    OEE = 0.608
    """
    result = _synthetic(run_seconds=8000, window=10000, count=160, scrap=8, ideal=40.0)
    assert abs(result.availability - 0.80) < 1e-9
    assert abs(result.performance - 0.80) < 1e-9
    assert abs(result.quality - 0.95) < 1e-9
    assert abs(result.oee - 0.608) < 1e-9
    assert result.total_count == 160
    assert result.good_count == 152


def test_planned_stops_leave_the_denominator() -> None:
    """The same run with a declared break must score higher, not lower.

    This is the error the module exists to avoid: a break counted as downtime.
    """
    without = _synthetic(8000, 10000, 160, 8, 40.0, planned_stop=0.0)
    with_break = _synthetic(8000, 10000, 160, 8, 40.0, planned_stop=1800.0)

    assert with_break.availability > without.availability
    assert abs(with_break.availability - 8000 / 8200) < 1e-9
    assert abs(with_break.planned_production_seconds - 8200) < 1e-9


def test_missing_planned_stops_is_warned_about() -> None:
    result = _synthetic(8000, 10000, 160, 8, 40.0, planned_stop=0.0)
    assert any("planned" in w.lower() for w in result.warnings)


def test_performance_above_one_is_flagged_not_hidden() -> None:
    """A wrong ideal cycle time is a data problem, and must be said so."""
    result = _synthetic(run_seconds=8000, window=10000, count=160, scrap=0, ideal=80.0)
    assert result.performance > 1.0
    assert any("above 100" in w for w in result.warnings)


def test_losses_waterfall_closes() -> None:
    result = _synthetic(8000, 10000, 160, 8, 40.0)
    losses = result.losses()
    remaining = (losses["theoretical"] - losses["availability_loss"]
                 - losses["performance_loss"] - losses["quality_loss"])
    assert abs(remaining - losses["good_parts"]) < 1e-9
    # And good parts must equal theoretical x OEE, by definition.
    assert abs(losses["good_parts"] - losses["theoretical"] * result.oee) < 1e-9


def test_zero_production_does_not_divide_by_zero() -> None:
    result = oee.compute(
        state_changes=[(at(0), "IDLE")],
        produced_changes=[(at(0), 0.0)],
        scrapped_changes=[],
        cycle_changes=[],
        ideal_cycle_seconds=42.0,
        end=at(3600),
    )
    assert result.oee == 0.0
    assert result.run_seconds == 0.0


def test_planned_stops_longer_than_the_window_are_rejected() -> None:
    try:
        _synthetic(8000, 10000, 160, 8, 40.0, planned_stop=20000.0)
    except ValueError:
        return
    raise AssertionError("accepted planned stops longer than the observed window")


# --- simulation -----------------------------------------------------------

def test_timeline_is_deterministic_for_a_seed() -> None:
    a = build_timeline(seed=7)
    b = build_timeline(seed=7)
    c = build_timeline(seed=8)
    assert a.events == b.events
    assert a.events != c.events


def test_timeline_is_ordered_and_in_range() -> None:
    timeline = build_timeline(seed=11)
    times = [t for t, _, _ in timeline.events]
    assert times == sorted(times)
    assert min(times) >= 0.0
    assert max(times) <= timeline.duration + 1e-6


def test_timeline_only_emits_known_states() -> None:
    timeline = build_timeline(seed=11)
    states = {v for _, tag, v in timeline.events if tag == "MachineState"}
    assert states <= set(STATES), states


def test_oee_recovers_the_simulation_ground_truth() -> None:
    """Analyse the simulated shift directly and check against what it built.

    This is the end-to-end check without the network: if the OEE maths and the
    simulation disagree about run time or part count, one of them is wrong.
    """
    config = ShiftConfig()
    timeline = build_timeline(config, seed=20261031)

    def tag_series(name: str) -> list[tuple[datetime, object]]:
        return [(at(t), v) for t, tag, v in timeline.events if tag == name]

    result = oee.compute(
        state_changes=tag_series("MachineState"),
        produced_changes=tag_series("PartsProduced"),
        scrapped_changes=tag_series("PartsScrapped"),
        cycle_changes=tag_series("CycleTime"),
        ideal_cycle_seconds=config.ideal_cycle_seconds,
        planned_stop_seconds=config.planned_break_seconds,
        end=at(timeline.duration),
    )

    assert result.total_count == int(timeline.truth["produced"])
    assert result.scrap_count == int(timeline.truth["scrapped"])
    assert abs(result.actual_mean_cycle_seconds - timeline.truth["mean_cycle"]) < 0.1

    # Measured RUNNING time must exceed the time spent on completed cycles:
    # each stop interrupts a part mid-cycle, and that partial cycle is running
    # time that produced nothing. The excess is bounded by one cycle per
    # production burst.
    bursts = sum(1 for i in result.intervals if i.state == RUNNING)
    excess = result.run_seconds - timeline.truth["cutting_seconds"]
    assert excess >= 0, excess
    assert excess < bursts * config.mean_cycle_seconds, (excess, bursts)
    # A plausible shift, not a degenerate one.
    assert 0.4 < result.oee < 0.95


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {test.__name__}  {exc}")
        else:
            print(f"pass  {test.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
