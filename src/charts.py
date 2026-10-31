"""Chart rendering: state timeline, OEE waterfall, cycle-time distribution."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

from oee import OEEResult

INK = "#1b2733"
GRID = "#d9e0e6"
LINE = "#20638f"
LIMIT = "#b23a2e"

STATE_COLORS = {
    "RUNNING": "#2f7d4f",
    "SETUP": "#4b7fa8",
    "IDLE": "#b9a24a",
    "ALARM": "#b23a2e",
    "MAINTENANCE": "#7a6ba8",
    "UNKNOWN": "#9aa7b1",
}


def _style(ax) -> None:
    ax.set_facecolor("white")
    ax.grid(True, color=GRID, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)
    ax.tick_params(colors=INK, labelsize=8)


def timeline_png(result: OEEResult, out: Path) -> Path:
    """Every state interval across the shift, on one row.

    This is the chart that makes downtime arguable: a manager can see whether
    the losses are one long stop or thirty short ones, and those have
    completely different fixes.
    """
    fig, ax = plt.subplots(figsize=(9.6, 2.5))
    if not result.intervals:
        ax.text(0.5, 0.5, "no state data", ha="center")
        fig.savefig(out, dpi=140, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return out

    origin = result.intervals[0].start
    for interval in result.intervals:
        start_min = (interval.start - origin).total_seconds() / 60.0
        width_min = interval.seconds / 60.0
        ax.barh(
            0, width_min, left=start_min, height=0.6,
            color=STATE_COLORS.get(interval.state, STATE_COLORS["UNKNOWN"]),
            zorder=3, edgecolor="none",
        )

    total_min = (result.intervals[-1].end - origin).total_seconds() / 60.0
    ax.set_xlim(0, total_min)
    ax.set_ylim(-0.6, 0.6)
    ax.set_yticks([])
    ax.set_xlabel("minutes into the collected window", color=INK, fontsize=9)
    ax.set_title("Machine state over the shift", color=INK, fontsize=11, loc="left")

    seen = []
    for interval in result.intervals:
        if interval.state not in seen:
            seen.append(interval.state)
    ax.legend(
        handles=[mpatches.Patch(color=STATE_COLORS.get(s, STATE_COLORS["UNKNOWN"]), label=s)
                 for s in seen],
        frameon=False, fontsize=8, ncol=len(seen), loc="upper left",
        bbox_to_anchor=(0, -0.42),
    )
    _style(ax)
    ax.grid(axis="y", visible=False)

    fig.savefig(out, dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def waterfall_png(result: OEEResult, out: Path) -> Path:
    """Theoretical output down to good parts, one bar per loss."""
    losses = result.losses()
    fig, ax = plt.subplots(figsize=(9.6, 3.6))

    theoretical = losses["theoretical"]
    after_a = theoretical - losses["availability_loss"]
    after_p = after_a - losses["performance_loss"]
    good = losses["good_parts"]

    # (label, bottom, height, colour) -- the three loss bars float between the
    # level before and after that loss, which is what makes it a waterfall.
    bars = [
        ("Theoretical\nmax", 0.0, theoretical, "#4b7fa8"),
        ("Availability\nloss", after_a, losses["availability_loss"], "#b23a2e"),
        ("Performance\nloss", after_p, losses["performance_loss"], "#b9a24a"),
        ("Quality\nloss", good, losses["quality_loss"], "#7a6ba8"),
        ("Good\nparts", 0.0, good, "#2f7d4f"),
    ]
    labels = [label for label, _, _, _ in bars]

    for i, (_, bottom, height, color) in enumerate(bars):
        ax.bar(i, height, bottom=bottom, color=color, zorder=3, width=0.6)
        ax.text(i, bottom + height + theoretical * 0.02, f"{height:,.0f}",
                ha="center", fontsize=9, color=INK)
        # Connector to the next bar's level, so the steps read as one flow.
        if 0 < i < len(bars) - 1:
            ax.plot([i - 0.3, i + 0.3], [bottom, bottom], color=INK,
                    linewidth=0.8, alpha=0.35, zorder=2)

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylabel("parts", color=INK, fontsize=9)
    ax.set_ylim(0, losses["theoretical"] * 1.14)
    ax.set_title(
        f"Where the shift went  —  OEE {result.oee:.1%} "
        f"(A {result.availability:.1%} x P {result.performance:.1%} x Q {result.quality:.1%})",
        color=INK, fontsize=11, loc="left",
    )
    _style(ax)
    ax.grid(axis="x", visible=False)

    fig.savefig(out, dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def cycle_png(cycles: list[float], ideal: float, out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(9.6, 3.0))
    if not cycles:
        ax.text(0.5, 0.5, "no cycle data", ha="center")
        fig.savefig(out, dpi=140, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return out

    data = np.asarray(cycles, dtype=float)
    ax.hist(data, bins=40, color="#cfe0ec", edgecolor="#7fa6c0", linewidth=0.5, zorder=3)
    ax.axvline(ideal, color=LIMIT, linewidth=1.6, zorder=5)
    ax.text(ideal, ax.get_ylim()[1] * 0.95, f" ideal {ideal:.1f}s",
            color=LIMIT, fontsize=8.5, va="top")
    ax.axvline(data.mean(), color="#2f7d4f", linewidth=1.3, linestyle="--", zorder=5)
    ax.text(data.mean(), ax.get_ylim()[1] * 0.78, f" actual {data.mean():.1f}s",
            color="#2f7d4f", fontsize=8.5, va="top")

    ax.set_xlabel("cycle time [s]", color=INK, fontsize=9)
    ax.set_ylabel("parts", color=INK, fontsize=9)
    ax.set_title("Cycle time against the ideal", color=INK, fontsize=11, loc="left")
    _style(ax)

    fig.savefig(out, dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out
