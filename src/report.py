"""Single self-contained HTML shift report."""

from __future__ import annotations

import base64
import html
from datetime import datetime
from pathlib import Path

from oee import OEEResult

CSS = """
:root { --bg:#f4f6f8; --card:#fff; --ink:#1b2733; --muted:#5d6f7e; --rule:#dde4ea;
  --good:#2f7d4f; --warn:#8a6d1f; --bad:#b23a2e; }
* { box-sizing: border-box; }
body { margin:0; padding:32px 20px 64px; background:var(--bg); color:var(--ink);
  font:15px/1.55 "Segoe UI", system-ui, -apple-system, sans-serif; }
.wrap { max-width:1020px; margin:0 auto; }
h1 { font-size:24px; margin:0 0 4px; letter-spacing:-0.01em; }
h2 { font-size:17px; margin:0 0 12px; padding-bottom:6px; border-bottom:1px solid var(--rule); }
.sub { color:var(--muted); font-size:13px; margin:0 0 24px; }
.card { background:var(--card); border:1px solid var(--rule); border-radius:8px;
  padding:18px 20px; margin:0 0 18px; }
table { width:100%; border-collapse:collapse; font-size:13.5px; }
th,td { text-align:right; padding:7px 9px; border-bottom:1px solid var(--rule); }
th:first-child, td:first-child { text-align:left; }
th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
tbody tr:last-child td { border-bottom:none; }
.num { font-variant-numeric: tabular-nums; }
.kpi { display:flex; gap:30px; flex-wrap:wrap; margin:2px 0 6px; }
.kpi .v { font-size:30px; font-variant-numeric:tabular-nums; line-height:1.1; }
.kpi .l { font-size:11.5px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.kpi .big .v { font-size:40px; }
img { width:100%; display:block; margin:14px 0 0; border:1px solid var(--rule); border-radius:6px; }
.finding { margin:12px 0 0; padding:11px 13px; border-left:3px solid var(--warn);
  background:#fbf8ef; font-size:13.5px; }
.finding.bad { border-left-color:var(--bad); background:#fdf4f3; }
.finding.ok { border-left-color:var(--good); background:#f3f9f5; }
code { font-family: ui-monospace,"Cascadia Mono",monospace; font-size:12.5px; }
footer { color:var(--muted); font-size:12.5px; margin-top:40px; }
@media print { body { background:#fff; padding:0; } .card { break-inside:avoid; } }
"""


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def build(result: OEEResult, session: dict, figures: dict[str, Path],
          out_path: Path) -> Path:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts: list[str] = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Machine Shift Report</title>",
        f"<style>{CSS}</style></head><body><div class='wrap'>",
        "<h1>Machine shift report</h1>",
        f"<p class='sub'>Collected over OPC UA from <code>"
        f"{html.escape(str(session.get('endpoint')))}</code> &middot; "
        f"{session.get('samples', 0):,} samples &middot; "
        f"window {_hms(result.observed_seconds)} &middot; generated {generated}</p>",
    ]

    # Headline
    parts.append("<div class='card'><h2>OEE</h2><div class='kpi'>")
    parts.append(f"<div class='big'><div class='v'>{result.oee:.1%}</div>"
                 f"<div class='l'>OEE</div></div>")
    for label, value in (("Availability", result.availability),
                         ("Performance", result.performance),
                         ("Quality", result.quality)):
        parts.append(f"<div><div class='v'>{value:.1%}</div><div class='l'>{label}</div></div>")
    parts.append(f"<div><div class='v'>{result.good_count:,}</div>"
                 f"<div class='l'>good parts</div></div>")
    parts.append("</div>")
    parts.append(f"<img alt='oee waterfall' src='data:image/png;base64,"
                 f"{_b64(figures['waterfall'])}'>")

    for warning in result.warnings:
        parts.append(f"<div class='finding'>{html.escape(warning)}</div>")
    parts.append("</div>")

    # Time
    parts.append("<div class='card'><h2>Where the time went</h2><table>")
    parts.append("<thead><tr><th>Bucket</th><th>Duration</th><th>Share of planned "
                 "production time</th></tr></thead><tbody>")
    planned = result.planned_production_seconds
    rows = [("RUNNING", result.run_seconds)]
    rows += sorted(result.downtime_by_state.items(), key=lambda kv: kv[1], reverse=True)
    for state, seconds in rows:
        parts.append(
            f"<tr><td>{html.escape(state)}</td><td class='num'>{_hms(seconds)}</td>"
            f"<td class='num'>{seconds / planned * 100:.1f}%</td></tr>"
        )
    parts.append(
        f"<tr><td><b>Planned production time</b></td>"
        f"<td class='num'><b>{_hms(planned)}</b></td><td class='num'>100%</td></tr>"
    )
    if result.planned_stop_seconds:
        parts.append(
            f"<tr><td>Planned stops (excluded)</td>"
            f"<td class='num'>{_hms(result.planned_stop_seconds)}</td>"
            f"<td class='num'>&mdash;</td></tr>"
        )
    parts.append("</tbody></table>")
    parts.append(f"<img alt='state timeline' src='data:image/png;base64,"
                 f"{_b64(figures['timeline'])}'>")

    stops = [i for i in result.intervals if i.state not in {"RUNNING"}]
    if stops:
        longest = max(stops, key=lambda i: i.seconds)
        parts.append(
            f"<div class='finding'><b>{len(stops)} stops</b> totalling "
            f"{_hms(sum(i.seconds for i in stops))}. The longest was "
            f"{longest.state} for {longest.seconds / 60:.0f} minutes. "
            "Many short stops and one long stop need different responses, which "
            "is why the timeline is here and not just the total.</div>"
        )
    parts.append("</div>")

    # Rate
    parts.append("<div class='card'><h2>Rate and quality</h2><table><tbody>")
    for label, value in (
        ("Parts produced", f"{result.total_count:,}"),
        ("Good", f"{result.good_count:,}"),
        ("Scrap", f"{result.scrap_count:,} ({result.scrap_count / max(result.total_count, 1):.2%})"),
        ("Ideal cycle time", f"{result.ideal_cycle_seconds:.1f} s"),
        ("Actual mean cycle time", f"{result.actual_mean_cycle_seconds:.1f} s"),
        ("Run time", _hms(result.run_seconds)),
    ):
        parts.append(f"<tr><td>{label}</td><td class='num'>{value}</td></tr>")
    parts.append("</tbody></table>")
    parts.append(f"<img alt='cycle times' src='data:image/png;base64,"
                 f"{_b64(figures['cycle'])}'>")

    gap = result.actual_mean_cycle_seconds - result.ideal_cycle_seconds
    if gap > 0:
        lost = gap * result.total_count
        parts.append(
            f"<div class='finding'>Every part runs <b>{gap:.1f} s over the ideal "
            f"cycle</b>. Across {result.total_count:,} parts that is "
            f"{_hms(lost)} of the shift, or about "
            f"{lost / result.ideal_cycle_seconds:.0f} parts of lost output &mdash; "
            "the performance loss, expressed as something a process engineer can "
            "chase.</div>"
        )
    parts.append("</div>")

    latency = session.get("latency") or {}
    if latency:
        parts.append(
            "<div class='card'><h2>Collection quality</h2><table><tbody>"
            f"<tr><td>Samples</td><td class='num'>{session.get('samples', 0):,}</td></tr>"
            f"<tr><td>Dropped locally</td><td class='num'>{session.get('dropped', 0)}</td></tr>"
            f"<tr><td>Median latency</td><td class='num'>{latency['median_ms']:.1f} ms</td></tr>"
            f"<tr><td>95th percentile</td><td class='num'>{latency['p95_ms']:.1f} ms</td></tr>"
            f"<tr><td>Worst</td><td class='num'>{latency['max_ms']:.1f} ms</td></tr>"
            "</tbody></table>"
            "<div class='finding ok'>Latency is the gap between the server's "
            "timestamp and local receipt. OEE here is computed from the "
            "<b>source timestamp</b>, so this latency does not bias the result "
            "&mdash; it only tells you how live a dashboard built on this feed "
            "would be.</div></div>"
        )

    parts.append(
        "<footer>OEE = Availability x Performance x Quality. Availability is run "
        "time over planned production time; performance is ideal cycle time times "
        "total count over run time; quality is good count over total count. "
        "Planned stops are supplied from the shift plan, not inferred from the "
        "machine data, because a controller reports the same IDLE state for a "
        "planned break and for waiting on material. Counters are summed as "
        "positive deltas so a counter reset does not corrupt the total."
        "</footer></div></body></html>"
    )

    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path
