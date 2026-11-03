"""CLI for the OPC UA machine monitor.

    python run.py demo                  # server + collector + report, one command
    python run.py server                # just the server, for a separate client
    python run.py collect --duration 60 # collect from a running server
    python run.py analyse               # re-analyse what is already in the database

The demo runs the server and the collector in one process for convenience.
Everything they do goes over a real OPC UA socket between them -- the client
browses the address space and subscribes exactly as it would against a
controller on the shop floor.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import charts  # noqa: E402
import collector  # noqa: E402
import oee as oee_mod  # noqa: E402
import report  # noqa: E402
import server as server_mod  # noqa: E402
import store  # noqa: E402
from machine import ShiftConfig  # noqa: E402

DB_PATH = ROOT / "data" / "machine.sqlite"
OUT_DIR = ROOT / "out"


def _print_summary(result: oee_mod.OEEResult, session: dict) -> None:
    def hms(seconds: float) -> str:
        seconds = int(round(seconds))
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"

    print(f"\n  samples collected   {session.get('samples', 0):,}"
          f"   (dropped {session.get('dropped', 0)})")
    latency = session.get("latency") or {}
    if latency:
        print(f"  latency             median {latency['median_ms']:.1f} ms, "
              f"p95 {latency['p95_ms']:.1f} ms")

    print(f"\n  observed window     {hms(result.observed_seconds)}")
    print(f"  planned stops       {hms(result.planned_stop_seconds)}")
    print(f"  planned production  {hms(result.planned_production_seconds)}")
    print(f"  run time            {hms(result.run_seconds)}")
    for state, seconds in sorted(result.downtime_by_state.items(),
                                 key=lambda kv: kv[1], reverse=True):
        print(f"    {state:<16}{hms(seconds)}")

    print(f"\n  parts               {result.total_count:,} total, "
          f"{result.good_count:,} good, {result.scrap_count:,} scrap")
    print(f"  cycle time          ideal {result.ideal_cycle_seconds:.1f} s, "
          f"actual {result.actual_mean_cycle_seconds:.1f} s")

    print(f"\n  Availability        {result.availability:>7.1%}")
    print(f"  Performance         {result.performance:>7.1%}")
    print(f"  Quality             {result.quality:>7.1%}")
    print(f"  OEE                 {result.oee:>7.1%}")

    for warning in result.warnings:
        print(f"\n  ! {warning}")


def _analyse(db_path: Path, ideal_cycle: float, planned_stop: float,
             session_id: int | None = None) -> int:
    conn = store.connect(db_path)
    session_id = session_id or store.latest_session(conn)
    if session_id is None:
        print("no sessions in the database - run `python run.py demo` first")
        return 1

    result = oee_mod.from_store(conn, session_id, ideal_cycle, planned_stop)
    session = store.session_info(conn, session_id)
    session["latency"] = store.latency_stats(conn, session_id)

    _print_summary(result, session)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cycles = [float(v) for _, v in store.load_tag(conn, session_id, "CycleTime")
              if v is not None and float(v) > 0]
    figures = {
        "waterfall": charts.waterfall_png(result, OUT_DIR / "waterfall.png"),
        "timeline": charts.timeline_png(result, OUT_DIR / "timeline.png"),
        "cycle": charts.cycle_png(cycles, result.ideal_cycle_seconds,
                                  OUT_DIR / "cycle.png"),
    }
    path = report.build(result, session, figures, OUT_DIR / "report.pdf")
    print(f"\n  report: {path}")
    conn.close()
    return 0


async def _demo(args) -> int:
    config = ShiftConfig()
    wall_seconds = config.shift_seconds / args.speed
    print(f"replaying an {config.shift_seconds / 3600:.0f}-hour shift at {args.speed:g}x "
          f"- about {wall_seconds:.0f} s of wall clock")

    server_task = asyncio.create_task(
        server_mod.run(args.endpoint, speed=args.speed, seed=args.seed,
                       config=config,
                       # Stay up until the collector is finished, otherwise the
                       # client sees the socket vanish mid-subscription.
                       stop_after_shift=False,
                       # Hold t=0 until the subscription exists.
                       start_delay=3.0)
    )
    # Let the endpoint come up before the client dials it.
    await asyncio.sleep(1.5)

    session = await collector.collect(
        endpoint=args.endpoint,
        db_path=args.db,
        duration=wall_seconds + 3.0,
    )
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass

    return _analyse(args.db, config.ideal_cycle_seconds,
                    config.planned_break_seconds)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("demo", "server", "collect", "analyse"))
    ap.add_argument("--endpoint", default=server_mod.DEFAULT_ENDPOINT)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--speed", type=float, default=480.0,
                    help="simulated seconds per real second (demo/server)")
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("--duration", type=float, default=None,
                    help="seconds to collect for (collect)")
    ap.add_argument("--ideal-cycle", type=float, default=42.0)
    ap.add_argument("--planned-stop", type=float, default=1800.0,
                    help="planned stop seconds to exclude from availability")
    ap.add_argument("--session", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )
    # asyncua is chatty at INFO.
    logging.getLogger("asyncua").setLevel(logging.WARNING)

    if args.command == "demo":
        return asyncio.run(_demo(args))
    if args.command == "server":
        asyncio.run(server_mod.run(args.endpoint, speed=args.speed, seed=args.seed,
                                   stop_after_shift=False))
        return 0
    if args.command == "collect":
        session = asyncio.run(collector.collect(args.endpoint, args.db, args.duration))
        print(f"collected {session['samples']:,} samples into session {session['id']}")
        return 0
    return _analyse(args.db, args.ideal_cycle, args.planned_stop, args.session)


if __name__ == "__main__":
    raise SystemExit(main())
