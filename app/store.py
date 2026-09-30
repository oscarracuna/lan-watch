from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.config import data_dir


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "lan-watch.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_key TEXT NOT NULL,
                    ts REAL NOT NULL,
                    ok INTEGER NOT NULL,
                    rtt_ms REAL
                );
                CREATE INDEX IF NOT EXISTS idx_samples_host_ts
                    ON samples(host_key, ts);
                CREATE TABLE IF NOT EXISTS host_state (
                    host_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    status TEXT NOT NULL,
                    consecutive_ok INTEGER NOT NULL DEFAULT 0,
                    consecutive_fail INTEGER NOT NULL DEFAULT 0,
                    last_rtt_ms REAL,
                    last_ok_ts REAL,
                    last_fail_ts REAL,
                    last_change_ts REAL,
                    last_sample_ts REAL
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    down_ts REAL NOT NULL,
                    up_ts REAL,
                    duration_seconds REAL
                );
                CREATE INDEX IF NOT EXISTS idx_incidents_down
                    ON incidents(down_ts);
                CREATE INDEX IF NOT EXISTS idx_incidents_open
                    ON incidents(host_key, up_ts);
                """
            )
            existing = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(samples)").fetchall()
            }
            for name, decl in (
                ("loss_pct", "REAL"),
                ("rtt_min_ms", "REAL"),
                ("rtt_max_ms", "REAL"),
                ("packets_ok", "INTEGER"),
                ("packets", "INTEGER"),
            ):
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE samples ADD COLUMN {name} {decl}")
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def load_states(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM host_state").fetchall()
        return {row["host_key"]: dict(row) for row in rows}

    def upsert_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO host_state (
                    host_key, name, ip, status, consecutive_ok, consecutive_fail,
                    last_rtt_ms, last_ok_ts, last_fail_ts, last_change_ts, last_sample_ts
                ) VALUES (
                    :host_key, :name, :ip, :status, :consecutive_ok, :consecutive_fail,
                    :last_rtt_ms, :last_ok_ts, :last_fail_ts, :last_change_ts, :last_sample_ts
                )
                ON CONFLICT(host_key) DO UPDATE SET
                    name=excluded.name,
                    ip=excluded.ip,
                    status=excluded.status,
                    consecutive_ok=excluded.consecutive_ok,
                    consecutive_fail=excluded.consecutive_fail,
                    last_rtt_ms=excluded.last_rtt_ms,
                    last_ok_ts=excluded.last_ok_ts,
                    last_fail_ts=excluded.last_fail_ts,
                    last_change_ts=excluded.last_change_ts,
                    last_sample_ts=excluded.last_sample_ts
                """,
                state,
            )
            self.conn.commit()

    def add_sample(
        self,
        host_key: str,
        ts: float,
        ok: bool,
        rtt_ms: float | None,
        *,
        loss_pct: float | None = None,
        rtt_min_ms: float | None = None,
        rtt_max_ms: float | None = None,
        packets_ok: int | None = None,
        packets: int | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO samples (
                    host_key, ts, ok, rtt_ms, loss_pct, rtt_min_ms, rtt_max_ms,
                    packets_ok, packets
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    host_key,
                    ts,
                    1 if ok else 0,
                    rtt_ms,
                    loss_pct,
                    rtt_min_ms,
                    rtt_max_ms,
                    packets_ok,
                    packets,
                ),
            )
            self.conn.commit()

    def history(self, host_key: str, since_ts: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT ts, ok, rtt_ms, loss_pct, rtt_min_ms, rtt_max_ms, packets_ok, packets
                FROM samples
                WHERE host_key = ? AND ts >= ?
                ORDER BY ts ASC
                """,
                (host_key, since_ts),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_ticks(self, host_key: str, limit: int) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT ok, loss_pct FROM samples
                WHERE host_key = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (host_key, limit),
            ).fetchall()
        ticks = [_tick(row["ok"], row["loss_pct"]) for row in reversed(rows)]
        return ticks

    def uptimes(self, since_ts: float) -> dict[str, float]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT host_key,
                       COUNT(*) AS n,
                       COALESCE(SUM(ok), 0) AS ok
                FROM samples
                WHERE ts >= ?
                GROUP BY host_key
                """,
                (since_ts,),
            ).fetchall()
        out: dict[str, float] = {}
        for row in rows:
            n = int(row["n"] or 0)
            if n <= 0:
                continue
            out[row["host_key"]] = 100.0 * int(row["ok"] or 0) / n
        return out

    def open_incident(self, host_key: str, name: str, ip: str, ts: float) -> None:
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM incidents WHERE host_key = ? AND up_ts IS NULL",
                (host_key,),
            ).fetchone()
            if existing:
                return
            self.conn.execute(
                """
                INSERT INTO incidents (host_key, name, ip, down_ts)
                VALUES (?, ?, ?, ?)
                """,
                (host_key, name, ip, ts),
            )
            self.conn.commit()

    def close_incident(self, host_key: str, ts: float) -> None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT id, down_ts FROM incidents
                WHERE host_key = ? AND up_ts IS NULL
                ORDER BY down_ts DESC
                LIMIT 1
                """,
                (host_key,),
            ).fetchone()
            if not row:
                return
            self.conn.execute(
                """
                UPDATE incidents
                SET up_ts = ?, duration_seconds = ?
                WHERE id = ?
                """,
                (ts, max(0.0, ts - float(row["down_ts"])), row["id"]),
            )
            self.conn.commit()

    def incidents(self, since_ts: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, host_key, name, ip, down_ts, up_ts, duration_seconds
                FROM incidents
                WHERE down_ts >= ? OR up_ts IS NULL
                ORDER BY down_ts DESC
                LIMIT 200
                """,
                (since_ts,),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, history_hours: float) -> None:
        cutoff = time.time() - history_hours * 3600
        with self._lock:
            self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self.conn.execute(
                """
                DELETE FROM incidents
                WHERE up_ts IS NOT NULL AND down_ts < ?
                """,
                (cutoff,),
            )
            self.conn.commit()


def _tick(ok: Any, loss_pct: Any) -> str:
    if not ok:
        return "fail"
    try:
        loss = float(loss_pct) if loss_pct is not None else 0.0
    except (TypeError, ValueError):
        loss = 0.0
    if loss > 0:
        return "loss"
    return "ok"
