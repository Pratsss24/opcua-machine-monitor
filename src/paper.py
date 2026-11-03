"""Compose a printable PDF report out of matplotlib pages.

This replaces the hand-written HTML the earlier version of this project
emitted. The reason is not that the HTML was bad - it travelled fine -
but that it was the one part of the repository written in a language the
rest of it does not use, with its own stylesheet to maintain and its own
way of going wrong. A report built out of matplotlib pages is the same
stack as the figures it contains.

The unit is an A4 page and the layout is deliberately rigid: a section
gets a heading, an optional note, some prose, an optional figure and an
optional table, in that order, flowing down from the top margin. When a
table will not fit under its figure it spills onto a continuation page
rather than being squeezed. There is no clever reflow, because a report
that always looks the same is worth more than one that occasionally
packs better.

Figures are embedded from the PNGs `charts.py` has already written, at
their own resolution, on a band sized to their own aspect ratio - so
nothing is ever upscaled. The PNGs stay the artefacts the README links
to; this is a second way of reading the same thing, in one file, that
prints.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt                              # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages          # noqa: E402
from matplotlib.patches import Rectangle                      # noqa: E402

A4 = (8.27, 11.69)
MARGIN = 0.085
WIDTH = 1 - 2 * MARGIN
INSET = 0.022          # keeps text inside a tinted box off its accent bar

INK = "#16181d"
MUTED = "#5b6472"
RULE = "#d8dce1"
WASH = "#eef0f3"
COOL = "#1f5fa8"
WARM = "#b4232c"

TITLE = 23
HEAD = 14.5
BODY = 9.8
NOTE = 8.8
CELL = 8.4


def _points(size: float, leading: float = 1.5) -> float:
    """A line of type at `size`, as a fraction of the page height."""
    return size * leading / (A4[1] * 72)


def _as_paths(given) -> list[Path]:
    """One figure, several, or none, as a list either way."""
    if given is None:
        return []
    if isinstance(given, (str, Path)):
        return [Path(given)]
    return [Path(one) for one in given]


class Paper:
    """One PDF, written a page at a time.

    Used as a context manager so an exception halfway through a report
    still closes the file rather than leaving a truncated PDF behind.
    """

    def __init__(self, path: Path, title: str, subtitle: str = "",
                 *, author: str = "") -> None:
        self.path = Path(path)
        self.title = title
        self.subtitle = subtitle
        self.pdf = PdfPages(self.path)
        self.pages = 0
        self._author = author

    def __enter__(self) -> "Paper":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------ pieces

    def _blank(self):
        figure = plt.figure(figsize=A4)
        figure.patch.set_facecolor("white")
        return figure

    def _stamp(self, figure) -> None:
        """A running foot, so a loose page still says where it came from."""
        figure.text(MARGIN, 0.035, self.title, size=7.5, color=MUTED,
                    va="center")
        figure.text(1 - MARGIN, 0.035, f"{self.pages + 1}", size=7.5,
                    color=MUTED, va="center", ha="right")
        figure.add_artist(plt.Line2D(
            [MARGIN, 1 - MARGIN], [0.055, 0.055], color=RULE,
            linewidth=0.8, transform=figure.transFigure))

    def _close(self, figure) -> None:
        self._stamp(figure)
        self.pdf.savefig(figure)
        plt.close(figure)
        self.pages += 1

    def _width(self, figure, text: str, size: float,
               weight: str = "normal") -> float:
        """How wide `text` would be set, as a fraction of the page width.

        Drawn, measured and removed again: matplotlib has no way to ask
        for a string's extent without an artist to hang it on.
        """
        artist = figure.text(0, 0, text, size=size, weight=weight)
        renderer = figure.canvas.get_renderer()
        wide = artist.get_window_extent(renderer).width / figure.bbox.width
        artist.remove()
        return wide

    def _prose(self, figure, y: float, text: str, *, size: float = BODY,
               colour: str = INK, weight: str = "normal",
               width: int = 96, indent: float = 0.0,
               fit: bool = False) -> float:
        """Wrapped type from `y` downwards, returning where it ran out.

        `indent` insets the block, which is what keeps text that sits
        inside a tinted box clear of the box's own accent bar.

        `fit` shrinks the type until the widest line fits the measure.
        Wrapping counts characters, which is close enough for body text
        but not for a display size, where one wide word is the whole
        line - so a headline asks for this and a paragraph does not,
        since nobody wants two paragraphs set at different sizes.
        """
        lines: list[str] = []
        for block in text.split("\n"):
            lines.extend(textwrap.wrap(block, width) if block.strip()
                         else [""])
        if fit and lines:
            room = WIDTH - indent
            widest = max(self._width(figure, one, size, weight)
                         for one in lines)
            if widest > room:
                size *= room / widest
        step = _points(size)
        for line in lines:
            figure.text(MARGIN + indent, y, line, size=size, color=colour,
                        weight=weight, va="top")
            y -= step
        return y

    def _heading(self, figure, title: str, note: str = "") -> float:
        y = 0.945
        y = self._prose(figure, y, title, size=HEAD, weight="bold",
                        width=70, fit=True)
        figure.add_artist(plt.Line2D(
            [MARGIN, 1 - MARGIN], [y + 0.004, y + 0.004], color=COOL,
            linewidth=1.4, transform=figure.transFigure))
        y -= 0.012
        if note:
            y = self._prose(figure, y, note, size=NOTE, colour=MUTED,
                            width=100)
        return y - 0.014

    def _image(self, figure, y: float, path: Path,
               width: float = WIDTH) -> float:
        picture = plt.imread(str(path))
        tall, wide = picture.shape[0], picture.shape[1]
        height = width * (tall / wide) * (A4[0] / A4[1])
        ceiling = y - 0.09
        if height > ceiling:                 # never let a figure run off
            width *= ceiling / height
            height = ceiling
        axis = figure.add_axes([MARGIN + (WIDTH - width) / 2,
                                y - height, width, height])
        axis.imshow(picture, interpolation="none")
        axis.axis("off")
        return y - height - 0.022

    def _rows(self, figure, y: float, head: list[str],
              rows: list[list[str]]) -> float:
        if not rows:
            return y
        columns = len(head)
        # Measure rather than count characters. A column of part numbers
        # (HOUSING_BORE_DEPTH) is far wider than a column of lap times
        # with the same number of characters, and guessing at that is how
        # a cell ends up sitting on top of its neighbour.
        widest = [max([self._width(figure, head[one], CELL, "bold")]
                      + [self._width(figure, row[one], CELL)
                         for row in rows])
                  for one in range(columns)]
        # Wide enough that a column filling its share still reads as a
        # separate column from the one beside it.
        gutter = 0.014
        free = WIDTH - gutter * (columns - 1)
        total = sum(widest) or 1
        shares = [free * one / total for one in widest]
        edges, run = [], MARGIN
        for share in shares:
            edges.append(run)
            run += share + gutter

        # Eight columns of part numbers will not fit at the body size.
        # Rather than let them collide, set the whole table smaller: a
        # table a point down is still readable, an overlapping one is
        # not. Text width is linear in point size, so this one factor
        # makes every column fit its share at once.
        size = CELL * min(1.0, free / total)

        step = _points(size, 2.1)
        figure.add_artist(Rectangle(
            (MARGIN, y - step * 0.78), WIDTH, step * 0.95, facecolor=WASH,
            edgecolor="none", transform=figure.transFigure, zorder=0))
        for index, label in enumerate(head):
            left = index == 0
            figure.text(edges[index] + (0.004 if left else shares[index]
                                        - 0.004),
                        y - step * 0.30, label, size=size, color=INK,
                        weight="bold", va="center",
                        ha="left" if left else "right")
        y -= step

        for row in rows:
            for index, cell in enumerate(row):
                left = index == 0
                figure.text(edges[index] + (0.004 if left else shares[index]
                                            - 0.004),
                            y - step * 0.30, cell, size=size, color=INK,
                            va="center", ha="left" if left else "right")
            figure.add_artist(plt.Line2D(
                [MARGIN, 1 - MARGIN], [y - step * 0.78, y - step * 0.78],
                color=RULE, linewidth=0.6, transform=figure.transFigure))
            y -= step
        return y - 0.012

    def _tiles(self, figure, y: float,
               tiles: list[tuple[str, str]]) -> float:
        """A row of boxed headline numbers, up to four across."""
        tiles = tiles[:4]
        if not tiles:
            return y
        gap = 0.012
        box = (WIDTH - gap * (len(tiles) - 1)) / len(tiles)
        height = 0.062
        for index, (key, value) in enumerate(tiles):
            left = MARGIN + index * (box + gap)
            figure.add_artist(Rectangle(
                (left, y - height), box, height, facecolor="#f7f9fb",
                edgecolor=RULE, linewidth=0.8,
                transform=figure.transFigure, zorder=0))
            figure.text(left + 0.012, y - 0.024, value, size=HEAD + 1,
                        color=INK, weight="bold", va="center")
            for offset, line in enumerate(textwrap.wrap(key, 26)[:2]):
                figure.text(left + 0.012, y - 0.040 - offset * 0.011, line,
                            size=6.8, color=MUTED, va="center")
        return y - height - 0.022

    # ------------------------------------------------------------- pages

    def cover(self, *, headline: str, small: str = "", blurb: str = "",
              tiles: list[tuple[str, str]] | None = None,
              figure_path: Path | None = None) -> None:
        figure = self._blank()
        y = self._prose(figure, 0.945, self.title, size=TITLE,
                        weight="bold", width=46, fit=True)
        y -= 0.006
        if self.subtitle:
            y = self._prose(figure, y, self.subtitle, size=NOTE + 1.2,
                            colour=MUTED, width=94)
        y -= 0.022

        headlines = len(textwrap.wrap(headline, 50))
        block = (_points(HEAD + 3) * headlines
                 + (_points(NOTE) * len(textwrap.wrap(small, 88))
                    if small else 0) + 0.030)
        figure.add_artist(Rectangle(
            (MARGIN, y - block), WIDTH, block, facecolor="#f7f9fb",
            edgecolor=RULE, linewidth=0.8, transform=figure.transFigure,
            zorder=0))
        figure.add_artist(Rectangle(
            (MARGIN, y - block), 0.0075, block, facecolor=COOL,
            edgecolor="none", transform=figure.transFigure, zorder=1))
        inner = self._prose(figure, y - 0.016, headline, size=HEAD + 3,
                            weight="bold", width=50, indent=INSET,
                            fit=True)
        if small:
            self._prose(figure, inner - 0.005, small, size=NOTE,
                        colour=MUTED, width=88, indent=INSET)
        y -= block + 0.026

        if tiles:
            y = self._tiles(figure, y, list(tiles))
        if blurb:
            y = self._prose(figure, y, blurb, width=96) - 0.012
        if figure_path is not None:
            self._image(figure, y, Path(figure_path))
        self._close(figure)

    def section(self, title: str, *, note: str = "", body: str = "",
                warn: str = "", figure_path=None,
                table: tuple[list[str], list[list[str]]] | None = None,
                after: str = "") -> None:
        figure = self._blank()
        y = self._heading(figure, title, note)
        if body:
            y = self._prose(figure, y, body) - 0.012
        if warn:
            lines = len(textwrap.wrap(warn, 88))
            block = _points(BODY) * lines + 0.028
            figure.add_artist(Rectangle(
                (MARGIN, y - block), WIDTH, block, facecolor="#fff7f6",
                edgecolor="#f0d4d1", linewidth=0.8,
                transform=figure.transFigure, zorder=0))
            figure.add_artist(Rectangle(
                (MARGIN, y - block), 0.0075, block, facecolor=WARM,
                edgecolor="none", transform=figure.transFigure, zorder=1))
            self._prose(figure, y - 0.016, warn, width=88, indent=INSET)
            y -= block + 0.018
        for one in _as_paths(figure_path):
            y = self._image(figure, y, one)

        head, rows = table if table else ([], [])
        step = _points(CELL, 2.1)
        while rows:
            # Whatever is left of the page, in whole rows, less the header.
            room = int((y - 0.1) / step) - 1
            if room < 3:
                self._close(figure)
                figure = self._blank()
                y = self._heading(figure, f"{title} (continued)")
                room = int((y - 0.1) / step) - 1
            here, rows = rows[:room], rows[room:]
            y = self._rows(figure, y, head, here)
        if after:
            self._prose(figure, y - 0.004, after, size=NOTE, colour=MUTED,
                        width=100)
        self._close(figure)

    def close(self) -> None:
        info = self.pdf.infodict()
        info["Title"] = self.title
        if self._author:
            info["Author"] = self._author
        if self.subtitle:
            info["Subject"] = self.subtitle
        self.pdf.close()
