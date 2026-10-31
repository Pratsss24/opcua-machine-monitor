"""SQLite store for collected OPC UA samples.

One row per data-change notification. Numeric and text values live in separate
columns so the numeric ones stay queryable without casting, and three
timestamps are kept because they answer different questions:

* `source_ts` -- when the machine says the value changed. Use this for OEE.
* `server_ts` -- when the OPC UA server processed it.
* `received_ts` -- when this collector got it. The gap against source_ts is
  collection latency, which is worth seeing when a line gets busy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint     TEXT NOT NULL,
    server_name  TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    tag         TEXT NOT NULL,
    value_num   REAL,
    value_text  TEXT,
    source_ts   TEXT NOT NULL,
    server_ts   TEXT,
    received_ts TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Good'
);

CREATE INDEX IF NOT EXISTS idx_samples_session_tag
    ON samples(session_id, tag, source_ts);
CREATE INDEX IF NOT EXISTS idx_samples_source
    ON samples(source_ts);
"""


@dataclass
class Sample:
    tag: str
    value: object
    source_ts: datetime
    server_ts: datetime | None
    received_ts: datetime
    status: str = "Good"

    @property
    def numeric(self) -> float | None:
        if isinstance(self.value, bool):
            return float(self.value)
        if isinstance(self.value, (int, float)):
            return float(self.value)
        return None

    @property
    def text(self) -> str | None:
        return None if self.numeric is not None else str(self.value)


def connect(path: Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL lets the analyser read while the collector is still writing, which
    # is the normal case when watching a running shift.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def start_session(conn: sqlite3.Connection, endpoint: str, server_name: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO sessions (endpoint, server_name, started_at) VALUES (?, ?, ?)",
        (endpoint, server_name, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_session(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute(
        "UPDATE sessions SET finished_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), session_id),
    )
    conn.commit()


def write_samples(conn: sqlite3.Connection, session_id: int, samples: list[Sample]) -> int:
    """Batch insert. Called from one writer task, so no cross-thread sharing."""
    if not samples:
        return 0
    rows = [
        (
            session_id,
            s.tag,
            s.numeric,
            s.text,
            s.source_ts.isoformat(),
            s.server_ts.isoformat() if s.server_ts else None,
            s.received_ts.isoformat(),
            s.status,
        )
        for s in samples
    ]
    conn.executemany(
        "INSERT INTO samples (session_id, tag, value_num, value_text, source_ts,"
        " server_ts, received_ts, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def latest_session(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def load_tag(conn: sqlite3.Connection, session_id: int, tag: str) -> list[tuple[datetime, object]]:
    """All changes for one tag in source-timestamp order."""
    rows = conn.execute(
        "SELECT value_num, value_text, source_ts FROM samples"
        " WHERE session_id = ? AND tag = ? AND status = 'Good'"
        " ORDER BY source_ts, id",
        (session_id, tag),
    ).fetchall()
    out: list[tuple[datetime, object]] = []
    for row in rows:
        value = row["value_text"] if row["value_num"] is None else row["value_num"]
        out.append((datetime.fromisoformat(row["source_ts"]), value))
    return out


def tag_names(conn: sqlite3.Connection, session_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT tag FROM samples WHERE session_id = ? ORDER BY tag",
        (session_id,),
    ).fetchall()
    return [row["tag"] for row in rows]


def session_info(conn: sqlite3.Connection, session_id: int) -> dict:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    counts = conn.execute(
        "SELECT COUNT(*) AS n FROM samples WHERE session_id = ?", (session_id,)
    ).fetchone()
    return {
        "id": session_id,
        "endpoint": row["endpoint"] if row else None,
        "server_name": row["server_name"] if row else None,
        "started_at": row["started_at"] if row else None,
        "finished_at": row["finished_at"] if row else None,
        "samples": int(counts["n"]),
    }


def latency_stats(conn: sqlite3.Connection, session_id: int) -> dict[str, float]:
    """Milliseconds between the server timestamp and local receipt."""
    rows = conn.execute(
        "SELECT server_ts, received_ts FROM samples"
        " WHERE session_id = ? AND server_ts IS NOT NULL",
        (session_id,),
    ).fetchall()
    deltas = [
        (datetime.fromisoformat(r["received_ts"])
         - datetime.fromisoformat(r["server_ts"])).total_seconds() * 1000.0
        for r in rows
    ]
    if not deltas:
        return {}
    deltas.sort()
    return {
        "n": float(len(deltas)),
        "median_ms": deltas[len(deltas) // 2],
        "p95_ms": deltas[min(len(deltas) - 1, int(len(deltas) * 0.95))],
        "max_ms": deltas[-1],
    }
