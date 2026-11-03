"""Build the shift report as a printable PDF.

One file that can be pinned to a board or attached to a shift handover.
`paper.py` owns the page furniture; the findings below are this project's
own.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from oee import OEEResult
from paper import Paper

FOOTER = (
    "OEE = availability × performance × quality. Availability is "
    "run time over planned production time; performance is ideal cycle "
    "time times total count over run time; quality is good count over "
    "total count. Planned stops are supplied from the shift plan, not "
    "inferred from the machine data, because a controller reports the "
    "same IDLE state for a planned break and for waiting on material. "
    "Counters are summed as positive deltas so a counter reset does not "
    "corrupt the total.")


def _hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _time(result: OEEResult) -> tuple[list[str], list[list[str]]]:
    planned = result.planned_production_seconds
    rows = [("RUNNING", result.run_seconds)]
    rows += sorted(result.downtime_by_state.items(), key=lambda kv: kv[1],
                   reverse=True)
    out = [[state, _hms(seconds), f"{seconds / planned * 100:.1f}%"]
           for state, seconds in rows]
    out.append(["planned production time", _hms(planned), "100%"])
    if result.planned_stop_seconds:
        out.append(["planned stops (excluded)",
                    _hms(result.planned_stop_seconds), "—"])
    return (["bucket", "duration", "share of planned production time"], out)


def _rate(result: OEEResult) -> tuple[list[str], list[list[str]]]:
    scrap = result.scrap_count / max(result.total_count, 1)
    rows = [
        ["parts produced", f"{result.total_count:,}"],
        ["good", f"{result.good_count:,}"],
        ["scrap", f"{result.scrap_count:,} ({scrap:.2%})"],
        ["ideal cycle time", f"{result.ideal_cycle_seconds:.1f} s"],
        ["actual mean cycle time",
         f"{result.actual_mean_cycle_seconds:.1f} s"],
        ["run time", _hms(result.run_seconds)],
    ]
    return (["measure", "value"], rows)


def build(result: OEEResult, session: dict, figures: dict[str, Path],
          out_path: Path) -> Path:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = Path(out_path)
    out = out.with_name("report.pdf") if out.suffix != ".pdf" else out
    out.parent.mkdir(parents=True, exist_ok=True)

    stops = [one for one in result.intervals if one.state != "RUNNING"]
    gap = result.actual_mean_cycle_seconds - result.ideal_cycle_seconds

    with Paper(out, "Machine shift report",
               f"Collected over OPC UA from {session.get('endpoint')} "
               f"· {session.get('samples', 0):,} samples · window "
               f"{_hms(result.observed_seconds)} · generated "
               f"{generated}") as paper:

        paper.cover(
            headline=f"OEE {result.oee:.1%}",
            small=f"Availability {result.availability:.1%}, performance "
                  f"{result.performance:.1%}, quality "
                  f"{result.quality:.1%}, over "
                  f"{_hms(result.planned_production_seconds)} of planned "
                  f"production time.",
            tiles=[("availability", f"{result.availability:.1%}"),
                   ("performance", f"{result.performance:.1%}"),
                   ("quality", f"{result.quality:.1%}"),
                   ("good parts", f"{result.good_count:,}")],
            blurb="\n\n".join(result.warnings) if result.warnings else
                  "The three factors multiply, so a shift can lose as much "
                  "to a slow cycle as to a stoppage: the waterfall shows "
                  "which one actually cost the parts.",
            figure_path=figures["waterfall"])

        longest = max(stops, key=lambda one: one.seconds) if stops else None
        paper.section(
            "Where the time went",
            warn=(f"{len(stops)} stops totalling "
                  f"{_hms(sum(one.seconds for one in stops))}. The longest "
                  f"was {longest.state} for {longest.seconds / 60:.0f} "
                  f"minutes. Many short stops and one long stop need "
                  f"different responses, which is why the timeline is here "
                  f"and not just the total." if longest else ""),
            figure_path=figures["timeline"], table=_time(result))

        paper.section(
            "Rate and quality",
            warn=(f"Every part runs {gap:.1f} s over the ideal cycle. "
                  f"Across {result.total_count:,} parts that is "
                  f"{_hms(gap * result.total_count)} of the shift, or about "
                  f"{gap * result.total_count / result.ideal_cycle_seconds:.0f}"
                  f" parts of lost output — the performance loss, "
                  f"expressed as something a process engineer can chase."
                  if gap > 0 else ""),
            figure_path=figures["cycle"], table=_rate(result))

        latency = session.get("latency") or {}
        if latency:
            paper.section(
                "Collection quality",
                body="Latency is the gap between the server's timestamp and "
                     "local receipt. OEE here is computed from the source "
                     "timestamp, so this latency does not bias the result "
                     "— it only tells you how live a dashboard built "
                     "on this feed would be.",
                table=(["measure", "value"],
                       [["samples", f"{session.get('samples', 0):,}"],
                        ["dropped locally", f"{session.get('dropped', 0)}"],
                        ["median latency",
                         f"{latency['median_ms']:.1f} ms"],
                        ["95th percentile", f"{latency['p95_ms']:.1f} ms"],
                        ["worst", f"{latency['max_ms']:.1f} ms"]]),
                after=FOOTER)
        else:
            paper.section("How these numbers are defined", body=FOOTER)

    return out
